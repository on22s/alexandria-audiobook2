"""Exercise the real lightweight tracker without importing ASR/model setup."""
import ast
from pathlib import Path
import unittest
from unittest.mock import Mock


class PreparerProgressStepTests(unittest.TestCase):
    def get_tracker(self):
        path = Path(__file__).resolve().parents[2] / 'alexandria_preparer_rocm_compatible.py'
        node = next(node for node in ast.parse(path.read_text()).body
                    if isinstance(node, ast.ClassDef) and node.name == 'ProgressTracker')
        logger = Mock()
        scope = {'logger': logger}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
        return scope['ProgressTracker'](), logger

    def test_optional_steps_register_in_execution_order_without_overwriting_load(self):
        tracker, logger = self.get_tracker()
        for name in ('Validate inputs', 'Load audio', 'Transcribe audio', 'Annotate chunks', 'Create output dataset'):
            tracker.add_step(name)
        tracker.start('Load audio')
        tracker.start('Detect speakers')
        tracker.complete()
        self.assertEqual(2, tracker.current_step)
        self.assertEqual('Detect speakers', tracker.steps[2])
        self.assertEqual('▶ [3/6] Detect speakers...', logger.info.call_args_list[-2].args[0])
        self.assertEqual('✓ Step 3/6 completed', logger.info.call_args.args[0])
        tracker.start('Diarize speakers')
        self.assertEqual(3, tracker.current_step)
        self.assertEqual('Diarize speakers', tracker.steps[3])
        tracker.start('Transcribe audio')
        self.assertEqual('▶ [5/7] Transcribe audio...', logger.info.call_args.args[0])
        tracker.start('Detect speakers')
        self.assertEqual(7, len(tracker.steps), 'restarting a registered step must not duplicate it')
        self.assertEqual(2, tracker.current_step)

    def test_registered_only_flow_keeps_existing_numbers(self):
        tracker, logger = self.get_tracker()
        for name in ('first', 'second'):
            tracker.add_step(name)
        tracker.start('first'); tracker.complete(); tracker.start('second'); tracker.complete()
        self.assertEqual(['▶ [1/2] first...', '✓ Step 1/2 completed',
                          '▶ [2/2] second...', '✓ Step 2/2 completed'],
                         [call.args[0] for call in logger.info.call_args_list])
