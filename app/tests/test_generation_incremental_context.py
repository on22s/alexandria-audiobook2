"""Real CLI/checkpoint flow, measuring context work and preserving prompt bytes."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import generate_script as gs
from tests import test_generation_model_binding as fixtures

if os.environ.get('GENERATION_SOURCE'):
    spec = importlib.util.spec_from_file_location('generation_before', os.environ['GENERATION_SOURCE'])
    gs = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gs
    spec.loader.exec_module(gs)


class IncrementalGenerationContextTests(unittest.TestCase):
    def test_prompt_identity_order_and_tail_match_full_history_on_fresh_and_resumed_cli(self):
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = [[{'speaker': ('ALICE', 'BOB', 'NARRATOR')[j % 3],
                          'text': f'The patient gardener inspected marker {i} number {j} beside the gate.',
                          'instruct': 'Plain.'} for j in range(1 if i < 3 else 20)]
                        for i in range(12)]
                chunks = ['\n'.join('"' + row['text'] + '"' if row['speaker'] != 'NARRATOR'
                                   else row['text'] for row in batch) for batch in rows]
                source = root / 'book.txt'
                source.write_text('\n\n'.join(chunks), encoding='utf-8')
                (root / 'config.json').write_text(json.dumps(fixtures.config()))
                output = root / 'script.json'
                source_before = source.read_bytes()
                history = []
                sizes = []
                failed = False

                def work(client, model, chunk, number, total, params, **kwargs):
                    nonlocal failed
                    previous = kwargs.get('previous_entries')
                    expected = gs.build_chunk_request_prompt(chunk, number, total,
                        params.user_prompt_template, history or None)
                    actual = gs.build_chunk_request_prompt(chunk, number, total,
                        params.user_prompt_template, previous)
                    self.assertEqual(expected, actual, 'roster/tail prompt changed')
                    expected_names = [row['speaker'] for row in history]
                    actual_names = [row['speaker'] for row in (previous or [])]
                    probe = [{'speaker': 'alice', 'text': 'Hello.', 'instruct': 'Plain.'}]
                    self.assertEqual(gs.stabilize_speaker_identities(probe, expected_names),
                                     gs.stabilize_speaker_identities(probe, actual_names))
                    sizes.append((len(previous or []), len(history)))
                    if resume and number == 6 and not failed:
                        failed = True
                        return [], False
                    batch = copy.deepcopy(rows[number - 1])
                    history.extend(copy.deepcopy(batch))
                    return batch, False

                with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(root)}), \
                     patch.object(sys, 'argv', ['generate_script', str(source), '--output', str(output)]), \
                     patch.object(gs, 'split_into_chunks', return_value=chunks), \
                     patch.object(gs, 'ensure_ideal_settings', return_value=(False,
                         {'context_length': 8192, 'parallel': 1}, 'CPU fixture')), \
                     patch('llm_provider.make_llm_client', return_value=SimpleNamespace()), \
                     patch.object(gs, 'process_chunk_adaptively', side_effect=work), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if resume:
                        with self.assertRaises(SystemExit) as stopped:
                            gs.main()
                        self.assertEqual(1, stopped.exception.code)
                        self.assertFalse(output.exists())
                        self.assertTrue(Path(gs.get_generation_checkpoint_path(str(output))).exists())
                    gs.main()
                self.assertEqual(source_before, source.read_bytes())
                self.assertEqual(sum(map(len, rows)), len(json.loads(output.read_text())))
                self.assertEqual('complete', json.loads(Path(str(output) + '.generation_quality.json').read_text())['status'])
                self.assertTrue(any(old > 100 for _, old in sizes))
                print(f"Measured context entries: resume={resume}, full_history_max={max(old for _, old in sizes)}, supplied_max={max(size for size, _ in sizes)}")
                self.assertLessEqual(max(size for size, _ in sizes), 6,
                                     'context still scans accumulated book history')
