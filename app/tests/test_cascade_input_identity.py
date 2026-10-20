"""Run both actual cascade phases against changed source/checkpoint/config inputs."""
import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openai
import three_pass_generate


SCRIPT = Path(__file__).parents[1] / 'experiments/cascade.py'


class CascadeInputIdentityTests(unittest.TestCase):
    def run_phase(self, root, phase, calls, extra=None, altered_prompt=False):
        tree = ast.parse(SCRIPT.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'REPO' for t in node.targets):
                node.value = ast.copy_location(ast.Constant(str(root)), node.value)
        ast.fix_missing_locations(tree)
        def attribute(client, model, frozen, *args, **kwargs):
            calls.append((phase, model, copy.deepcopy(frozen)))
            speakers = ['ALICE' if row['text'] == 'First line.' else 'BOB' for row in frozen]
            if phase == 'cheap' and len([c for c in calls if c[0] == 'cheap']) == 2:
                speakers[0] = 'BOB'
            return [{'speaker': name} for name in speakers]
        env = {'EXPERIMENT_PHASE': phase, 'EXPERIMENT_BOOK': 'fixture', 'EXPERIMENT_TAG': 'identity',
               'EXPERIMENT_MODEL': 'cheap-fixture', 'EXPERIMENT_BIG_MODEL': 'big-fixture',
               'EXPERIMENT_BASE_URL': 'http://fixture/v1', 'EXPERIMENT_TRIGGER': 'width',
               'EXPERIMENT_GOLD': 'fixtures/attribution_gold_fixture.json',
               'EXPERIMENT_ENV': json.dumps({'loaded': True, 'context_length': 32768, 'parallel': 1})}
        env.update(extra or {})
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict('os.environ', env))
            stack.enter_context(patch.object(openai, 'OpenAI', return_value=object()))
            stack.enter_context(patch.object(three_pass_generate, 'attribute_batch', side_effect=attribute))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if altered_prompt:
                stack.enter_context(patch.object(three_pass_generate, 'load_attribute_prompts',
                                                 return_value=('Changed system.', '{roster}\n{batch}')))
            try:
                exec(compile(tree, str(SCRIPT), 'exec'), {'__file__': str(SCRIPT), '__name__': '__main__'})
            except SystemExit as error:
                if phase != 'cheap' or error.code != 0:
                    raise

    def prepare(self, root):
        source = root / 'ab_test_runtime/results/matrix_20260725-115148/inputs/fixture.txt'
        checkpoint = root / ('ab_test_runtime/results/matrix_20260725-115148/'
                            'qwen3.5-9b-uncensored-hauhaucs-aggressive/fixture/result.json.threepass_checkpoint.json')
        gold = root / 'app/fixtures/attribution_gold_fixture.json'
        for path in (source, checkpoint, gold):
            path.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('First line. Second line.')
        rows = [{'type': 'SPOKEN', 'text': 'First line.'}, {'type': 'SPOKEN', 'text': 'Second line.'}]
        checkpoint.write_text(json.dumps({'segmented': rows,
            'named': [{'speaker': 'ALICE', 'text': 'First line.'}, {'speaker': 'BOB', 'text': 'Second line.'}]}))
        gold.write_text(json.dumps({'entries': [
            {'id': 'a', 'line': 'First line.', 'expected_speaker': 'ALICE'},
            {'id': 'b', 'line': 'Second line.', 'expected_speaker': 'BOB'}]}))
        return source, checkpoint, gold

    def test_expensive_phase_refuses_changed_inputs_before_inference_or_publication(self):
        for change in ('segment_order', 'segment_text', 'source', 'gold', 'named', 'prompt', 'trigger', 'model', 'legacy'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, checkpoint, gold = self.prepare(root)
                calls = []
                self.run_phase(root, 'cheap', calls)
                state = root / 'ab_test_runtime/experiments/cascade_state__fixture__identity.json'
                extra = None
                if change.startswith('segment') or change == 'named':
                    data = json.loads(checkpoint.read_text())
                    if change == 'segment_order':
                        data['segmented'].reverse()
                    elif change == 'segment_text':
                        data['segmented'][0]['text'] = 'Changed line.'
                    else:
                        data['named'][0]['speaker'] = 'OTHER'
                    checkpoint.write_text(json.dumps(data))
                elif change == 'source':
                    source.write_text('Changed source. First line. Second line.')
                elif change == 'gold':
                    data = json.loads(gold.read_text()); data['entries'][0]['expected_speaker'] = 'OTHER'
                    gold.write_text(json.dumps(data))
                elif change == 'trigger':
                    extra = {'EXPERIMENT_TRIGGER': 'batch'}
                elif change == 'model':
                    extra = {'EXPERIMENT_MODEL': 'different-cheap'}
                elif change == 'legacy':
                    data = json.loads(state.read_text()); data.pop('input_identity', None)
                    state.write_text(json.dumps(data))
                previous = state.read_bytes()
                with self.assertRaisesRegex(SystemExit, 'identity|rerun.*cheap'):
                    self.run_phase(root, 'expensive', calls, extra, change == 'prompt')
                self.assertEqual(2, len(calls), 'A mismatched state must not spend an expensive model call')
                self.assertEqual(previous, state.read_bytes())
                self.assertEqual([], list(state.parent.glob('cascade__*.json')))

    def test_matching_width_and_batch_phases_publish_the_bound_gold_rows(self):
        for trigger in ('width', 'batch'):
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); self.prepare(root); calls = []
                self.run_phase(root, 'cheap', calls, {'EXPERIMENT_TRIGGER': trigger})
                self.run_phase(root, 'expensive', calls, {'EXPERIMENT_TRIGGER': trigger})
                output = root / 'ab_test_runtime/experiments/cascade__fixture__big-fixture__identity.json'
                artifact = json.loads(output.read_text())
                self.assertEqual(3, len(calls))
                self.assertEqual(4, len(artifact['rows']))
                self.assertTrue(all(row['correct'] for row in artifact['rows']))
