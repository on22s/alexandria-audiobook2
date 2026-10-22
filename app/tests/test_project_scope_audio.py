"""Scope preview binds real PCM and refuses missing, replaced or corrupt files."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave
import project_scope_audio as scope


class ScopeAudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root/'retained.wav'
        with wave.open(str(self.path), 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
            out.writeframes(b'\x01\x00'*800)
        self.rows = [{'uid': 'removed'}, {'uid': 'retained', 'status': 'done',
                      'audio_path': 'retained.wav', 'audio_revision': 'legacy-revision'}]

    def test_real_audio_is_bound_without_rewriting_rows_files_or_receipts(self):
        before = copy.deepcopy(self.rows), self.path.read_bytes()
        facts = scope.get_scope_audio_plan(self.root, self.rows, 1)
        self.assertEqual((800, 8000, 1), tuple(facts[0]['audio'][k]
                                           for k in ('frames','sample_rate','channels')))
        self.assertEqual('retained', facts[0]['uid'])
        self.assertFalse(facts[0]['origin_publication_present'])
        self.assertEqual(before, (self.rows, self.path.read_bytes()))
        self.assertEqual(['retained.wav'], [p.name for p in self.root.iterdir()])

    def test_missing_corrupt_or_escaping_audio_cannot_be_retained(self):
        for path in ('missing.wav', '../retained.wav', str(self.path), ''):
            with self.subTest(path=path), self.assertRaises(ValueError):
                scope.get_scope_audio_plan(self.root, [self.rows[0], dict(self.rows[1], audio_path=path)], 1)
        self.path.write_bytes(b'corrupt container')
        with self.assertRaises((ValueError, RuntimeError)):
            scope.get_scope_audio_plan(self.root, self.rows, 1)

    def test_pending_without_audio_is_explicit_and_completed_missing_is_refused(self):
        rows = [self.rows[0], {'uid': 'pending', 'status': 'pending', 'audio_path': None}]
        self.assertIsNone(scope.get_scope_audio_plan(self.root, rows, 1)[0]['audio'])
        for status in ('done', 'generating'):
            rows[1]['status'] = status
            with self.assertRaises(ValueError):scope.get_scope_audio_plan(self.root, rows, 1)

    def test_change_between_snapshot_and_decode_is_refused(self):
        real_read = scope._read_audio
        def replace_before_read(*args, **kwargs):
            raw = self.path.read_bytes(); self.path.write_bytes(raw[:-2] + b'\x02\x00')
            return real_read(*args, **kwargs)
        with patch.object(scope, '_read_audio', replace_before_read), self.assertRaises(ValueError):
            scope.get_scope_audio_plan(self.root, self.rows, 1)

    def test_saved_publication_must_match_actual_bytes_and_remains_immutable(self):
        audio, _ = scope._read_audio(self.root, self.path, None)
        row = self.rows[1]; row['audio_revision'] = 'b'*32
        row['take_publication'] = dict(version=1, kind='native_custom_windows_v1',
            book_token='a'*64, uid=row['uid'], audio_revision=row['audio_revision'], index=1,
            dependencies={key:'c'*64 for key in ('inputs_sha256','voice_sha256','model_sha256',
                'pronunciation_sha256','recipe_sha256','serving_sha256')},
            speech_sha256='d'*64, pcm=copy.deepcopy(audio), audio=copy.deepcopy(audio),
            synthesis_sha256='e'*64)
        before = copy.deepcopy(row)
        self.assertTrue(scope.get_scope_audio_plan(self.root, self.rows, 1)[0]['origin_publication_present'])
        self.assertEqual(before, row)
        raw = self.path.read_bytes(); self.path.write_bytes(raw[:-2] + b'\x02\x00')
        with self.assertRaisesRegex(ValueError, 'publication evidence'):
            scope.get_scope_audio_plan(self.root, self.rows, 1)
        self.assertEqual(before, row)
