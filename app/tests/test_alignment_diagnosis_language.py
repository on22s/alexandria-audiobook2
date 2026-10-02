"""Real PCM and stored reader errors distinguish Chinese and Japanese inputs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from experiments import alignment_diagnosis as diagnosis

REPO = Path(__file__).resolve().parents[2]


class AlignmentDiagnosisLanguageTests(unittest.TestCase):
    def run_diagnosis(self, language='zh', build_language='zh', readers='zh'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wav = root / 'voice.wav'
            with wave.open(str(wav), 'wb') as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(24000)
                handle.writeframes(b'\x00\x10' * 24000)
            build = root / 'build.json'
            build.write_text(json.dumps({'language': build_language, 'test': [
                {'id': 'chinese-1', 'book': 'S0002', 'human_wav': str(wav)}]}))
            before = build.read_bytes()
            (root / f'asr_{readers}_reader__S0002.json').write_text(json.dumps(
                {'alignment': {'whisper_cpp': {'median_error_s': .123}}}))
            (root / 'asr_ja_reader__botchan.json').write_text(json.dumps(
                {'alignment': {'whisper_cpp': {'median_error_s': .987}}}))
            output = root / 'result.json'
            argv = ['diagnosis', '--builds', str(build), '--per-reader', str(root),
                    '--language', language, '--out', str(output)]
            with patch.object(sys, 'argv', argv):
                try:
                    diagnosis.main()
                    error = None
                except SystemExit as failure:
                    error = str(failure)
            self.assertEqual(before, build.read_bytes())
            return error, json.loads(output.read_text()) if output.exists() else None

    def test_chinese_pcm_uses_chinese_reader_errors(self):
        error, result = self.run_diagnosis()
        self.assertIsNone(error)
        self.assertEqual('zh', result['language'])
        self.assertEqual(['S0002'], list(result['per_reader']))
        self.assertEqual('chinese-1', result['rows'][0]['id'])
        self.assertEqual(.123, result['rows'][0]['reader_align_s'])
        self.assertEqual(1.0, result['rows'][0]['seconds'])
        self.assertIn('1 readers', result['note'])

    def test_japanese_requested_as_chinese_refuses_before_measurement(self):
        with patch.object(diagnosis, 'clip_properties', side_effect=AssertionError('wrong corpus measured')):
            error, result = self.run_diagnosis(build_language='ja')
        self.assertIn('does not match requested zh', error)
        self.assertIsNone(result)

    def test_japanese_reader_files_cannot_stand_in_for_chinese(self):
        error, result = self.run_diagnosis(readers='ja')
        self.assertIn('no zh reader alignment results match', error)
        self.assertIsNone(result)

    def test_missing_language_cannot_claim_chinese(self):
        error, result = self.run_diagnosis(build_language=None)
        self.assertIn('does not match requested zh', error)
        self.assertIsNone(result)

    def test_existing_japanese_language_and_legacy_build_still_work(self):
        for build_language in ('ja', None):
            with self.subTest(build_language=build_language):
                error, result = self.run_diagnosis('ja', build_language, 'ja')
                self.assertIsNone(error)
                self.assertEqual(.123, result['rows'][0]['reader_align_s'])

    def test_actual_buffer_stage_dispatches_chinese_build_and_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chain = root / 'run_chains/overnight_buffer_2026_08_16.sh'
            chain.parent.mkdir()
            chain.write_bytes((REPO / 'run_chains' / chain.name).read_bytes())
            python = root / 'app/env/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$FIXTURE_CAPTURE"\n')
            python.chmod(0o755)
            # Existing results skip every independent stage except the target.
            results = root / 'ab_test_runtime/experiments'
            results.mkdir(parents=True)
            for name in ('prosody_fidelity_ja_n40', 'prosody_fidelity_zh_n40',
                         'prosody_fidelity_en_n40', 'expected_prosody_ja_n200',
                         'expected_prosody_zh_n200', 'asr_ja_largev3_readings'):
                (results / (name + '.json')).write_text('{}')
            import os
            capture = root / 'args'
            result = subprocess.run(['bash', str(chain)], capture_output=True,
                text=True, timeout=10, env=dict(os.environ, FIXTURE_CAPTURE=str(capture)))
            self.assertEqual(0, result.returncode, result.stderr)
            args = capture.read_text().splitlines()
            self.assertEqual(str(root / 'ab_test_runtime/aishell3_eval/build.json'),
                             args[args.index('--builds') + 1])
            self.assertEqual('zh', args[args.index('--language') + 1])
