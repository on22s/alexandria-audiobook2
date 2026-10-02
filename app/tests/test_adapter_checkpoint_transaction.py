"""Whole adapter generations: actual tensor/PCM artifacts and process interruption."""
from pathlib import Path
import json
import multiprocessing
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from adapter_checkpoint_transaction import (apply_adapter_checkpoint_locked, ensure_adapter_checkpoint,
    get_adapter_checkpoint_journal, get_adapter_generation_sha256, recover_adapter_checkpoint_locked,
    save_adapter_checkpoint, validate_adapter_checkpoint_generation)
from adapter_publication import CLI_PUBLICATION_JOURNAL, NAMING_PUBLICATION_JOURNAL
from lora_evidence import get_file_sha256
from tests.test_support import write_test_adapter, assert_file_lock_released

SCRIPT=Path(__file__).resolve().parent.parent/'adapter_checkpoint_transaction.py'


def _write(path, value=2):
    path=Path(path);write_test_adapter(path,value=value)
    sf.write(str(path/'ref_sample.wav'),np.full(2400,0.05*value,dtype=np.float32),24000)
    meta={'ref_sample_text':f'generation {value}', 'checkpoint_sha256':get_file_sha256(str(path/'adapter_model.safetensors')),
          'reference_audio_sha256':get_file_sha256(str(path/'ref_sample.wav')), 'num_samples':1}
    (path/'training_meta.json').write_text(json.dumps(meta))
    (path/'README.md').write_text(f'generation {value}')


