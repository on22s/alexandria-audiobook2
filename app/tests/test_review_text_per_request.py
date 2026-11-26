"""#2080: review text-per-request packing, config bounds and call-count parity.

Fixtures are synthetic sentences; no book text is used.
"""
import copy
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import core
from config_settings import GenerationConfig
from generate_script import LLMGenParams
import review_script
from routers import script
import three_pass_generate


def _entry(index, text_length):
    # Exactly text_length characters, so packing expectations are unambiguous.
    text = str(index).zfill(3) + "x" * (text_length - 3)
    return {"speaker": "ALICE", "text": text, "instruct": "calm"}


class ReviewPackingTests(unittest.TestCase):
    """The single batching authority must pack whole entries by text only."""

    def test_zero_reproduces_entry_count_batching_exactly(self):
        entries = [_entry(i, 10) for i in range(57)]
        windows = review_script.get_review_entry_windows(
            entries, {"review_batch_size": 25})
        self.assertEqual([(0, 25), (25, 50), (50, 57)], windows)
        self.assertEqual([entries[0:25], entries[25:50], entries[50:57]],
                         [entries[start:end] for start, end in windows])
        # Explicit zero behaves identically to the missing key.
        self.assertEqual(windows, review_script.get_review_entry_windows(
            entries, {"review_batch_size": 25, "review_target_chars": 0}))

    def test_stops_before_entry_that_would_exceed_target(self):
        entries = [_entry(0, 10), _entry(1, 12), _entry(2, 8), _entry(3, 15)]
        windows = review_script.get_review_entry_windows(
            entries, {"review_batch_size": 25, "review_target_chars": 25})
        self.assertEqual([(0, 2), (2, 4)], windows)
        for start, end in windows:
            self.assertLessEqual(sum(len(e["text"]) for e in entries[start:end]), 25)
        # Adding the next whole entry to the first window would exceed 25.
        self.assertGreater(sum(len(e["text"]) for e in entries[0:3]), 25)

    def test_oversize_single_entry_goes_alone(self):
        entries = [_entry(0, 5), _entry(1, 100), _entry(2, 5)]
        windows = review_script.get_review_entry_windows(
            entries, {"review_batch_size": 25, "review_target_chars": 20})
        self.assertEqual([(0, 1), (1, 2), (2, 3)], windows)
        start, end = windows[1]
        self.assertEqual([entries[1]], entries[start:end])

    def test_only_entry_text_is_counted(self):
        entries = [
            {"speaker": "N" * 5000, "instruct": "I" * 5000, "text": "hi"},
            {"speaker": "N" * 5000, "instruct": "I" * 5000, "text": "bye"},
        ]
        windows = review_script.get_review_entry_windows(
            entries, {"review_batch_size": 25, "review_target_chars": 10,
                      "review_system_prompt": "P" * 5000})
        self.assertEqual([(0, 2)], windows)

    def test_reuses_step2_windowing(self):
        entries = [_entry(i, 7) for i in range(8)]
        original = three_pass_generate.iter_entry_windows
        with patch.object(three_pass_generate, "iter_entry_windows", wraps=original) as spy:
            windows = review_script.get_review_entry_windows(
                entries, {"review_batch_size": 25, "review_target_chars": 30})
            spy.assert_called_once_with(entries, 25, 30)
        self.assertEqual(windows, [(window[0][0], window[-1][0] + 1)
                                   for window in original(entries, 25, 30)])

    def test_invalid_batch_settings_fail_loud(self):
        entries = [_entry(0, 3)]
        for config, message in (
            ({"review_batch_size": 0}, "review_batch_size"),
            ({"review_target_chars": -1}, "review_target_chars"),
            ({"review_target_chars": 1000001}, "review_target_chars"),
            ({"review_target_chars": "10"}, "review_target_chars"),
        ):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, message):
                review_script.get_review_entry_windows(entries, config)


