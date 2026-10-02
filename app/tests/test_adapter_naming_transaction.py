"""Native naming transactions: complete artifacts, hard exits and failure admission."""
import contextlib
import importlib.util
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adapter_publication import NAMING_PUBLICATION_JOURNAL, CLI_PUBLICATION_JOURNAL
from adapter_naming_transaction import _apply_adapter_changes_locked, apply_adapter_naming_locked, lock_adapter_naming, recover_adapter_naming_locked
from tests.test_support import write_test_adapter

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/voice_lab/name_voices.py'
subject_path = os.environ.get('NAMING_BASELINE_FILE') or str(SCRIPT)
spec = importlib.util.spec_from_file_location('naming_subject', subject_path)
subject = importlib.util.module_from_spec(spec); spec.loader.exec_module(subject)


def _files(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def _invoke(models, *args):
    with patch.object(sys, 'argv', ['name_voices.py', '--models-dir', str(models),
                                     '--manifest', str(models/'manifest.json'), *args]):
        return subject.main()


def _crash_apply(models, point):
    rename, replace = os.rename, os.replace
    def moved(source, target):
        result = rename(source, target)
        target = Path(target)
        if ((point == 'collect' and target.parent.name == 'adapters' and target.name == '0')
                or (point == 'first' and target.name.startswith('warm_'))
                or (point == 'second' and target.name.startswith('silky_'))):
            os._exit(71)
        return result
    def replaced(source, target):
        result = replace(source, target)
        target = Path(target)
        reached = (point == 'manifest' and target == models/'manifest.json') or (point == 'aliases' and target == models/'adapter_id_aliases.json')
        if target.name == NAMING_PUBLICATION_JOURNAL:
            reached = reached or (point in ('publish', 'committed') and json.loads(target.read_text())['phase'] == point)
        if reached:
            os._exit(71)
        return result
    with patch('os.rename', side_effect=moved), patch('os.replace', side_effect=replaced):
        os._exit(_invoke(models, '--apply'))


class NamingTransactionTests(unittest.TestCase):
    def fixture(self, root):
        models = root/'models'; models.mkdir()
        rows = [{'id':'raw_a', 'dataset_id':'raw_a', 'name':'raw_a', 'voice_profile':'Warm baritone in his 30s; best for fantasy.', 'extra':{'keep':[1]}},
                {'id':'raw_b', 'dataset_id':'raw_b', 'name':'raw_b', 'voice_profile':'Silky soprano in her 20s; best for anime.', 'extra':{'keep':[2]}},
                {'id':'stable', 'name':'Stable', 'keep':9}]
        for index, row in enumerate(rows):
            write_test_adapter(models/row['id'], value=index+1)
            (models/row['id']/'ref_sample.wav').write_bytes(b'reference:'+row['id'].encode())
            (models/row['id']/'keep').mkdir()
            (models/row['id']/'keep'/'probe.txt').write_text('owned:'+row['id'])
        manifest = models/'manifest.json'; manifest.write_bytes(json.dumps(rows, ensure_ascii=False).encode()+b'\n')
        (models/'manifest.json.bak').write_bytes(b'previous rollback evidence\n')
        return models, rows

    def assert_clean(self, models):
        self.assertFalse((models/NAMING_PUBLICATION_JOURNAL).exists())
        self.assertEqual([], list(models.glob('.naming-*')))
        with lock_adapter_naming(str(models), str(models/'manifest.json')):
            pass

    def child_crash(self, models, point):
        child = multiprocessing.get_context('fork').Process(target=_crash_apply, args=(models, point))
        child.start(); child.join(10)
        if child.is_alive():
            child.kill(); child.join(5); self.fail('Naming crash fixture hung')
        self.assertEqual(71, child.exitcode)

    def test_alias_registry_publication_crash_recovers_original_identity_and_all_bytes(self):
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp))
            (models/'adapter_id_aliases.json').write_text(json.dumps({'version':1,'aliases':{'ancient':'raw_a'}}))
            before=_files(models)
            self.child_crash(models,'aliases')
            self.assertEqual(0,_invoke(models,'--recover'))
            self.assertEqual({**before,'manifest.json.lock':b''},_files(models))
            self.assertEqual(str(models/'raw_a'),get_resolved_adapter_path(str(models/'ancient')))
            self.assertEqual(0,_invoke(models,'--apply'))
            self.assertNotEqual(str(models/'raw_a'),get_resolved_adapter_path(str(models/'ancient')))
            self.assert_clean(models)

    def test_late_rename_failure_restores_all_directories_manifest_and_old_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            models, _rows = self.fixture(Path(tmp)); before = _files(models)
            rename = os.rename
            def fail_second(source, target):
                if Path(target).name.startswith('silky_'):
                    raise OSError('later rename failed')
                return rename(source, target)
            with patch('os.rename', side_effect=fail_second):
                self.assertEqual(1, _invoke(models, '--apply'))
            self.assertEqual({**before, 'manifest.json.lock':b''}, _files(models))
            self.assert_clean(models)

    def test_crash_each_apply_phase_then_fresh_cli_recover(self):
        for point in ('collect', 'publish', 'first', 'second', 'manifest', 'committed'):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as tmp:
                models, rows = self.fixture(Path(tmp)); before = _files(models)
                bundles = {r['id']:_files(models/r['id']) for r in rows}
                self.child_crash(models, point)
                self.assertTrue((models/NAMING_PUBLICATION_JOURNAL).is_file())
                for _ in range(2):
                    result = subprocess.run([sys.executable, str(SCRIPT), '--manifest',str(models/'manifest.json'),
                                             '--models-dir',str(models),'--recover'], capture_output=True, text=True, timeout=10)
                    self.assertEqual(0, result.returncode, result.stderr)
                if point == 'committed':
                    named = json.loads((models/'manifest.json').read_text())
                    for old, new in zip(rows, named):
                        self.assertEqual(bundles[old['id']], _files(models/new['id']))
                        if old['id'] != 'stable':
                            self.assertEqual(new['id'], new['name'])
                            self.assertEqual(old['dataset_id'], new['dataset_id'])
                    self.assertEqual(before['manifest.json'], (models/'manifest.json.bak').read_bytes())
                    archives=list(models.glob('manifest.json.bak.*'))
                    self.assertEqual(1, len(archives)); self.assertEqual(before['manifest.json.bak'], archives[0].read_bytes())
                else:
                    self.assertEqual({**before,'manifest.json.lock':b''}, _files(models))
                self.assert_clean(models)

    def test_missing_source_and_occupied_target_refuse_before_any_rename(self):
        for kind in ('missing', 'occupied'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                models, rows = self.fixture(Path(tmp))
                if kind == 'missing':
                    shutil.rmtree(models/'raw_b')
                else:
                    slug=subject.derive_base_slug(rows[1]['voice_profile'])
                    (models/slug).mkdir();(models/slug/'unrelated').write_text('keep')
                before=_files(models)
                self.assertEqual(1, _invoke(models,'--apply'))
                self.assertEqual({**before,'manifest.json.lock':b''},_files(models))
                self.assert_clean(models)

    def test_unsafe_and_symlinked_source_ids_never_move_external_data(self):
        for kind in ('parent','absolute','symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);models,_rows=self.fixture(root)
                outside=root/'outside';outside.mkdir();(outside/'keep').write_text('external')
                source_id='../outside' if kind=='parent' else str(outside) if kind=='absolute' else 'alias'
                if kind=='symlink':os.symlink(outside,models/'alias',target_is_directory=True)
                (models/'manifest.json').write_text(json.dumps([{'id':source_id,'dataset_id':source_id,'voice_profile':'Warm baritone in his 30s.'}]))
                before=_files(models);external=_files(outside)
                self.assertEqual(1,_invoke(models,'--apply'))
                self.assertEqual(external,_files(outside));self.assertEqual({**before,'manifest.json.lock':b''},_files(models))
                if kind=='symlink':self.assertTrue((models/'alias').is_symlink())
                self.assert_clean(models)

    def test_dry_run_and_verify_refuse_pending_transaction_without_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));self.child_crash(models,'first')
            before=_files(models)
            self.assertEqual(1,_invoke(models));self.assertEqual(before,_files(models))
            self.assertEqual(1,_invoke(models,'--verify'));self.assertEqual(before,_files(models))
            self.assertEqual(0,_invoke(models,'--recover'));self.assert_clean(models)

    def test_cyclic_name_exchange_commits_complete_original_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));before={n:_files(models/n) for n in ('raw_a','raw_b')}
            manifest_path=models/'manifest.json';rows=json.loads(manifest_path.read_text())
            for row in rows:
                if row['id']=='raw_a':row['id']='raw_b';row['name']='raw_b'
                elif row['id']=='raw_b':row['id']='raw_a';row['name']='raw_a'
            with lock_adapter_naming(str(models),str(manifest_path)):
                _apply_adapter_changes_locked(str(models),str(manifest_path),rows,[{'old':'raw_a','new':'raw_b'},{'old':'raw_b','new':'raw_a'}])
            self.assertEqual(before['raw_a'],_files(models/'raw_b'))
            self.assertEqual(before['raw_b'],_files(models/'raw_a'))
            self.assert_clean(models)

    def test_interrupted_cyclic_publication_restores_without_confusing_bundle_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));before=_files(models)
            def cycle():
                rename=os.rename
                def crash(source,target):
                    result=rename(source,target)
                    if Path(target)==models/'raw_b':os._exit(72)
                    return result
                rows=json.loads((models/'manifest.json').read_text())
                with lock_adapter_naming(str(models),str(models/'manifest.json')),patch('os.rename',side_effect=crash):
                    _apply_adapter_changes_locked(str(models),str(models/'manifest.json'),rows,[{'old':'raw_a','new':'raw_b'},{'old':'raw_b','new':'raw_a'}])
            child=multiprocessing.get_context('fork').Process(target=cycle);child.start();child.join(10)
            if child.is_alive():child.kill();child.join(5);self.fail('Cycle fixture hung')
            self.assertEqual(72,child.exitcode)
            self.assertEqual(0,_invoke(models,'--recover'))
            self.assertEqual({**before,'manifest.json.lock':b''},_files(models));self.assert_clean(models)

    def test_naming_benchmark_executes_actual_stage_and_preserves_existing_contract(self):
        from naming_benchmark import execute_payload
        with tempfile.TemporaryDirectory() as tmp:
            _models,rows=self.fixture(Path(tmp))
            result=execute_payload({'python':sys.executable,'script':str(SCRIPT),'fixture':{'entries':rows[:2]}})
            self.assertEqual('passed',result['status']);self.assertEqual(sorted(result['named_ids']),result['adapter_dirs'])
            self.assertTrue(result['backup_created'])

    def test_manifest_write_failure_rolls_back_all_renames_and_previous_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));before=_files(models)
            replace=os.replace;failed=False
            def fail_once(source,target):
                nonlocal failed
                if Path(target)==models/'manifest.json' and not failed:
                    failed=True;raise OSError('manifest publication failed')
                return replace(source,target)
            with patch('os.replace',side_effect=fail_once):
                self.assertEqual(1,_invoke(models,'--apply'))
            self.assertTrue(failed)
            self.assertEqual({**before,'manifest.json.lock':b''},_files(models));self.assert_clean(models)

    def test_recovery_survives_more_hard_exits_in_gather_restore_metadata_and_cleanup(self):
        for point in ('gather','restore','manifest','terminal','cleanup'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                models,_rows=self.fixture(Path(tmp));before=_files(models)
                self.child_crash(models,'second')
                def recovery():
                    rename,replace,rmtree=os.rename,os.replace,shutil.rmtree
                    def moved(source,target):
                        result=rename(source,target);dst=Path(target)
                        if ((point=='gather' and dst.parent.name=='adapters' and dst.name=='0')
                                or (point=='restore' and dst==models/'raw_a')):os._exit(73)
                        return result
                    def replaced(source,target):
                        result=replace(source,target);dst=Path(target)
                        if point=='manifest' and dst==models/'manifest.json':os._exit(73)
                        if point=='terminal' and dst.name==NAMING_PUBLICATION_JOURNAL and json.loads(dst.read_text())['phase']=='recovered':os._exit(73)
                        return result
                    def cleaned(target,*args,**kwargs):
                        result=rmtree(target,*args,**kwargs)
                        if point=='cleanup' and Path(target).name.startswith('.naming-'):os._exit(73)
                        return result
                    with patch('os.rename',side_effect=moved),patch('os.replace',side_effect=replaced),patch('shutil.rmtree',side_effect=cleaned):
                        _invoke(models,'--recover')
                child=multiprocessing.get_context('fork').Process(target=recovery);child.start();child.join(10)
                if child.is_alive():child.kill();child.join(5);self.fail('Recovery interruption fixture hung')
                self.assertEqual(73,child.exitcode)
                self.assertEqual(0,_invoke(models,'--recover'));self.assertEqual(0,_invoke(models,'--recover'))
                self.assertEqual({**before,'manifest.json.lock':b''},_files(models));self.assert_clean(models)

    def test_corrupt_snapshot_refuses_before_any_recovery_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));self.child_crash(models,'first')
            journal=json.loads((models/NAMING_PUBLICATION_JOURNAL).read_text())
            snapshot=models/journal['workspace']/'manifest.before'
            original=snapshot.read_bytes();snapshot.write_bytes(b'corrupt');before=_files(models)
            self.assertEqual(1,_invoke(models,'--recover'));self.assertEqual(before,_files(models))
            snapshot.write_bytes(original);self.assertEqual(0,_invoke(models,'--recover'));self.assert_clean(models)

    def test_pending_naming_blocks_promotion_and_actual_http_writer(self):
        import copy
        import core
        import promote_adapters
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import lora
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));self.child_crash(models,'first');before=_files(models)
            with patch.object(promote_adapters,'MODELS',str(models)):
                for action in (lambda:promote_adapters.promote(['raw_a'],'stamp',False),lambda:promote_adapters.rollback('stamp'),promote_adapters.recover_publication):
                    with self.assertRaisesRegex(ValueError,'naming recovery'):
                        action()
            state=copy.deepcopy(core.process_state)
            for entry in state.values():entry['running']=False
            app=FastAPI();app.include_router(lora.router)
            with patch.object(core,'process_state',state),patch.object(lora,'process_state',state),patch.object(lora,'LORA_MODELS_DIR',str(models)),patch.object(lora,'LORA_MODELS_MANIFEST',str(models/'manifest.json')),TestClient(app) as client:
                response=client.post('/api/lora/models/raw_b/promote')
                self.assertEqual(409,response.status_code,response.text)
                self.assertIn('name_voices.py --recover',response.json()['detail'])
                self.assertFalse(state['lora_training']['running'])
            self.assertEqual(before,_files(models));self.assertEqual(0,_invoke(models,'--recover'));self.assert_clean(models)

    def test_root_manifest_lock_admits_no_plan_reads_until_other_writer_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp))
            entered=multiprocessing.get_context('fork').Event()
            def run():
                read=subject.get_voice_manifest
                def observed(path):entered.set();return read(path)
                with patch.object(subject,'get_voice_manifest',side_effect=observed):
                    os._exit(_invoke(models,'--apply'))
            child=None
            try:
                with lock_adapter_naming(str(models),str(models/'manifest.json')):
                    child=multiprocessing.get_context('fork').Process(target=run);child.start()
                    self.assertFalse(entered.wait(0.15));self.assertTrue(child.is_alive())
                child.join(10);self.assertEqual(0,child.exitcode);self.assertTrue(entered.is_set())
            finally:
                if child is not None:
                    if child.is_alive():child.kill()
                    child.join(5)
            self.assert_clean(models)

    def test_apply_recovers_interruption_before_computing_fresh_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=self.fixture(Path(tmp));bundles={r['id']:_files(models/r['id']) for r in rows}
            self.child_crash(models,'manifest')
            self.assertEqual(0,_invoke(models,'--apply'))
            named=json.loads((models/'manifest.json').read_text())
            for original,current in zip(rows,named):
                self.assertEqual(bundles[original['id']],_files(models/current['id']))
            self.assert_clean(models)

    def test_naming_refuses_pending_promotion_without_modifying_resources(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp));(models/CLI_PUBLICATION_JOURNAL).write_text('{broken')
            with lock_adapter_naming(str(models),str(models/'manifest.json')):pass
            before=_files(models)
            self.assertEqual(1,_invoke(models,'--apply'));self.assertEqual(1,_invoke(models,'--recover'))
            self.assertEqual(before,_files(models));self.assertFalse((models/NAMING_PUBLICATION_JOURNAL).exists())

    def test_stage_runs_without_site_packages_or_ml_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            models,_rows=self.fixture(Path(tmp))
            result=subprocess.run([sys.executable,'-S',str(SCRIPT),'--manifest',str(models/'manifest.json'),
                                   '--models-dir',str(models),'--apply'],capture_output=True,text=True,timeout=10)
            self.assertEqual(0,result.returncode,result.stderr)
            self.assertTrue((models/'manifest.json.bak').is_file())
            self.assertEqual(3,len(json.loads((models/'manifest.json').read_text())))
            self.assert_clean(models)
