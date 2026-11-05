import argparse
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
parser.add_argument('--precision', choices=['fp32', 'bf16'], required=True)
args = parser.parse_args()
sys.path.insert(0, str(Path(args.repo) / 'app'))
import numpy as np
import psutil
import soundfile as sf
import torch
from peft import PeftModel
from qwen_tts import Qwen3TTSModel
from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock

root = Path(__file__).parent
out = root / 'rocm-qualification'
out.mkdir(exist_ok=True)
texts = [
    'The lantern stood beside the open door.',
    'Please close the window before the storm arrives.',
    'Across the quiet valley, a distant bell began to ring.',
    'I left the blue notebook on the kitchen table.',
    'When the door opened, nobody was standing outside.',
    'At last, she smiled and whispered, we are finally home.',
]

class PrefillCaptured(BaseException):
    pass


def save_json(path, value):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def measure_logits(talker, cases, dtype):
    records = []
    with torch.inference_mode():
        for case in cases:
            kwargs = {key: value.to(device='cuda:0', dtype=dtype if value.is_floating_point() else value.dtype)
                      for key, value in case.items()}
            base = talker.get_base_model() if isinstance(talker, PeftModel) else talker
            base.rope_deltas = None
            torch.cuda.synchronize()
            started = time.monotonic()
            logits = talker(**kwargs, use_cache=False, return_dict=True).logits[0, -1].float()
            torch.cuda.synchronize()
            assert torch.isfinite(logits).all().item(), 'non-finite logits'
            records.append({'logits': logits.cpu(), 'seconds': time.monotonic() - started})
    return records

handle = acquire_gpu_lock()
try:
    assert torch.version.hip, 'ROCm qualification requires actual HIP'
    required = 12 if args.precision == 'fp32' else 10
    assert torch.cuda.mem_get_info()[0] >= required * 2**30, 'insufficient GPU headroom'
    assert psutil.virtual_memory().available >= 12 * 2**30, 'insufficient RAM headroom'
    torch.manual_seed(1710)
    dtype = torch.float32 if args.precision == 'fp32' else torch.bfloat16
    started = time.monotonic()
    wrapper = Qwen3TTSModel.from_pretrained(str(root / 'qwen-model'), dtype=dtype, device_map='cuda:0')
    wrapper.model.eval()
    talker = wrapper.model.talker
    actual_dtype = next(talker.parameters()).dtype
    assert actual_dtype is dtype, (actual_dtype, dtype)
    input_path = out / 'canonical-inputs.pt'
    if args.precision == 'fp32':
        audio, rate = sf.read(root / 'public-adapter/watson/ref_sample.wav')
        prompt = wrapper.create_voice_clone_prompt(ref_audio=(audio, rate), x_vector_only_mode=True)
        cases = []
        def capture(module, positional, kwargs):
            if kwargs.get('inputs_embeds') is None or kwargs['inputs_embeds'].shape[1] < 2:
                return
            case = {}
            for key in ('inputs_embeds', 'attention_mask'):
                value = kwargs.get(key)
                if value is not None:
                    value = value.detach().cpu()
                    if value.is_floating_point():
                        value = value.to(torch.bfloat16).to(torch.float32)
                    case[key] = value
            cases.append(case)
            raise PrefillCaptured()
        hook = talker.register_forward_pre_hook(capture, with_kwargs=True)
        try:
            for text in texts:
                try:
                    wrapper.generate_voice_clone(text=text, language='English', voice_clone_prompt=prompt,
                        non_streaming_mode=True, max_new_tokens=256)
                except PrefillCaptured:
                    pass
        finally:
            hook.remove()
        assert len(cases) == len(texts)
        torch.save(cases, input_path)
        save_json(out / 'texts.json', texts)
    else:
        cases = torch.load(input_path, weights_only=True, map_location='cpu')
    records = {'base': measure_logits(talker, cases, dtype)}
    peft = PeftModel.from_pretrained(talker, str(root / 'public-adapter/watson'), autocast_adapter_dtype=True)
    peft.eval()
    adapter_dtypes = sorted({str(value.dtype) for name, value in peft.named_parameters() if 'lora_' in name})
    assert adapter_dtypes == ['torch.float32'], adapter_dtypes
    records['unmerged'] = measure_logits(peft, cases, dtype)
    merged = peft.merge_and_unload()
    merged.eval()
    records['merged'] = measure_logits(merged, cases, dtype)
    torch.save(records, out / (args.precision + '-logits.pt'))
    save_json(out / (args.precision + '-numeric.json'), {
        'scope': 'six_prompt_prefill_last_position_logits_only', 'precision': args.precision,
        'device': torch.cuda.get_device_name(), 'torch': torch.__version__, 'hip': torch.version.hip,
        'inputs_sha256': hashlib.sha256(input_path.read_bytes()).hexdigest(),
        'adapter_sha256': hashlib.sha256((root / 'public-adapter/watson/adapter_model.safetensors').read_bytes()).hexdigest(),
        'adapter_dtypes': adapter_dtypes, 'seconds': time.monotonic() - started,
        'peak_allocated_gib': torch.cuda.max_memory_allocated() / 2**30,
        'shapes': [list(case['inputs_embeds'].shape) for case in cases],
    })
    print('NUMERIC_DONE', args.precision, time.monotonic() - started, flush=True)
finally:
    release_gpu_lock(handle)
