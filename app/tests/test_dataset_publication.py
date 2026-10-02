"""Native publication artifacts: readers never see a partially built dataset."""
import asyncio
import contextlib
import io
import multiprocessing
import os
import queue
import tempfile
import shutil
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException, UploadFile
from routers import dataset_builder as builder, lora


class DatasetPublicationTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        from tests.test_dataset_builder_ownership import DatasetBuilderOwnershipTests
        with DatasetBuilderOwnershipTests().fixture() as context:
            context[-2]['dataset_builder']['running'] = False
            yield context

    def test_builder_is_invisible_until_all_audio_reference_and_metadata_are_ready(self):
        with self.fixture() as (_,_,work,output,_,_), patch.object(lora,'LORA_DATASETS_DIR',str(output)):
            entered=threading.Event();release=threading.Event();real_copy=shutil.copy2
            def held_copy(source,destination):
                result=real_copy(source,destination)
                if Path(destination).name=='sample_000.wav':
                    entered.set();self.assertTrue(release.wait(3))
                return result
            with patch.object(builder.shutil,'copy2',side_effect=held_copy),ThreadPoolExecutor(1) as pool:
                future=pool.submit(builder._dataset_builder_save_sync,builder.DatasetSaveRequest(name='voice',ref_index=1))
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertFalse((output/'voice').exists(),'final directory exposed mid-copy')
                    self.assertEqual([],asyncio.run(lora.lora_list_datasets()))
                finally:release.set()
                self.assertEqual('saved',future.result()['status'])
            final=output/'voice'
            self.assertEqual((work/'sample_001.wav').read_bytes(),(final/'ref.wav').read_bytes())
            self.assertTrue((final/'ref_text.txt').is_file());self.assertTrue((final/'metadata.jsonl').is_file())
            self.assertEqual([{'dataset_id':'voice','sample_count':2}],asyncio.run(lora.lora_list_datasets()))

    def test_failed_copy_cannot_delete_a_destination_created_by_another_writer(self):
        with self.fixture() as (_,_,work,output,_,_):
            def failed_copy(source,destination):
                final=output/'voice';final.mkdir(parents=True,exist_ok=True)
                (final/'other-writer.txt').write_bytes(b'preserve this exact published artifact')
                raise OSError('injected copy failure')
            with patch.object(builder.shutil,'copy2',side_effect=failed_copy):
                with self.assertRaises(HTTPException) as error:
                    builder._dataset_builder_save_sync(builder.DatasetSaveRequest(name='voice',ref_index=0))
            self.assertEqual(500,error.exception.status_code)
            self.assertTrue((output/'voice/other-writer.txt').is_file(),'another writer artifact was deleted')
            self.assertEqual(b'preserve this exact published artifact',(output/'voice/other-writer.txt').read_bytes())
            self.assertFalse(any(p.is_dir() and p.name.startswith('.dataset-stage-') for p in output.iterdir()))

    def test_reference_reuses_the_staged_sample_instead_of_rereading_working_audio(self):
        with self.fixture() as (_,_,work,output,_,_):
            real_copy=shutil.copy2;calls=[]
            def copy(source,destination):
                calls.append((Path(source),Path(destination)));return real_copy(source,destination)
            with patch.object(builder.shutil,'copy2',side_effect=copy):
                builder._dataset_builder_save_sync(builder.DatasetSaveRequest(name='voice',ref_index=1))
            reference=[source for source,destination in calls if destination.name=='ref.wav']
            self.assertEqual(1,len(reference));self.assertNotEqual(work,reference[0].parent)
            self.assertEqual('sample_001.wav',reference[0].name)
            self.assertEqual(2,sum(source.parent==work for source,_ in calls))
            self.assertEqual((output/'voice/sample_001.wav').read_bytes(),(output/'voice/ref.wav').read_bytes())

    def test_upload_is_invisible_during_extraction_and_publishes_validated_rows(self):
        with self.fixture() as (_,_,work,output,_,_),patch.object(lora,'LORA_DATASETS_DIR',str(output)):
            document=io.BytesIO()
            with zipfile.ZipFile(document,'w') as archive:
                archive.writestr('sample.wav',(work/'sample_000.wav').read_bytes())
                archive.writestr('metadata.jsonl','{"audio_filepath":"sample.wav","text":"Hello"}\n')
            entered=threading.Event();release=threading.Event();real_extract=lora._extract_lora_dataset_archive
            def held_extract(*args):
                real_extract(*args);entered.set();self.assertTrue(release.wait(3))
            async def exercise():
                task=asyncio.create_task(lora.lora_upload_dataset(UploadFile(filename='voice.zip',file=io.BytesIO(document.getvalue()))))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,2))
                    self.assertFalse((output/'voice').exists(),'upload directory advertised before validation')
                    self.assertEqual([],await lora.lora_list_datasets())
                finally:release.set();result=await task
                return result
            with patch.object(lora,'_extract_lora_dataset_archive',side_effect=held_extract):result=asyncio.run(exercise())
            self.assertEqual(1,result['sample_count']);self.assertTrue((output/'voice/metadata.jsonl').is_file())

    def test_two_native_processes_cannot_interleave_same_destination_saves(self):
        # Fork is the native Linux evidence here; do not imply Windows coverage.
        context=multiprocessing.get_context('fork')
        with self.fixture() as (_,_,work,output,_,_):
            entered=context.Event();release=context.Event();attempted=context.Event();results=context.Queue()
            def save(hold):
                if not hold:attempted.set()
                real_copy=shutil.copy2
                def copy(source,destination):
                    result=real_copy(source,destination)
                    if hold and Path(destination).name=='sample_000.wav':
                        entered.set()
                        if not release.wait(4):raise RuntimeError('fixture timeout')
                    return result
                try:
                    with patch.object(builder.shutil,'copy2',side_effect=copy):
                        result=builder._dataset_builder_save_sync(builder.DatasetSaveRequest(name='voice',ref_index=1))
                    results.put((hold,result['status']))
                except HTTPException as error:results.put((hold,error.status_code))
            first=context.Process(target=save,args=(True,));second=context.Process(target=save,args=(False,))
            first.start()
            try:
                self.assertTrue(entered.wait(2));second.start();self.assertTrue(attempted.wait(2))
                with self.assertRaises(queue.Empty):results.get(timeout=.15)
            finally:
                release.set();first.join(5)
                if second.pid is not None:second.join(5)
                for process in (first,second):
                    if process.pid is not None and process.is_alive():process.terminate();process.join()
            self.assertEqual(0,first.exitcode);self.assertEqual(0,second.exitcode)
            self.assertEqual({(True,'saved'),(False,400)},{results.get(timeout=1),results.get(timeout=1)})
            self.assertEqual((work/'sample_001.wav').read_bytes(),(output/'voice/ref.wav').read_bytes())
            self.assertTrue((output/'voice/metadata.jsonl').is_file())

    def test_interrupted_private_stage_is_hidden_and_does_not_block_a_new_publish(self):
        from dataset_publication import apply_dataset_publication
        context=multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp,patch.object(lora,'LORA_DATASETS_DIR',tmp):
            def crash():
                with apply_dataset_publication(tmp,'voice') as stage:
                    (Path(stage)/'partial.wav').write_bytes(b'partial private bytes')
                    os._exit(77)
            process=context.Process(target=crash);process.start();process.join(5)
            try:self.assertEqual(77,process.exitcode)
            finally:
                if process.is_alive():process.terminate();process.join()
            self.assertFalse((Path(tmp)/'voice').exists());self.assertEqual([],asyncio.run(lora.lora_list_datasets()))
            abandoned=[p for p in Path(tmp).iterdir() if p.is_dir() and p.name.startswith('.dataset-stage-')];self.assertEqual(1,len(abandoned))
            self.assertEqual(0, os.stat(abandoned[0]).st_mode & 0o077)
            with apply_dataset_publication(tmp,'voice') as stage:
                (Path(stage)/'metadata.jsonl').write_text('{"text":"complete"}\n')
            self.assertEqual([{'dataset_id':'voice','sample_count':1}],asyncio.run(lora.lora_list_datasets()))
            self.assertEqual(b'partial private bytes',(abandoned[0]/'partial.wav').read_bytes())

    def test_cancelled_upload_leaves_archive_owned_until_worker_finishes_publication(self):
        with self.fixture() as (_,_,work,output,_,_),patch.object(lora,'LORA_DATASETS_DIR',str(output)):
            document=io.BytesIO()
            with zipfile.ZipFile(document,'w') as archive:
                archive.writestr('sample.wav',(work/'sample_000.wav').read_bytes())
                archive.writestr('metadata.jsonl','{"audio":"sample.wav","text":"Hello"}\n')
            entered=threading.Event();release=threading.Event();finished=threading.Event();archive_paths=[]
            real_extract=lora._extract_lora_dataset_archive
            def held_extract(archive_path,stage):
                archive_paths.append(Path(archive_path));entered.set()
                try:
                    self.assertTrue(release.wait(3));self.assertTrue(Path(archive_path).is_file())
                    real_extract(archive_path,stage)
                finally:finished.set()
            async def exercise():
                task=asyncio.create_task(lora.lora_upload_dataset(UploadFile(filename='voice.zip',file=io.BytesIO(document.getvalue()))))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait,2));task.cancel()
                    with self.assertRaises(asyncio.CancelledError):await task
                    self.assertTrue(archive_paths[0].is_file());self.assertFalse((output/'voice').exists())
                finally:release.set()
                self.assertTrue(await asyncio.to_thread(finished.wait,2))
            with patch.object(lora,'_extract_lora_dataset_archive',side_effect=held_extract):asyncio.run(exercise())
            # asyncio.run joins executor workers, including validation/publication cleanup.
            self.assertFalse(archive_paths[0].exists());self.assertTrue((output/'voice/metadata.jsonl').is_file())

    def test_internal_paths_cannot_be_selected_or_deleted_and_lock_suffix_is_a_valid_dataset_name(self):
        from dataset_publication import apply_dataset_publication
        with tempfile.TemporaryDirectory() as tmp,patch.object(lora,'LORA_DATASETS_DIR',tmp):
            for name in ('voice','voice.lock'):
                with apply_dataset_publication(tmp,name) as stage:
                    (Path(stage)/'metadata.jsonl').write_text('{"text":"complete"}\n')
            self.assertEqual(['voice','voice.lock'],[d['dataset_id'] for d in asyncio.run(lora.lora_list_datasets())])
            for name in ('.dataset-locks','.dataset-stage-test','alias/../.dataset-locks'):
                with self.subTest(name=name),self.assertRaises(HTTPException) as error:lora.get_lora_dataset_path(name)
                self.assertEqual(400,error.exception.status_code)
            self.assertTrue((Path(tmp)/'.dataset-locks/voice.lock').is_file())

    def test_upload_and_builder_share_the_destination_lock_and_preserve_upload_bytes(self):
        with self.fixture() as (_,_,work,output,_,_),patch.object(lora,'LORA_DATASETS_DIR',str(output)):
            document=io.BytesIO();audio=(work/'sample_000.wav').read_bytes()
            with zipfile.ZipFile(document,'w') as archive:
                archive.writestr('uploaded.wav',audio)
                archive.writestr('metadata.jsonl','{"audio":"uploaded.wav","text":"Uploaded"}\n')
            entered=threading.Event();release=threading.Event();attempted=threading.Event();real_extract=lora._extract_lora_dataset_archive
            def held_extract(*args):
                real_extract(*args);entered.set();self.assertTrue(release.wait(3))
            def save():
                attempted.set()
                try:builder._dataset_builder_save_sync(builder.DatasetSaveRequest(name='voice',ref_index=0))
                except HTTPException as error:return error.status_code
                return 200
            def upload():return asyncio.run(lora.lora_upload_dataset(UploadFile(filename='voice.zip',file=io.BytesIO(document.getvalue()))))
            with patch.object(lora,'_extract_lora_dataset_archive',side_effect=held_extract),ThreadPoolExecutor(2) as pool:
                first=pool.submit(upload)
                try:
                    self.assertTrue(entered.wait(2));second=pool.submit(save);self.assertTrue(attempted.wait(2))
                    from concurrent.futures import TimeoutError
                    with self.assertRaises(TimeoutError):second.result(timeout=.15)
                finally:release.set()
                self.assertEqual('uploaded',first.result()['status']);self.assertEqual(400,second.result())
            self.assertEqual(audio,(output/'voice/uploaded.wav').read_bytes())
            self.assertEqual('{"audio":"uploaded.wav","text":"Uploaded"}\n',(output/'voice/metadata.jsonl').read_text())
            self.assertFalse((output/'voice/ref.wav').exists())

    def test_directory_publication_failure_cleans_only_private_stage_and_preserves_working_audio(self):
        with self.fixture() as (_,_,work,output,_,_):
            before={p.name:p.read_bytes() for p in work.iterdir()}
            with patch('dataset_publication.os.rename',side_effect=OSError('injected rename failure')):
                with self.assertRaises(HTTPException) as error:
                    builder._dataset_builder_save_sync(builder.DatasetSaveRequest(name='voice',ref_index=1))
            self.assertEqual(500,error.exception.status_code);self.assertFalse((output/'voice').exists())
            self.assertEqual(before,{p.name:p.read_bytes() for p in work.iterdir()})
            self.assertEqual(['.dataset-locks'],[p.name for p in output.iterdir()])