class ReviewTargetCharsConfigTests(unittest.TestCase):
    def test_default_is_zero(self):
        self.assertEqual(0, GenerationConfig().review_target_chars)

    def test_bounds_are_enforced(self):
        self.assertEqual(0, GenerationConfig(review_target_chars=0).review_target_chars)
        self.assertEqual(1_000_000, GenerationConfig(review_target_chars=1_000_000).review_target_chars)
        for bad in (-1, 1_000_001):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                GenerationConfig(review_target_chars=bad)


class ReviewBatchSizeConfigTests(unittest.TestCase):
    """#2335: the config model and review must apply one batching rule."""

    def test_model_and_review_accept_the_same_values(self):
        for value in (1, 10, 25):
            with self.subTest(value=value):
                self.assertEqual(value, GenerationConfig(
                    review_batch_size=value).review_batch_size)
                self.assertEqual((value, 0),
                                 review_script.get_review_batching(
                                     {"review_batch_size": value}))

    def test_model_and_review_refuse_the_same_values(self):
        for value in (0, -3, True, False, "10", 10.5):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    GenerationConfig(review_batch_size=value)
                with self.assertRaisesRegex(ValueError, "review_batch_size"):
                    review_script.get_review_batching({"review_batch_size": value})

    def test_absent_key_keeps_the_existing_default(self):
        self.assertEqual(25, GenerationConfig().review_batch_size)
        self.assertEqual((25, 0), review_script.get_review_batching({}))
        self.assertEqual(review_script.get_review_batching({}),
                         review_script.get_review_batching(GenerationConfig().model_dump()))


class ReviewBatchingIdentityTests(unittest.TestCase):
    def _fingerprint(self, generation):
        from types import SimpleNamespace
        args = SimpleNamespace(context_window=0, dedupe_speakers=False,
                               alias_registry=None, remap_voice_config=None)
        params = LLMGenParams(system_prompt="system", user_prompt_template="{batch}")
        config = {"generation": generation, "llm_mode": "local"}
        profile = {"base_url": "http://fixture/v1", "model_name": "model", "api_key": "local"}
        return review_script.get_completed_review_fingerprint(
            config, profile, params, args, object(), "model")

    def test_default_zero_keeps_preexisting_receipt_identity(self):
        self.assertEqual(self._fingerprint({"review_batch_size": 25}),
                         self._fingerprint({"review_batch_size": 25, "review_target_chars": 0}))

    def test_opt_in_changes_receipt_identity(self):
        self.assertNotEqual(self._fingerprint({"review_batch_size": 25}),
                            self._fingerprint({"review_batch_size": 25, "review_target_chars": 60}))


