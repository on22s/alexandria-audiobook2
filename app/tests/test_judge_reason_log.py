"""Native JSONL evidence, run isolation and descriptor lifecycle regressions."""
import builtins
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import attribution_prompt_variants as apv
import judge_reason_log as storage

if os.environ.get('JUDGE_SOURCE'):
    spec = importlib.util.spec_from_file_location('judge_before', os.environ['JUDGE_SOURCE'])
    apv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(apv)

FROZEN = [{'type': 'SPOKEN', 'text': 'Hello.'}]
NAMED = [{'n': 0, 'speaker': 'ANN', 'why': 'ANN speaks here.'}]


def _write_batches(path, index):
    with patch.dict(os.environ, {'JUDGE_WHY_PATH': path}), storage.record_judge_run():
        for batch in range(8):
            storage.record_judge_rows([{'text': f'{index}:{batch}:{row}', 'speaker': 'ANN',
                                       'why': '証拠' * 10000} for row in range(3)])


class JudgeReasonLogTests(unittest.TestCase):
    def test_actual_accepted_batches_share_one_open_and_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'why.jsonl'
            opens = []
            handles = []
            native_open = builtins.open

            def observe(name, *args, **kwargs):
                handle = native_open(name, *args, **kwargs)
                if os.fspath(name) == str(path):
                    opens.append(name)
                    handles.append(handle)
                return handle

            with patch.dict(os.environ, {'JUDGE_WHY_PATH': str(path)}), \
                 patch('builtins.open', side_effect=observe):
                with storage.record_judge_run() as run:
                    for count in range(1, 4):
                        apv.record_judge_reasons(FROZEN, NAMED)
                        rows = [json.loads(line) for line in path.read_text().splitlines()]
                        self.assertEqual(count, len(rows))
                        self.assertEqual({run.run_id}, {row.get('run_id') for row in rows})
                self.assertTrue(all(handle.closed for handle in handles))
            self.assertEqual(1, len(opens))
            self.assertEqual(['ANN'] * 3, [row['speaker'] for row in rows])

    def test_full_pipeline_reuses_handle_across_judge_windows(self):
        import three_pass_generate as tp
        from generate_script import LLMGenParams
        from tests import test_three_pass_generate as fixtures
        source = 'Alice waited. "First." Alice answered. "Second."'
        seg = [{"type": "NARRATOR", "text": "Alice waited."},
               {"type": "SPOKEN", "text": "First."},
               {"type": "NARRATOR", "text": "Alice answered."},
               {"type": "SPOKEN", "text": "Second."}]
        named = [{"n": 0, "speaker": "NARRATOR"},
                 {"n": 1, "speaker": "ALICE", "why": "Alice speaks next."}]
        instructed = [{"n": i, "head": row["text"], "instruct": "Plain."}
                      for i, row in enumerate(seg)]
        client = fixtures._client_returning([seg, named, named, instructed])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "why"
            handles = []
            native_open = builtins.open

            def observe(name, *args, **kwargs):
                handle = native_open(name, *args, **kwargs)
                if os.fspath(name) == str(path):
                    handles.append(handle)
                return handle

            cast = fixtures._load_orchestration_fixture_cast(("ALICE",))
            with patch.dict(os.environ, {"JUDGE_WHY_PATH": str(path)}), \
                 patch("builtins.open", side_effect=observe):
                result = tp.run_three_pass(client, "m", source,
                    LLMGenParams(max_tokens=500, temperature=0.1, structured_output="off"),
                    chunk_size=6000, attribute_batch_size=2,
                    attribute_prompt_variant="judge", cast=cast)
            self.assertEqual(1, len(handles))
            self.assertTrue(handles[0].closed)
            self.assertEqual([row["text"] for row in seg], [row["text"] for row in result])
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(["First.", "Second."], [row["text"] for row in rows])
            self.assertEqual(1, len({row["run_id"] for row in rows}))

    def test_exception_closes_descriptor_and_next_run_has_fresh_identity(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'JUDGE_WHY_PATH': str(Path(tmp) / 'why')}):
            with self.assertRaisesRegex(ValueError, 'later failure'):
                with storage.record_judge_run() as first:
                    apv.record_judge_reasons(FROZEN, NAMED)
                    handle = first._handle
                    raise ValueError('later failure')
            self.assertTrue(handle.closed)
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                first.record_rows([{'why': 'cannot reuse'}])
            with storage.record_judge_run() as second:
                apv.record_judge_reasons(FROZEN, NAMED)
                self.assertNotEqual(first.run_id, second.run_id)
            rows = [json.loads(line) for line in Path(second.path).read_text().splitlines()]
            self.assertEqual([first.run_id, second.run_id], [row['run_id'] for row in rows])

    def test_coordinated_path_replacement_reopens_current_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'why'
            with patch.dict(os.environ, {'JUDGE_WHY_PATH': str(path)}), storage.record_judge_run() as run:
                apv.record_judge_reasons(FROZEN, NAMED)
                old = run._handle
                with storage.file_lock(path):
                    path.rename(Path(tmp) / 'prior')
                    path.write_text('')
                apv.record_judge_reasons(FROZEN, NAMED)
                self.assertTrue(old.closed)
                self.assertEqual(1, len(path.read_text().splitlines()))
                self.assertEqual(1, len((Path(tmp) / 'prior').read_text().splitlines()))

    def test_native_concurrent_processes_preserve_complete_batch_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'why')
            ctx = multiprocessing.get_context('spawn')
            workers = [ctx.Process(target=_write_batches, args=(path, i)) for i in range(3)]
            try:
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(20)
                    self.assertEqual(0, worker.exitcode)
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                    worker.join()
            rows = [json.loads(line) for line in Path(path).read_text().splitlines()]
            self.assertEqual(72, len(rows))
            self.assertEqual(72, len({row['text'] for row in rows}))
            identities = {}
            for offset in range(0, len(rows), 3):
                batch = rows[offset:offset + 3]
                self.assertEqual(1, len({row['text'].rsplit(':', 1)[0] for row in batch}))
                self.assertEqual(1, len({row['run_id'] for row in batch}))
                for row in batch:
                    self.assertEqual('証拠' * 10000, row['why'])
                    identities.setdefault(row['text'].split(':')[0], set()).add(row['run_id'])
            self.assertEqual([1, 1, 1], sorted(map(len, identities.values())))
            self.assertEqual(3, len({row['run_id'] for row in rows}))

    def test_disabled_path_has_no_handle(self):
        with patch.dict(os.environ, {'JUDGE_WHY_PATH': ''}), storage.record_judge_run() as run:
            apv.record_judge_reasons(FROZEN, NAMED)
            self.assertIsNone(run._handle)
