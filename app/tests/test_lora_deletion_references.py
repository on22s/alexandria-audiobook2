"""Deletion uses actual flat-file state and native locks; no synthesis."""
import asyncio
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from routers import lora
from utils import file_lock


class LoraDeletionReferencesTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); models = root/'lora_models'; models.mkdir()
            scripts = root/'scripts'; scripts.mkdir(); adapter = models/'voice'; adapter.mkdir()
            (adapter/'weights').write_bytes(b'unchanged adapter')
            manifest = models/'manifest.json'; manifest.write_text('[{"id":"voice","previous_ids":["oldvoice"]}]')
            config = root/'voice_config.json'; config.write_text('{}')
            library = root/'voice_library.json'; library.write_text('{"casts":{},"shared":{},"favorites":[]}')
            manager = SimpleNamespace(engine=object())
            for name, value in (('VOICE_CONFIG_PATH', str(config)), ('VOICE_LIBRARY_PATH', str(library)),
                                ('SCRIPTS_DIR', str(scripts)), ('LORA_MODELS_DIR', str(models)),
                                ('LORA_MODELS_MANIFEST', str(manifest)), ('project_manager', manager)):
                stack.enter_context(patch.object(lora, name, value))
            stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest', return_value=[]))
            claim = stack.enter_context(patch.object(lora, 'claim_gpu_task', return_value='claim'))
            release = stack.enter_context(patch.object(lora, 'release_gpu_task_claim'))
            yield root, config, library, scripts, adapter, manifest, manager, claim, release

    def test_rejects_current_saved_nested_cast_shared_favorite_and_historical_references(self):
        cases = [('active', {'ALICE': {'type':'lora','adapter_id':'voice'}}),
                 ('active', {'ALICE': {'versions': {'v': {'adapter_id':'oldvoice'}}}}),
                 ('saved', {'ALICE': {'adapter_path':'lora_models/oldvoice'}}),
                 ('library', {'casts': {'cast': {'members': {'ALICE': {'config': {'adapter_id':'voice'}}}}}}),
                 ('library', {'shared': {'NARRATOR': {'config': {'adapter_id':'oldvoice'}}}}),
                 ('library', {'favorites':['oldvoice']})]
        for target, data in cases:
            with self.subTest(target=target, data=data), self.fixture() as f:
                root, config, library, scripts, adapter, manifest, manager, claim, release = f
                path = {'active':config, 'library':library, 'saved':scripts/'book.voice_config.json'}[target]
                path.write_text(json.dumps(data))
                before = {p: p.read_bytes() for p in (path, manifest, adapter/'weights')}
                engine = manager.engine
                with self.assertRaises(HTTPException) as caught:
                    asyncio.run(lora.lora_delete_model('voice'))
                self.assertEqual(409, caught.exception.status_code)
                self.assertIn(path.name, caught.exception.detail)
                self.assertIs(engine, manager.engine)
                self.assertEqual(before, {p:p.read_bytes() for p in before})
                claim.assert_called_once(); release.assert_called_once_with('lora_training','claim')

    def test_corrupt_reference_file_refuses_without_mutation(self):
        with self.fixture() as f:
            root, config, library, scripts, adapter, manifest, manager, claim, release = f
            config.write_text('{broken')
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(lora.lora_delete_model('voice'))
            self.assertEqual(409, caught.exception.status_code)
            self.assertTrue(adapter.is_dir()); self.assertEqual('{broken', config.read_text())
            release.assert_called_once_with('lora_training','claim')

    def test_unreferenced_and_same_named_builtin_allow_deletion_without_changing_configs(self):
        with self.fixture() as f:
            root, config, library, scripts, adapter, manifest, manager, claim, release = f
            config.write_text('{"ALICE":{"type":"builtin_lora","adapter_id":"voice"}}')
            before = config.read_bytes()
            result = asyncio.run(lora.lora_delete_model('voice'))
            self.assertEqual('deleted', result['status']); self.assertFalse(adapter.exists())
            self.assertEqual([], json.loads(manifest.read_text())); self.assertEqual(before, config.read_bytes())
            release.assert_called_once_with('lora_training','claim')

    def test_config_and_library_writers_wait_until_deletion_releases_snapshot(self):
        for target in ('config', 'library'):
            with self.subTest(target=target), self.fixture() as f:
                root, config, library, scripts, adapter, manifest, manager, claim, release = f
                entered = threading.Event(); finish = threading.Event(); started = threading.Event()
                def collect():
                    entered.set()
                    if not finish.wait(3):
                        raise RuntimeError('fixture deadline')
                def writer():
                    started.set()
                    with file_lock(str(config if target == 'config' else library)):
                        return adapter.exists()
                with patch.object(lora.gc, 'collect', side_effect=collect), ThreadPoolExecutor(2) as pool:
                    deletion = pool.submit(lambda: asyncio.run(lora.lora_delete_model('voice')))
                    try:
                        self.assertTrue(entered.wait(2))
                        pending = pool.submit(writer); self.assertTrue(started.wait(2))
                        with self.assertRaises(FutureTimeoutError):
                            pending.result(timeout=.05)
                    finally:
                        finish.set()
                    self.assertEqual('deleted', deletion.result(timeout=2)['status'])
                    self.assertFalse(pending.result(timeout=2))
