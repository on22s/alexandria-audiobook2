import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
from pathlib import Path

os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
parser = argparse.ArgumentParser()
parser.add_argument('--repo', required=True)
parser.add_argument('--arm', choices=['no_lora', 'merged', 'unmerged'], required=True)
args = parser.parse_args()
sys.path.insert(0, str(Path(args.repo) / 'app'))
import numpy as np
import psutil
import soundfile as sf
import torch
from peft import PeftModel
from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
from tts import TTSEngine

root = Path(__file__).parent
out = root / 'rocm-qualification'
texts = json.loads((out / 'texts.json').read_text(encoding='utf-8'))
arm_root = out / args.arm
arm_root.mkdir(exist_ok=True)
source = Path(args.repo) / 'app/tts.py'
source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
result = {'arm': args.arm, 'scope': 'public_watson_six_text_two_seed_batch_one',
          'source_sha256': source_sha, 'samples': [], 'state': 'running'}

def checkpoint():
    temporary = arm_root / 'result.json.tmp'
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, arm_root / 'result.json')

handle = acquire_gpu_lock()
try:
    assert torch.version.hip, 'actual HIP required'
    assert torch.cuda.mem_get_info()[0] >= 10 * 2**30, 'insufficient GPU headroom'
    assert psutil.virtual_memory().available >= 12 * 2**30, 'insufficient RAM headroom'
    started = time.monotonic()
    engine = TTSEngine({'tts': {'mode': 'local', 'device': 'cuda:0', 'compile_codec': False,
                               'max_new_tokens': 256, 'language': 'English'}})
    resolve = TTSEngine._resolve_local_model_path
    TTSEngine._resolve_local_model_path = staticmethod(lambda name: str(root / 'qwen-model')
        if name == 'Qwen/Qwen3-TTS-12Hz-1.7B-Base' else resolve(name))
    adapter = str(root / 'public-adapter/watson')
    model, prompt = engine._ensure_local_lora_generation(adapter)
    peft = model.model.talker
    assert isinstance(peft, PeftModel)
    assert all(not module.training for module in peft.modules()), 'training mode remains active'
    adapter_dtypes = sorted({str(value.dtype) for name, value in peft.named_parameters() if 'lora_' in name})
    assert adapter_dtypes == ['torch.float32'], adapter_dtypes
    if args.arm == 'merged':
        model.model.talker = peft.merge_and_unload()
        model.model.talker.eval()
    disabled = peft.disable_adapter() if args.arm == 'no_lora' else contextlib.nullcontext()
    with disabled, torch.inference_mode():
        result.update(load_seconds=time.monotonic()-started, torch=torch.__version__, hip=torch.version.hip,
                      device=torch.cuda.get_device_name(), adapter_dtypes=adapter_dtypes,
                      adapter_sha256=hashlib.sha256((Path(adapter)/'adapter_model.safetensors').read_bytes()).hexdigest())
        assert engine.generate_lora_voice(texts[0], '', {'adapter_path': adapter, 'seed': 1710}, str(arm_root / 'warmup.wav'))
        checkpoint()
        for index, text in enumerate(texts):
            for seed in (1710, 2710):
                destination = arm_root / f'text-{index}-seed-{seed}.wav'
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
                measured = time.monotonic()
                success = engine.generate_lora_voice(text, '', {'adapter_path': adapter, 'seed': seed}, str(destination))
                torch.cuda.synchronize()
                seconds = time.monotonic() - measured
                assert success and destination.is_file(), 'generation failed'
                audio, rate = sf.read(destination, dtype='float32')
                assert rate == 24000 and audio.ndim == 1 and np.isfinite(audio).all()
                duration = len(audio) / rate
                assert 0 < duration < 256 / 12, 'empty or token-cap-length output'
                features = model.model.extract_speaker_embedding(audio, rate).float().cpu().numpy()
                norm = float(np.linalg.norm(features))
                assert np.isfinite(features).all() and norm > 0
                record = {'text_id': index, 'seed': seed, 'text': text, 'seconds': seconds,
                          'audio_seconds': duration, 'seconds_per_audio_second': seconds / duration,
                          'peak_allocated_gib': torch.cuda.max_memory_allocated() / 2**30,
                          'sha256': hashlib.sha256(destination.read_bytes()).hexdigest(),
                          'speaker_embedding': (features / norm).tolist(), 'path': str(destination)}
                result['samples'].append(record)
                checkpoint()
                print(json.dumps({'arm': args.arm, 'completed': len(result['samples']),
                                  'of': 12, 'seconds': seconds, 'audio_seconds': duration}), flush=True)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha, 'source changed mid-run'
    result.update(state='done', total_seconds=time.monotonic()-started)
    checkpoint()
except BaseException as error:
    result.update(state='failed', error=type(error).__name__ + ': ' + str(error))
    checkpoint()
    raise
finally:
    release_gpu_lock(handle)
