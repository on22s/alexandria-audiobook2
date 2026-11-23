"""A real killed publication must reconcile bytes, receipt eligibility and cleanup."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import io
import json
import os
import signal
import tempfile
import threading
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI

import project_scope_exports as exports
from book_state_transaction import ensure_book_state
from routers import editor
from tests import test_project_export_quality

OLD = b'previous complete artifact'
NEW = b'new complete artifact'


@unittest.skipIf(os.name == 'nt', 'precise SIGKILL boundary control is POSIX-only')
class PublicationCrashTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.target = self.root / 'cloned_audiobook.mp3'
        self.target.write_bytes(OLD)
        (self.root / 'state.json').write_text(
            json.dumps({'active_book_id': 'book', 'book_generation': 'gen'}))
        exports.record_export_publication(self.root, self.target)
        (self.root / 'staged').write_bytes(NEW)
        app = FastAPI()
        app.include_router(editor.router)
        self.stack.enter_context(patch.object(editor, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(editor, 'AUDIOBOOK_PATH', str(self.target)))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url='http://fixture')
        self.addCleanup(lambda: asyncio.run(self.client.aclose()))

    def _status(self, route='/api/audiobook'):
        async def read():
            response = await self.client.get(route)
            return response.status_code, response.content
        return asyncio.run(read())

    def test_kill_after_target_install_recovers_prior_eligible_output(self):
        pid = os.fork()
        if pid == 0:
            real_replace = os.replace

            def replace(source, destination, *args, **kwargs):
                real_replace(source, destination, *args, **kwargs)
                if Path(destination) == self.target:
                    os.kill(os.getpid(), signal.SIGKILL)

            try:
                with ensure_book_state(self.root), \
                        patch.object(os, 'replace', replace):
                    exports.publish_export_with_receipt(self.root,
                        self.root / 'staged', self.target, None,
                        lambda: os.replace(self.root / 'staged', self.target))
            finally:
                os._exit(0)
        _pid, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status))
        self.assertEqual(signal.SIGKILL, os.WTERMSIG(status))
        with ensure_book_state(self.root):
            pass
        self.assertEqual(OLD, self.target.read_bytes())
        self.assertEqual((200, OLD), self._status())
        self.assertEqual([], list(self.root.glob('.export-*-*')))
        self.assertFalse((self.root / exports.PUBLICATION_JOURNAL).exists())
        self.assertFalse((self.root / exports.PUBLICATION_SET_JOURNAL).exists())

    def test_other_publication_recovery_preserves_a_live_validated_audacity_stage(self):
        from audacity_export_quality import validate_audacity_export

        manager, _ = test_project_export_quality.ProjectExportQualityTests().fixture(str(self.root))
        ready, release = threading.Event(), threading.Event()
        staged = []
        foreign = self.root / ('audacity_export.zip.pending.' + 'a' * 32)
        foreign.write_bytes(b'unowned')

        def pause_after_validation(path, *args, **kwargs):
            result = validate_audacity_export(path, *args, **kwargs)
            staged.append(Path(path))
            ready.set()
            if not release.wait(15):
                raise TimeoutError('fixture did not release validated Audacity stage')
            return result

        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(write_fd)
            real_replace = os.replace

            def stop_after_install(source, destination, *args, **kwargs):
                real_replace(source, destination, *args, **kwargs)
                if Path(destination) == self.target:
                    os.kill(os.getpid(), signal.SIGKILL)

            try:
                if os.read(read_fd, 1) == b'1':
                    with ensure_book_state(self.root), patch.object(os, 'replace', stop_after_install):
                        exports.publish_export_with_receipt(self.root, self.root / 'staged',
                            self.target, None, lambda: os.replace(self.root / 'staged', self.target))
            finally:
                os._exit(2)
        os.close(read_fd)
        with ThreadPoolExecutor(1) as pool, \
                patch('audacity_export_quality.validate_audacity_export', pause_after_validation):
            builder = pool.submit(manager.export_audacity)
            reaped = False
            try:
                with os.fdopen(write_fd, 'wb') as start:
                    self.assertTrue(ready.wait(10), 'Audacity builder did not validate a ZIP')
                    raw = staged[0].read_bytes()
                    start.write(b'1')
                status = os.waitpid(pid, 0)[1]
                reaped = True
                self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(status))
                self.assertEqual(409, self._status()[0])
                for _ in range(2):
                    with ensure_book_state(self.root):
                        pass
                    self.assertEqual((200, OLD), self._status())
                    self.assertEqual(raw, staged[0].read_bytes())
                    self.assertEqual(b'unowned', foreign.read_bytes())
            finally:
                release.set()
                if not reaped:
                    os.waitpid(pid, 0)
            self.assertTrue(builder.result(timeout=10)[0])
        self.assertEqual((200, raw), self._status('/api/export_audacity'))
        self.assertFalse(staged[0].parent.exists())

    def test_kill_before_audacity_intent_reclaims_only_owned_stage_on_admission(self):
        manager, _ = test_project_export_quality.ProjectExportQualityTests().fixture(str(self.root))
        prior = self.root / 'audacity_export.zip'
        prior.write_bytes(OLD)
        exports.record_export_publication(self.root, prior)
        foreign = self.root / ('audacity_export.zip.pending.' + 'b' * 32)
        foreign.write_bytes(b'unowned')
        trace = self.root / 'observed-stage.txt'
        pid = os.fork()
        if pid == 0:
            def stop_before_intent(_root, path, *_args, **_kwargs):
                trace.write_text(os.fspath(path))
                os.kill(os.getpid(), signal.SIGKILL)

            try:
                with patch.object(exports, 'publish_export_with_receipt', stop_before_intent):
                    manager.export_audacity()
            finally:
                os._exit(2)
        self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
        staged = Path(trace.read_text())
        self.assertTrue(staged.is_file())
        self.assertFalse((self.root / exports.PUBLICATION_JOURNAL).exists())
        for _ in range(2):
            with ensure_book_state(self.root):
                pass
            self.assertFalse(staged.exists())
            self.assertEqual(b'unowned', foreign.read_bytes())
            self.assertEqual((200, OLD), self._status('/api/export_audacity'))

    def test_chapter_recovery_retries_nested_directory_sync_before_retiring_intent(self):
        chapter_dir = self.root / 'chapter_exports'
        chapter_dir.mkdir()
        chapter, backup = chapter_dir / '01.wav', chapter_dir / 'previous.wav'
        chapter.write_bytes(OLD)
        exports.record_export_publication(self.root, chapter)
        exports.write_set_journal(self.root, [chapter], [backup], None)
        chapter.replace(backup)
        chapter.write_bytes(NEW)
        journal = self.root / exports.PUBLICATION_SET_JOURNAL
        sync = exports.sync_parent_directory
        nested_attempts = []

        def fail_one_nested_sync(root, name=None):
            if name is not None and Path(name).parent == Path('chapter_exports'):
                self.assertTrue(journal.exists())
                nested_attempts.append(name)
                if len(nested_attempts) == 1:
                    raise OSError(errno.EIO, 'chapter directory sync')
            return sync(root, name)

        with patch.object(exports, 'sync_parent_directory', fail_one_nested_sync):
            with ensure_book_state(self.root):
                pass
            self.assertEqual(OLD, chapter.read_bytes())
            self.assertFalse(backup.exists())
            self.assertTrue(journal.exists())
            for _ in range(2):
                with ensure_book_state(self.root):
                    pass
                self.assertFalse(journal.exists())
                self.assertEqual(OLD, chapter.read_bytes())
                self.assertFalse(exports.is_scope_export_historical(self.root, chapter))
        self.assertEqual(['chapter_exports/01.wav'] * 2, nested_attempts)

    def test_ledger_install_crash_preserves_raw_state_and_refuses_foreign_inode(self):
        from contextlib import contextmanager, nullcontext
        from export_quality import get_export_file_identity
        from readalong_export import _publish_pair_transaction

        for is_set in (False, True):
            for prior in ('absent', 'formatted', 'current', 'foreign'):
                with self.subTest(is_set=is_set, prior=prior), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    stage = root / 'stage'
                    stage.mkdir()
                    target, source = root / 'cloned_audiobook.mp3', stage / 'output'
                    source.write_bytes(NEW)
                    ledger = root / exports.PUBLICATIONS_FILE
                    journal = root / (exports.PUBLICATION_SET_JOURNAL if is_set else exports.PUBLICATION_JOURNAL)
                    raw = None
                    before_version = None
                    if prior != 'absent':
                        sibling = root / 'audiobook.m4b'
                        sibling.write_bytes(OLD)
                        exports.record_export_publication(root, sibling)
                        if prior == 'current':
                            target.write_bytes(OLD)
                            exports.record_export_publication(root, target)
                        prior_records = exports.read_publication_receipts(root)
                        if prior == 'current':
                            before_version = prior_records[target.name]['file_version']
                        raw = b'  ' + json.dumps(prior_records, indent=1).encode() + b'\n'
                        ledger.write_bytes(raw)

                    class Snapshot:
                        @contextmanager
                        def ensure_publication_transaction(self, _outputs):
                            with ensure_book_state(root):
                                yield nullcontext

                    pid = os.fork()
                    if pid == 0:
                        real_replace = os.replace

                        def kill_after_ledger(source, destination, **kwargs):
                            real_replace(source, destination, **kwargs)
                            if Path(destination) == ledger:
                                os.kill(os.getpid(), signal.SIGKILL)

                        try:
                            with patch.object(os, 'replace', kill_after_ledger):
                                if is_set:
                                    snapshot = Snapshot()
                                    snapshot.root = root
                                    _publish_pair_transaction(snapshot, [
                                        (str(source), str(target), get_export_file_identity(source))],
                                        None, stage, [False])
                                else:
                                    with ensure_book_state(root):
                                        exports.publish_export_with_receipt(root, source, target,
                                            None, lambda: os.replace(source, target))
                        finally:
                            os._exit(2)
                    self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
                    if prior == 'foreign':
                        incoming = ledger.read_bytes()
                        alternate = root / 'foreign-ledger'
                        alternate.write_bytes(incoming)
                        alternate.replace(ledger)
                        for _ in range(2):
                            with self.assertRaisesRegex(ValueError, 'changed outside publication'):
                                with ensure_book_state(root):
                                    pass
                            self.assertEqual(incoming, ledger.read_bytes())
                            self.assertTrue(journal.is_file())
                    else:
                        for _ in range(2):
                            with ensure_book_state(root):
                                pass
                            actual = ledger.read_bytes() if ledger.is_file() else None
                            if before_version is None:
                                self.assertEqual(raw, actual)
                                self.assertFalse(target.exists())
                            else:
                                self.assertEqual(OLD, target.read_bytes())
                                self.assertFalse(exports.is_scope_export_historical(root, target))
                                current = exports.read_publication_receipts(root)[target.name]['file_version']
                                # Compare all bytes outside the one affected version array.
                                expected = {**prior_records, target.name: {
                                    **prior_records[target.name], 'file_version': current}}
                                self.assertEqual(b'  ' + json.dumps(expected, indent=1).encode() + b'\n',
                                                 actual)
                            self.assertFalse(journal.exists())

    def test_chapter_zip_during_manifest_backup_uses_only_proven_unaffected_members(self):
        from tests import test_chapter_output_integrity

        manager, out, manifest = test_chapter_output_integrity.RetainedChapterIntegrityTests().manager(self.root)
        affected, retained = (row['file'] for row in manifest['chapters'])

        async def read(names=None):
            response = await self.client.get('/api/chapter_exports/zip',
                params={'names': names} if names is not None else {})
            if response.status_code != 200:
                return response.status_code, {}
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                return response.status_code, {
                    name: hashlib.sha256(archive.read(name)).hexdigest() for name in archive.namelist()
                }

        original = asyncio.run(read())
        self.assertEqual(200, original[0])
        pid = os.fork()
        if pid == 0:
            import book_state_transaction
            move = book_state_transaction._move

            def kill_on_manifest_backup(source, destination, **kwargs):
                move(source, destination, **kwargs)
                if Path(source) == out / 'manifest.json' and Path(destination).parent.name == 'old':
                    os.kill(os.getpid(), signal.SIGKILL)

            try:
                with patch.object(book_state_transaction, '_move', kill_on_manifest_backup):
                    manager.export_chapters(fmt='wav', chapters=[0])
            finally:
                os._exit(2)
        self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]))
        self.assertEqual(409, asyncio.run(read())[0])
        self.assertEqual(409, asyncio.run(read(affected))[0])
        self.assertEqual((200, {retained: original[1][retained]}), asyncio.run(read(retained)))
        for _ in range(2):
            with ensure_book_state(self.root):
                pass
            self.assertEqual(original, asyncio.run(read()))

    def test_ordinary_admission_preserves_unregistered_publication_scratch_names(self):
        directory = self.root / '.export-copy-user-backup'
        directory.mkdir()
        blobs = {directory / 'data': b'backup',
                 self.root / '.export-ledger-notes': b'notes',
                 self.root / '.export-meta-user-file': b'metadata'}
        for path, raw in blobs.items():
            path.write_bytes(raw)
        for _ in range(2):
            with ensure_book_state(self.root):
                pass
            self.assertEqual(blobs, {path: path.read_bytes() for path in blobs})

    def test_readalong_set_preserves_independent_second_target_and_rolls_back_audio(self):
        from contextlib import contextmanager, nullcontext
        from export_quality import get_export_file_identity
        from readalong_export import _publish_pair_transaction

        class Snapshot:
            root = self.root

            @contextmanager
            def ensure_publication_transaction(self, _outputs):
                with ensure_book_state(self.root):
                    yield nullcontext

        sidecar = self.root / 'readalong.sidecar.json'
        publish = exports.publish_export_set_member
        for existed in (True, False):
            with self.subTest(existed=existed):
                if existed:
                    sidecar.write_bytes(b'prior sidecar')
                    exports.record_export_publication(self.root, sidecar)
                else:
                    sidecar.unlink()
                with tempfile.TemporaryDirectory(dir=self.root) as directory:
                    stage = Path(directory)
                    pairs = []
                    for target in (self.target, sidecar):
                        source = stage / target.name
                        source.write_bytes(NEW)
                        pairs.append((str(source), str(target), get_export_file_identity(source)))

                    def replace_after_first(root, member, report):
                        version = publish(root, member, report)
                        if member['target'] == self.target.name:
                            writer = self.root / 'independent'
                            writer.write_bytes(b'independent sidecar')
                            writer.replace(sidecar)
                        return version

                    with patch.object(exports, 'publish_export_set_member', replace_after_first):
                        with self.assertRaises(exports.ExportSetChanged):
                            _publish_pair_transaction(Snapshot(), pairs, None, stage, [False])
                for _ in range(2):
                    with ensure_book_state(self.root):
                        pass
                    self.assertEqual((200, OLD), self._status())
                    self.assertEqual(b'independent sidecar', sidecar.read_bytes())
                    self.assertFalse((self.root / exports.PUBLICATION_SET_JOURNAL).exists())

    def _install_registered_export_set(self, staging):
        from export_quality import get_export_file_identity

        payload = staging.path / 'combined.pcm'
        payload.write_bytes(b'owned PCM')
        source = staging.path / 'new.mp3'
        source.write_bytes(NEW)
        backup = staging.path / 'old' / self.target.name
        backup.parent.mkdir()
        report = get_export_file_identity(source)
        exports.write_set_journal(self.root, [self.target], [backup],
                                  staged=[report], sources=[source])
        member, = exports.read_set_journal(self.root)['members']
        report = exports.publish_export_set_member(self.root, member, report)
        exports.record_export_publication(self.root, self.target, report['sha256'],
                                          report['file_version'], export_set=True)
        return backup, payload

    def test_set_rollback_cleanup_retries_without_touching_live_or_replaced_slot(self):
        from export_staging import RECOVERY, create_export_staging
        from export_staging_directory import PosixStageDirectory

        with ensure_book_state(self.root):
            staging = create_export_staging(self.root, 'sidecar')
            self.addCleanup(staging.finish, remove=False)
            backup, payload = self._install_registered_export_set(staging)
        # The owner is still live. The book may recover its delivery, and a
        # separate process could still be holding the stage's owner lock.
        with ensure_book_state(self.root):
            self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
            self.assertEqual(b'owned PCM', payload.read_bytes())
        staging.finish(remove=False)
        self.assertFalse(backup.exists())
        self.assertEqual(OLD, self.target.read_bytes())
        slot = staging.path.parent
        displaced = slot.with_name('displaced-slot')
        slot.rename(displaced)
        try:
            staging.path.mkdir(parents=True)
            payload.write_bytes(b'foreign payload')
            for _ in range(2):
                with ensure_book_state(self.root):
                    self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
                    self.assertEqual(b'foreign payload', payload.read_bytes())
                    self.assertEqual(b'owned PCM', (displaced / 'stage' / payload.name).read_bytes())
        finally:
            if payload.exists():
                payload.unlink()
            staging.path.rmdir()
            slot.rmdir()
            displaced.rename(slot)
        guard = staging.path / RECOVERY
        guard.write_bytes(b'foreign metadata')
        with ensure_book_state(self.root):
            self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
            self.assertEqual(b'foreign metadata', guard.read_bytes())
            self.assertEqual(b'owned PCM', payload.read_bytes())
        guard.write_bytes(b'1')
        with patch.object(PosixStageDirectory, 'unlink_recovery_guard',
                          side_effect=OSError(errno.EIO, 'forced guard unlink failure')) as fail:
            with ensure_book_state(self.root):
                self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
                self.assertTrue(guard.is_file())
                self.assertFalse(payload.exists())
                self.assertEqual(OLD, self.target.read_bytes())
            fail.assert_called_once()
            self.assertEqual(409, self._status()[0])
        for _ in range(2):
            with ensure_book_state(self.root):
                self.assertIsNone(exports.read_set_journal(self.root))
                self.assertFalse(staging.path.exists())
                self.assertFalse(slot.exists())
            self.assertEqual((200, OLD), self._status())

    def test_set_cleanup_recovers_empty_guard_with_original_marker(self):
        from export_staging import MARKER, RECOVERY, create_export_staging
        from export_staging_directory import PosixStageDirectory

        with ensure_book_state(self.root):
            staging = create_export_staging(self.root, 'sidecar')
            self.addCleanup(staging.finish, remove=False)
            backup, payload = self._install_registered_export_set(staging)
        marker = staging.path / MARKER
        guard = staging.path / RECOVERY
        original = marker.read_bytes()
        write = os.write

        def fail_guard_write(fd, data):
            if data == b'1' and os.path.samestat(os.fstat(fd), guard.stat()):
                raise OSError(errno.EIO, 'guard write failed')
            return write(fd, data)

        with patch.object(os, 'write', fail_guard_write):
            with self.assertRaisesRegex(OSError, 'guard write failed'):
                staging.finish(remove=False)
        self.assertEqual(b'', guard.read_bytes())
        self.assertEqual(OLD, backup.read_bytes())

        saved = self.root / 'original-stage-marker'
        marker.rename(saved)
        with ensure_book_state(self.root):
            self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
            self.assertEqual(b'', guard.read_bytes())
            self.assertEqual(b'owned PCM', payload.read_bytes())
        saved.rename(marker)
        guard.chmod(0o644)
        with ensure_book_state(self.root):
            self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
            self.assertEqual(b'owned PCM', payload.read_bytes())
        guard.chmod(0o600)
        raw = original.strip()
        owner = json.loads(raw)['owner_identity']
        conflicting = json.dumps({'owner_identity': [owner[0], owner[1] + 1]}).encode()
        duplicate = conflicting[1:-1]
        for order, malformed in (
                ('conflict last', raw[:-1] + b',' + duplicate + b'}'),
                ('conflict first', b'{' + duplicate + b',' + raw[1:])):
            with self.subTest(order=order):
                marker.write_bytes(malformed)
                with ensure_book_state(self.root):
                    self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
                    self.assertEqual(malformed, marker.read_bytes())
                    self.assertEqual(b'', guard.read_bytes())
                    self.assertEqual(b'owned PCM', payload.read_bytes())
                self.assertEqual(409, self._status()[0])
        marker.write_bytes(original)
        unlink = PosixStageDirectory.unlink_recovery_guard

        def fail_after_unlink(authority, proof):
            self.assertEqual(original, marker.read_bytes())
            unlink(authority, proof)
            raise OSError(errno.EIO, 'cleanup interrupted after unlink')

        with patch.object(PosixStageDirectory, 'unlink_recovery_guard', fail_after_unlink):
            with ensure_book_state(self.root):
                self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
                self.assertFalse(guard.exists())
                self.assertEqual(original, marker.read_bytes())
                self.assertEqual(b'owned PCM', payload.read_bytes())
            self.assertEqual(409, self._status()[0])
        for _ in range(2):
            with ensure_book_state(self.root):
                self.assertIsNone(exports.read_set_journal(self.root))
                self.assertFalse(staging.path.parent.exists())
            self.assertEqual((200, OLD), self._status())
            receipt = exports.read_publication_receipts(self.root)[self.target.name]
            self.assertEqual(hashlib.sha256(OLD).hexdigest(), receipt['sha256'])

    def test_set_commit_cleanup_survives_kill_after_retiring_registered_slot(self):
        from export_staging import create_export_staging

        unlink = Path.unlink
        with ensure_book_state(self.root):
            staging = create_export_staging(self.root, 'sidecar')
            self.addCleanup(staging.finish, remove=False)
            backup, payload = self._install_registered_export_set(staging)

            def fail_backup(path, *args, **kwargs):
                if path == backup:
                    raise OSError(errno.EIO, 'forced backup unlink failure')
                return unlink(path, *args, **kwargs)

            with patch.object(Path, 'unlink', fail_backup):
                self.assertFalse(exports.write_set_journal(self.root, [self.target], [backup],
                                                          committed=True))
            self.assertEqual(OLD, backup.read_bytes())
        staging.finish(remove=False)
        self.assertEqual(b'owned PCM', payload.read_bytes())
        self.assertEqual(409, self._status()[0])
        journal = self.root / exports.PUBLICATION_SET_JOURNAL
        pid = os.fork()
        if pid == 0:
            def stop_before_unlink(path, *args, **kwargs):
                if path == journal:
                    os.kill(os.getpid(), signal.SIGKILL)
                return unlink(path, *args, **kwargs)

            try:
                with patch.object(Path, 'unlink', stop_before_unlink), ensure_book_state(self.root):
                    pass
            finally:
                os._exit(2)
        status = os.waitpid(pid, 0)[1]
        self.assertEqual(-signal.SIGKILL, os.waitstatus_to_exitcode(status))
        self.assertEqual('settled', exports.read_set_journal(self.root)['phase'])
        self.assertFalse(staging.path.parent.exists())
        self.assertEqual(NEW, self.target.read_bytes())
        for _ in range(2):
            with ensure_book_state(self.root):
                self.assertIsNone(exports.read_set_journal(self.root))
                self.assertFalse(staging.path.parent.exists())
            self.assertEqual((200, NEW), self._status())

    def test_chapter_commit_marker_remains_fenced_until_its_root_sync_succeeds(self):
        from tests import test_chapter_output_integrity as fixture
        manager, out, manifest = fixture.RetainedChapterIntegrityTests().manager(self.root)
        t = out / manifest['chapters'][0]['file']
        old = t.read_bytes()
        fixture.sf.write(self.root / 'voicelines/0.wav', fixture.tone(hz=560), 24000)
        journal = self.root / exports.PUBLICATION_SET_JOURNAL
        sync = exports.sync_parent_directory
        faults = []

        def fail_commit_sync(root, name=None):
            if (name is None and journal.exists()
                    and json.loads(journal.read_bytes())['phase'] == 'committed'):
                faults.append(True)
                raise OSError(errno.EIO, 'commit durability')
            return sync(root, name)

        async def download():
            response = await self.client.get('/api/chapter_exports/zip')
            return response.status_code

        with patch.object(exports, 'sync_parent_directory', fail_commit_sync):
            with self.assertRaisesRegex(exports.ExportCommitUncertain, 'pending recovery'):
                manager.export_chapters(fmt='wav', chapters=[0])
            self.assertEqual([True, True], faults)
            self.assertTrue(journal.exists())
            record = exports.read_set_journal(self.root)
            self.assertTrue(all((self.root / member['backup']).is_file() for member in record['members']))
            self.assertEqual(409, asyncio.run(download()))
        for _ in range(2):
            with ensure_book_state(self.root):
                pass
            self.assertFalse(journal.exists())
            self.assertNotEqual(old, t.read_bytes())
            self.assertEqual(200, asyncio.run(download()))

    def test_late_chapter_writer_cannot_be_overwritten_during_exclusive_install(self):
        import book_state_transaction
        from tests import test_chapter_output_integrity as fixture
        manager, out, manifest = fixture.RetainedChapterIntegrityTests().manager(self.root)
        first, second = (out / row['file'] for row in manifest['chapters'])
        original, unaffected = first.read_bytes(), second.read_bytes()
        writer_bytes = b'late independent chapter'
        move = book_state_transaction._move
        injected = []

        def write_after_backup(source, destination, **kwargs):
            if destination == first and not injected:
                self.assertFalse(first.exists())
                first.write_bytes(writer_bytes)
                injected.append(True)
            return move(source, destination, **kwargs)

        with patch.object(book_state_transaction, '_move', write_after_backup):
            result = manager.export_chapters(fmt='wav', chapters=[0])
        self.assertEqual([True], injected)
        self.assertFalse(result[0], result)
        for _ in range(2):
            with ensure_book_state(self.root):
                pass
            self.assertEqual(writer_bytes, first.read_bytes())
            self.assertEqual(unaffected, second.read_bytes())
            record = exports.read_set_journal(self.root)
            self.assertEqual('intent', record['phase'])
            member = next(row for row in record['members'] if Path(row['target']).name == first.name)
            self.assertEqual(original, (self.root / member['backup']).read_bytes())
            self.assertTrue(exports.is_scope_export_historical(self.root, first))
