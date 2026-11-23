"""Book-transition and scope readers must not present old exports as current."""
import asyncio
from contextlib import ExitStack, contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from fastapi import HTTPException

import book_state_transaction as books
import core
import project_scope_exports as exports
from book_state_transaction import apply_book_input_selection
from book_workspace import get_book_workspace
from project import CHAPTER_EXPORT_DIR, group_into_chunks
from project_scope_apply import UNDO, apply_source_scope, apply_source_scope_undo
from project_scope_admission import ensure_scope_admission
from project_scope_exports import (BASE_EXPORTS, LEGACY_EXPORTS, MIXED_DELIVERIES,
                                   get_scope_export_inventory, read_publication_receipts,
                                   record_export_publication)
from project_scope_plan import get_scope_preview
from routers import editor, scripts_library as library, sound_design
from script_selection_binding import capture_generation_selection, publish_generated_script
from sound_design_download import open_sound_delivery
from tests.test_support import intercept_book_publications

OLD = {name: ('OLD ' + name).encode() for name in (
    'mp3', 'm4b', 'audacity', 'chapter', 'mixed.mp3', 'mixed.wav', 'mixed.m4b')}


class ExportReaderFixture:
    """Synthetic exports and the real reader functions on a disposable root."""

    def make_root(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack, self.root = stack, root
        self.script = [{'speaker': 'ALICE', 'text': 'First synthetic line.', 'instruct': ''},
                       {'speaker': 'BOB', 'text': 'Retained exact line.', 'instruct': ''}]
        self.source = root / 'source.txt'
        self.source.write_text('\n'.join(row['text'] for row in self.script))
        rows = [dict(row, id=i, uid=f'uid-{i}', status='pending', audio_path=None)
                for i, row in enumerate(group_into_chunks(self.script))]
        state = {'active_book_id': 'oldbook', 'book_generation': 'oldgen',
                 'input_file_path': str(self.source)}
        for name, value in (('state.json', state), ('annotated_script.json', self.script),
                            ('voice_config.json', {}), ('chunks.json', rows)):
            (root / name).write_text(json.dumps(value, indent=3))
        self.paths = {
            'mp3': root / 'cloned_audiobook.mp3', 'm4b': root / 'audiobook.m4b',
            'audacity': root / 'audacity_export.zip',
            'chapter': root / CHAPTER_EXPORT_DIR / 'one.mp3',
            'mixed.mp3': root / 'mixed_audiobook.mp3',
            'mixed.wav': root / 'mixed_audiobook.wav',
            'mixed.m4b': root / 'mixed_audiobook.m4b'}
        for name, path in self.paths.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(OLD[name])
        (self.paths['chapter'].parent / 'manifest.json').write_text(json.dumps(
            {'chapters': [{'index': 1, 'file': 'one.mp3'}]}))
        for module in (editor, sound_design):
            stack.enter_context(patch.object(module, 'DATA_DIR', str(root), create=True))
        stack.enter_context(patch.object(editor, 'AUDIOBOOK_PATH',
                                        str(self.paths['mp3']), create=True))
        stack.enter_context(patch.object(editor, 'M4B_PATH',
                                        str(self.paths['m4b']), create=True))
        stack.enter_context(patch.object(editor, '_chapter_export_dir',
                                        return_value=str(self.paths['chapter'].parent)))
        stack.enter_context(patch.object(core, 'DATA_DIR', str(root)))
        stack.enter_context(patch.object(core, 'process_state', {}))
        self.addCleanup(self._remove_zip_downloads)

    def _remove_zip_downloads(self):
        for path in self.root.glob('.alexandria-export-*.zip'):
            path.unlink(missing_ok=True)

    @contextmanager
    def admission(self):
        with ensure_scope_admission(self.root):
            yield

    @staticmethod
    def status(call):
        try:
            result = call()
            stream = getattr(result, 'stream', None) or getattr(result, '_pinned_stream', None)
            if stream is not None:
                stream.close()
            return 200
        except HTTPException as error:
            return error.status_code
        except ValueError:
            return 409

    @staticmethod
    def digest(name):
        return hashlib.sha256(OLD[name]).hexdigest()

    def zip_members(self, response):
        path = Path(response.path)
        self.addCleanup(path.unlink, missing_ok=True)
        with zipfile.ZipFile(path) as archive:
            return {name: archive.read(name) for name in archive.namelist()}

    def _publish_verified_selection(self):
        selection = capture_generation_selection(str(self.root), str(self.source))
        script_bytes = (self.root / 'annotated_script.json').read_bytes()
        state = json.loads((self.root / 'state.json').read_text())
        state.update(script_generation_run_id='f' * 32,
                     script_generation_base_sha256=hashlib.sha256(script_bytes).hexdigest())
        (self.root / 'state.json').write_text(json.dumps(state))
        publish_generated_script(str(self.root), selection['book_token'], str(self.source),
                                selection['source_sha256'], script_bytes, expected_run_id='f' * 32)
        rows = json.loads((self.root / 'annotated_script.json').read_text())
        (self.root / 'chunks.json').write_text(json.dumps([
            dict(row, id=i, uid=f'uid-{i}', status='pending', audio_path=None)
            for i, row in enumerate(group_into_chunks(rows))]))

    def apply_scope(self):
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            return apply_source_scope(self.root, 1, plan['revision'])

    def assert_old_bytes_preserved(self):
        for name, path in self.paths.items():
            self.assertEqual(OLD[name], path.read_bytes(), name)

    def _download(self, name):
        if name == 'mp3':
            return asyncio.run(editor.get_audiobook())
        if name == 'm4b':
            return asyncio.run(editor.get_audiobook_m4b())
        if name == 'audacity':
            return asyncio.run(editor.get_audacity_export())
        if name.startswith('mixed.'):
            return sound_design.download_sound_delivery(
                name.split('.')[-1], self.digest(name))
        return asyncio.run(editor.download_chapter('one.mp3'))

    def publish_output(self, name, payload=None):
        path = self.paths[name]
        raw = path.read_bytes() if payload is None else payload
        staged = path.with_name(path.name + '.republish')
        staged.write_bytes(raw)
        staged.replace(path)
        record_export_publication(self.root, path)
        return raw

    def assert_old_downloads_fenced(self, *, chapter_list_false=True):
        expected = {'mp3': 409, 'm4b': 409, 'audacity': 409, 'chapter': 409,
                    'mixed.mp3': 409, 'mixed.wav': 409, 'mixed.m4b': 409}
        calls = {
            'mp3': lambda: asyncio.run(editor.get_audiobook()),
            'm4b': lambda: asyncio.run(editor.get_audiobook_m4b()),
            'audacity': lambda: asyncio.run(editor.get_audacity_export()),
            'chapter': lambda: asyncio.run(editor.download_chapter('one.mp3')),
            'mixed.mp3': lambda: sound_design.download_sound_delivery('mp3', self.digest('mixed.mp3')),
            'mixed.wav': lambda: sound_design.download_sound_delivery('wav', self.digest('mixed.wav')),
            'mixed.m4b': lambda: sound_design.download_sound_delivery('m4b', self.digest('mixed.m4b'))}
        for name, call in calls.items():
            with self.subTest(download=name):
                self.assertEqual(expected[name], self.status(call))
        with self.subTest(download='export_zip'):
            self.assertEqual(409, self.status(lambda: asyncio.run(editor.export_zip())))
        with self.subTest(download='chapter_zip'):
            self.assertEqual(409, self.status(lambda: asyncio.run(editor.download_chapters_zip())))
        listing = asyncio.run(editor.list_chapter_exports())
        row = next(row for row in listing['chapters'] if row['file'] == 'one.mp3')
        self.assertEqual(not chapter_list_false, row['exists'])

    def assert_old_downloads_current(self):
        calls = {
            'mp3': lambda: asyncio.run(editor.get_audiobook()),
            'm4b': lambda: asyncio.run(editor.get_audiobook_m4b()),
            'audacity': lambda: asyncio.run(editor.get_audacity_export()),
            'chapter': lambda: asyncio.run(editor.download_chapter('one.mp3')),
            'export_zip': lambda: asyncio.run(editor.export_zip()),
            'chapter_zip': lambda: asyncio.run(editor.download_chapters_zip())}
        for name, call in calls.items():
            with self.subTest(download=name):
                self.assertEqual(200, self.status(call))
        for fmt in ('mp3', 'wav', 'm4b'):
            with self.subTest(download='mixed.' + fmt):
                self.assertEqual(200, self.status(lambda: sound_design.download_sound_delivery(
                    fmt, self.digest('mixed.' + fmt))))
        self.assertEqual({'one.mp3': OLD['chapter']},
                         self.zip_members(asyncio.run(editor.download_chapters_zip())))
        self.assertEqual({'audiobook.mp3': OLD['mp3'], 'audiobook.m4b': OLD['m4b']},
                         self.zip_members(asyncio.run(editor.export_zip())))
        for fmt in ('mp3', 'wav', 'm4b'):
            stream, size, suffix = open_sound_delivery(
                self.root, fmt, self.digest('mixed.' + fmt))
            try:
                payload = b''.join(iter(lambda: stream.read(65536), b''))
                self.assertEqual(OLD['mixed.' + fmt], payload)
                self.assertEqual(len(payload), size)
                self.assertEqual('.' + fmt, suffix)
            finally:
                stream.close()


class ScopeAndSelectionReaderTests(ExportReaderFixture, unittest.TestCase):
    def setUp(self):
        self.make_root()

    def test_scope_apply_fences_standard_and_all_mixed_deliveries(self):
        self.assert_old_downloads_current()
        result = self.apply_scope()
        self.assertEqual({'uid-1'}, set(result['retained_uids']))
        self.assert_old_downloads_fenced()
        self.assert_old_bytes_preserved()
        self.assertEqual(set(BASE_EXPORTS) | set(MIXED_DELIVERIES),
                         set(json.loads((self.root / 'state.json').read_text())['_source_scope_exports']))

    def test_new_publications_after_scope_apply_are_admitted(self):
        self.apply_scope()
        self.assert_old_downloads_fenced()
        fresh = {'mp3': b'NEW MP3 AFTER SCOPE', 'mixed.wav': b'NEW MIXED WAV AFTER SCOPE'}
        for name, payload in fresh.items():
            self.paths[name].write_bytes(payload)
            record_export_publication(self.root, self.paths[name])
        with self.subTest(download='new mp3'):
            self.assertEqual(200, self.status(lambda: asyncio.run(editor.get_audiobook())))
        with self.subTest(download='new mixed wav'):
            digest = hashlib.sha256(fresh['mixed.wav']).hexdigest()
            self.assertEqual(200, self.status(
                lambda: sound_design.download_sound_delivery('wav', digest)))
        with self.subTest(download='other old mixed still fenced'):
            self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                'mp3', self.digest('mixed.mp3'))))

    def test_upload_selection_fences_old_exports_without_prior_scope(self):
        self.assert_old_downloads_current()
        apply_book_input_selection(self.root, str(self.root / 'new-source.txt'), 'newbook')
        state = json.loads((self.root / 'state.json').read_text())
        self.assertEqual('newbook', state['active_book_id'])
        self.assertNotEqual('oldgen', state['book_generation'])
        self.assert_old_downloads_fenced()
        self.assert_old_bytes_preserved()

    def test_upload_selection_after_scope_keeps_fence_and_exact_undo_refused(self):
        self.apply_scope()
        apply_book_input_selection(self.root, str(self.root / 'new-source.txt'), 'newbook')
        self.assert_old_downloads_fenced()
        self.assert_old_bytes_preserved()
        with self.admission():
            from project_scope_apply import get_source_scope_undo_status
            status = get_source_scope_undo_status(self.root)
            self.assertTrue(status['available'])
            self.assertFalse(status['can_undo'])
            self.assertTrue((self.root / UNDO).exists())

    def test_scope_undo_still_restores_exact_pre_scope_state(self):
        self.apply_scope()
        self.assert_old_downloads_fenced()
        with self.admission():
            from project_scope_apply import get_source_scope_undo_preview
            undo = get_source_scope_undo_preview(self.root)
            apply_source_scope_undo(self.root, undo['revision'])
        self.assertFalse((self.root / UNDO).exists())
        self.assert_old_downloads_current()
        self.assert_old_bytes_preserved()

    def _remove_scope_undo(self):
        # A legacy inventory with surviving mixed files is intentionally not
        # exactly undoable; this disposable fixture simply drops the record.
        (self.root / UNDO).unlink(missing_ok=True)

    def _apply_legacy_shape(self, keys):
        with patch('project_scope_exports.EXPORTS', keys):
            with self.admission():
                plan = get_scope_preview(self.root, 1)
                apply_source_scope(self.root, 1, plan['revision'])
        inventory = json.loads((self.root / 'state.json').read_text())['_source_scope_exports']
        self.assertEqual(set(keys), set(inventory))
        self.assert_old_downloads_fenced()
        self.assert_old_bytes_preserved()

    def test_legacy_four_key_inventory_fences_mixed_delivery(self):
        self._apply_legacy_shape(LEGACY_EXPORTS)

    def test_legacy_base_key_inventory_fences_mixed_delivery(self):
        self._apply_legacy_shape(BASE_EXPORTS)

    def test_unsupported_inventory_shape_refuses_without_touching_bytes(self):
        state = json.loads((self.root / 'state.json').read_text())
        state['_source_scope_exports'] = {'unexpected_export': {}}
        (self.root / 'state.json').write_text(json.dumps(state))
        before = self.root / 'state.json'
        snapshot = before.read_bytes()
        with self.assertRaisesRegex(ValueError, 'needs inspection'):
            asyncio.run(editor.get_audiobook())
        self.assertEqual(snapshot, before.read_bytes())
        self.assert_old_bytes_preserved()

    def _legacy_published_mix_is_current(self, keys):
        self._apply_legacy_shape(keys)
        self._remove_scope_undo()
        rebuilt = self.publish_output('mixed.wav', b'GENUINE CURRENT MIX')
        digest = hashlib.sha256(rebuilt).hexdigest()
        with self.subTest(download='published mix'):
            self.assertEqual(200, self.status(
                lambda: sound_design.download_sound_delivery('wav', digest)))
        for fmt in ('mp3', 'm4b'):
            with self.subTest(download='untouched ' + fmt):
                self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                    fmt, self.digest('mixed.' + fmt))))

    def test_legacy_four_key_record_admits_published_mix(self):
        self._legacy_published_mix_is_current(LEGACY_EXPORTS)

    def test_legacy_seven_key_record_admits_published_mix(self):
        self._legacy_published_mix_is_current(BASE_EXPORTS)

    def test_verified_same_source_reselection_keeps_eligible_and_historical_outputs(self):
        self._publish_verified_selection()
        self.apply_scope()
        current = self.publish_output('mixed.wav', b'CURRENT MIX FOR RESELECTION')
        current_digest = hashlib.sha256(current).hexdigest()
        apply_book_input_selection(self.root, str(self.source), 'booka')
        with self.subTest(phase='after verified same-source reselect'):
            self.assertEqual(200, self.status(lambda: sound_design.download_sound_delivery(
                'wav', current_digest)))
            self.assertEqual(409, self.status(lambda: asyncio.run(editor.get_audiobook())))
            self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                'mp3', self.digest('mixed.mp3'))))
        self.source.write_text('changed synthetic source bytes')
        apply_book_input_selection(self.root, str(self.source), 'booka')
        with self.subTest(phase='changed source negative control'):
            self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                'wav', current_digest)))

    def test_real_readalong_pair_publication_is_current_after_legacy_fence(self):
        from contextlib import nullcontext
        from export_quality import get_export_file_identity
        from project_scope_exports import is_scope_export_historical, read_publication_receipts
        from readalong_export import _publish_pair_transaction
        self._apply_legacy_shape(BASE_EXPORTS)
        self._remove_scope_undo()
        staging = self.root / 'readalong-stage'
        staging.mkdir()
        pairs = []
        for name in ('readalong.sidecar.json', 'readaloud.epub'):
            staged = staging / name
            staged.write_bytes(('valid synthetic ' + name).encode())
            pairs.append((str(staged), str(self.root / name),
                          get_export_file_identity(staged)))

        class Snapshot:
            root = str(self.root)

            @contextmanager
            def ensure_publication_transaction(self, outputs):
                yield lambda: nullcontext()

        retain = [False]
        _publish_pair_transaction(Snapshot(), pairs, None, staging, retain)
        self.assertFalse(retain[0])
        receipts = read_publication_receipts(self.root)
        for _source, target, report in pairs:
            self.assertIn(Path(target).name, receipts)
            self.assertEqual(report['sha256'], receipts[Path(target).name]['sha256'])
            self.assertFalse(is_scope_export_historical(self.root, Path(target)))

    def test_nine_raw_retirements_all_remain_historical(self):
        import itertools
        payloads = [f'retired mp3 {index}'.encode() for index in range(9)]
        for index, payload in enumerate(payloads):
            self.paths['mp3'].write_bytes(payload)
            record_export_publication(self.root, self.paths['mp3'])
            apply_book_input_selection(self.root,
                str(self.root / f'source-{index}.txt'), f'book-{index}')
        for payload in payloads:
            with self.subTest(payload=payload):
                self.paths['mp3'].write_bytes(payload)
                self.assertEqual(409,
                    self.status(lambda: asyncio.run(editor.get_audiobook())))

    def test_oversized_sparse_export_does_not_block_book_selection(self):
        with self.paths['mixed.wav'].open('wb') as stream:
            stream.truncate(2 * 1024**3 + 1)
        apply_book_input_selection(self.root, str(self.root / 'new-source.txt'), 'newbook')
        state = json.loads((self.root / 'state.json').read_text())
        self.assertEqual('newbook', state['active_book_id'])

    def test_many_chapter_receipts_do_not_block_a_current_mp3(self):
        from project_scope_exports import read_publication_receipts
        chapter_dir = self.paths['chapter'].parent
        for index in range(300):
            path = chapter_dir / f'chapter-{index}.mp3'
            path.write_bytes(f'chapter bytes {index}'.encode())
            record_export_publication(self.root, path)
        before = len(read_publication_receipts(self.root))
        self.assertGreater(before, 256)
        self.publish_output('mp3')
        self.assertEqual(before + 1, len(read_publication_receipts(self.root)))
        self.assertEqual(200, self.status(lambda: asyncio.run(editor.get_audiobook())))

    def test_workspace_reason_distinguishes_previous_book_from_previous_scope(self):
        def statuses():
            with books.ensure_book_state(self.root):
                view = get_book_workspace(str(self.root),
                    {'mp3': str(self.paths['mp3']), 'm4b': str(self.paths['m4b'])},
                    lambda *_: [], lambda *_: None)
            return [row['status'] for row in view['outputs']]
        self.assertEqual(['present_unverified', 'present_unverified'], statuses())
        self.apply_scope()
        self.assertEqual(['historical_source_scope', 'historical_source_scope'], statuses())
        with self.admission():
            from project_scope_apply import get_source_scope_undo_preview
            undo = get_source_scope_undo_preview(self.root)
            apply_source_scope_undo(self.root, undo['revision'])
        apply_book_input_selection(self.root, str(self.root / 'new-source.txt'), 'newbook')
        self.assertEqual(['historical_previous_book', 'historical_previous_book'], statuses())


