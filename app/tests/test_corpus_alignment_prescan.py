"""Hand-check the actual alignment estimator before using corpus-wide reports."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import sys
import os
from contextlib import ExitStack

from tests.test_preparer_run_state import preparer
from alexandria_run_manifest import get_file_identity, get_run_identity, RunStateError


class CorpusAlignmentPrescanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'book.wav'
        self.audio.write_bytes(b'controlled CPU transcript boundary')
        self.source = self.root / 'book.txt'
        self.text = 'We walked beside the river and watched the silver moon while the autumn leaves fell softly around us.'
        self.source.write_text((self.text + '\n') * 10)
        self.words = [{'word': word, 'start': i * 2.0, 'end': i * 2.0 + 0.7}
                      for i, word in enumerate((self.text + ' ') .split() * 4)]
        self.args = SimpleNamespace(alignment_report=str(self.root / 'alignment.json'),
            audio=str(self.audio), source=str(self.source), chunk_size=10.0,
            source_start=None, source_start_text=None, no_auto_anchor=False,
            source_threshold=0.5, asr_output=None, scratch_audio=None)
        self.identity = get_run_identity(self.args)

    def save(self):
        preparer.save_source_alignment_prescan(self.words, self.args, self.identity)
        return json.loads(Path(self.args.alignment_report).read_text())

    def test_known_match_reports_actual_sample_quality_identity_and_scope(self):
        before = copy.deepcopy(self.words)
        report = self.save()
        self.assertEqual(1, report['version'])
        self.assertEqual(self.identity, report['identity'])
        self.assertEqual('initial_provisional_chunks', report['scope'])
        self.assertAlmostEqual(1.0, report['quality']['average_ratio'])
        self.assertGreater(report['quality']['sampled'], 2)
        self.assertLessEqual(report['quality']['sampled'], 30)
        self.assertEqual(0, report['quality']['below_60_percent'])
        self.assertEqual(0, report['quality']['review_needed'])
        self.assertEqual(before, self.words)

    def test_known_wrong_source_reports_low_quality_instead_of_plausible_match(self):
        self.source.write_text('quartz xenon zephyr orbital isotope nebula plasma muon neutrino cosmic galaxy electron ' * 30)
        self.identity = get_run_identity(self.args)
        report = self.save()
        self.assertLess(report['quality']['average_ratio'], 0.5)
        self.assertGreater(report['quality']['sampled'], 2)
        self.assertEqual(report['quality']['sampled'], report['quality']['below_60_percent'])
        self.assertEqual(report['quality']['sampled'], report['quality']['review_needed'])

    def test_empty_sample_refuses_and_preserves_previous_report(self):
        self.save()
        path = Path(self.args.alignment_report)
        before = path.read_bytes()
        self.words = []
        with self.assertRaisesRegex(RunStateError, 'no evaluable samples'):
            self.save()
        self.assertEqual(before, path.read_bytes())

    def test_source_or_audio_replacement_before_scan_refuses(self):
        self.save()
        before = Path(self.args.alignment_report).read_bytes()
        for path in (self.audio, self.source):
            with self.subTest(path=path.name):
                old = path.read_bytes()
                path.write_bytes(b'replaced same path')
                with self.assertRaisesRegex(RunStateError, 'input changed'):
                    self.save()
                self.assertEqual(before, Path(self.args.alignment_report).read_bytes())
                path.write_bytes(old)
                self.identity = get_run_identity(self.args)

    def test_change_during_real_estimation_refuses_report_publication(self):
        self.save()
        before = Path(self.args.alignment_report).read_bytes()
        original = preparer.get_source_alignment_prescan
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.source.write_text('changed after alignment before publication')
            return result
        with patch.object(preparer, 'get_source_alignment_prescan', side_effect=changed):
            with self.assertRaisesRegex(RunStateError, 'input changed'):
                self.save()
        self.assertEqual(before, Path(self.args.alignment_report).read_bytes())

    def test_report_cannot_overwrite_inputs_or_phase_artifacts(self):
        for path in (self.audio, self.source, Path('dataset_temp/.run_manifest.json')):
            with self.subTest(path=str(path)):
                self.args.alignment_report = str(path)
                with self.assertRaisesRegex(RunStateError, 'cannot overwrite'):
                    self.save()
        original = self.source.read_bytes()
        self.args.alignment_report = str(self.root / 'reserved-asr.json')
        self.args.asr_output = self.args.alignment_report
        with self.assertRaisesRegex(RunStateError, 'phase artifacts'):
            self.save()
        self.assertEqual(original, self.source.read_bytes())

    def test_unused_report_option_preserves_old_run_identity(self):
        before = get_run_identity(self.args)
        self.args.alignment_report = None
        without = get_run_identity(self.args)
        del self.args.alignment_report
        self.assertEqual(without, get_run_identity(self.args))
        self.assertIn('alignment_report', before['options'])

    def test_invalid_cli_report_modes_refuse_before_gpu_or_model_work(self):
        with patch.object(preparer, 'acquire_run_lock') as lock:
            for extra in (['--phase', 'annotate', '--source', str(self.source)], ['--phase', 'asr']):
                with self.subTest(extra=extra), patch.object(sys, 'argv', ['preparer', '--audio', str(self.audio),
                    '--alignment-report', str(self.root / 'report.json'), *extra]):
                    with self.assertRaises(SystemExit) as refusal:
                        preparer.main()
                    self.assertEqual(2, refusal.exception.code)
            lock.assert_not_called()

    def test_actual_asr_phase_publishes_report_without_annotation_or_dataset_export(self):
        import math
        import numpy as np
        import soundfile as sf
        from scipy.signal import resample_poly
        sf.write(self.audio, np.zeros(180 * 16000, dtype=np.float32), 16000)
        def load(path, **kwargs):
            return sf.read(path)
        def resample(wav, orig_sr, target_sr):
            common = math.gcd(orig_sr, target_sr)
            return resample_poly(wav, target_sr // common, orig_sr // common)
        original_lock = preparer.acquire_run_lock
        descriptors = []
        def lock(path):
            descriptor = original_lock(path)
            descriptors.append(descriptor)
            return descriptor
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(sys, 'argv', ['preparer', '--audio', str(self.audio),
                    '--source', str(self.source), '--phase', 'asr',
                    '--alignment-report', self.args.alignment_report]))
                stack.enter_context(patch.object(preparer, 'acquire_run_lock', side_effect=lock))
                stack.enter_context(patch.object(preparer, '_lazy_import_torch', return_value=object()))
                stack.enter_context(patch.object(preparer, 'resolve_cuda_device', return_value='cpu'))
                stack.enter_context(patch.object(preparer, 'log_torch_info'))
                stack.enter_context(patch.object(preparer, '_lazy_import_librosa', return_value=SimpleNamespace(load=load, resample=resample)))
                stack.enter_context(patch.object(preparer, 'choose_and_transcribe', return_value=(self.words, 'en')))
                stack.enter_context(patch.object(preparer, 'clear_vram'))
                annotate = stack.enter_context(patch.object(preparer, 'annotate_chunks'))
                export = stack.enter_context(patch.object(preparer, '_create_zip_dataset'))
                self.assertEqual(0, preparer.main())
                annotate.assert_not_called()
                export.assert_not_called()
            report = json.loads(Path(self.args.alignment_report).read_text())
            self.assertAlmostEqual(1.0, report['quality']['average_ratio'])
            self.assertEqual(get_file_identity(self.audio), report['identity']['audio'])
            asr = json.loads((self.root / 'dataset_temp/asr_segments.json').read_text())
            self.assertEqual(self.words, asr['word_segments'])
            self.assertAlmostEqual(180.0, asr['audio_duration'])
            self.assertFalse(list(self.root.glob('*.zip')))
        finally:
            os.chdir(previous)
            for descriptor in descriptors:
                os.close(descriptor)
