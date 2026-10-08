import os
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hf_utils


class BuiltinAdapterDownloadTests(unittest.TestCase):
    def test_interrupted_copy_is_not_accepted_on_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builtin = root / "builtin"
            cache = root / "cache"
            cache.mkdir()
            (cache / "manifest.json").write_text(json.dumps([{"id": "voice"}]))
            for filename in hf_utils.REQUIRED_ADAPTER_FILES + hf_utils.OPTIONAL_ADAPTER_FILES:
                (cache / filename).write_bytes((filename + " complete").encode())

            def hub_download(*, filename, **_kwargs):
                return str(cache / Path(filename).name)

            real_copy = shutil.copy2
            interrupted = False

            def partial_copy(source, target):
                nonlocal interrupted
                if not interrupted:
                    interrupted = True
                    Path(target).write_bytes(b"partial")
                    raise OSError("interrupted")
                return real_copy(source, target)

            module = SimpleNamespace(hf_hub_download=hub_download)
            with patch.dict(sys.modules, {"huggingface_hub": module}), \
                 patch.object(hf_utils.shutil, "copy2", side_effect=partial_copy):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    hf_utils.download_builtin_adapter("builtin_voice", str(builtin))

            adapter = builtin / "builtin_voice"
            self.assertFalse(hf_utils.is_adapter_downloaded("builtin_voice", str(builtin)))
            self.assertFalse((adapter / "adapter_config.json").exists())
            # A pre-existing partial file from an older version also lacks a
            # completion marker and must be replaced, not skipped.
            (adapter / "adapter_model.safetensors").write_bytes(b"partial")
            with patch.dict(sys.modules, {"huggingface_hub": module}):
                hf_utils.download_builtin_adapter("builtin_voice", str(builtin))
            self.assertTrue(hf_utils.is_adapter_downloaded("builtin_voice", str(builtin)))
            self.assertEqual((cache / "adapter_model.safetensors").read_bytes(),
                             (adapter / "adapter_model.safetensors").read_bytes())


if __name__ == "__main__":
    unittest.main()


class PackageBatchAdapterTests(unittest.TestCase):
    def test_package_and_flat_batch_download_missing_adapter_and_publish_audio(self):
        import builtins
        import contextlib
        import importlib.util
        import io
        import json
        from types import ModuleType
        import numpy as np
        import soundfile as sf
        import tts

        app_dir = Path(__file__).resolve().parent.parent
        package_name = "alexandria_batch_package_fixture"
        package = ModuleType(package_name)
        package.__path__ = [str(app_dir)]
        spec = importlib.util.spec_from_file_location(package_name + ".tts", app_dir / "tts.py")
        package_tts = importlib.util.module_from_spec(spec)
        real_import = builtins.__import__

        def import_without_flat_hf(name, *args, **kwargs):
            level = kwargs.get("level", args[3] if len(args) > 3 else 0)
            if name == "hf_utils" and level == 0:
                raise ModuleNotFoundError("flat hf_utils unavailable in package fixture")
            return real_import(name, *args, **kwargs)

        package_modules = {package_name: package, package_name + ".tts": package_tts}
        with patch.dict(sys.modules, package_modules):
            spec.loader.exec_module(package_tts)
            for module in (package_tts, tts):
                with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as tmp:
                    directory = Path(tmp)
                    cache = directory / "cache"
                    cache.mkdir()
                    (cache / "manifest.json").write_text(json.dumps([{"id": "voice"}]))
                    samples = np.full(4000, 0.1, dtype="float32")
                    sf.write(cache / "ref_sample.wav", samples, 16000)
                    (cache / "training_meta.json").write_text(json.dumps({"ref_sample_text": "reference"}))
                    (cache / "adapter_config.json").write_text("{}")
                    (cache / "adapter_model.safetensors").write_bytes(b"fixture adapter weights")
                    sf.write(cache / "preview_sample.wav", samples, 16000)
                    adapter = directory / "adapters" / "builtin_voice"
                    output = directory / "output"
                    output.mkdir()
                    engine = module.TTSEngine.__new__(module.TTSEngine)
                    engine._max_new_tokens = 100
                    engine._language = "English"
                    engine._clear_gpu_cache = lambda: None
                    engine._estimate_max_batch_size = lambda *args: 2
                    engine._build_sub_batches = lambda texts, **kwargs: [(0, len(texts))]
                    requests = []

                    def generate(**kwargs):
                        requests.append(kwargs)
                        return [samples.copy() for _ in kwargs["text"]], 16000

                    model = SimpleNamespace(generate_voice_clone=generate)
                    engine._init_local_lora = lambda path, **kwargs: model
                    engine._ensure_lora_prompt = lambda *args, **kwargs: [SimpleNamespace(ref_code=None, ref_text="reference")]
                    hub = SimpleNamespace(hf_hub_download=lambda **kw: str(cache / Path(kw["filename"]).name))
                    torch = ModuleType("torch")
                    chunks = [{"speaker": "ANN", "text": "longer first", "index": 7},
                              {"speaker": "ANN", "text": "short", "index": 3}]
                    config = {"ANN": {"type": "lora", "adapter_path": str(adapter)}}
                    with patch.dict(sys.modules, {"huggingface_hub": hub, "torch": torch}), \
                         patch.object(builtins, "__import__", side_effect=import_without_flat_hf if module is package_tts else real_import), \
                         contextlib.redirect_stdout(io.StringIO()):
                        result = engine._local_batch_lora(chunks, config, str(output))
                        generated_calls = len(requests)
                        unknown_adapter = adapter.parent / "builtin_unlisted"
                        rejected = engine._local_batch_lora(chunks,
                            {"ANN": {"type": "lora", "adapter_path": str(unknown_adapter)}}, str(output))
                    self.assertEqual(generated_calls, len(requests))
                    self.assertEqual({3, 7}, {index for index, _ in rejected["failed"]})
                    self.assertEqual([], rejected["completed"])
                    self.assertFalse(unknown_adapter.exists())
                    self.assertEqual([], result["failed"])
                    self.assertEqual({3, 7}, set(result["completed"]))
                    self.assertTrue(hf_utils.is_adapter_downloaded("builtin_voice", str(adapter.parent)))
                    self.assertEqual((cache / "adapter_model.safetensors").read_bytes(),
                                     (adapter / "adapter_model.safetensors").read_bytes())
                    self.assertEqual(["short", "longer first"], requests[0]["text"])
                    for index in (3, 7):
                        actual, rate = sf.read(output / f"temp_batch_{index}.wav")
                        self.assertEqual(16000, rate)
                        np.testing.assert_allclose(samples, actual, atol=1e-4)