class SavedBookLoadReaderTests(ExportReaderFixture, unittest.TestCase):
    def setUp(self):
        self.make_root()
        self.scripts = self.root / 'scripts'
        self.scripts.mkdir()
        (self.scripts / 'book_b.json').write_text('[]')
        self.active = self.root / 'annotated_script.json'
        for name, value in (('DATA_DIR', str(self.root)),
                            ('AUDIOBOOK_PATH', str(self.paths['mp3'])),
                            ('M4B_PATH', str(self.paths['m4b'])),
                            ('SCRIPT_PATH', str(self.active)),
                            ('SCRIPTS_DIR', str(self.scripts)),
                            ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                            ('CHUNKS_PATH', str(self.root / 'chunks.json')),
                            ('process_state', {name: {'running': False} for name in (
                                'audio', 'script', 'review', 'persona', 'nicknames',
                                'audacity_export', 'm4b_export', 'chapter_export')})):
            self.stack.enter_context(patch.object(library, name, value))
        self.stack.enter_context(patch.object(library, '_save_active_book_id'))
        self.stack.enter_context(patch.object(library, '_get_saved_book_id', return_value='b'))

    def test_load_preserves_mixed_deliveries_as_historical_and_removes_standard_exports(self):
        self.assertEqual('loaded', asyncio.run(library.load_script(
            library.ScriptLoadRequest(name='book_b')))['status'])
        for name in ('mixed.mp3', 'mixed.wav', 'mixed.m4b'):
            with self.subTest(export=name):
                self.assertTrue(self.paths[name].exists())
                self.assertEqual(OLD[name], self.paths[name].read_bytes())
        for name in ('mp3', 'm4b', 'audacity', 'chapter'):
            with self.subTest(export=name):
                self.assertFalse(self.paths[name].exists())
        for fmt in ('mp3', 'wav', 'm4b'):
            with self.subTest(download='mixed.' + fmt):
                self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                    fmt, self.digest('mixed.' + fmt))))
        with self.subTest(download='chapter zip'):
            self.assertEqual(404, self.status(lambda: asyncio.run(editor.download_chapters_zip())))

    def _load(self):
        return asyncio.run(library.load_script(library.ScriptLoadRequest(name='book_b')))

    def test_failed_load_keeps_every_export_historical_and_real_publication_admits_identical_bytes(self):
        self.apply_scope()
        state_before = (self.root / 'state.json').read_bytes()
        originals = {name: path.read_bytes() for name, path in self.paths.items()}
        ledger = self.root / exports.PUBLICATIONS_FILE
        existed = ledger.exists()
        ledger_before = ledger.read_bytes() if existed else None
        injected = []

        def fail(target):
            if target.name == 'chunks.json' and not injected:
                injected.append(target.name)
                raise OSError('injected storage failure')

        with intercept_book_publications(books, before=fail):
            with self.assertRaises(Exception):
                self._load()
        self.assertEqual([True], [bool(injected)])
        self.assertEqual(state_before, (self.root / 'state.json').read_bytes())
        self.assertEqual(ledger_before, ledger.read_bytes() if ledger.exists() else None)
        for name, raw in originals.items():
            self.assertEqual(raw, self.paths[name].read_bytes(), name)
        self.assert_old_downloads_fenced()
        published = {name: self.publish_output(name) for name in ('mp3', 'mixed.wav')}
        with self.subTest(download='published mp3'):
            self.assertEqual(200, self.status(lambda: asyncio.run(editor.get_audiobook())))
        with self.subTest(download='published mixed wav'):
            self.assertEqual(200, self.status(lambda: sound_design.download_sound_delivery(
                'wav', hashlib.sha256(published['mixed.wav']).hexdigest())))
        with self.subTest(download='remaining m4b'):
            self.assertEqual(409, self.status(lambda: asyncio.run(editor.get_audiobook_m4b())))
        with self.subTest(download='remaining mixed m4b'):
            self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                'm4b', self.digest('mixed.m4b'))))

    def test_failed_load_after_all_formats_current_keeps_every_eligibility(self):
        for name in self.paths:
            self.publish_output(name)
        self.assert_old_downloads_current()
        state_before = (self.root / 'state.json').read_bytes()
        injected = []

        def fail(target):
            if target.name == 'chunks.json' and not injected:
                injected.append(target.name)
                raise OSError('injected storage failure')

        with intercept_book_publications(books, before=fail):
            with self.assertRaises(Exception):
                self._load()
        self.assertEqual(state_before, (self.root / 'state.json').read_bytes())
        self.assert_old_downloads_current()

    def test_malformed_active_json_rolls_back_exactly_and_allows_a_saved_book_load(self):
        self.publish_output('mp3')
        state = self.root / 'state.json'
        broken = b'{malformed JSON'
        state.write_bytes(broken)
        ledger = self.root / exports.PUBLICATIONS_FILE
        prior = ledger.read_bytes()
        save_journal = books._save_journal

        def fail_before_commit(root, data, **kwargs):
            if data['phase'] == 'committed':
                raise OSError(errno.EIO, 'uncommitted load')
            return save_journal(root, data, **kwargs)

        with patch.object(books, '_save_journal', side_effect=fail_before_commit):
            with self.assertRaisesRegex(OSError, 'uncommitted load'):
                self._load()
        self.assertEqual(broken, state.read_bytes())
        self.assertEqual(prior, ledger.read_bytes())
        self.assertEqual(409, self.status(lambda: self._download('mp3')))
        self.assertEqual('loaded', self._load()['status'])
        self.assertEqual('b', json.loads(state.read_bytes())['active_book_id'])
        self.assertFalse(self.paths['mp3'].exists())

    def test_failed_load_does_not_renew_a_ctime_stale_receipt(self):
        for name in ('mp3', 'm4b', 'mixed.wav'):
            self.publish_output(name)
        ledger = self.root / exports.PUBLICATIONS_FILE
        prior = read_publication_receipts(self.root)
        mp3, m4b, mix = (self.paths[name].name for name in ('mp3', 'm4b', 'mixed.wav'))
        # Simulate a receipt whose only stale evidence is ctime, even on
        # platforms that expose creation time through stat().st_ctime.
        prior[mp3]['file_version'][4] -= 1
        ledger.write_bytes(exports.serialize_publication_receipts(prior))
        self.assertEqual(409, self.status(lambda: self._download('mp3')))

        save_journal = books._save_journal

        def fail_before_commit(root, data, **kwargs):
            if data['phase'] == 'committed':
                raise OSError(errno.EIO, 'uncommitted load')
            return save_journal(root, data, **kwargs)

        with patch.object(books, '_save_journal', side_effect=fail_before_commit):
            with self.assertRaisesRegex(OSError, 'uncommitted load'):
                self._load()
        for _ in range(2):
            with books.ensure_book_state(self.root):
                pass
            self.assertEqual(409, self.status(lambda: self._download('mp3')))
            self.assertEqual(200, self.status(lambda: self._download('m4b')))
        restored = read_publication_receipts(self.root)
        self.assertEqual(prior[mp3], restored[mp3])
        self.assertEqual(prior[mix], restored[mix])
        self.assertEqual(prior[m4b]['sha256'], restored[m4b]['sha256'])

    @unittest.skipIf(os.name == 'nt', 'Windows stat ctime is creation time')
    def test_saved_book_rollback_retries_ledger_write_and_post_install_sync(self):
        self.publish_output('mp3')
        journal = self.root / books.JOURNAL
        original = (self.root / 'state.json').read_bytes()
        self.assertEqual(200, self.status(lambda: self._download('mp3')))
        save_journal = books._save_journal
        write_ledger = exports.write_publication_ledger
        installed = books._sync_recovered_export_ledger
        faults = []

        def fail_before_commit(root, data, **kwargs):
            if data['phase'] == 'committed':
                raise OSError(errno.EIO, 'uncommitted load')
            return save_journal(root, data, **kwargs)

        def fail_receipt_write(root, raw, **kwargs):
            if kwargs.get('before_replace') is not None:
                self.assertTrue(journal.exists())
                faults.append('write')
                raise OSError(errno.EIO, 'receipt write')
            return write_ledger(root, raw, **kwargs)

        with patch.object(books, '_save_journal', side_effect=fail_before_commit), \
                patch.object(exports, 'write_publication_ledger', side_effect=fail_receipt_write):
            with self.assertRaisesRegex(OSError, 'receipt write'):
                self._load()
        self.assertTrue(journal.exists())

        def fail_post_install(root, rollback):
            self.assertTrue(journal.exists())
            self.assertIn('ledger', rollback)
            faults.append('sync')
            raise OSError(errno.EIO, 'post-install sync')

        with patch.object(books, '_sync_recovered_export_ledger', side_effect=fail_post_install):
            with self.assertRaisesRegex((OSError, books.BookTransactionError),
                                        'post-install sync|needs inspection'):
                with books.ensure_book_state(self.root):
                    pass
        self.assertTrue(journal.exists())
        with patch.object(books, '_sync_recovered_export_ledger', wraps=installed) as sync:
            for _ in range(2):
                with books.ensure_book_state(self.root):
                    pass
                self.assertEqual(200, self.status(lambda: self._download('mp3')))
            sync.assert_called_once()
        self.assertEqual(['write', 'sync'], faults)
        self.assertFalse(journal.exists())
        self.assertEqual(original, (self.root / 'state.json').read_bytes())
        self.assertEqual(OLD['mp3'], self.paths['mp3'].read_bytes())

    @unittest.skipIf(os.name == 'nt', 'Windows stat ctime is creation time')
    def test_saved_book_rollback_keeps_receipt_evidence_on_workspace_io_error(self):
        self.publish_output('mp3')
        journal = self.root / books.JOURNAL
        self.assertEqual(200, self.status(lambda: self._download('mp3')))
        save_journal, lstat = books._save_journal, os.lstat
        workspaces, failures = [], []

        def fail_before_commit(root, data, **kwargs):
            if data['phase'] == 'committed':
                raise OSError(errno.EIO, 'uncommitted load')
            version = save_journal(root, data, **kwargs)
            if data['phase'] == 'recovered':
                workspaces.append(root / data['workspace'])
            return version

        def fail_one_workspace_lookup(path, *args, **kwargs):
            if workspaces and path == workspaces[-1] and not failures:
                failures.append(path)
                raise OSError(errno.EIO, 'workspace lookup')
            return lstat(path, *args, **kwargs)

        with patch.object(books, '_save_journal', side_effect=fail_before_commit), \
                patch.object(os, 'lstat', side_effect=fail_one_workspace_lookup):
            with self.assertRaisesRegex(ValueError, 'workspace could not be read'):
                self._load()
        self.assertEqual(workspaces, failures)
        self.assertEqual('recovered', json.loads(journal.read_bytes())['phase'])
        for _ in range(2):
            with books.ensure_book_state(self.root):
                pass
            self.assertFalse(journal.exists())
            self.assertEqual([], list(self.root.glob('.book-switch-quarantine-*')))
            self.assertEqual(200, self.status(lambda: self._download('mp3')))
            self.assertEqual(OLD['mp3'], self.paths['mp3'].read_bytes())

    def test_repeated_load_and_restored_standard_bytes_stay_historical(self):
        self.assertEqual('loaded', self._load()['status'])
        for name in ('mp3', 'm4b', 'audacity', 'chapter'):
            original = OLD[name]
            path = self.paths[name]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(original)
        for name in ('mp3', 'm4b', 'audacity', 'chapter'):
            with self.subTest(after='first load', export=name):
                self.assertNotEqual(200, self.status(lambda: self._download(name)))
        self.assertEqual('loaded', self._load()['status'])
        for name in ('mp3', 'm4b', 'audacity', 'chapter'):
            with self.subTest(after='second load', export=name):
                self.assertNotEqual(200, self.status(lambda: self._download(name)))
        self.assertNotEqual(200, self.status(lambda: asyncio.run(editor.export_zip())))

    def test_reader_during_load_is_refused_and_open_stream_keeps_pinned_bytes(self):
        self.apply_scope()
        current = self.publish_output('mixed.wav', b'PINNED MIX BEFORE LOAD')
        digest = hashlib.sha256(current).hexdigest()
        stream, _, _ = open_sound_delivery(self.root, 'wav', digest)
        try:
            self.assertEqual(current[:64], stream.read(64))
            ready, release = threading.Event(), threading.Event()
            observed = []

            def pause(target):
                if target.name == 'state.json' and not ready.is_set():
                    ready.set()
                    self.assertTrue(release.wait(10))

            with intercept_book_publications(books, after=pause):
                load = threading.Thread(target=lambda: observed.append(self._load()))
                load.start()
                try:
                    self.assertTrue(ready.wait(10))
                    with self.subTest(phase='during load'):
                        self.assertEqual(409, self.status(
                            lambda: sound_design.download_sound_delivery('wav', digest)))
                    with self.subTest(phase='already-open stream'):
                        self.assertTrue(stream.read(65536).startswith(current[64:]))
                finally:
                    release.set()
                    load.join(10)
            self.assertFalse(load.is_alive())
            self.assertEqual('loaded', observed[0]['status'])
            with self.subTest(phase='after load'):
                self.assertEqual(409, self.status(
                    lambda: sound_design.download_sound_delivery('wav', digest)))
            self.assertEqual(OLD['mixed.mp3'], self.paths['mixed.mp3'].read_bytes())
        finally:
            stream.close()

    def test_repeated_loads_preserve_mixes_and_then_export_frees_only_that_format(self):
        for _ in range(2):
            self.assertEqual('loaded', self._load()['status'])
        for name in ('mixed.mp3', 'mixed.wav', 'mixed.m4b'):
            with self.subTest(export=name):
                self.assertEqual(OLD[name], self.paths[name].read_bytes())
                self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                    name.split('.')[-1], self.digest(name))))
        current = self.publish_output('mixed.m4b', b'NEW M4B AFTER REPEATED LOADS')
        with self.subTest(download='new m4b'):
            self.assertEqual(200, self.status(lambda: sound_design.download_sound_delivery(
                'm4b', hashlib.sha256(current).hexdigest())))
        for fmt in ('mp3', 'wav'):
            with self.subTest(download='old ' + fmt):
                self.assertEqual(409, self.status(lambda: sound_design.download_sound_delivery(
                    fmt, self.digest('mixed.' + fmt))))