class ReviewEstimateParityTests(unittest.TestCase):
    """The HTTP call-count estimate must equal the requests review actually sends."""

    def test_estimate_equals_actual_requests_with_char_target(self):
        counts = [10, 20, 5, 30, 12, 8, 25, 6, 18, 11, 22, 9]
        entries = [_entry(i, length) for i, length in enumerate(counts)]
        generation = {"review_batch_size": 25, "review_target_chars": 60}
        windows = review_script.get_review_entry_windows(entries, generation)
        self.assertGreater(len(windows), 1)  # char batching, not one 25-entry batch
        self.assertEqual(len(windows), len(list(
            three_pass_generate.iter_entry_windows(entries, 25, 60))))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, config_path = root / "annotated_script.json", root / "config.json"
            source.write_text(json.dumps(entries))
            config_path.write_text(json.dumps({
                "llm_mode": "local",
                "llm_local": {"base_url": "http://cpu-fixture/v1", "model_name": "fixture",
                              "api_key": "local", "on_this_gpu": True},
                "generation": generation,
            }))
            calls, window_size = [], 2

            def fake_review(client, model, batch, index, total, params, **kwargs):
                start, end = windows[len(calls)]
                self.assertEqual(entries[start:end], batch)
                self.assertEqual(index, len(calls) + 1)
                self.assertEqual(len(windows), total)
                context = kwargs["source_context"]
                expected_before = entries[max(0, start - window_size):start]
                expected_after = entries[end:min(len(entries), end + window_size)]
                before = context.split("--- PREVIOUS ENTRIES (Context Only) ---\n")
                after = context.split("--- NEXT ENTRIES (Context Only) ---\n")
                self.assertEqual(bool(expected_before), len(before) == 2)
                self.assertEqual(bool(expected_after), len(after) == 2)
                if expected_before:
                    parsed = before[1].split("\n\n--- NEXT ENTRIES")[0].splitlines()
                    self.assertEqual(expected_before, [json.loads(line) for line in parsed if line])
                if expected_after:
                    self.assertEqual(expected_after,
                                     [json.loads(line) for line in after[1].splitlines() if line])
                calls.append(list(batch))
                return [dict(entry) for entry in batch]

            state = copy.deepcopy(core.process_state)
            for value in state.values():
                value["running"] = False

            def dispatch(command, name):
                self.assertEqual("review", name)
                self.assertTrue(state["review"]["running"])
                try:
                    with patch.object(review_script.sys, "argv", command[2:]):
                        review_script.main()
                finally:
                    state["review"]["running"] = False

            api = FastAPI()
            api.include_router(script.router)
            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(io.StringIO()))
                for module in (core, script):
                    stack.enter_context(patch.object(module, "process_state", state))
                stack.enter_context(patch.object(core, "acquire_gpu_lock", return_value=None))
                stack.enter_context(patch.object(core, "_gpu_leases", {}))
                stack.enter_context(patch.object(core, "_task_claims", {}))
                stack.enter_context(patch.object(core, "llm_is_on_this_gpu", return_value=True))
                stack.enter_context(patch.object(script, "SCRIPT_PATH", str(source)))
                stack.enter_context(patch.object(script, "CONFIG_PATH", str(config_path)))
                stack.enter_context(patch.object(script, "run_process", side_effect=dispatch))
                stack.enter_context(patch.object(review_script, "get_runtime_data_dir", return_value=tmp))
                stack.enter_context(patch.object(review_script, "get_app_config_path", return_value=str(config_path)))
                stack.enter_context(patch.object(review_script, "ensure_ideal_settings",
                                                 return_value=(False, {}, "CPU fixture settings")))
                stack.enter_context(patch.object(review_script, "get_current_status", return_value={"loaded": False}))
                stack.enter_context(patch.object(review_script, "make_run_client", return_value=object()))
                stack.enter_context(patch.object(review_script, "ensure_cached_or_benchmarked_concurrency", return_value=1))
                stack.enter_context(patch.object(review_script, "review_batch", side_effect=fake_review))
                client = stack.enter_context(TestClient(api))
                response = client.post("/api/review_script_contextual",
                                       json={"window_size": window_size, "dedupe_speakers": False})

            self.assertEqual(200, response.status_code, response.text)
            estimate = response.json()
            self.assertEqual(len(entries), estimate["total_entries"])
            self.assertEqual(25, estimate["batch_size"])
            self.assertEqual(len(windows), estimate["estimated_calls"])
            self.assertEqual(len(windows), len(calls))
            self.assertEqual(entries, [entry for batch in calls for entry in batch])