def _files(root):
    return {str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def _crash_save(adapter, point):
    import adapter_checkpoint_transaction as subject
    move, journal, rmtree=subject._move, subject._save_journal, shutil.rmtree
    def moved(source,target):
        move(source,target)
        if (point=='original' and Path(target).name=='previous') or (point=='replacement' and Path(target)==adapter):os._exit(79)
    def recorded(root,data):
        journal(root,data)
        if (point=='journal' and data['phase']=='publishing') or (point=='committed' and data['phase']=='committed'):os._exit(79)
    def removed(path,*args,**kwargs):
        if point=='partial-cleanup' and Path(path).name.startswith('.checkpoint-generation-'):
            rmtree(Path(path)/'previous'/'notes');os._exit(79)
        rmtree(path,*args,**kwargs)
        if point=='cleaned' and Path(path).name.startswith('.checkpoint-generation-'):os._exit(79)
    with patch.object(subject,'_move',side_effect=moved),patch.object(subject,'_save_journal',side_effect=recorded),patch('shutil.rmtree',side_effect=removed):
        save_adapter_checkpoint(adapter,lambda path:_write(path))
    os._exit(0)


def _crash_recover(adapter, point):
    import adapter_checkpoint_transaction as subject
    move,journal,rmtree=subject._move,subject._save_journal,shutil.rmtree
    def moved(source,target):
        move(source,target)
        if (point=='rejected' and Path(target).name=='rejected') or (point=='restored' and Path(target)==adapter):os._exit(81)
    def recorded(root,data):
        journal(root,data)
        if point=='terminal' and data['phase']=='recovered':os._exit(81)
    def removed(path,*args,**kwargs):
        rmtree(path,*args,**kwargs)
        if point=='cleaned' and Path(path).name.startswith('.checkpoint-generation-'):os._exit(81)
    with patch.object(subject,'_move',side_effect=moved),patch.object(subject,'_save_journal',side_effect=recorded),patch('shutil.rmtree',side_effect=removed):
        with ensure_adapter_checkpoint(adapter):pass
    os._exit(0)


class AdapterCheckpointTransactionTests(unittest.TestCase):
    def fixture(self,root,existing=True):
        adapter=root/'models'/'voice';adapter.parent.mkdir()
        if existing:
            _write(adapter,1);(adapter/'notes').mkdir();(adapter/'notes'/'keep.txt').write_text('human auxiliary evidence')
        return adapter

    def child(self,target,*args):
        process=multiprocessing.get_context('fork').Process(target=target,args=args);process.start();process.join(10)
        if process.is_alive():process.terminate();process.join(5);self.fail('owned child did not finish')
        return process.exitcode

    def recover(self,adapter):
        result=subprocess.run([sys.executable,'-S',str(SCRIPT),'--recover-output',str(adapter)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_successful_generation_preserves_auxiliaries_and_owns_matching_real_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=self.fixture(Path(tmp));before=get_adapter_generation_sha256(adapter)
            after=save_adapter_checkpoint(adapter,lambda path:_write(path))
            self.assertNotEqual(before,after);self.assertEqual(after,get_adapter_generation_sha256(adapter))
            self.assertEqual('generation 2',validate_adapter_checkpoint_generation(adapter)['ref_sample_text'])
            self.assertEqual('human auxiliary evidence',(adapter/'notes'/'keep.txt').read_text())
            self.assertFalse(get_adapter_checkpoint_journal(adapter).exists());self.assertFalse(list(adapter.parent.glob('.checkpoint-generation-*')))

    def test_failed_or_incomplete_staging_preserves_original_bundle_bytes(self):
        for kind in ('writer','weights','meta','reference','link'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                adapter=self.fixture(Path(tmp));before=_files(adapter)
                def writer(path):
                    path=Path(path)
                    if kind=='writer':(path/'adapter_model.safetensors').write_bytes(b'partial tensor');raise RuntimeError('save interrupted')
                    _write(path)
                    if kind=='weights':(path/'adapter_model.safetensors').write_bytes(b'not tensors')
                    if kind=='meta':(path/'training_meta.json').write_text('{"checkpoint_sha256":"wrong","ref_sample_text":"present"}')
                    if kind=='reference':
                        (path/'ref_sample.wav').write_bytes(b'bad reference');meta=json.loads((path/'training_meta.json').read_text());meta['reference_audio_sha256']=get_file_sha256(str(path/'ref_sample.wav'));(path/'training_meta.json').write_text(json.dumps(meta))
                    if kind=='link':(path/'adapter_model.safetensors').unlink();(path/'adapter_model.safetensors').symlink_to(adapter/'adapter_model.safetensors')
                with self.assertRaises((ValueError,RuntimeError)):save_adapter_checkpoint(adapter,writer)
                self.assertEqual(before,_files(adapter));self.assertFalse(get_adapter_checkpoint_journal(adapter).exists())

    def test_hard_exit_before_commit_restores_original_after_commit_keeps_new_on_fresh_cli(self):
        for existing in (True,False):
            for point in ('journal','original','replacement','committed','partial-cleanup','cleaned'):
                if not existing and point in ('original','partial-cleanup'):continue
                with self.subTest(existing=existing,point=point),tempfile.TemporaryDirectory() as tmp:
                    adapter=self.fixture(Path(tmp),existing);before=_files(adapter) if existing else None
                    self.assertEqual(79,self.child(_crash_save,adapter,point));self.assertTrue(get_adapter_checkpoint_journal(adapter).exists())
                    self.recover(adapter);self.recover(adapter)
                    if point in ('committed','partial-cleanup','cleaned'):
                        self.assertEqual('generation 2',validate_adapter_checkpoint_generation(adapter)['ref_sample_text'])
                        if existing:self.assertEqual('human auxiliary evidence',(adapter/'notes'/'keep.txt').read_text())
                    elif existing:self.assertEqual(before,_files(adapter))
                    else:self.assertFalse(adapter.exists())
                    self.assertFalse(get_adapter_checkpoint_journal(adapter).exists())

    def test_interrupted_recovery_resumes_after_each_native_restore_and_terminal_cleanup(self):
        for point in ('rejected','restored','terminal','cleaned'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                adapter=self.fixture(Path(tmp));before=_files(adapter)
                self.assertEqual(79,self.child(_crash_save,adapter,'replacement'));self.assertEqual(81,self.child(_crash_recover,adapter,point))
                self.recover(adapter);self.recover(adapter);self.assertEqual(before,_files(adapter));self.assertFalse(get_adapter_checkpoint_journal(adapter).exists())

    def test_corrupt_original_and_unowned_journal_paths_refuse_without_mutation(self):
        for kind in ('original','workspace','adapter','prepared'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                adapter=self.fixture(Path(tmp));self.assertEqual(79,self.child(_crash_save,adapter,'original'))
                journal=get_adapter_checkpoint_journal(adapter);data=json.loads(journal.read_text());workspace=adapter.parent/data['workspace']
                if kind=='original':(workspace/'previous'/'notes'/'keep.txt').write_text('corrupt original evidence')
                elif kind=='prepared':(workspace/'prepared'/'README.md').write_text('unowned changed replacement')
                else:data[kind]='../outside';journal.write_text(json.dumps(data))
                before=_files(adapter.parent)
                with self.assertRaises(ValueError):
                    with ensure_adapter_checkpoint(adapter):pass
                self.assertEqual(before,_files(adapter.parent));self.assertTrue(journal.exists())

    def test_reader_waits_on_publication_and_observes_one_complete_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=self.fixture(Path(tmp));arrived=threading.Event();release=threading.Event();read=[];errors=[]
            def writer(path):_write(path);arrived.set();self.assertTrue(release.wait(5))
            def publish():
                try:save_adapter_checkpoint(adapter,writer)
                except Exception as error:errors.append(error)
            def inspect():
                try:
                    with ensure_adapter_checkpoint(adapter):read.append(validate_adapter_checkpoint_generation(adapter))
                except Exception as error:errors.append(error)
            publishing=threading.Thread(target=publish);publishing.start();self.assertTrue(arrived.wait(5));reader=threading.Thread(target=inspect);reader.start()
            try:time.sleep(.05);self.assertEqual([],read);self.assertTrue(reader.is_alive())
            finally:release.set();publishing.join(5);reader.join(5)
            self.assertFalse(publishing.is_alive());self.assertFalse(reader.is_alive());self.assertEqual([],errors);self.assertEqual('generation 2',read[0]['ref_sample_text'])

    def test_shared_family_admission_refuses_pending_cli_or_legacy_swap(self):
        for marker in (CLI_PUBLICATION_JOURNAL,NAMING_PUBLICATION_JOURNAL,'.checkpoint_swap.json'):
            with self.subTest(marker=marker),tempfile.TemporaryDirectory() as tmp:
                adapter=self.fixture(Path(tmp));target=adapter/marker if marker=='.checkpoint_swap.json' else adapter.parent/marker;target.write_text('{broken')
                (adapter.parent/'manifest.json.lock').touch()
                before=_files(adapter.parent)
                with self.assertRaises(ValueError):save_adapter_checkpoint(adapter,lambda path:_write(path))
                self.assertEqual(before,_files(adapter.parent))
                assert_file_lock_released(str(adapter.parent/'manifest.json'))

    def test_journal_replaced_before_durability_error_is_recovered_from_actual_marker(self):
        import adapter_checkpoint_transaction as subject
        with tempfile.TemporaryDirectory() as tmp:
            adapter=self.fixture(Path(tmp));before=_files(adapter);save=subject._save_journal
            def failed_sync(root,data):
                save(root,data)
                if data['phase']=='publishing':raise OSError('directory fsync failed after journal rename')
            with patch.object(subject,'_save_journal',side_effect=failed_sync):
                with self.assertRaisesRegex(OSError,'fsync failed'):save_adapter_checkpoint(adapter,lambda path:_write(path))
            self.assertEqual(before,_files(adapter));self.assertFalse(get_adapter_checkpoint_journal(adapter).exists());assert_file_lock_released(str(adapter.parent/'manifest.json'))

    def test_generation_identity_includes_each_serving_file_and_excludes_auxiliary_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=self.fixture(Path(tmp));original=get_adapter_generation_sha256(adapter)
            (adapter/'notes'/'keep.txt').write_text('updated non-serving evidence');self.assertEqual(original,get_adapter_generation_sha256(adapter))
            for name in ('adapter_config.json','adapter_model.safetensors','ref_sample.wav','training_meta.json'):
                with self.subTest(name=name):
                    path=adapter/name;before=path.read_bytes();path.write_bytes(before+b'x');self.assertNotEqual(original,get_adapter_generation_sha256(adapter));path.write_bytes(before);self.assertEqual(original,get_adapter_generation_sha256(adapter))

    def test_unlocked_auxiliary_change_during_staging_is_preserved_and_publication_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter=self.fixture(Path(tmp));weights=(adapter/'adapter_model.safetensors').read_bytes()
            def writer(staging):
                _write(staging);(adapter/'notes'/'keep.txt').write_text('new human note during staging')
            with self.assertRaisesRegex(ValueError,'changed'):save_adapter_checkpoint(adapter,writer)
            self.assertEqual(weights,(adapter/'adapter_model.safetensors').read_bytes());self.assertEqual('new human note during staging',(adapter/'notes'/'keep.txt').read_text());self.assertFalse(get_adapter_checkpoint_journal(adapter).exists())


class CheckpointGenerationFamilyAdmissionTests(unittest.TestCase):
    def test_pending_generation_fences_other_training_http_cli_and_naming_without_mutation(self):
        from fastapi import HTTPException
        from routers import lora
        import promote_adapters as promotion
        from tests.test_adapter_naming_transaction import _invoke
        from adapter_publication import get_adapter_publication_recovery_command
        from utils import file_lock
        for mode in ('valid', 'malformed', 'symlink', 'wrong_identity'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                models=Path(tmp);first=models/'first';second=models/'second'
                _write(first);_write(second);manifest=models/'manifest.json';manifest.write_text('[]')
                journal=get_adapter_checkpoint_journal(first)
                if mode=='valid':journal.write_text(json.dumps({'adapter':'first'}))
                elif mode=='malformed':journal.write_text('{broken')
                elif mode=='wrong_identity':journal.write_text(json.dumps({'adapter':'../outside'}))
                else:journal.symlink_to(models/'absent')
                with file_lock(str(manifest)):pass
                before={str(p.relative_to(models)):p.read_bytes() for p in models.rglob('*') if p.is_file() and not p.is_symlink()}
                links={str(p.relative_to(models)):os.readlink(p) for p in models.rglob('*') if p.is_symlink()}
                command=get_adapter_publication_recovery_command(str(models))
                self.assertTrue(command)
                self.assertIn('--recover-output' if mode=='valid' else 'inspect retained',command)
                reached=[]
                with self.assertRaises(ValueError):save_adapter_checkpoint(second,lambda stage:reached.append(stage))
                self.assertEqual([],reached)
                for operation in (lora._promote_lora_candidate,lora._rollback_lora_promotion,
                                  lora._recover_checkpoint_swap,lora._delete_rollback_backup):
                    with self.assertRaises(HTTPException) as rejected:operation('second',str(models),str(manifest))
                    self.assertEqual(409,rejected.exception.status_code)
                with patch.object(promotion,'MODELS',str(models)):
                    for operation in (lambda:promotion.promote([], 'stamp', True),lambda:promotion.rollback('stamp'),promotion.recover_publication):
                        with self.assertRaises(ValueError):operation()
                for arguments in ((),('--apply',),('--verify',),('--recover',)):
                    self.assertEqual(1,_invoke(models,*arguments))
                self.assertEqual(before,{str(p.relative_to(models)):p.read_bytes() for p in models.rglob('*') if p.is_file() and not p.is_symlink()})
                self.assertEqual(links,{str(p.relative_to(models)):os.readlink(p) for p in models.rglob('*') if p.is_symlink()})
                journal.unlink()
                with ensure_adapter_checkpoint(second):pass

    def test_real_interrupted_owner_can_recover_before_other_generation_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            models=Path(tmp);first=models/'first';second=models/'second';_write(first,1);_write(second,1)
            original=_files(first)
            helper=AdapterCheckpointTransactionTests()
            self.assertEqual(79,helper.child(_crash_save,first,'original'))
            self.assertFalse(first.exists())
            self.assertTrue(get_adapter_checkpoint_journal(first).exists())
            with self.assertRaises(ValueError):
                with ensure_adapter_checkpoint(second):self.fail('unrecovered family admitted')
            helper.recover(first)
            self.assertEqual(original,_files(first))
            self.assertFalse(get_adapter_checkpoint_journal(first).exists())
            with ensure_adapter_checkpoint(second):
                self.assertEqual('generation 1',validate_adapter_checkpoint_generation(second)['ref_sample_text'])


def _crash_manifest_publish(adapter, point):
    import adapter_checkpoint_transaction as subject
    move,journal,rmtree=subject._move,subject._save_journal,shutil.rmtree
    def moved(source,target):
        move(source,target)
        if ((point=='original' and Path(target).name=='previous')
                or (point=='adapter' and Path(target)==adapter)
                or (point=='manifest' and Path(target)==adapter.parent/'manifest.json')):os._exit(83)
    def recorded(root,data):
        journal(root,data)
        if (point=='journal' and data['phase']=='publishing') or (point=='committed' and data['phase']=='committed'):os._exit(83)
    def removed(path,*args,**kwargs):
        rmtree(path,*args,**kwargs)
        if point=='cleaned' and Path(path).name.startswith('.checkpoint-generation-'):os._exit(83)
    with patch.object(subject,'_move',side_effect=moved),patch.object(subject,'_save_journal',side_effect=recorded),patch('shutil.rmtree',side_effect=removed):
        save_adapter_checkpoint(adapter,lambda stage:_write(stage,2),manifest_entries=[{'id':adapter.name,'generation':2}])


def _crash_manifest_recover(adapter, point):
    import adapter_checkpoint_transaction as subject
    move,save=subject._move,subject.save_adapter_publication_bytes
    def moved(source,target):
        move(source,target)
        if point=='adapter_restored' and Path(target)==adapter:os._exit(84)
    def saved(path,*args,**kwargs):
        save(path,*args,**kwargs)
        if point=='manifest_restored' and Path(path)==adapter.parent/'manifest.json':os._exit(84)
    with patch.object(subject,'_move',side_effect=moved),patch.object(subject,'save_adapter_publication_bytes',side_effect=saved):
        with ensure_adapter_checkpoint(adapter):pass


class CheckpointManifestTransactionTests(unittest.TestCase):
    def fixture(self,root):
        adapter=root/'voice';_write(adapter,1);(adapter/'notes.txt').write_text('human auxiliary')
        manifest=root/'manifest.json';manifest.write_text('[ {"id": "voice", "generation": 1} ]\n')
        return adapter,manifest,_files(adapter),manifest.read_bytes()

    def test_publication_commits_adapter_and_manifest_without_mutating_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter,manifest,old,_=self.fixture(Path(tmp));entries=[{'id':'voice','generation':2}]
            save_adapter_checkpoint(adapter,lambda stage:_write(stage,2),manifest_entries=entries)
            self.assertEqual([{'id':'voice','generation':2}],entries)
            self.assertEqual(entries,json.loads(manifest.read_text()))
            self.assertEqual('generation 2',validate_adapter_checkpoint_generation(adapter)['ref_sample_text'])
            self.assertEqual(old['notes.txt'],(adapter/'notes.txt').read_bytes())
            self.assertFalse(get_adapter_checkpoint_journal(adapter).exists())

    def test_hard_exit_recovers_both_artifacts_before_commit_and_keeps_both_after_commit(self):
        helper=AdapterCheckpointTransactionTests()
        for point in ('journal','original','adapter','manifest','committed','cleaned'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                adapter,manifest,old,old_manifest=self.fixture(Path(tmp))
                self.assertEqual(83,helper.child(_crash_manifest_publish,adapter,point))
                helper.recover(adapter);helper.recover(adapter)
                if point in ('committed','cleaned'):
                    self.assertEqual(2,json.loads(manifest.read_text())[0]['generation'])
                    self.assertEqual('generation 2',validate_adapter_checkpoint_generation(adapter)['ref_sample_text'])
                else:
                    self.assertEqual(old,_files(adapter));self.assertEqual(old_manifest,manifest.read_bytes())

    def test_manifest_and_adapter_recovery_remain_repeatable_after_recovery_hard_exit(self):
        helper=AdapterCheckpointTransactionTests()
        for point in ('adapter_restored','manifest_restored'):
            with self.subTest(point=point),tempfile.TemporaryDirectory() as tmp:
                adapter,manifest,old,old_manifest=self.fixture(Path(tmp))
                self.assertEqual(83,helper.child(_crash_manifest_publish,adapter,'manifest'))
                self.assertEqual(84,helper.child(_crash_manifest_recover,adapter,point))
                helper.recover(adapter);helper.recover(adapter)
                self.assertEqual(old,_files(adapter));self.assertEqual(old_manifest,manifest.read_bytes())

    def test_changed_live_manifest_or_corrupt_snapshot_refuses_recovery_before_adapter_moves(self):
        helper=AdapterCheckpointTransactionTests()
        for target in ('live','original'):
            with self.subTest(target=target),tempfile.TemporaryDirectory() as tmp:
                adapter,manifest,_,_=self.fixture(Path(tmp))
                self.assertEqual(83,helper.child(_crash_manifest_publish,adapter,'manifest'))
                data=json.loads(get_adapter_checkpoint_journal(adapter).read_text())
                path=manifest if target=='live' else adapter.parent/data['workspace']/'manifest.original'
                path.write_text('[{"id":"external change"}]')
                before=_files(adapter.parent)
                with self.assertRaises(ValueError):
                    with ensure_adapter_checkpoint(adapter):pass
                self.assertEqual(before,_files(adapter.parent))
                self.assertTrue(get_adapter_checkpoint_journal(adapter).exists())

    def test_staging_does_not_overwrite_an_unlocked_manifest_edit_and_failed_manifest_move_restores_both(self):
        import adapter_checkpoint_transaction as subject
        for mode in ('external_edit','failed_move'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                adapter,manifest,old,old_manifest=self.fixture(Path(tmp));entries=[{'id':'voice','generation':2}]
                external=b'[{"id":"voice","human_note":"preserve"}]'
                def writer(stage):
                    _write(stage,2)
                    if mode=='external_edit':manifest.write_bytes(external)
                move=subject._move
                def moved(source,target):
                    move(source,target)
                    if Path(target)==manifest:raise OSError('manifest move completed before durability error')
                if mode=='external_edit':
                    with self.assertRaises(ValueError):save_adapter_checkpoint(adapter,writer,manifest_entries=entries)
                else:
                    with patch.object(subject,'_move',side_effect=moved),self.assertRaises(OSError):
                        save_adapter_checkpoint(adapter,writer,manifest_entries=entries)
                self.assertEqual(old,_files(adapter))
                self.assertEqual(external if mode=='external_edit' else old_manifest,manifest.read_bytes())
                self.assertEqual([{'id':'voice','generation':2}],entries)
                self.assertFalse(get_adapter_checkpoint_journal(adapter).exists())

    def test_broken_own_generation_journal_is_not_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter,manifest,old,old_manifest=self.fixture(Path(tmp));journal=get_adapter_checkpoint_journal(adapter)
            journal.symlink_to(adapter.parent/'absent')
            with self.assertRaises(ValueError):
                with ensure_adapter_checkpoint(adapter):self.fail('unsafe own marker admitted')
            self.assertEqual(old,_files(adapter));self.assertEqual(old_manifest,manifest.read_bytes())
            self.assertTrue(journal.is_symlink())
