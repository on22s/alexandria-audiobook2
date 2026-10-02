"""Structured annotation metrics come from actual counters and saved metadata."""
import copy
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_preparer_run_state import preparer
from alexandria_run_manifest import get_run_identity, RunStateError


class AnnotationSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'book.wav'
        self.audio.write_bytes(b'CPU summary fixture audio')
        self.source = self.root / 'source.txt'
        self.source.write_text('source content')
        self.output = self.root / 'attempt-summary.json'
        self.args = SimpleNamespace(audio=str(self.audio), source=str(self.source),
            summary_output=str(self.output), asr_output=None, scratch_audio=None)
        self.identity = get_run_identity(self.args)
        self.metadata = [{'duration': 1.2}, {'duration': 2.0}, {'duration': 3.5}]
        self.stats = {'cut_strategy': Counter(sentence_end=2, pause=1),
            'source_action': Counter(replace=2, dropped=1), 'llm_success': 2,
            'llm_fail': 1, 'sanitize_changed': 1, 'chunk_durations': [2.0, 3.5],
            'realign_events': 2, 'reanchor_events': 1, 'reanchor_backward': 0}
        self.state = {'cursor': 3, 'orig_match': ['one', 'two', 'three', 'four']}

    def save(self):
        preparer.save_annotation_summary(str(self.output), self.identity, self.metadata, 1, self.stats, self.state)
        return json.loads(self.output.read_text())

    def test_real_counter_and_checkpoint_totals_are_scoped_and_inputs_unchanged(self):
        before = copy.deepcopy((self.identity, self.metadata, self.stats, self.state))
        report = self.save()
        self.assertEqual(1, report['version'])
        self.assertEqual('annotation_complete', report['phase'])
        self.assertEqual('current_attempt', report['counter_scope'])
        self.assertEqual({'segments_total': 3, 'segments_this_run': 2, 'resumed_segments': 1,
                          'dataset_seconds': 6.7}, report['totals'])
        self.assertEqual({'sentence_end': 2, 'pause': 1}, report['counters']['cut_strategy'])
        self.assertEqual({'replace': 2, 'dropped': 1}, report['counters']['source_action'])
        self.assertEqual({'word': 3, 'total_words': 4}, report['source_cursor'])
        self.assertEqual(before, (self.identity, self.metadata, self.stats, self.state))

    def test_changed_input_or_failed_atomic_publication_keeps_previous_report(self):
        self.save()
        before = self.output.read_bytes()
        self.source.write_text('different source')
        with self.assertRaisesRegex(RunStateError, 'input changed'):
            self.save()
        self.assertEqual(before, self.output.read_bytes())
        self.identity = get_run_identity(self.args)
        with patch.object(preparer, 'write_json_atomic', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                self.save()
        self.assertEqual(before, self.output.read_bytes())

    def test_reports_cannot_replace_inputs_or_phase_workspace(self):
        for target in (self.audio, self.source, Path('dataset_temp/.run_manifest.json')):
            with self.subTest(target=str(target)):
                self.output = target
                with self.assertRaisesRegex(RunStateError, 'cannot overwrite'):
                    self.save()

    def test_optional_summary_does_not_change_old_resume_identity(self):
        self.args.summary_output = None
        identity = get_run_identity(self.args)
        del self.args.summary_output
        self.assertEqual(identity, get_run_identity(self.args))

    def test_actual_chunker_saves_summary_matching_generated_waveforms_and_checkpoint(self):
        import numpy as np
        import soundfile as sf
        words = [{'word': word, 'start': i * 1.2, 'end': (i + 1) * 1.2,
                  'confidence': 1, 'speaker': 'ONLY'} for i, word in enumerate(('First.', 'Second.', 'Third.'))]
        samples = np.sin(np.arange(86400) * 0.05).astype('float32') * 0.1
        def complete(**request):
            text = request['messages'][1]['content'].split('Annotate this segment:\n', 1)[1]
            return {'choices': [{'message': {'content': text}}]}
        llm = SimpleNamespace(create_chat_completion=Mock(side_effect=complete))
        previous = Path.cwd()
        os.chdir(self.root)
        try:
            with patch.object(preparer, 'ensure_run_manifest'), patch.object(preparer, '_load_llm', return_value=llm), \
                 patch.object(preparer, 'log_gpu_stats'), patch.object(preparer, '_calculate_chunk_snr', return_value=80):
                metadata = preparer.annotate_chunks(words, 'mock.gguf', 1, samples,
                    min_chunk_duration=1, batch_size=1, run_identity=self.identity, summary_output=str(self.output))
            report = json.loads(self.output.read_text())
            self.assertEqual(3, report['totals']['segments_total'])
            self.assertEqual(3, report['totals']['segments_this_run'])
            self.assertEqual(0, report['totals']['resumed_segments'])
            self.assertAlmostEqual(3.6, report['totals']['dataset_seconds'])
            self.assertEqual(3, report['counters']['llm_success'])
            self.assertEqual(0, report['counters']['llm_fail'])
            persisted = [json.loads(row) for row in Path('dataset_temp/metadata.jsonl').read_text().splitlines()]
            self.assertEqual(metadata, [{key: value for key, value in row.items() if key != 'wav_path'}
                                        for row in persisted])
            self.assertEqual(['First.', 'Second.', 'Third.'], [row['text'] for row in persisted])
            for item in persisted:
                samples, rate = sf.read(Path('dataset_temp') / item['audio_filepath'])
                self.assertEqual(24000, rate)
                self.assertAlmostEqual(item['duration'], len(samples) / rate, delta=1 / rate)
        finally:
            os.chdir(previous)
