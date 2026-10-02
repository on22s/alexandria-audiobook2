"""Backup deletion commits metadata before irreversibly removing owned bytes."""
import ast
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from routers import lora
import adapter_naming_transaction as transaction
from adapter_publication import NAMING_PUBLICATION_JOURNAL, get_adapter_publication_recovery_command
from tests.test_adapter_deletion_transaction import tree_bytes
from tests.test_support import write_test_adapter


def crash_delete(root, point):
    original_rename, original_replace, original_remove = os.rename, os.replace, transaction.shutil.rmtree
    def rename(source, target):
        original_rename(source,target)
        if point=='staged' and Path(target).parent.name=='adapters':os._exit(71)
    def replace(source,target):
        original_replace(source,target)
        target=Path(target)
        if point=='manifest' and target==root/'manifest.json':os._exit(71)
        if point=='committed' and target.name==NAMING_PUBLICATION_JOURNAL and json.loads(target.read_text())['phase']=='committed':os._exit(71)
    def remove(path):
        if point=='partial-cleanup' and Path(path).name.startswith('.naming-'):
            (Path(path)/'adapters/0/extra.txt').unlink()
            os._exit(71)
        return original_remove(path)
    with patch('os.rename',side_effect=rename),patch('os.replace',side_effect=replace),patch.object(transaction.shutil,'rmtree',side_effect=remove):
        lora._delete_rollback_backup('voice',str(root),str(root/'manifest.json'))


class RollbackBackupDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.adapter=self.root/'voice'
        self.backup=self.adapter/'promotion_backups/backup-1'
        write_test_adapter(self.adapter,1)
        write_test_adapter(self.backup,2)
        (self.backup/'extra.txt').write_bytes(b'rollback metadata survives publication failure')
        (self.backup/'nested').mkdir();(self.backup/'nested/checkpoint').write_bytes(b'nested bytes')
        (self.adapter/'promotion_backups/unrelated').mkdir()
        (self.adapter/'promotion_backups/unrelated/weights').write_bytes(b'other backup')
        self.manifest=self.root/'manifest.json'
        self.manifest.write_text(json.dumps([{'id':'voice','promotion':{'status':'promoted','backup_id':'backup-1'}},{'id':'other'}]))
        self.metadata_backup=self.root/'manifest.json.bak';self.metadata_backup.write_bytes(b'previous metadata backup')
        self.before_manifest=self.manifest.read_bytes();self.before_metadata_backup=self.metadata_backup.read_bytes()
        self.before=tree_bytes(self.adapter)

    def invoke(self):
        subject=lora._delete_rollback_backup
        baseline=os.environ.get('ROLLBACK_DELETE_BASELINE_FILE')
        if baseline:
            node=next(n for n in ast.parse(Path(baseline).read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='_delete_rollback_backup')
            namespace=dict(vars(lora));exec(compile(ast.Module(body=[node],type_ignores=[]),baseline,'exec'),namespace);subject=namespace['_delete_rollback_backup']
        return subject('voice',str(self.root),str(self.manifest))

    def assert_restored(self):
        self.assertEqual(self.before,tree_bytes(self.adapter))
        self.assertEqual(self.before_manifest,self.manifest.read_bytes())
        self.assertEqual(self.before_metadata_backup,self.metadata_backup.read_bytes())
        self.assertFalse((self.root/NAMING_PUBLICATION_JOURNAL).exists())

    def test_manifest_publication_failure_preserves_all_backup_production_and_metadata_bytes(self):
        original=transaction.save_adapter_publication_bytes
        failed=False
        def save(path,data,**kwargs):
            nonlocal failed
            if Path(path)==self.manifest and not failed:
                failed=True;raise OSError('manifest publication failed')
            return original(path,data,**kwargs)
        with patch.object(lora,'_save_manifest',side_effect=OSError('manifest publication failed')),patch.object(transaction,'save_adapter_publication_bytes',side_effect=save):
            with self.assertRaisesRegex(OSError,'manifest publication failed'):self.invoke()
        self.assert_restored()

    def test_success_deletes_only_target_backup_and_clears_its_pointer(self):
        self.assertEqual({'status':'deleted','adapter_id':'voice','backup_id':'backup-1'},self.invoke())
        self.assertFalse(self.backup.exists())
        self.assertEqual({k:v for k,v in self.before.items() if not k.startswith('promotion_backups/backup-1/')},tree_bytes(self.adapter))
        manifest=json.loads(self.manifest.read_text())
        self.assertIsNone(manifest[0]['promotion']['backup_id'])
        self.assertIn('backup_deleted_at',manifest[0]['promotion'])
        self.assertEqual({'id':'other'},manifest[1])

    def test_crashes_restore_precommit_and_finish_committed_partial_cleanup_idempotently(self):
        for point in ('staged','manifest','committed','partial-cleanup'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                import shutil
                shutil.copytree(self.adapter,root/'voice')
                (root/'manifest.json').write_bytes(self.before_manifest)
                (root/'manifest.json.bak').write_bytes(self.before_metadata_backup)
                process=multiprocessing.get_context('fork').Process(target=crash_delete,args=(root,point))
                process.start();process.join(5)
                try:
                    self.assertFalse(process.is_alive());self.assertEqual(71,process.exitcode)
                    self.assertIn('name_voices.py --recover',get_adapter_publication_recovery_command(root))
                    with transaction.lock_adapter_naming(str(root),str(root/'manifest.json')):
                        self.assertTrue(transaction.recover_adapter_naming_locked(str(root),str(root/'manifest.json')))
                        self.assertFalse(transaction.recover_adapter_naming_locked(str(root),str(root/'manifest.json')))
                    if point in ('staged','manifest'):
                        self.assertEqual(self.before,tree_bytes(root/'voice'))
                        self.assertEqual(self.before_manifest,(root/'manifest.json').read_bytes())
                        self.assertEqual(self.before_metadata_backup,(root/'manifest.json.bak').read_bytes())
                    else:
                        self.assertFalse((root/'voice/promotion_backups/backup-1').exists())
                        self.assertEqual({k:v for k,v in self.before.items() if not k.startswith('promotion_backups/backup-1/')},tree_bytes(root/'voice'))
                        self.assertIsNone(json.loads((root/'manifest.json').read_text())[0]['promotion']['backup_id'])
                    self.assertIsNone(get_adapter_publication_recovery_command(root))
                finally:
                    if process.is_alive():process.kill();process.join(5)

    def test_cleanup_failure_is_committed_and_fences_writers_until_explicit_recovery(self):
        with patch.object(transaction.shutil,'rmtree',side_effect=OSError('cleanup failed')):
            with self.assertRaisesRegex(OSError,'cleanup failed'):self.invoke()
        self.assertIsNone(json.loads(self.manifest.read_text())[0]['promotion']['backup_id'])
        journal=json.loads((self.root/NAMING_PUBLICATION_JOURNAL).read_text())
        self.assertEqual('committed',journal['phase'])
        self.assertTrue((self.root/journal['workspace']/'adapters/0/extra.txt').is_file())
        with self.assertRaises(lora.HTTPException) as caught:self.invoke()
        self.assertEqual(409,caught.exception.status_code)
        command=[sys.executable,str(Path(__file__).resolve().parents[2]/'tools/voice_lab/name_voices.py'),
                 '--recover','--models-dir',str(self.root),'--manifest',str(self.manifest)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=5)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('committed',result.stdout)
        self.assertIsNone(get_adapter_publication_recovery_command(self.root))

    def test_unsafe_backup_id_parent_or_bundle_is_rejected_before_mutation(self):
        for value in ('../backup-1','',{'id':'backup-1'}):
            with self.subTest(value=value):
                document=json.loads(self.before_manifest);document[0]['promotion']['backup_id']=value
                self.manifest.write_text(json.dumps(document));before=self.manifest.read_bytes()
                with self.assertRaises((lora.HTTPException,ValueError,TypeError)):self.invoke()
                self.assertEqual(before,self.manifest.read_bytes());self.assertEqual(self.before,tree_bytes(self.adapter))
        self.manifest.write_bytes(self.before_manifest)
        actual=self.adapter/'promotion_backups';moved=self.root/'outside-backups';actual.rename(moved);actual.symlink_to(moved,target_is_directory=True)
        before=tree_bytes(moved)
        with self.assertRaises((lora.HTTPException,ValueError)):self.invoke()
        self.assertEqual(before,tree_bytes(moved));self.assertEqual(self.before_manifest,self.manifest.read_bytes())