class BuiltinManifestConsumerTests(unittest.TestCase):
    def test_remote_fallback_and_cached_models_skip_malformed_ids(self):
        import asyncio
        import copy
        import json
        import core
        from routers import lora
        from contextlib import ExitStack
        entries = [{}, {'id': None}, {'id': 3}, {'id': []}, None, 'bad',
                   {'id': '../escape'}, {'id': ' '}, {'id': ' alice ', 'name': 'Alice'},
                   {'id': 'builtin_bob', 'name': 'Bob'}]
        for remote in (True, False):
            with self.subTest(remote=remote), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                directory = Path(tmp)
                manifest = directory / 'manifest.json'
                source = directory / 'remote.json'
                payload = json.dumps(entries)
                manifest.write_text(payload, encoding='utf-8')
                source.write_text(payload, encoding='utf-8')
                download = unittest.mock.Mock(return_value=str(source))
                if not remote:
                    download.side_effect = OSError('offline')
                stack.enter_context(patch.dict(sys.modules, {'huggingface_hub': SimpleNamespace(hf_hub_download=download)}))
                for attr, value in (('_manifest_cache', None), ('_manifest_cache_key', None), ('_manifest_cache_time', 0)):
                    stack.enter_context(patch.object(hf_utils, attr, value))
                stack.enter_context(patch.object(core, 'BUILTIN_LORA_DIR', str(directory)))
                stack.enter_context(patch.object(lora, '_load_manifest', return_value=[]))
                stack.enter_context(patch.object(lora, '_load_voice_library', return_value={}))
                first = asyncio.run(lora.lora_list_models())
                cached = copy.deepcopy(hf_utils._manifest_cache)
                second = asyncio.run(lora.lora_list_models())
                self.assertEqual(['builtin_alice', 'builtin_bob'], [row['id'] for row in first])
                self.assertEqual(first, second)
                self.assertTrue(all(row['builtin'] and not row['downloaded'] and row['preview_audio_url'] is None for row in first))
                self.assertEqual(cached, hf_utils._manifest_cache)
                self.assertEqual(['alice', 'builtin_bob'], [row['id'] for row in cached])
                download.assert_called_once()
                self.assertEqual(payload, source.read_text(encoding='utf-8'))
                if not remote:
                    self.assertEqual(payload, manifest.read_text(encoding='utf-8'))
