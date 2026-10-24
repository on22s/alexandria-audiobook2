"""Fixed-line render pilots (#1545, #1546): real capture plumbing, no model inference."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments import line_render_pilot as pilot
from experiments.run_dialogue_continuity import run_owned_audio_experiment
from tests.test_dialogue_continuity_controller import isolated_core_ownership
from tests.test_question_punctuation_ab import FakeEngine


class LineRenderPilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.out = self.root / 'run'
        self.out.mkdir(); (self.out / 'wav').mkdir()
        self.speaker = 'a/../../../victim'
        self.entries = {self.speaker: {'type': 'lora', 'adapter_path': str(self.root / 'adapter'),
                                     'adapter_generation_sha256': 'a' * 64}}

    def run_lines(self, engine, core, seeds, lines):
        with patch.object(core.project_manager, 'get_engine', return_value=engine), patch.object(
                pilot, 'freeze_question_voices', side_effect=copy.deepcopy):
            return run_owned_audio_experiment(core, lambda admitted: pilot.render_pilot_lines(
                admitted, self.entries, seeds, lines, self.out, {'provenance': {'fixture': True}}, 'alice'),
                self.out / 'run')

    def test_each_seed_renders_the_exact_line_once_inside_the_run(self):
        events = []
        with isolated_core_ownership(events) as (core, _state):
            engine = FakeEngine(events)
            result = self.run_lines(engine, core, [11, 12, 13], pilot.ALICE[:2])
        self.assertEqual(result['status'], 'captured')
        self.assertEqual(len(result['rows']), 6)
        self.assertEqual([voice['seed'] for _text, voice, _path in engine.calls], ['11', '12', '13'] * 2)
        self.assertEqual([row['submitted_text'] for row in result['rows']],
                         [pilot.ALICE[0][1]] * 3 + [pilot.ALICE[1][1]] * 3)
        self.assertEqual(len({row['clip_id'] for row in result['rows']}), 6)
        for row in result['rows']:
            self.assertEqual(Path(row['wav']).parent, self.out / 'wav')
            self.assertNotIn('victim', Path(row['wav']).name)
            self.assertEqual(row['wav_sha256'], pilot.file_sha256(row['wav']))
        manifest = (self.out / 'manifest.jsonl').read_text().splitlines()
        self.assertEqual(len(manifest), 6)

    def test_prepare_text_submits_what_the_app_would_and_keeps_the_written_line(self):
        # Without --prepare-text the line is sent as written, which is not what
        # production sends: "15%" reaches the model as "15 percent" there.
        events = []
        with isolated_core_ownership(events) as (core, _state):
            engine = FakeEngine(events)
            with patch.object(core.project_manager, 'get_engine', return_value=engine), patch.object(
                    pilot, 'freeze_question_voices', side_effect=copy.deepcopy):
                result = run_owned_audio_experiment(core, lambda admitted: pilot.render_pilot_lines(
                    admitted, self.entries, [1], [('n15', 'Prices rose by 15% in the 1920s.')], self.out,
                    {'provenance': {'fixture': True}}, 'numbers', prepare_text=True), self.out / 'run')
        row, = result['rows']
        self.assertEqual('Prices rose by 15% in the 1920s.', row['requested_text'])
        self.assertEqual('Prices rose by 15 percent in the 1920s.', row['submitted_text'])
        self.assertEqual(['Prices rose by 15 percent in the 1920s.'], [text for text, _voice, _path in engine.calls])
        self.assertTrue(row['prepared_text'])

    def test_changed_submitted_text_never_publishes_a_manifest(self):
        events = []
        with isolated_core_ownership(events) as (core, _state):
            with self.assertRaisesRegex(ValueError, 'submitted-input'):
                self.run_lines(FakeEngine(events, changed_text=True), core, [1], pilot.NUMBERS[:1])
        self.assertFalse((self.out / 'manifest.jsonl').exists())

    def test_line_sets_are_fixed_and_unknown_sets_refuse(self):
        self.assertEqual((len(pilot.NUMBERS), len(pilot.ALICE)), (30, 20))
        for lines in pilot.LINE_SETS.values():
            self.assertEqual(len({line_id for line_id, _ in lines}), len(lines))
        config = self.root / 'config.json'; config.write_text(json.dumps({'tts': {'mode': 'local'}}))
        voices = self.root / 'voices.json'; voices.write_text(json.dumps(
            {'NARRATOR': {'type': 'lora', 'adapter_path': str(self.root / 'adapter')}}))
        args = SimpleNamespace(config=str(config), voice_config=str(voices), speakers='NARRATOR',
                               seeds='1,2', limit=0, set='numbers')
        self.assertEqual(pilot.get_question_inputs(args)[4], pilot.NUMBERS)
        with self.assertRaisesRegex(ValueError, 'known line set'):
            pilot.get_question_inputs(SimpleNamespace(**{**vars(args), 'set': 'other'}))

    def test_each_window_passage_splits_once_at_its_labelled_seam(self):
        """The real planner, not a fake: every passage gives one seam of the kind its id names."""
        from synthesis_windows import plan_synthesis_windows
        self.assertEqual(len(pilot.WINDOWS), 12)
        for line_id, text in pilot.WINDOWS:
            windows = plan_synthesis_windows(text, max_chars=150).windows
            self.assertEqual([w.boundary for w in windows], [pilot.WINDOW_SEAMS[line_id[0]], 'end'], line_id)
            self.assertEqual(''.join(w.text for w in windows), text)

    def test_windows_budget_enables_windows_only_in_the_run_config_copy(self):
        config = self.root / 'config.json'; config.write_text(json.dumps({'tts': {'mode': 'local'}}))
        source = config.read_bytes()
        voices = self.root / 'voices.json'; voices.write_text(json.dumps(
            {'NARRATOR': {'type': 'lora', 'adapter_path': str(self.root / 'adapter')}}))
        args = SimpleNamespace(config=str(config), voice_config=str(voices), speakers='NARRATOR',
                               seeds='1', limit=0, set='windows', windows_budget=150)
        run_config = json.loads(pilot.get_question_inputs(args)[0])
        self.assertEqual(run_config['tts']['synthesis_windows'], pilot.get_window_settings(150))
        self.assertEqual(run_config['tts']['synthesis_windows']['budgets'], {'local.lora': {'max_chars': 150, 'max_words': None}})
        self.assertEqual(config.read_bytes(), source)
        # A config that enables windows itself is still refused, flag or not.
        config.write_text(json.dumps({'tts': {'mode': 'local', 'synthesis_windows': pilot.get_window_settings(150)}}))
        for budget in (None, 150):
            with self.assertRaisesRegex(ValueError, 'Disable synthesis windows'):
                pilot.get_question_inputs(SimpleNamespace(**{**vars(args), 'windows_budget': budget}))

    def run_windowed(self, engine, core, budget):
        with patch.object(core.project_manager, 'get_engine', return_value=engine), patch.object(
                pilot, 'freeze_question_voices', side_effect=copy.deepcopy):
            return run_owned_audio_experiment(core, lambda admitted: pilot.render_pilot_lines(
                admitted, self.entries, [7], pilot.WINDOWS[:1], self.out, {'provenance': {'fixture': True}},
                'windows', budget), self.out / 'run')

    def test_window_mode_records_the_plan_and_requires_composed_proof(self):
        events = []
        with isolated_core_ownership(events) as (core, _state):
            engine = WindowFakeEngine(events, 'composed_windows_api_inputs')
            result = self.run_windowed(engine, core, 150)
        row = result['rows'][0]
        self.assertEqual([w['boundary'] for w in row['window_plan']], ['sentence', 'end'])
        self.assertEqual(row['submitted_text'], pilot.WINDOWS[0][1])
        self.assertGreaterEqual(row['render_seconds'], 0)

    def test_window_mode_rejects_single_request_proof_and_mismatched_settings(self):
        events = []
        with isolated_core_ownership(events) as (core, _state):
            with self.assertRaisesRegex(ValueError, 'submitted-input'):
                self.run_windowed(WindowFakeEngine(events, 'local_lora_api'), core, 150)
        self.assertFalse((self.out / 'manifest.jsonl').exists())
        for engine_budget, requested in ((150, None), (None, 150), (150, 200)):
            with isolated_core_ownership([]) as (core, _state):
                engine = WindowFakeEngine([], 'composed_windows_api_inputs', budget=engine_budget)
                with self.assertRaisesRegex(ValueError, 'exactly the requested synthesis windows'):
                    self.run_windowed(engine, core, requested)


class WindowFakeEngine(FakeEngine):
    """FakeEngine whose settings carry windows and whose proof has a chosen provenance."""
    def __init__(self, events, provenance_kind, budget=150):
        super().__init__(events)
        self.provenance_kind = provenance_kind
        if budget is not None:
            self.settings['synthesis_windows'] = pilot.get_window_settings(budget)

    def _get_synthesis_window_plan(self, text, category):
        from synthesis_windows import plan_synthesis_windows
        return plan_synthesis_windows(text, max_chars=150)

    def _generate_lora_voice_prepared(self, text, instruct, voice, path):
        from synthesis_input import capture_speech_input
        result = super()._generate_lora_voice_prepared(text, instruct, voice, path)
        capture_speech_input(path, text, 'English', self.provenance_kind)
        return result


if __name__ == '__main__':
    unittest.main()
