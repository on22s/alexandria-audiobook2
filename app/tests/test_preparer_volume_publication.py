"""Native multi-volume failure, crash, reader and training-generation contracts."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import preparer as routes
from tests.test_preparer_run_state import preparer, ROOT
import alexandria_zip_publication as publication
import alexandria_batch_processor as batch
import corpus_run_report as corpus
from tools.voice_lab import batch_train_lora as training


class VolumePublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / 'dataset.zip'
        self.work = self.root / 'dataset_temp'
        self.work.mkdir()
        for i in range(2):
            with wave.open(str(self.work / f'sample_{i:04d}.wav'), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(24000)
                stream.writeframes(((16 + i).to_bytes(2, 'little', signed=True)
                                    + (-16 - i).to_bytes(2, 'little', signed=True)) * 120)
        self.old = [{'audio_filepath': f'sample_{i:04d}.wav', 'duration': 1.0,
                     'speaker_labels': ['UNKNOWN'], 'text': f'Old{i}'} for i in range(2)]
        self.new = [{**row, 'text': f'New{i}'} for i, row in enumerate(self.old)]
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        self.export(self.old)
        self.before = self.family_bytes()

    def export(self, rows):
        preparer._create_zip_dataset(rows, str(self.output), zip_max_files=1)

    def family_bytes(self):
        return {path.name: path.read_bytes() for path in self.root.iterdir()
                if path.suffix == '.zip' or path.name.endswith('.volumes.json')}

    def evidence(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob('*') if path.is_file()
                and not path.name.endswith('.volumes.lock')}

    def assert_old(self):
        self.assertEqual(self.before, self.family_bytes())
        self.assertFalse(list(self.root.glob('*' + publication.JOURNAL_SUFFIX)))
        self.assertFalse(list(self.root.glob('.*.volumes-stage-*')))

    def test_staged_fsync_requires_write_access_without_truncation_and_keeps_faults_visible(self):
        # Model FlushFileBuffers' writable-handle requirement with actual opened
        # descriptors; this is an access-contract control, not native Windows.
        open_file, fsync = Path.open, os.fsync
        handles, flushed, fail = {}, {}, [False]
        def get_bytes(path):
            with open_file(path, 'rb') as stream:
                return stream.read()
        def tracked_open(path, *args, **kwargs):
            staged = path.parent.name == 'next' and '.volumes-stage-' in path.parent.parent.name
            before = get_bytes(path) if staged and path.exists() else None
            stream = open_file(path, *args, **kwargs)
            if staged:
                handles[stream.fileno()] = (path, stream, before)
            return stream
        def checked_fsync(descriptor):
            item = handles.get(descriptor)
            if item is not None and not item[1].closed:
                path, stream, before = item
                if not stream.writable():
                    raise OSError(9, 'FlushFileBuffers requires write access')
                if before is not None:
                    self.assertEqual(before, get_bytes(path))
                if path.suffix == '.zip':
                    if fail[0]:
                        raise OSError('staged flush disk fault')
                    flushed[path.name] = before
            return fsync(descriptor)
        with patch.object(Path, 'open', tracked_open), patch.object(os, 'fsync', checked_fsync):
            self.export(self.new)
            self.assertEqual({'dataset_vol01.zip', 'dataset_vol02.zip'}, set(flushed))
            for name, content in flushed.items():
                self.assertEqual(content, (self.root / name).read_bytes())
            self.before = self.family_bytes()
            fail[0] = True
            with self.assertRaisesRegex(OSError, 'staged flush disk fault'):
                self.export(self.old)
            self.assert_old()

    def test_second_zip_write_retains_entire_old_family(self):
        real_zip = zipfile.ZipFile
        writes = []
        def fail(path, mode='r', *args, **kwargs):
            if mode == 'w':
                writes.append(path)
                if len(writes) == 2:
                    raise OSError('second volume disk fault')
            return real_zip(path, mode, *args, **kwargs)
        with patch.object(preparer.zipfile, 'ZipFile', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'second volume'):
                self.export(self.new)
        self.assertEqual(2, len(writes))
        self.assert_old()

    def test_stale_volume_removal_fault_restores_every_old_zip_and_manifest(self):
        replace, remove = os.replace, os.remove
        hit = []
        stale = self.root / 'dataset_vol02.zip'
        def fail_replace(source, destination, *args, **kwargs):
            if Path(source) == stale and Path(destination).parent.name == 'previous':
                hit.append('backup')
                raise OSError('stale removal fault')
            return replace(source, destination, *args, **kwargs)
        def fail_remove(path, *args, **kwargs):
            if Path(path) == stale:
                hit.append('delete')
                raise OSError('stale removal fault')
            return remove(path, *args, **kwargs)
        with patch.object(os, 'replace', side_effect=fail_replace), patch.object(os, 'remove', side_effect=fail_remove):
            with self.assertRaisesRegex(OSError, 'stale removal'):
                self.export(self.new[:1])
        self.assertEqual(1, len(hit))
        self.assert_old()

    def test_last_manifest_replace_fault_restores_entire_old_family(self):
        replace = os.replace
        hit = []
        manifest = Path(str(self.output) + '.volumes.json')
        def fail(source, destination, *args, **kwargs):
            if Path(destination) == manifest and Path(source).parent.name != 'previous' and not hit:
                hit.append(str(source))
                raise OSError('manifest replacement fault')
            return replace(source, destination, *args, **kwargs)
        with patch.object(os, 'replace', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'manifest replacement'):
                self.export(self.new)
        self.assertEqual(1, len(hit))
        self.assert_old()

    def test_baseexception_after_first_new_volume_rolls_back_exact_old_set(self):
        class Interrupted(BaseException):
            pass
        replace = os.replace
        hit = []
        def interrupt(source, destination, *args, **kwargs):
            result = replace(source, destination, *args, **kwargs)
            if Path(source).parent.name == 'next' and Path(destination).name == 'dataset_vol01.zip' and not hit:
                hit.append(True)
                raise Interrupted('controlled interrupt')
            return result
        with patch.object(os, 'replace', side_effect=interrupt):
            with self.assertRaises(Interrupted):
                self.export(self.new)
        self.assert_old()

    def crash(self, phase='new'):
        code = r'''
import json,os,sys
from pathlib import Path
sys.path[:0]=[sys.argv[1]+'/app',sys.argv[1]]
from tests.test_preparer_run_state import preparer
import alexandria_zip_publication as publication
replace=os.replace;write=publication.write_json_atomic
close=preparer.zipfile.ZipFile.close
def stage_exit(archive):
 result=close(archive)
 if sys.argv[3]=='stage' and archive.mode=='w' and Path(archive.filename).parent.name=='next':os._exit(73)
 return result
def terminate(source,destination,*a,**k):
 result=replace(source,destination,*a,**k)
 if (sys.argv[3]=='backup' and Path(destination).parent.name=='previous'
     or sys.argv[3]=='new' and Path(source).parent.name=='next' and str(destination).endswith('_vol01.zip')
     or sys.argv[3]=='manifest' and Path(source).parent.name=='next' and str(destination).endswith('.volumes.json')):
  os._exit(73)
 return result
def commit(document,path):
 result=write(document,path)
 if sys.argv[3]=='cleanup' and document.get('phase')=='cleanup':os._exit(73)
 return result
os.replace=terminate;publication.write_json_atomic=commit;preparer.zipfile.ZipFile.close=stage_exit
preparer._create_zip_dataset(json.loads(sys.argv[4]),sys.argv[2],zip_max_files=1)
'''
        result = subprocess.run([sys.executable, '-c', code, str(ROOT), str(self.output),
                                 phase, json.dumps(self.new)], cwd=self.root,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(73, result.returncode, result.stdout + result.stderr)

    def test_owned_process_death_at_publication_phases_is_fenced_and_recovers_old_set(self):
        for phase in ('backup', 'new', 'manifest'):
            with self.subTest(phase=phase):
                self.crash(phase)
                with self.assertRaisesRegex(publication.ZipPublicationError, 'recovery'):
                    batch.get_output_volumes(str(self.output))
                with self.assertRaisesRegex(publication.ZipPublicationError, 'recovery'):
                    corpus.get_dataset_identity(self.output)
                publication.ensure_recovered_zip_family(self.output)
                self.assert_old()

    def test_dead_staging_publisher_leaves_old_family_readable_without_claiming_orphan(self):
        identity = corpus.get_dataset_identity(self.output)
        self.crash('stage')
        self.assertEqual(self.before, self.family_bytes())
        self.assertFalse(Path(str(self.output) + publication.JOURNAL_SUFFIX).exists())
        stages = list(self.root.glob('.*.volumes-stage-*'))
        self.assertEqual(1, len(stages))
        self.assertEqual(2, len(batch.get_output_volumes(str(self.output))))
        self.assertEqual(identity, corpus.get_dataset_identity(self.output))
        publication.ensure_recovered_zip_family(self.output)
        self.assertTrue(stages[0].exists(), 'An unjournaled workspace is not borrowed as recovery evidence')
        self.export(self.new)
        self.assertTrue(stages[0].exists())
        self.assertEqual(2, len(batch.get_output_volumes(str(self.output))))

    def test_managed_batch_uses_exact_manifest_while_legacy_prefixes_hold_actual_archive_leases(self):
        own = sorted(str(self.root / name) for name in self.before if name.endswith('.zip'))
        other = self.root / 'dataset_aux.zip'
        with zipfile.ZipFile(other, 'w') as archive:
            archive.writestr('metadata.jsonl', '{"audio_filepath":"train/aux.wav","text":"Auxiliary"}\n')
            archive.writestr('train/aux.wav', (self.work / 'sample_0000.wav').read_bytes())
        raw = other.read_bytes()
        audio = self.root / 'book.wav'
        audio.write_bytes(b'bounded source identity')
        source = batch.get_source_identity(str(audio))
        with publication.ensure_zip_archive_readable(other):
            with self.subTest(consumer='list'):
                self.assertEqual(own, batch.get_output_volumes(str(self.output)))
            with self.subTest(consumer='state'):
                self.assertEqual(own, sorted(batch.get_volume_state(str(self.output))))
            with self.subTest(consumer='receipt'):
                result = batch.ensure_completed_volume_receipt(str(audio), str(self.output), {}, source, None, None)
                self.assertEqual(own, result)
                self.assertEqual(own, json.loads(Path(str(self.output) + '.complete.json').read_text())['volumes'])
            with self.subTest(consumer='resume'):
                batch.save_batch_receipt(str(self.output) + '.complete.json', {
                    'source': source, 'volumes': [own[0], str(other)]})
                self.assertEqual([], batch.get_completed_volumes(str(audio), str(self.output)))
        self.assertEqual(raw, other.read_bytes())
        Path(str(self.output) + '.volumes.json').unlink()
        with publication.ensure_zip_archive_readable(other):
            with self.assertRaisesRegex(publication.ZipPublicationError, 'busy'):
                batch.get_output_volumes(str(self.output))
        self.assertEqual(sorted(own + [str(other)]), batch.get_output_volumes(str(self.output)))

    def test_recovery_validates_all_evidence_before_any_mutation(self):
        for fault in ('edited', 'missing', 'symlink', 'malformed', 'duplicate'):
            with self.subTest(fault=fault):
                self.crash()
                journal = Path(str(self.output) + publication.JOURNAL_SUFFIX)
                document = json.loads(journal.read_text())
                workspace = self.root / document['workspace']
                target = workspace / 'previous' / 'dataset_vol02.zip'
                if fault == 'edited':
                    target.write_bytes(target.read_bytes() + b'edited')
                elif fault == 'missing':
                    target.unlink()
                elif fault == 'symlink':
                    target.unlink()
                    target.symlink_to(self.root / 'dataset_vol01.zip')
                elif fault == 'malformed':
                    journal.write_text('{"version":99}')
                else:
                    (workspace / 'next' / 'dataset_vol01.zip').write_bytes((self.root / 'dataset_vol01.zip').read_bytes())
                before = self.evidence()
                with self.assertRaises(publication.ZipPublicationError):
                    publication.ensure_recovered_zip_family(self.output)
                self.assertEqual(before, self.evidence())
                # Restore bounded fixture evidence only to prepare the next case.
                for name, raw in self.before.items():
                    (self.root / name).write_bytes(raw)
                import shutil
                shutil.rmtree(workspace)
                journal.unlink()

    def test_committed_cleanup_interruption_keeps_complete_new_set_fenced_until_retirement(self):
        self.crash('cleanup')
        after = self.family_bytes()
        self.assertNotEqual(self.before, after)
        for name in ('dataset_vol01.zip', 'dataset_vol02.zip'):
            with zipfile.ZipFile(self.root / name) as archive:
                self.assertIn('New', archive.read('metadata.jsonl').decode())
        with self.assertRaises(publication.ZipPublicationError):
            with publication.ensure_zip_family_readable(self.output):
                pass
        publication.ensure_recovered_zip_family(self.output)
        self.assertEqual(after, self.family_bytes())
        self.assertFalse(list(self.root.glob('*' + publication.JOURNAL_SUFFIX)))

    def test_partial_committed_cleanup_error_preserves_new_set_and_retires_on_retry(self):
        import shutil
        remove = shutil.rmtree
        hit = []
        def fail(path, *args, **kwargs):
            if Path(path).name.startswith('.dataset.zip.volumes-stage-') and not hit:
                hit.append(True)
                (Path(path) / 'previous' / 'dataset_vol01.zip').unlink()
                raise OSError('cleanup interrupted')
            return remove(path, *args, **kwargs)
        with patch.object(publication.shutil, 'rmtree', side_effect=fail):
            with self.assertRaisesRegex(publication.ZipPublicationError, 'committed; cleanup recovery'):
                self.export(self.new)
        after = self.family_bytes()
        self.assertNotEqual(self.before, after)
        with self.assertRaises(publication.ZipPublicationError):
            batch.get_output_volumes(str(self.output))
        publication.ensure_recovered_zip_family(self.output)
        self.assertEqual(after, self.family_bytes())
        self.assertFalse(list(self.root.glob('*' + publication.JOURNAL_SUFFIX)))

    def test_other_managed_family_and_symlink_cannot_be_replaced(self):
        with self.assertRaisesRegex(publication.ZipPublicationError, 'owned by another'):
            with publication.apply_zip_volume_set(self.root / 'dataset_vol01.zip', ['dataset_vol01.zip']):
                self.fail('Must refuse before staging or modifying another manifest family')
        self.assert_old()
        target = self.root / 'dataset_vol02.zip'
        target.unlink()
        target.symlink_to(self.root / 'dataset_vol01.zip')
        before = self.evidence()
        with self.assertRaisesRegex(publication.ZipPublicationError, 'regular file'):
            self.export(self.new)
        self.assertEqual(before, self.evidence())

    def test_failed_rollback_retains_fence_and_later_recovery_restores_old_bytes(self):
        replace = os.replace
        def fail(source, destination, *args, **kwargs):
            if str(destination).endswith('.volumes.json') or Path(source).parent.name == 'previous':
                raise OSError('publication and rollback unavailable')
            return replace(source, destination, *args, **kwargs)
        with patch.object(os, 'replace', side_effect=fail):
            with self.assertRaisesRegex(publication.ZipPublicationError, 'evidence retained'):
                self.export(self.new)
        with self.assertRaises(publication.ZipPublicationError):
            batch.get_volume_state(str(self.output))
        publication.ensure_recovered_zip_family(self.output)
        self.assert_old()

    def test_unchanged_names_publish_new_metadata_and_preserve_unrelated_outputs(self):
        other = self.root / 'unrelated.zip'
        other.write_bytes(b'unrelated preserved')
        other_manifest = Path(str(other) + '.volumes.json')
        other_manifest.write_bytes(b'unrelated malformed evidence')
        self.export(self.new)
        self.assertEqual(b'unrelated preserved', other.read_bytes())
        self.assertEqual(b'unrelated malformed evidence', other_manifest.read_bytes())
        for i in range(2):
            with zipfile.ZipFile(self.root / f'dataset_vol{i+1:02d}.zip') as archive:
                row = json.loads(archive.read('metadata.jsonl'))
                self.assertEqual(f'New{i}', row['text'])
                self.assertEqual(f'train/sample_{i:04d}.wav', row['audio_filepath'])
        self.assertEqual(['dataset_vol01.zip', 'dataset_vol02.zip'],
                         json.loads(Path(str(self.output) + '.volumes.json').read_text())['volumes'])
        self.assertEqual(2, len(batch.get_output_volumes(str(self.output))))
        self.assertEqual(2, len(corpus.get_dataset_identity(self.output)['volumes']))

    def test_actual_http_fence_and_file_response_stream_ownership(self):
        app = FastAPI()
        app.include_router(routes.router)
        with patch.object(routes, 'PREPARER_OUTPUT_DIR', str(self.root)), TestClient(app) as client:
            name = 'dataset_vol01.zip'
            self.assertEqual(self.before[name], client.get('/api/preparer/download/' + name).content)
            async def drain():
                response = await routes.preparer_download(name)
                reached, release = asyncio.Event(), asyncio.Event()
                chunks = []
                async def send(message):
                    if message['type'] == 'http.response.body':
                        chunks.append(message.get('body', b''))
                        reached.set()
                        await release.wait()
                pending = asyncio.create_task(response({'type': 'http', 'method': 'GET', 'headers': []}, None, send))
                await asyncio.wait_for(reached.wait(), 3)
                try:
                    with self.assertRaisesRegex(publication.ZipPublicationError, 'busy'):
                        self.export(self.new)
                    self.assertEqual(self.before, self.family_bytes())
                finally:
                    release.set()
                    await asyncio.wait_for(pending, 3)
                self.assertEqual(self.before[name], b''.join(chunks))
            asyncio.run(drain())
            self.crash()
            other = self.root / 'unrelated.ZIP'
            other.write_bytes(b'other readable')
            self.assertEqual(['unrelated.ZIP'], [row['filename'] for row in client.get('/api/preparer/list').json()['files']])
            for name in ('dataset_vol01.zip', 'dataset_vol02.zip'):
                self.assertEqual(409, client.get('/api/preparer/download/' + name).status_code)
            self.assertEqual(b'other readable', client.get('/api/preparer/download/unrelated.ZIP').content)

    def test_actual_training_collection_and_extraction_refuse_pending_or_changed_generation(self):
        snapshots = publication.get_zip_directory_snapshot(self.root)
        source = str(self.root / 'dataset_vol01.zip')
        destination = self.root / 'extracted'
        destination.mkdir()
        (destination / 'prior.txt').write_bytes(b'prior extracted dataset')
        self.crash()
        with self.assertRaises(publication.ZipPublicationError):
            publication.get_zip_directory_snapshot(self.root)
        with self.assertRaises(publication.ZipPublicationError):
            training.extract_zip(source, str(destination), expected=snapshots[source])
        self.assertEqual(b'prior extracted dataset', (destination / 'prior.txt').read_bytes())
        publication.ensure_recovered_zip_family(self.output)
        training.extract_zip(source, str(destination), expected=snapshots[source])
        self.assertIn('Old0', (destination / 'metadata.jsonl').read_text())
        report = training.get_batch_archive_preflight(list(snapshots))
        self.assertEqual([], report['errors'])
        self.assertEqual([1, 1], [row['sample_count'] for row in report['datasets']])
        self.export(self.new)
        old_extraction = {path.name: path.read_bytes() for path in destination.rglob('*') if path.is_file()}
        with self.assertRaisesRegex(publication.ZipPublicationError, 'changed since collection'):
            training.extract_zip(source, str(destination), expected=snapshots[source])
        self.assertEqual(old_extraction, {path.name: path.read_bytes() for path in destination.rglob('*') if path.is_file()})
        fresh = publication.get_zip_directory_snapshot(self.root)
        training.extract_zip(source, str(destination), expected=fresh[source])
        self.assertIn('New0', (destination / 'metadata.jsonl').read_text())

    def test_native_lock_retirement_closes_all_handles_after_unlock_error(self):
        release = publication.release_exclusive_file_lock
        hit = []
        def fail(descriptor):
            release(descriptor)
            if not hit:
                hit.append(descriptor)
                raise OSError('controlled unlock error')
        with patch.object(publication, 'release_exclusive_file_lock', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'unlock error'):
                with publication.ensure_zip_family_readable(self.output):
                    pass
        with publication.ensure_zip_family_readable(self.output) as snapshot:
            self.assertEqual(2, len(snapshot['files']))

    def test_actual_dedup_refuses_stable_pending_mixed_family_before_model_or_cache_reads(self):
        from tests.test_voice_dedup_source_cache import VoiceDedupSourceCacheTests
        from contextlib import redirect_stdout
        import io
        import shutil
        fixture = VoiceDedupSourceCacheTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for path in fixture.narrator.iterdir():
            path.unlink()
        for path in self.root.iterdir():
            if path.name.startswith('dataset') and path.suffix == '.zip' or path.name.endswith('.volumes.json'):
                shutil.copy2(path, fixture.narrator / path.name)
        original_output = self.output
        self.output = fixture.narrator / 'dataset.zip'
        try:
            self.crash('backup')
            document = json.loads(Path(str(self.output) + publication.JOURNAL_SUFFIX).read_text())
            workspace = fixture.narrator / document['workspace']
            os.replace(workspace / 'next/dataset_vol01.zip', fixture.narrator / 'dataset_vol01.zip')
            texts = []
            for name in ('dataset_vol01.zip', 'dataset_vol02.zip'):
                with zipfile.ZipFile(fixture.narrator / name) as archive:
                    texts.append(json.loads(archive.read('metadata.jsonl'))['text'])
            self.assertEqual(['New0', 'Old1'], texts, 'Reader fixture must actually be a stable mixed family')
            before = {path.name: path.read_bytes() for path in fixture.narrator.iterdir()
                      if path.suffix == '.zip' or path.name.endswith('.volumes.json')}
            with redirect_stdout(io.StringIO()), self.assertRaisesRegex(publication.ZipPublicationError, 'recovery'):
                fixture.ns['run_dedup'](fixture.ns['fixture_model'], 'cpu', fixture.zips, fixture.output)
            fixture.ns['extract_embedding'].assert_not_called()
            self.assertFalse(fixture.cache.exists())
            self.assertFalse(list((fixture.zips / '_deduped').glob('*.zip')))
            self.assertEqual(before, {path.name: path.read_bytes() for path in fixture.narrator.iterdir()
                                     if path.suffix == '.zip' or path.name.endswith('.volumes.json')})
            publication.ensure_recovered_zip_family(self.output)
        finally:
            self.output = original_output

    def test_actual_unique_dedup_preserves_legacy_renamed_archive_bytes_and_cached_provider(self):
        from tests.test_voice_dedup_source_cache import VoiceDedupSourceCacheTests
        fixture = VoiceDedupSourceCacheTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        renamed = fixture.zip.with_suffix('.legacy')
        fixture.zip.rename(renamed)
        raw = renamed.read_bytes()
        fixture.run_dedup()
        outputs = list((fixture.zips / '_deduped').iterdir())
        self.assertEqual(1, len(outputs))
        self.assertEqual(raw, outputs[0].read_bytes())
        original_cache = fixture.cache.read_bytes()
        fixture.run_dedup()
        self.assertEqual(1, fixture.ns['extract_embedding'].call_count)
        self.assertEqual(original_cache, fixture.cache.read_bytes())
        self.assertEqual(raw, outputs[0].read_bytes())

    def test_actual_merge_admits_same_and_multiple_families_without_nested_locks(self):
        from voice_dataset_merge import merge_voice_datasets, get_source_records
        paths = [self.root / 'dataset_vol02.zip', self.root / 'dataset_vol01.zip']
        expected = publication.get_zip_directory_snapshot(self.root)
        destination = self.root / 'merged.zip'
        self.assertEqual('merged', merge_voice_datasets(paths, destination, expected_inputs=expected)['status'])
        self.assertEqual(2, len(get_source_records(paths)))
        self.assertEqual('reused', merge_voice_datasets(list(reversed(paths)), destination, expected_inputs=expected)['status'])
        with zipfile.ZipFile(destination) as archive:
            self.assertEqual(['Old0', 'Old1'], [json.loads(row)['text'] for row in
                                              archive.read('metadata.jsonl').decode().splitlines()])
        other = self.root / 'other.legacy'
        with zipfile.ZipFile(other, 'w') as archive:
            archive.writestr('train/other.wav', (self.work / 'sample_0000.wav').read_bytes())
            archive.writestr('metadata.jsonl', '{"audio_filepath":"train/other.wav","text":"Old0"}\n')
        self.assertEqual('merged', merge_voice_datasets([other, paths[1]], self.root / 'combined.zip')['status'])
        with self.assertRaises(FileNotFoundError):
            get_source_records([self.root / 'missing.zip'])
        before = self.family_bytes()
        with self.assertRaisesRegex(publication.ZipPublicationError, 'source ZIP family'):
            merge_voice_datasets(paths, self.output)
        self.assertEqual(before, self.family_bytes())
        old_destination = destination.read_bytes()
        self.export(self.new)
        with self.assertRaisesRegex(publication.ZipPublicationError, 'changed since collection'):
            merge_voice_datasets(paths, destination, expected_inputs=expected)
        self.assertEqual(old_destination, destination.read_bytes())

    def test_actual_merge_and_source_collector_refuse_pending_family_before_reuse_or_output_write(self):
        from voice_dataset_merge import merge_voice_datasets, get_source_records
        paths = [self.root / 'dataset_vol01.zip', self.root / 'dataset_vol02.zip']
        destination = self.root / 'merged.zip'
        merge_voice_datasets(paths, destination)
        before = destination.read_bytes()
        self.crash('backup')
        document = json.loads(Path(str(self.output) + publication.JOURNAL_SUFFIX).read_text())
        workspace = self.root / document['workspace']
        os.replace(workspace / 'next/dataset_vol01.zip', self.root / 'dataset_vol01.zip')
        with self.assertRaisesRegex(publication.ZipPublicationError, 'recovery'):
            get_source_records(paths)
        with self.assertRaisesRegex(publication.ZipPublicationError, 'recovery'):
            merge_voice_datasets(paths, destination)
        self.assertEqual(before, destination.read_bytes())
        self.assertFalse(list(self.root.glob('.merged.zip.*.tmp')))

    def test_actual_confirmed_dedup_merge_uses_the_captured_native_family(self):
        from tests.test_voice_dedup_source_cache import VoiceDedupSourceCacheTests
        from voice_clustering import cluster_voices, load_cluster_overrides
        from voice_dataset_merge import merge_voice_datasets
        from types import SimpleNamespace
        from unittest.mock import Mock
        import librosa
        fixture = VoiceDedupSourceCacheTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ns.update(librosa=librosa, cluster_voices=cluster_voices,
                          load_cluster_overrides=load_cluster_overrides,
                          merge_voice_datasets=merge_voice_datasets)
        fixture.ns['plt'] = SimpleNamespace(subplots=Mock(return_value=(Mock(), Mock())),
            tight_layout=Mock(), savefig=Mock(), close=Mock())
        fixture.ns['sns'] = SimpleNamespace(heatmap=Mock())
        fixture.ns['tqdm'].write = Mock()
        fixture.zip.unlink()
        for name, raw in self.before.items():
            (fixture.narrator / name).write_bytes(raw)
        fixture.run_dedup()
        self.assertEqual(2, fixture.ns['extract_embedding'].call_count)
        outputs = list((fixture.zips / '_deduped').glob('*.zip'))
        self.assertEqual(1, len(outputs))
        with zipfile.ZipFile(outputs[0]) as archive:
            self.assertEqual(2, len(archive.read('metadata.jsonl').decode().splitlines()))
        original_cache = fixture.cache.read_bytes()
        fixture.run_dedup()
        self.assertEqual(2, fixture.ns['extract_embedding'].call_count)
        self.assertEqual(original_cache, fixture.cache.read_bytes())

    def test_actual_training_main_refuses_stale_preflight_list_before_adapter_claim(self):
        def preflight(*args):
            self.export(self.new)
            return {'datasets': [], 'errors': []}
        argv = ['batch', '--zips_dir', str(self.root), '--models_dir', str(self.root / 'models'),
                '--datasets_dir', str(self.root / 'datasets'), '--manifest', str(self.root / 'models/manifest.json'),
                '--device', 'cpu']
        with patch.object(sys, 'argv', argv), patch.object(training, 'load_manifest', return_value=[]), \
             patch.object(training, 'get_selected_interpreter_preflight', side_effect=preflight), \
             patch.object(training, 'get_batch_output_preflight', return_value=[]), \
             patch.object(training, 'acquire_batch_registration_lease') as lease, \
             patch.object(training, 'train_one') as train:
            self.assertEqual(1, training.main())
        lease.assert_not_called()
        train.assert_not_called()
        self.assertFalse((self.root / 'models').exists())

    def test_staged_progress_rechecks_full_scratch_and_stops_actual_child_before_publication(self):
        from tests.batch_mock_support import adapt_owner_fixture
        adapt_owner_fixture(self)
        audio = self.root / 'book.wav'
        audio.write_bytes(b'bounded synthetic source')
        processor = batch.BatchProcessor('fixture.gguf')
        processor.audio_durations[str(audio)] = 3600
        processor.output_bytes_per_second = 2 * 1024 ** 3 / 3600
        code = 'import time\nprint("Volume 1 staged: fixture (staged_bytes=805306368)",flush=True)\ntime.sleep(30)\n'
        children = []
        def launch(*args, **kwargs):
            process = subprocess.Popen([sys.executable, '-u', '-c', code], **kwargs)
            children.append(process)
            return process
        try:
            with patch.object(batch, 'get_output_name', return_value=str(self.output)), \
                 patch.object(batch, 'get_disk_probe_path', return_value=str(self.root)), \
                 patch.object(batch, 'check_disk_space', side_effect=[True, False]) as disk, \
                 patch.object(batch, 'start_owned_subprocess', side_effect=launch):
                processor.process_file(str(audio), 1, 1)
            scratch = 0.75 + 3600 * (208000 + 32000) / 1024 ** 3
            self.assertEqual([2 + scratch, 1.25 + scratch],
                             [call.kwargs['required_gb_per_file'] for call in disk.call_args_list])
            self.assertIsNotNone(children[0].poll())
            self.assertNotEqual(0, children[0].returncode)
            self.assertTrue(processor.disk_refused)
            self.assertEqual([], processor.results['succeeded'])
            self.assertFalse(Path(str(self.output) + '.complete.json').exists())
            self.assertEqual(self.before, self.family_bytes())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait()


if __name__ == '__main__':
    unittest.main()
