"""Deletion failure and hard-exit recovery preserve the real adapter bundle."""
import ast
import asyncio
import copy
from contextlib import ExitStack
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import HTTPException

import core
from routers import lora
import adapter_naming_transaction as transaction
from adapter_publication import NAMING_PUBLICATION_JOURNAL, get_adapter_publication_recovery_command
from tests.test_support import write_test_adapter


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def crash_delete(root, point):
    original_rename, original_replace = os.rename, os.replace
    def rename(source, target):
        original_rename(source, target)
        if point == 'staged' and Path(target).parent.name == 'adapters':
            os._exit(71)
    def replace(source, target):
        original_replace(source, target)
        target = Path(target)
        if point == 'manifest' and target == root / 'manifest.json':
            os._exit(71)
        if point == 'committed' and target.name == NAMING_PUBLICATION_JOURNAL:
            if json.loads(target.read_text())['phase'] == 'committed':
                os._exit(71)
    original_remove = transaction.shutil.rmtree
    def remove(path):
        if point == 'partial-cleanup' and Path(path).name.startswith('.naming-'):
            bundle = Path(path) / 'adapters' / '0'
            (bundle / 'extra.txt').unlink()
            os._exit(71)
        return original_remove(path)
    with patch('os.rename', side_effect=rename), patch('os.replace', side_effect=replace), \
         patch.object(transaction.shutil, 'rmtree', side_effect=remove), \
         transaction.lock_adapter_naming(str(root), str(root / 'manifest.json')):
        transaction.apply_adapter_deletion_locked(str(root), str(root / 'manifest.json'), [{'id': 'other'}], 'voice')


class AdapterDeletionTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = self.root / 'voice'
        write_test_adapter(self.adapter)
        (self.adapter / 'extra.txt').write_bytes(b'preserved source and checkpoint metadata')
        nested = self.adapter / 'nested'
        nested.mkdir()
        (nested / 'checkpoint').write_bytes(b'checkpoint bytes')
        self.other = self.root / 'other'
        write_test_adapter(self.other)
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps([{'id': 'voice', 'name': 'Original'}, {'id': 'other'}]))
        self.backup = self.root / 'manifest.json.bak'
        self.backup.write_bytes(b'prior backup bytes, must survive rollback')
        self.before_adapter = tree_bytes(self.adapter)
        self.before_other = tree_bytes(self.other)
        self.before_manifest = self.manifest.read_bytes()
        self.before_backup = self.backup.read_bytes()

    def invoke_route(self):
        state = copy.deepcopy(core.process_state)
        for row in state.values():
            row['running'] = False
        with ExitStack() as stack:
            data = self.root / 'data'; data.mkdir(exist_ok=True)
            stack.enter_context(patch.object(core, 'DATA_DIR', str(data)))
            (data / 'scripts').mkdir(exist_ok=True)
            for name, value in (('VOICE_CONFIG_PATH', str(data / 'voice_config.json')),
                                ('VOICE_LIBRARY_PATH', str(data / 'voice_library.json')),
                                ('SCRIPTS_DIR', str(data / 'scripts'))):
                stack.enter_context(patch.object(lora, name, value))
            for module in (core, lora):
                stack.enter_context(patch.object(module, 'process_state', state))
            stack.enter_context(patch.object(lora, 'LORA_MODELS_DIR', str(self.root)))
            stack.enter_context(patch.object(lora, 'LORA_MODELS_MANIFEST', str(self.manifest)))
            stack.enter_context(patch.object(lora, 'project_manager', SimpleNamespace(engine=object())))
            stack.enter_context(patch.object(lora, '_load_builtin_lora_manifest', return_value=[]))
            stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
            subject = lora.lora_delete_model
            baseline = os.environ.get('ADAPTER_DELETE_BASELINE_FILE')
            if baseline:
                tree = ast.parse(Path(baseline).read_text())
                node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'lora_delete_model')
                node.decorator_list = []
                namespace = dict(vars(lora))
                exec(compile(ast.Module(body=[node], type_ignores=[]), baseline, 'exec'), namespace)
                subject = namespace['lora_delete_model']
            try:
                return asyncio.run(subject('voice'))
            finally:
                self.assertFalse(state['lora_training']['running'])

    def assert_restored(self):
        self.assertEqual(self.before_adapter, tree_bytes(self.adapter))
        self.assertEqual(self.before_other, tree_bytes(self.other))
        self.assertEqual(self.before_manifest, self.manifest.read_bytes())
        self.assertEqual(self.before_backup, self.backup.read_bytes())
        self.assertFalse((self.root / NAMING_PUBLICATION_JOURNAL).exists())
        self.assertEqual([], list(self.root.glob('.naming-*')))

    def test_manifest_write_failure_restores_bundle_manifest_backup_and_other_adapter(self):
        original = transaction.save_adapter_publication_bytes
        failed = False
        def save(path, data, **kwargs):
            nonlocal failed
            if Path(path) == self.manifest and not failed:
                failed = True
                raise OSError('manifest publication failed')
            return original(path, data, **kwargs)
        with patch.object(lora, '_save_manifest', side_effect=OSError('manifest publication failed')), \
             patch.object(transaction, 'save_adapter_publication_bytes', side_effect=save):
            with self.assertRaisesRegex(OSError, 'manifest publication failed'):
                self.invoke_route()
        self.assert_restored()

    def test_success_removes_only_requested_bundle_and_commits_manifest(self):
        self.assertEqual({'status': 'deleted', 'adapter_id': 'voice'}, self.invoke_route())
        self.assertFalse(self.adapter.exists())
        self.assertEqual([{'id': 'other'}], json.loads(self.manifest.read_text()))
        self.assertEqual(self.before_other, tree_bytes(self.other))
        self.assertFalse((self.root / NAMING_PUBLICATION_JOURNAL).exists())
        self.assertEqual([], list(self.root.glob('.naming-*')))

    def test_deletion_rejects_a_manifest_that_still_advertises_the_bundle(self):
        manifest = json.loads(self.before_manifest)
        before = copy.deepcopy(manifest)
        with transaction.lock_adapter_naming(str(self.root), str(self.manifest)):
            with self.assertRaisesRegex(ValueError, 'still present'):
                transaction.apply_adapter_deletion_locked(str(self.root), str(self.manifest), manifest, 'voice')
        self.assertEqual(before, manifest)
        self.assert_restored()

    def test_bundle_symlinks_do_not_delete_external_assets(self):
        external = self.root / 'external-source'
        external.mkdir()
        (external / 'weights').write_bytes(b'external assets')
        (self.adapter / 'source-link').symlink_to(external, target_is_directory=True)
        self.invoke_route()
        self.assertFalse(self.adapter.exists())
        self.assertEqual({'weights': b'external assets'}, tree_bytes(external))

    def test_crash_before_commit_recovers_and_after_commit_finishes_cleanup(self):
        for point in ('staged', 'manifest', 'committed', 'partial-cleanup'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                # Each crash has independently saved bundle/metadata bytes.
                import shutil
                shutil.copytree(self.adapter, root / 'voice')
                shutil.copytree(self.other, root / 'other')
                (root / 'manifest.json').write_bytes(self.before_manifest)
                (root / 'manifest.json.bak').write_bytes(self.before_backup)
                process = multiprocessing.get_context('fork').Process(target=crash_delete, args=(root, point))
                process.start()
                process.join(5)
                try:
                    self.assertFalse(process.is_alive())
                    self.assertEqual(71, process.exitcode)
                    self.assertIn('name_voices.py --recover', get_adapter_publication_recovery_command(root))
                    with transaction.lock_adapter_naming(str(root), str(root / 'manifest.json')):
                        self.assertTrue(transaction.recover_adapter_naming_locked(str(root), str(root / 'manifest.json')))
                        self.assertFalse(transaction.recover_adapter_naming_locked(str(root), str(root / 'manifest.json')))
                    self.assertEqual(self.before_other, tree_bytes(root / 'other'))
                    if point in ('staged', 'manifest'):
                        self.assertEqual(self.before_adapter, tree_bytes(root / 'voice'))
                        self.assertEqual(self.before_manifest, (root / 'manifest.json').read_bytes())
                        self.assertEqual(self.before_backup, (root / 'manifest.json.bak').read_bytes())
                    else:
                        self.assertFalse((root / 'voice').exists())
                        self.assertEqual([{'id': 'other'}], json.loads((root / 'manifest.json').read_text()))
                    self.assertIsNone(get_adapter_publication_recovery_command(root))
                    self.assertEqual([], list(root.glob('.naming-*')))
                finally:
                    if process.is_alive():
                        process.terminate()
                        process.join(2)

    def test_failed_committed_cleanup_retains_journal_and_can_be_retried(self):
        original = transaction.shutil.rmtree
        failed = False
        def remove(path):
            nonlocal failed
            if Path(path).name.startswith('.naming-') and not failed:
                failed = True
                raise OSError('cleanup failed')
            return original(path)
        with patch.object(transaction.shutil, 'rmtree', side_effect=remove):
            with self.assertRaisesRegex(OSError, 'cleanup failed'):
                self.invoke_route()
        self.assertEqual([{'id': 'other'}], json.loads(self.manifest.read_text()))
        self.assertEqual('committed', json.loads((self.root / NAMING_PUBLICATION_JOURNAL).read_text())['phase'])
        retained = tree_bytes(self.root)
        with self.assertRaises(HTTPException) as refusal:
            self.invoke_route()
        self.assertEqual(409, refusal.exception.status_code)
        self.assertEqual(retained, tree_bytes(self.root))
        with transaction.lock_adapter_naming(str(self.root), str(self.manifest)):
            transaction.recover_adapter_naming_locked(str(self.root), str(self.manifest))
        self.assertFalse(self.adapter.exists())
        self.assertEqual(self.before_other, tree_bytes(self.other))
        self.assertFalse((self.root / NAMING_PUBLICATION_JOURNAL).exists())