class ReceiptAuthorityTests(ExportReaderFixture, unittest.TestCase):
    def setUp(self):
        self.make_root()
        self.scripts = self.root / 'scripts'
        self.scripts.mkdir()
        (self.scripts / 'book_b.json').write_text('[]')
        for name, value in (('DATA_DIR', str(self.root)),
                            ('AUDIOBOOK_PATH', str(self.paths['mp3'])),
                            ('M4B_PATH', str(self.paths['m4b'])),
                            ('SCRIPT_PATH', str(self.root / 'annotated_script.json')),
                            ('SCRIPTS_DIR', str(self.scripts)),
                            ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                            ('CHUNKS_PATH', str(self.root / 'chunks.json')),
                            ('process_state', {name: {'running': False} for name in (
                                'audio', 'script', 'review', 'persona', 'nicknames',
                                'audacity_export', 'm4b_export', 'chapter_export')})):
            self.stack.enter_context(patch.object(library, name, value))
        self.stack.enter_context(patch.object(library, '_save_active_book_id'))
        self.stack.enter_context(patch.object(library, '_get_saved_book_id', return_value='b'))

    def test_same_source_reselect_never_rebinds_foreign_receipt(self):
        self._publish_verified_selection()
        foreign = hashlib.sha256(b'FOREIGN MIX').hexdigest()
        self.paths['mixed.wav'].write_bytes(b'FOREIGN MIX')
        record_export_publication(self.root, self.paths['mixed.wav'])
        own = self.publish_output('mp3')
        self.assertEqual('loaded', asyncio.run(library.load_script(
            library.ScriptLoadRequest(name='book_b')))['status'])
        self.assertEqual(409, self.status(
            lambda: sound_design.download_sound_delivery('wav', foreign)))
        source = self.root / 'saved-source.txt'
        source.write_text('Saved book synthetic source.')
        apply_book_input_selection(self.root, str(source), 'b')
        self.assertEqual(409, self.status(
            lambda: sound_design.download_sound_delivery('wav', foreign)))
        self.assertEqual(b'FOREIGN MIX', self.paths['mixed.wav'].read_bytes())

    def test_receipt_record_refuses_mismatched_digest(self):
        self.paths['mp3'].write_bytes(b'ACTUAL BYTES')
        with self.assertRaisesRegex(ValueError, 'changed before receipt'):
            record_export_publication(self.root, self.paths['mp3'], '0' * 64)
        self.assertEqual({}, read_publication_receipts(self.root))

    def test_identity_proof_rejects_byte_identical_replacement_inode(self):
        from project_scope_exports import (read_publication_receipts,
                                           refresh_publication_versions)
        path = self.paths['mp3']
        raw = self.publish_output('mp3')
        receipt = read_publication_receipts(self.root)['cloned_audiobook.mp3']
        proof = {'sha256': receipt['sha256'],
                 'file_version': [value + 1 for value in receipt['file_version']]}
        staged = path.with_name(path.name + '.swap')
        staged.write_bytes(raw)
        staged.replace(path)
        refresh_publication_versions(self.root, {'cloned_audiobook.mp3': [0, 0, 0, 0, 0, 0]})
        self.assertEqual(list(receipt['file_version']),
                         read_publication_receipts(self.root)['cloned_audiobook.mp3']['file_version'])

    def test_oversized_state_is_refused_by_publication_identity_reader(self):
        from book_state_transaction import get_book_operation_token
        with (self.root / 'state.json').open('wb') as stream:
            stream.truncate(67 * 1024**2 + 2)
        with self.assertRaises(ValueError):
            get_book_operation_token(self.root)
        with self.assertRaises(ValueError):
            record_export_publication(self.root, self.paths['mp3'])

    def test_changed_only_chapter_reuse_requires_current_eligibility(self):
        from project import get_chapter_reuse_duration
        self.apply_scope()
        chapter = self.paths['chapter']
        raw = self.publish_output('chapter')
        old = {'index': 1, 'file': 'one.mp3', 'duration_ms': 100,
               'output_bytes': len(raw), 'output_sha256': hashlib.sha256(raw).hexdigest(),
               'fingerprint': 'f' * 64, 'source_fingerprint': 's' * 64,
               'chapter_fingerprint': 'c' * 64}
        plan = {'index': 1, 'file': 'one.mp3', 'fingerprint': 'f' * 64,
                'source_fingerprint': 's' * 64}
        out_dir = str(chapter.parent)
        self.assertIsNotNone(get_chapter_reuse_duration(plan, old, {1}, True, out_dir))
        staged = chapter.with_name('one.swap')
        staged.write_bytes(raw)
        staged.replace(chapter)
        self.assertIsNone(get_chapter_reuse_duration(plan, old, {1}, True, out_dir))
