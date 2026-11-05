"""Known-answer timing rejects plausible-but-wrong spans and stale takes."""
from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wave

from editor_word_timing import publish_word_timing, get_cached_word_timing, get_timing_cache_path
from project import ProjectManager
from word_timing import get_validated_word_spans, render_word_timing, run_timing_command
import pronunciation


class WordTimingTests(unittest.TestCase):
    def test_known_answer_words_and_contractions_keep_real_bounds(self):
        text = "Don't wait, Amber."
        rows = [{'word': "Don't", 'start': .1, 'end': .4},
                {'word': 'wait', 'start': .5, 'end': .7},
                {'word': 'Amber', 'start': .9, 'end': 1.3}]
        spans = get_validated_word_spans(text, rows, 2)
        self.assertEqual(["Don't", 'wait', 'Amber'], [text[s['from']:s['to']] for s in spans])
        self.assertEqual([(.1, .4), (.5, .7), (.9, 1.3)], [(s['start'], s['end']) for s in spans])

    def test_repeated_missing_and_wrong_words_do_not_receive_invented_times(self):
        text = 'go Amber go home'
        rows = [{'word': 'go', 'start': 0, 'end': .2}, {'word': 'Amber', 'start': .3, 'end': .5},
                {'word': 'go', 'start': .6, 'end': .8}, {'word': 'wrong', 'start': .9, 'end': 1.1}]
        spans = get_validated_word_spans(text, rows, 2)
        self.assertEqual(['Amber'], [text[s['from']:s['to']] for s in spans])
        self.assertEqual([], get_validated_word_spans(text, [{'word': 'unrelated', 'start': 0, 'end': 1}], 2))

    def test_native_multiword_groups_retain_group_timing(self):
        text = '🌟 Amber arrives.'
        spans = get_validated_word_spans(text, [{'word': 'Amber arrives', 'start': .1, 'end': 1}], 2)
        self.assertEqual([{'from': 2, 'to': 15, 'start': .1, 'end': 1.0}], spans)

    def test_invalid_overlap_saturation_and_nonfinite_timestamps_are_rejected(self):
        for start, end in [(float('nan'), 1), (0, float('inf')), (-1, 1), (1, 1), (2, 1), (0, 3), (True, 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                get_validated_word_spans('Amber', [{'word': 'Amber', 'start': start, 'end': end}], 2)
        for rows in [
            [{'word': 'Amber', 'start': 0, 'end': 1}, {'word': 'arrives', 'start': 0, 'end': 1}],
            [{'word': 'Amber', 'start': 0, 'end': 1}, {'word': 'arrives', 'start': .9, 'end': 1.5}],
        ]:
            with self.assertRaises(ValueError):
                get_validated_word_spans('Amber arrives', rows, 2)

    def test_cancel_before_start_does_not_launch_a_child(self):
        with patch('word_timing.start_owned_subprocess') as start:
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                run_timing_command(['must-not-run'], lambda: True)
            start.assert_not_called()

    def test_real_cancelled_child_is_reaped_and_timeout_is_finite(self):
        import os, sys
        with tempfile.TemporaryDirectory() as tmp:
            pid = Path(tmp) / 'pid'
            script = 'import os,pathlib,time;pathlib.Path(' + repr(str(pid)) + ').write_text(str(os.getpid()));time.sleep(30)'
            with self.assertRaisesRegex(ValueError, 'cancelled'):
                run_timing_command([sys.executable, '-c', script], pid.exists, 10)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pid.read_text()), 0)
            with self.assertRaisesRegex(ValueError, 'timed out'):
                run_timing_command([sys.executable, '-c', 'import time;time.sleep(30)'], lambda: False, .1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        scratch = patch('cpu_asr_scratch.get_cpu_asr_scratch_parent', return_value=self.root)
        scratch.start(); self.addCleanup(scratch.stop)
        self.pm = ProjectManager(str(self.root))
        self.pm.voicelines_dir = str(self.root / 'voicelines')
        Path(self.pm.voicelines_dir).mkdir(exist_ok=True)
        self.audio = Path(self.pm.voicelines_dir) / 'line.wav'
        with wave.open(str(self.audio), 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000)
            stream.writeframes(b'\0\0' * 32000)
        self.pm.save_chunks([{'id': 0, 'uid': 'row', 'text': 'Amber arrives',
            'speaker': 'Hero', 'instruct': '', 'audio_path': 'voicelines/line.wav', 'audio_revision': 'take1'}])
        self.runtime = {'binary': str(self.root / 'binary'), 'model': str(self.root / 'model')}
        for path in self.runtime.values():
            Path(path).write_bytes(b'fixture')
        self.request = SimpleNamespace(expected_book_token='a'*64, expected_uid='row', expected_audio_revision='take1')
        self.check = lambda *_: None
        patcher = patch.object(pronunciation, 'DEFAULT_PATH', str(self.root / 'pronunciation.json'))
        patcher.start(); self.addCleanup(patcher.stop)

    def render(self, _audio, _runtime, text, _cancel, _directory):
        rows = [{'word': 'Amber', 'start': .1, 'end': .5}, {'word': 'arrives', 'start': .7, 'end': 1.2}]
        return {'display_text': text, 'spans': get_validated_word_spans(text, rows, 2), 'segments': rows,
                'duration': 2, 'notice': 'Estimate', 'provider': 'fixture', 'language': 'en'}

    def run_case(self):
        return publish_word_timing(self.pm, 0, self.request, self.runtime, self.check, lambda: False)

    def test_cache_is_bound_to_audio_text_and_model_and_never_rewrites_them(self):
        original = self.audio.read_bytes()
        rows = Path(self.pm.chunks_path).read_bytes()
        with patch('editor_word_timing.render_word_timing', side_effect=self.render) as render:
            first = self.run_case()
            self.assertEqual(rows, Path(self.pm.chunks_path).read_bytes())
            second = self.run_case()
            self.assertEqual(rows, Path(self.pm.chunks_path).read_bytes())
            self.assertFalse(first['cached']); self.assertTrue(second['cached']); self.assertEqual(1, render.call_count)
            self.audio.write_bytes(original + b'changed')
            expected = Path(self.pm.chunks_path).read_bytes()
            self.assertFalse(self.run_case()['cached']); self.assertEqual(2, render.call_count)
            self.assertEqual(expected, Path(self.pm.chunks_path).read_bytes())
            Path(self.runtime['model']).write_bytes(b'changed model')
            expected = Path(self.pm.chunks_path).read_bytes()
            self.assertFalse(self.run_case()['cached']); self.assertEqual(3, render.call_count)
            self.assertEqual(expected, Path(self.pm.chunks_path).read_bytes())
            chunks = self.pm.load_chunks(); chunks[0]['text'] = 'Amber arrives later'; self.pm.save_chunks(chunks)
            expected = Path(self.pm.chunks_path).read_bytes()
            self.assertFalse(self.run_case()['cached']); self.assertEqual(4, render.call_count)
            self.assertEqual(expected, Path(self.pm.chunks_path).read_bytes())
        self.assertNotEqual(rows, Path(self.pm.chunks_path).read_bytes())  # only our deliberate edit
        self.assertEqual(original + b'changed', self.audio.read_bytes())

    def test_mutation_during_asr_is_refused_without_publishing_or_overwriting_cache(self):
        with patch('editor_word_timing.render_word_timing', side_effect=self.render):
            first = self.run_case()
        cache = get_timing_cache_path(self.pm, 'a'*64, 'row')
        prior = cache.read_bytes()
        self.audio.write_bytes(self.audio.read_bytes() + b'new input')
        def changed(*args):
            output = self.render(*args)
            chunks = self.pm.load_chunks(); chunks[0]['audio_revision'] = 'take2'; self.pm.save_chunks(chunks)
            return output
        with patch('editor_word_timing.render_word_timing', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'take changed'):
                self.run_case()
        self.assertEqual(prior, cache.read_bytes())
        self.assertEqual([], list(self.root.glob('.word-timing-*')))

    def test_foreign_audio_and_stale_revision_refused_before_asr(self):
        for patch_row in [{'audio_path': '../outside.wav'}, {'audio_revision': 'other'}]:
            with self.subTest(patch_row=patch_row):
                original = self.pm.load_chunks()
                self.pm.save_chunks([{**original[0], **patch_row}])
                with patch('editor_word_timing.render_word_timing') as render, self.assertRaises(ValueError):
                    self.run_case()
                render.assert_not_called()
                self.pm.save_chunks(original)

    def test_api_claim_lifetime_and_stale_uid_use_actual_editor_contract(self):
        import copy
        import core
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        from tests.test_support import use_private_cpu_fixture_lock
        @use_private_cpu_fixture_lock()
        def check():
            with ExitStack() as stack:
                state = copy.deepcopy(core.process_state)
                for row in state.values():
                    row['running'] = False
                for module, name, value in [
                    (core, 'process_state', state), (editor, 'process_state', state),
                    (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                    (editor, 'project_manager', self.pm),
                    (editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json')),
                ]:
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch('whisper_cpp_runtime.get_whisper_cpp_runtime', return_value={**self.runtime, 'available': True}))
                def render(*args):
                    self.assertTrue(state['word_timing']['running'])
                    self.assertIn('word_timing', core._task_claims)
                    return self.render(*args)
                mocked = stack.enter_context(patch('editor_word_timing.render_word_timing', side_effect=render))
                api = FastAPI(); api.include_router(editor.router)
                client = stack.enter_context(TestClient(api))
                token = editor.get_snapshot_digest(editor.get_editor_book_identity(str(self.root)))
                body = {'expected_book_token': token, 'expected_uid': 'row', 'expected_audio_revision': 'take1'}
                response = client.post('/api/chunks/0/word_timing', json=body)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(1, mocked.call_count)
                import hashlib
                original = self.audio.read_bytes()
                audio_params = {**body, 'sha256': hashlib.sha256(original).hexdigest()}
                audio = client.get('/api/chunks/0/word_timing/audio', params=audio_params)
                self.assertEqual(200, audio.status_code, audio.text if audio.status_code != 200 else '')
                self.assertEqual(original, audio.content)
                self.assertEqual('no-store', audio.headers['cache-control'])
                self.audio.write_bytes(b'changed take')
                stale_audio = client.get('/api/chunks/0/word_timing/audio', params=audio_params)
                self.assertEqual(409, stale_audio.status_code)
                self.audio.write_bytes(original)
                self.assertEqual(1, mocked.call_count)
                self.assertFalse(state['word_timing']['running'])
                self.assertNotIn('word_timing', core._task_claims)
                bad = client.post('/api/chunks/0/word_timing', json={**body, 'expected_uid': 'stale'})
                self.assertEqual(409, bad.status_code, bad.text)
                self.assertEqual(1, mocked.call_count)
                self.assertFalse(state['word_timing']['running'])
        check()

    def test_verified_audio_uses_open_captured_inode_and_refuses_changed_bytes(self):
        import hashlib
        import os
        from editor_word_timing import open_verified_word_audio
        original = self.audio.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        stream, size, suffix = open_verified_word_audio(self.pm, 0, 'a'*64, 'row', 'take1', digest, self.check)
        try:
            replacement = self.root / 'new.wav'; replacement.write_bytes(b'new take bytes')
            try:
                os.replace(replacement, self.audio)
            except PermissionError:
                # Windows may deny replacement of an open file. Its handle
                # still returns the captured bytes; verify stale bytes below.
                self.assertEqual(original, stream.read())
                stream.close()
                os.replace(replacement, self.audio)
            else:
                self.assertEqual(original, stream.read())
            self.assertEqual(len(original), size)
            self.assertEqual('.wav', suffix)
        finally:
            stream.close()
        with self.assertRaisesRegex(ValueError, 'bytes changed'):
            open_verified_word_audio(self.pm, 0, 'a'*64, 'row', 'take1', digest, self.check)

    def prepare_gap_take(self):
        import numpy as np
        import soundfile as sf
        rows = self.pm.load_chunks()
        rows[0]['text'] = 'Amber arrives. Dawn waits.'
        rows[0]['pause_after'] = 900
        self.pm.save_chunks(rows)
        pcm = .2 * np.sin(np.arange(32000) * .17)
        pcm[9600:19200] = 0
        sf.write(str(self.audio), pcm, 16000, subtype='PCM_16')

    def render_gap_take(self, _audio, _runtime, text, _cancel, _directory):
        segments = [{'word': word, 'start': start, 'end': end} for word, start, end in [
            ('Amber', .1, .3), ('arrives', .3, .6), ('Dawn', 1.2, 1.5), ('waits', 1.5, 1.8)]]
        return {'display_text': text, 'spans': get_validated_word_spans(text, segments, 2),
                'segments': segments, 'duration': 2, 'provider': 'fixture', 'language': 'en', 'notice': 'Estimate'}

    def test_sentence_gap_analysis_refreshes_legacy_cache_then_reuses_observations(self):
        import hashlib
        self.prepare_gap_take()
        original = self.audio.read_bytes()
        script = Path(self.pm.chunks_path).read_bytes()
        with patch('editor_word_timing.render_word_timing', side_effect=self.render_gap_take) as render:
            self.run_case()
            cache = get_timing_cache_path(self.pm, 'a'*64, 'row')
            legacy = json.loads(cache.read_text(encoding='utf-8'))
            legacy.pop('segments')
            cache.write_text(json.dumps(legacy), encoding='utf-8')
            self.assertTrue(self.run_case()['cached'])
            self.assertEqual(1, render.call_count)
            self.request.analyze_sentence_gaps = True
            first = self.run_case()
            self.assertFalse(first['cached'])
            self.assertEqual(2, render.call_count)
            report = first['sentence_gap_report']
            self.assertEqual('sentence_gap_report_only', report['kind'])
            self.assertFalse(report['mutates_audio'])
            self.assertFalse(report['authorizes_edit'])
            self.assertEqual(hashlib.sha256(original).hexdigest(), report['provenance']['file_sha256'])
            proposal, = report['proposals']
            self.assertEqual('proposal', proposal['status'])
            self.assertGreater(proposal['proposed_extension_ms'], 0)
            self.assertEqual(.63, proposal['safe_quiet_interval']['start'])
            self.assertNotIn('sentence_gap_report', json.loads(cache.read_text(encoding='utf-8')))
            # A cached report is never trusted, even if a previous version wrote one.
            poisoned = json.loads(cache.read_text(encoding='utf-8'))
            poisoned['sentence_gap_report'] = {'kind': 'stale'}
            cache.write_text(json.dumps(poisoned), encoding='utf-8')
            second = self.run_case()
            self.assertTrue(second['cached'])
            self.assertEqual(report, second['sentence_gap_report'])
            self.assertEqual(2, render.call_count)
        self.assertEqual(original, self.audio.read_bytes())
        self.assertEqual(script, Path(self.pm.chunks_path).read_bytes())

    def test_sentence_gap_analysis_refuses_stale_or_cancelled_publication(self):
        from sentence_gap_report import get_sentence_gap_report_for_take
        self.prepare_gap_take()
        self.request.analyze_sentence_gaps = True
        with patch('editor_word_timing.render_word_timing', side_effect=self.render_gap_take):
            self.run_case()
        cache = get_timing_cache_path(self.pm, 'a'*64, 'row')
        previous = cache.read_bytes()
        for action in ('change_take', 'cancel'):
            cancelled = False
            def build(**kwargs):
                nonlocal cancelled
                report = get_sentence_gap_report_for_take(**kwargs)
                if action == 'change_take':
                    rows = self.pm.load_chunks(); rows[0]['audio_revision'] = 'take2'; self.pm.save_chunks(rows)
                else:
                    cancelled = True
                return report
            with patch('sentence_gap_report.get_sentence_gap_report_for_take', side_effect=build):
                with self.assertRaises(ValueError):
                    publish_word_timing(self.pm, 0, self.request, self.runtime, self.check, lambda: cancelled)
            self.assertEqual(previous, cache.read_bytes())
            rows = self.pm.load_chunks(); rows[0]['audio_revision'] = 'take1'; self.pm.save_chunks(rows)

    def test_sentence_gap_file_bridge_rejects_wrong_bytes_before_decode(self):
        from sentence_gap_report import get_sentence_gap_report_for_take
        with patch('soundfile.SoundFile') as decode:
            with self.assertRaisesRegex(ValueError, 'audio identity'):
                get_sentence_gap_report_for_take(audio_path=self.audio, expected_file_sha256='0'*64,
                    text='Amber arrives.', segments=[], provider='fixture')
        decode.assert_not_called()

    def test_sentence_gap_api_uses_real_report_and_preserves_book_and_take(self):
        import copy
        import core
        from contextlib import ExitStack
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        from tests.test_support import use_private_cpu_fixture_lock
        self.assertNotIn('analyze_sentence_gaps', editor.TakeSnapshotRequest.model_fields)
        self.assertNotIn('analyze_sentence_gaps', editor.TakeRestoreRequest.model_fields)
        self.prepare_gap_take()
        original = self.audio.read_bytes()
        script = Path(self.pm.chunks_path).read_bytes()
        @use_private_cpu_fixture_lock()
        def check():
            state = copy.deepcopy(core.process_state)
            for row in state.values(): row['running'] = False
            with ExitStack() as stack:
                for module, name, value in [
                    (core, 'process_state', state), (editor, 'process_state', state),
                    (core, '_task_claims', {}), (core, '_gpu_leases', {}),
                    (editor, 'project_manager', self.pm),
                    (editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json'))]:
                    stack.enter_context(patch.object(module, name, value))
                stack.enter_context(patch('whisper_cpp_runtime.get_whisper_cpp_runtime',
                    return_value={**self.runtime, 'available': True}))
                def render(*args):
                    self.assertTrue(core._task_claims['word_timing']['cpu_only'])
                    return self.render_gap_take(*args)
                engine = stack.enter_context(patch('editor_word_timing.render_word_timing', side_effect=render))
                api = FastAPI(); api.include_router(editor.router)
                client = stack.enter_context(TestClient(api))
                body = {'expected_book_token': editor.get_snapshot_digest(editor.get_editor_book_identity(str(self.root))),
                        'expected_uid': 'row', 'expected_audio_revision': 'take1', 'analyze_sentence_gaps': True}
                for cached in (False, True):
                    response = client.post('/api/chunks/0/word_timing', json=body)
                    self.assertEqual(200, response.status_code, response.text)
                    result = response.json()
                    self.assertEqual(cached, result['cached'])
                    self.assertEqual('proposal', result['sentence_gap_report']['proposals'][0]['status'])
                    self.assertEqual(result['binding']['audio_sha256'], result['sentence_gap_report']['provenance']['file_sha256'])
                    self.assertFalse(state['word_timing']['running'])
                    self.assertNotIn('word_timing', core._task_claims)
                self.assertEqual(1, engine.call_count)
                invalid = client.post('/api/chunks/0/word_timing', json={**body, 'analyze_sentence_gaps': 'true'})
                self.assertEqual(422, invalid.status_code)
                stale = client.post('/api/chunks/0/word_timing', json={**body, 'expected_uid': 'other'})
                self.assertEqual(409, stale.status_code)
                self.assertEqual(1, engine.call_count)
        check()
        self.assertEqual(original, self.audio.read_bytes())
        self.assertEqual(script, Path(self.pm.chunks_path).read_bytes())

    def test_sentence_gap_cache_keeps_unmatched_observed_speech_instead_of_inventing_words(self):
        self.prepare_gap_take()
        self.request.analyze_sentence_gaps = True
        with patch('editor_word_timing.render_word_timing', side_effect=self.render_gap_take) as render:
            self.run_case()
            cache = get_timing_cache_path(self.pm, 'a'*64, 'row')
            saved = json.loads(cache.read_text(encoding='utf-8'))
            saved['segments'].insert(2, {'word': 'unexpected', 'start': .8, 'end': .9})
            cache.write_text(json.dumps(saved), encoding='utf-8')
            report = self.run_case()['sentence_gap_report']
            self.assertEqual('unmatched_observed_speech_in_search_window', report['proposals'][0]['skip_reason'])
            self.assertEqual([], report['coordinate_map']['insertions'])
            self.assertEqual(1, render.call_count)
