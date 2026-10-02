"""Resolve source choices before any preparer dispatch."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import alexandria_batch_processor as batch

class SourceMappingAdmissionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.model = self.root / 'model.gguf'
        self.model.touch()
        self.sources = self.root / 'sources'
        self.sources.mkdir()

    def audio(self, name):
        p = self.root / (name + '.wav')
        p.touch()
        return str(p)

    def processor(self, **kw):
        return batch.BatchProcessor(str(self.model), source_folder=str(self.sources), **kw)

    def test_last_file_tie_refuses_before_any_processing(self):
        for name in ('first.txt', 'alpha zeta audiobook.txt', 'alpha zeta converted.txt'):
            (self.sources / name).touch()
        p = self.processor()
        with patch.object(batch.sys.stdin, 'isatty', return_value=False), patch.object(p, 'process_file') as work:
            self.assertFalse(p.run([self.audio('first'), self.audio('alpha zeta')]))
        work.assert_not_called()

    def test_unmatched_requires_explicit_option(self):
        audio = self.audio('unrelated')
        with patch.object(batch.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'allow-no-source'):
                self.processor().validate_files([audio])
            p = self.processor(allow_no_source=True)
            self.assertEqual([audio], p.validate_files([audio]))
            self.assertIsNone(p.source_matches[audio])

    def test_interactive_table_confirmation_once(self):
        for name in ('alpha zeta audiobook.txt', 'alpha zeta converted.txt'):
            (self.sources / name).touch()
        audios = [self.audio('alpha zeta'), self.audio('unrelated')]
        with patch.object(batch.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='yes') as confirm, self.assertLogs(batch.logger, level='INFO') as logs:
            self.assertEqual(audios, self.processor().validate_files(audios))
        self.assertEqual(1, confirm.call_count)
        for value in ('alpha zeta audiobook.txt', 'alpha zeta converted.txt', 'ASR-only', '1.000'):
            self.assertIn(value, '\n'.join(logs.output))
        for response in ('no', ''):
            with patch.object(batch.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=response), self.assertRaisesRegex(ValueError, 'not confirmed'):
                self.processor().validate_files(audios)
        with patch.object(batch.sys.stdin, 'isatty', return_value=True), patch('builtins.input', side_effect=EOFError), self.assertRaises(ValueError):
            self.processor().validate_files(audios)

    def test_match_report_exact_and_near_equal(self):
        candidates = [('one.txt', ['alpha']), ('two.txt', ['alpha', 'extra'])]
        report = batch.get_source_match_report('alpha.wav', str(self.sources), source_candidates=candidates)
        self.assertFalse(report['ambiguous'])
        candidates = [('one.txt', ['alpha']), ('two.txt', ['alpha'])]
        self.assertTrue(batch.get_source_match_report('alpha.wav', str(self.sources), source_candidates=candidates)['ambiguous'])
        for name in ('alpha.epub', 'alpha.txt'):
            (self.sources / name).touch()
        report = batch.get_source_match_report('alpha.wav', str(self.sources))
        self.assertEqual('exact', report['method'])
        self.assertEqual(str(self.sources / 'alpha.epub'), report['source'])

    def test_near_equal_fuzzy_scores_are_refused(self):
        words = ['term' + str(i) for i in range(20)]
        candidates = [('best.txt', words), ('near.txt', words + ['extra'])]
        report = batch.get_source_match_report(' '.join(words) + '.wav', str(self.sources), source_candidates=candidates)
        self.assertTrue(report['ambiguous'])
        self.assertLess(report['score'] - report['alternatives'][0]['score'], 0.05)
        self.assertGreater(report['score'] - report['alternatives'][0]['score'], 0)
        with patch.object(batch.sys.stdin, 'isatty', return_value=False), patch.object(batch, 'get_source_candidates', return_value=candidates), self.assertRaisesRegex(ValueError, 'Ambiguous'):
            self.processor().validate_files([self.audio(' '.join(words))])

    def test_direct_processing_cannot_bypass_mapping_admission(self):
        audio = self.audio('unrelated')
        with patch.object(batch.sys.stdin, 'isatty', return_value=False), patch.object(batch, 'get_audio_duration_seconds', return_value=3600), patch.object(batch.subprocess, 'Popen') as worker, self.assertRaisesRegex(ValueError, 'allow-no-source'):
            self.processor().process_file(audio, 1, 1)
        worker.assert_not_called()

    def test_cli_no_source_flag_is_forwarded_and_lease_is_released(self):
        from experiments.gpu_guard import acquire_gpu_lock, gpu_is_busy
        audio = self.audio('unrelated')
        lock = str(self.root / 'gpu.lock')

        def run(processor, files):
            self.assertTrue(processor.allow_no_source)
            self.assertTrue(gpu_is_busy(lock))
            return processor.validate_files(files) == [audio]
        argv = ['batch', audio, '--model', str(self.model), '--source-folder', str(self.sources), '--allow-no-source']
        with patch.object(batch.sys, 'argv', argv), patch.object(batch.sys.stdin, 'isatty', return_value=False), patch.object(batch, 'acquire_gpu_lock', side_effect=lambda: acquire_gpu_lock(lock)), patch.object(batch.BatchProcessor, 'run', run), self.assertRaises(SystemExit) as result:
            batch.main()
        self.assertEqual(0, result.exception.code)
        self.assertFalse(gpu_is_busy(lock))
