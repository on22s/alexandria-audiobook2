import base64
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from book_state_transaction import ensure_book_state
from epub_publication import get_epub_publication_candidate
import m4b_encode
from publication_preview import get_publication_review
from publication_source import get_publication_source_path
from tests import test_saved_publication_source as source_fixture
from tests import test_m4b_export_quality as audio_fixture
from tests import test_export_task_results as task_fixture
from routers import editor, script
import core
from tests.test_m4b_cover_admission import image_bytes
from tests.test_upload_publication_source import epub, TEXT


class M4bSourcePublicationTests(unittest.TestCase):
    def test_materialized_source_cover_change_before_snapshot_refuses_before_encoding(self):
        import export_publication
        for fault in ('missing','substituted'):
            with self.subTest(fault=fault), self.fixture() as (root,manager,row):
                choice = self.selection(root)
                before = self.prior(root,manager)
                snapshot = export_publication.ExportSourceSnapshot
                def changed(*args,**kwargs):
                    selected = next(root.glob('.m4b-source-cover-*/cover.*'))
                    if fault == 'missing':
                        selected.unlink()
                    else:
                        selected.write_bytes(image_bytes('JPEG'))
                    return snapshot(*args,**kwargs)
                with patch.object(export_publication,'ExportSourceSnapshot',side_effect=changed), \
                        patch.object(m4b_encode,'encode_m4b',wraps=m4b_encode.encode_m4b) as encode:
                    with self.assertRaisesRegex(ValueError,'cover changed before M4B source capture|cover is missing before source capture'):
                        manager.merge_m4b(source_cover=choice,
                            expected_book_token=choice['expected_book_token'])
                    encode.assert_not_called()
                self.assertEqual(before,(root/'audiobook.m4b').read_bytes())
                self.assert_clean(root)

    def test_missing_uploaded_cover_with_source_selection_refuses_before_encoding(self):
        with self.fixture() as (root,manager,row):
            before = self.prior(root,manager)
            choice = self.selection(root)
            selected = root/'missing-uploaded.png'
            with patch.object(m4b_encode,'encode_m4b',wraps=m4b_encode.encode_m4b) as encode:
                with self.assertRaisesRegex(ValueError,'Selected M4B cover is missing'):
                    manager.merge_m4b(metadata={'cover_path':str(selected)},source_cover=choice)
                encode.assert_not_called()
            self.assertEqual(before,(root/'audiobook.m4b').read_bytes())
            self.assert_clean(root)

    def test_uploaded_cover_missing_before_snapshot_refuses_before_encoding(self):
        import export_publication
        with self.fixture() as (root,manager,row):
            before = self.prior(root,manager)
            selected = root/'uploaded.png'
            selected.write_bytes(image_bytes('PNG'))
            snapshot = export_publication.ExportSourceSnapshot
            def changed(*args,**kwargs):
                selected.unlink()
                return snapshot(*args,**kwargs)
            with patch.object(export_publication,'ExportSourceSnapshot',side_effect=changed), \
                    patch.object(m4b_encode,'encode_m4b',wraps=m4b_encode.encode_m4b) as encode:
                with self.assertRaisesRegex(ValueError,'Selected M4B cover is missing'):
                    manager.merge_m4b(metadata={'cover_path':str(selected)})
                encode.assert_not_called()
            self.assertEqual(before,(root/'audiobook.m4b').read_bytes())
            self.assert_clean(root)

    def test_missing_or_substituted_finished_cover_preserves_prior_delivery(self):
        for fault in ('missing','substituted'):
            with self.subTest(fault=fault), self.fixture() as (root,manager,row):
                choice = self.selection(root)
                before = self.prior(root,manager)
                encode = m4b_encode.encode_m4b
                substitute = root/'substitute.jpg'
                substitute.write_bytes(image_bytes('JPEG'))
                def changed(command,*args,**kwargs):
                    command = list(command)
                    if fault == 'missing':
                        index = command.index('1:v')
                        del command[index-1:index+1]
                    else:
                        selected = next(root.glob('.m4b-source-cover-*/cover.*'))
                        command[command.index(str(selected))] = str(substitute)
                    return encode(command,*args,**kwargs)
                with patch.object(m4b_encode,'encode_m4b',side_effect=changed):
                    with self.assertRaisesRegex(ValueError,'attached picture|cover differs'):
                        manager.merge_m4b(source_cover=choice,
                            expected_book_token=choice['expected_book_token'])
                self.assertEqual(before,(root/'audiobook.m4b').read_bytes())
                self.assert_clean(root)

    def test_unexpected_finished_picture_refuses_no_cover_export(self):
        with self.fixture() as (root,manager,row):
            before = self.prior(root,manager)
            extra = root/'unexpected.png'; extra.write_bytes(image_bytes('PNG'))
            encode = m4b_encode.encode_m4b
            def changed(command,*args,**kwargs):
                command = list(command)
                index = command.index('-map_metadata')
                command[index+1] = '2'
                command[4:4] = ['-i',str(extra)]
                command[-1:-1] = ['-map','1:v','-c:v','copy','-disposition:v:0','attached_pic']
                return encode(command,*args,**kwargs)
            with patch.object(m4b_encode,'encode_m4b',side_effect=changed):
                with self.assertRaisesRegex(ValueError,'Unapproved M4B cover'):
                    manager.merge_m4b()
            self.assertEqual(before,(root/'audiobook.m4b').read_bytes())
            self.assert_clean(root)

    def test_read_only_capture_returns_native_source_bytes_without_materializing_cover(self):
        from m4b_publication import capture_m4b_publication_cover
        for fmt in ('PNG','JPEG'):
            with self.subTest(fmt=fmt), self.fixture(fmt) as (root,manager,row):
                choice = self.selection(root)
                before = {str(path.relative_to(root)) for path in root.rglob('*')}
                captured = capture_m4b_publication_cover(root,None,choice,choice['expected_book_token'])
                self.assertEqual(base64.b64decode(row['cover']['data_base64']),captured['cover_bytes'])
                self.assertIsNone(captured['cover_path'])
                self.assertIn(str(root/'state.json'),captured['expected_sources'])
                self.assertIn(str(root/'uploads/source.txt'),captured['expected_sources'])
                self.assertEqual(before,{str(path.relative_to(root)) for path in root.rglob('*')})
                self.assertFalse(list(root.glob('.m4b-source-cover-*')))
                # An approved mix owns this same native book lock already.
                # Read-only capture must reuse it rather than self-deadlock.
                with ensure_book_state(root,recover=False):
                    nested = capture_m4b_publication_cover(root,None,choice,choice['expected_book_token'])
                self.assertEqual(captured,nested)
                omitted = {key:value for key,value in choice.items() if key != 'reviewed'}
                self.assertEqual(captured,capture_m4b_publication_cover(root,None,omitted,choice['expected_book_token']))

    @contextmanager
    def fixture(self, fmt='PNG'):
        with source_fixture.SavedPublicationSourceTests().fixture() as (root, _):
            # The audio fixture owns explicit synthetic chunks, without script generation.
            (root / 'annotated_script.json').unlink()
            manager = audio_fixture.M4BExportQualityTests().fixture(str(root), rates=(24000,))
            row = get_epub_publication_candidate(epub(cover=image_bytes(fmt)), filename='source.epub',
                                                text_sha256=hashlib.sha256(TEXT).hexdigest())
            companion = Path(get_publication_source_path(root / 'uploads/source.txt'))
            companion.write_text(json.dumps({'version': 1, 'binding': {
                'kind': 'text', 'sha256': hashlib.sha256(TEXT).hexdigest()}, 'candidates': [row]}))
            yield root, manager, row

    def selection(self, root):
        with ensure_book_state(root, recover=False):
            review = get_publication_review(root)
        return {'expected_book_token': review['book_token'], 'expected_revision': review['revision'],
                'origin_id': review['captured']['document']['candidates'][0]['id'], 'reviewed': False}

    def probe(self, root):
        return json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format',
            '-show_streams', '-of', 'json', str(root / 'audiobook.m4b')], timeout=10))

    def assert_clean(self, root):
        self.assertFalse(list(root.glob('.m4b-source-cover-*')))
        self.assertFalse(list(root.glob('*.pending.*')))
        self.assertFalse(list(root.glob('_m4b_*')))

    def prior(self, root, manager):
        success, message = manager.merge_m4b(metadata={'title': 'Prior valid delivery'})
        self.assertTrue(success, message)
        return (root / 'audiobook.m4b').read_bytes()

    def test_real_m4b_uses_explicit_source_cover_exact_bytes_and_manual_tags(self):
        for fmt in ('PNG', 'JPEG'):
            with self.subTest(format=fmt), self.fixture(fmt) as (root, manager, row):
                choice = self.selection(root)
                success, message = manager.merge_m4b(metadata={'title': 'Entered title', 'author': 'Entered author'},
                    source_cover=choice, expected_book_token=choice['expected_book_token'])
                self.assertTrue(success, message)
                actual = self.probe(root)
                self.assertEqual('Entered title', actual['format']['tags']['title'])
                self.assertEqual('Entered author', actual['format']['tags']['artist'])
                pictures = [stream for stream in actual['streams'] if stream.get('disposition', {}).get('attached_pic')]
                self.assertEqual(1, len(pictures))
                self.assertEqual((32, 24), (pictures[0]['width'], pictures[0]['height']))
                image = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(root / 'audiobook.m4b'),
                    '-map', '0:v:0', '-frames:v', '1', '-c:v', 'copy', '-f', 'image2pipe', 'pipe:1'], timeout=10)
                self.assertEqual(row['cover']['sha256'], hashlib.sha256(image).hexdigest())
                self.assertFalse((root / 'm4b_cover.jpg').exists())
                self.assert_clean(root)

    def test_absent_and_intentionally_empty_title_differ_and_source_cover_is_opt_in(self):
        with self.fixture() as (root, manager, _):
            for metadata, expected in (({}, 'Audiobook'), ({'title': '', 'author': ''}, '')):
                success, message = manager.merge_m4b(metadata=metadata)
                self.assertTrue(success, message)
                actual = self.probe(root)
                self.assertEqual(expected, actual['format']['tags'].get('title', ''))
                self.assertFalse(any(stream.get('disposition', {}).get('attached_pic') for stream in actual['streams']))
                self.assert_clean(root)

    def test_uploaded_cover_wins_over_even_a_stale_source_selection(self):
        with self.fixture() as (root, manager, _):
            choice = self.selection(root); choice['expected_revision'] = '0' * 64
            manual = root / 'm4b_cover.jpg'; raw = image_bytes('JPEG'); manual.write_bytes(raw)
            success, message = manager.merge_m4b(metadata={'cover_path': str(manual)}, source_cover=choice)
            self.assertTrue(success, message)
            pictures = [stream for stream in self.probe(root)['streams'] if stream.get('disposition', {}).get('attached_pic')]
            self.assertEqual('mjpeg', pictures[0]['codec_name'])
            self.assertEqual(raw, manual.read_bytes()); self.assert_clean(root)

    def test_http_source_choice_uses_real_task_admission_result_and_clears_claims(self):
        with self.fixture() as (root, manager, _), task_fixture.ExportTaskResultTests().fixture() as (_, states, client):
            choice = self.selection(root)
            with patch.object(core, 'DATA_DIR', str(root)), patch.object(script, 'DATA_DIR', str(root)), \
                    patch.object(editor, 'DATA_DIR', str(root)), patch.object(editor, 'project_manager', manager):
                response = client.post('/api/merge_m4b', json={'title': 'HTTP entered title', 'author': '',
                    'source_cover': choice, 'expected_book_token': choice['expected_book_token']})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual('done', states['m4b_export']['result']['status'])
                self.assertFalse(states['m4b_export']['running'])
                self.assertFalse(core._task_claims); self.assertFalse(core._gpu_leases)
                actual = self.probe(root)
                self.assertEqual('HTTP entered title', actual['format']['tags']['title'])
                self.assertEqual('', actual['format']['tags'].get('artist', ''))
                self.assertTrue(any(row.get('disposition', {}).get('attached_pic') for row in actual['streams']))
                before = (root / 'audiobook.m4b').read_bytes()
                response = client.post('/api/merge_m4b', json={'source_cover': {**choice, 'cover_path': '/not/allowed'}})
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(before, (root / 'audiobook.m4b').read_bytes())
                self.assert_clean(root)

    def test_changed_dependencies_after_encode_preserve_prior_valid_output(self):
        for fault in ('companion', 'source', 'uploaded', 'book', 'temporary'):
            with self.subTest(fault=fault), self.fixture() as (root, manager, _):
                before = self.prior(root, manager); choice = self.selection(root); encode = m4b_encode.encode_m4b
                def changed(*args, **kwargs):
                    result = encode(*args, **kwargs)
                    if fault == 'companion':
                        path = Path(get_publication_source_path(root / 'uploads/source.txt'))
                        doc = json.loads(path.read_bytes()); doc['candidates'][0]['fields']['title'] = ['Changed']
                        path.write_text(json.dumps(doc))
                    elif fault == 'source':
                        (root / 'uploads/source.txt').write_bytes(TEXT + b' Changed input.')
                    elif fault == 'uploaded':
                        (root / 'm4b_cover.jpg').write_bytes(image_bytes('JPEG'))
                    elif fault == 'book':
                        path = root / 'state.json'; state = json.loads(path.read_bytes())
                        state['book_generation'] = 'changed'; path.write_text(json.dumps(state))
                    else:
                        next(root.glob('.m4b-source-cover-*/cover.*')).write_bytes(b'changed cover')
                    return result
                with patch.object(m4b_encode, 'encode_m4b', side_effect=changed):
                    with self.assertRaises(ValueError):
                        manager.merge_m4b(source_cover=choice)
                self.assertEqual(before, (root / 'audiobook.m4b').read_bytes()); self.assert_clean(root)

    def test_encoder_failure_and_post_encode_cancellation_clean_cover_and_preserve_prior(self):
        for fault in ('encoder', 'cancel'):
            with self.subTest(fault=fault), self.fixture() as (root, manager, _):
                before = self.prior(root, manager); choice = self.selection(root); cancelled = False
                encode = m4b_encode.encode_m4b
                def stopped(*args, **kwargs):
                    nonlocal cancelled
                    if fault == 'encoder':
                        return 1, 'fixture encoder failure'
                    result = encode(*args, **kwargs); cancelled = True; return result
                with patch.object(m4b_encode, 'encode_m4b', side_effect=stopped):
                    success, _message = manager.merge_m4b(source_cover=choice, cancel_check=lambda: cancelled)
                self.assertFalse(success)
                self.assertEqual(before, (root / 'audiobook.m4b').read_bytes()); self.assert_clean(root)

    def test_final_publication_failure_preserves_valid_delivery_and_cleans_selected_cover(self):
        with self.fixture() as (root, manager, _):
            before = self.prior(root, manager); choice = self.selection(root); replace = os.replace
            def refused(source, destination, *args, **kwargs):
                if str(destination) == str(root / 'audiobook.m4b'):
                    raise OSError('fixture final M4B publication failure')
                return replace(source, destination, *args, **kwargs)
            with patch('os.replace', side_effect=refused):
                with self.assertRaisesRegex(OSError, 'final M4B publication failure'):
                    manager.merge_m4b(source_cover=choice)
            self.assertEqual(before, (root / 'audiobook.m4b').read_bytes()); self.assert_clean(root)

    def test_invalid_cover_pixels_wrong_book_and_stale_choice_refuse_before_encoding(self):
        for fault in ('pixels', 'book', 'revision'):
            with self.subTest(fault=fault), self.fixture() as (root, manager, _):
                before = self.prior(root, manager)
                if fault == 'pixels':
                    path = Path(get_publication_source_path(root / 'uploads/source.txt')); doc = json.loads(path.read_bytes())
                    cover = doc['candidates'][0]['cover']; raw = b'not an image'
                    cover['data_base64'] = base64.b64encode(raw).decode(); cover['sha256'] = hashlib.sha256(raw).hexdigest()
                    path.write_text(json.dumps(doc))
                choice = self.selection(root)
                if fault == 'revision': choice['expected_revision'] = '0' * 64
                expected_book = '0' * 64 if fault == 'book' else choice['expected_book_token']
                with patch.object(m4b_encode, 'encode_m4b', side_effect=AssertionError('Encoder must not start')):
                    with self.assertRaises(ValueError):
                        manager.merge_m4b(source_cover=choice, expected_book_token=expected_book)
                self.assertEqual(before, (root / 'audiobook.m4b').read_bytes()); self.assert_clean(root)