class ReviewResumeAndFailureTests(unittest.TestCase):
    """Resume must never lose entries; a truncated reply must keep the original."""

    def _run(self, entries, generation, *, resume_offset=None, fake_review=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root / "annotated_script.json", root / "reviewed.json"
            config_path = root / "config.json"
            source.write_text(json.dumps(entries))
            config_path.write_text(json.dumps({
                "llm_mode": "local",
                "llm_local": {"base_url": "http://cpu-fixture/v1", "model_name": "fixture",
                              "api_key": "local", "on_this_gpu": True},
                "generation": generation,
            }))
            calls, sections = [], []
            original_section = review_script.get_failed_section

            def record_section(*args, **kwargs):
                section = original_section(*args, **kwargs)
                sections.append(section)
                return section

            def review(client, model, batch, index, total, params, **kwargs):
                calls.append(list(batch))
                return fake_review(batch) if fake_review else [dict(entry) for entry in batch]

            with ExitStack() as stack:
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(patch.object(review_script, "get_runtime_data_dir", return_value=tmp))
                stack.enter_context(patch.object(review_script, "get_app_config_path", return_value=str(config_path)))
                stack.enter_context(patch.object(review_script, "ensure_ideal_settings",
                                                 return_value=(False, {}, "CPU fixture settings")))
                stack.enter_context(patch.object(review_script, "get_current_status", return_value={"loaded": False}))
                stack.enter_context(patch.object(review_script, "make_run_client", return_value=object()))
                stack.enter_context(patch.object(review_script, "ensure_cached_or_benchmarked_concurrency", return_value=1))
                stack.enter_context(patch.object(review_script, "wait_for_vram_headroom", return_value=True))
                stack.enter_context(patch.object(review_script, "review_batch", side_effect=review))
                stack.enter_context(patch.object(review_script, "get_failed_section", side_effect=record_section))
                if resume_offset is not None:
                    def forced_resume(output_path, total_batches, batch_size, context_window,
                                      active_entries, active_corrected, active_stats, **kwargs):
                        stats = {**{key: 0 for key in review_script.REVIEW_EDIT_COUNTERS},
                                 "batches_failed": 0, "batches_skipped_vram": 0}
                        return (list(active_entries[:resume_offset]), stats, None, 1,
                                [resume_offset], [], resume_offset, None)
                    stack.enter_context(patch.object(review_script, "_load_resume_state",
                                                     side_effect=forced_resume))
                with patch.object(review_script.sys, "argv",
                                  ["review_script.py", "--input", str(source),
                                   "--output", str(output), "--force-review"]):
                    review_script.main()
            return {"calls": calls, "sections": sections,
                    "output": json.loads(output.read_text(encoding="utf-8"))}

    def test_misaligned_resume_offset_is_refused(self):
        entries = [_entry(i, 7) for i in range(8)]
        generation = {"review_batch_size": 25, "review_target_chars": 30}
        windows = review_script.get_review_entry_windows(entries, generation)
        self.assertTrue(any(start < 3 < end for start, end in windows), "offset 3 must be inside a window")
        with self.assertRaisesRegex(ValueError, "start the review over"):
            self._run(entries, generation, resume_offset=3)

    def test_aligned_resume_reviews_exactly_remaining_entries(self):
        entries = [_entry(i, 7) for i in range(8)]
        generation = {"review_batch_size": 25, "review_target_chars": 30}
        windows = review_script.get_review_entry_windows(entries, generation)
        resume_offset = windows[1][0]
        result = self._run(entries, generation, resume_offset=resume_offset)
        remaining = [entries[start:end] for start, end in windows if start >= resume_offset]
        self.assertEqual(remaining, result["calls"])
        self.assertEqual(sum(len(batch) for batch in remaining),
                         sum(len(batch) for batch in result["calls"]))
        self.assertEqual(len(entries), len(result["output"]))

    def test_truncated_reply_fails_the_batch_and_keeps_original_text(self):
        # A reply cut short by the output ceiling leaves fewer/shorter entries.
        entries = [_entry(i, 12) for i in range(4)]
        generation = {"review_batch_size": 25, "review_target_chars": 200}
        self.assertEqual(1, len(review_script.get_review_entry_windows(entries, generation)))

        def truncated(batch):
            return [dict(batch[0])]  # only the first entry made it back

        result = self._run(entries, generation, fake_review=truncated)
        self.assertEqual(entries, result["output"], "original text must be kept")
        self.assertEqual(["text_length_mismatch"], [section["category"] for section in result["sections"]])
