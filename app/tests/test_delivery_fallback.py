import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams
from tests.test_three_pass_generate import _client_returning


class DeliveryFallbackTests(unittest.TestCase):
    def setUp(self):
        self.params = LLMGenParams(max_tokens=500, temperature=0.1)
        self.rows = [
            {"speaker": "NARRATOR", "text": "The room was cold."},
            {"speaker": "NARRATOR", "text": "The lamps flickered."},
        ]
        self.bad = [{"n": 0, "head": "wrong frozen text", "instruct": ""}]

    def test_exhaustion_marks_defaults_without_mutating_inputs(self):
        original = copy.deepcopy(self.rows)
        out = tp.instruct_batch(_client_returning([self.bad] * 10), "m",
                                self.rows, self.params, max_retries=1)
        self.assertEqual(original, self.rows)
        self.assertEqual([True, True], [row.get("instruct_unchecked") for row in out])
        self.assertEqual([tp.default_instruct(row) for row in original],
                         [row["instruct"] for row in out])
        self.assertEqual([row["text"] for row in original], [row["text"] for row in out])

    def test_validated_success_clears_prior_marker_only(self):
        prior = dict(self.rows[0], instruct_unchecked=True, extra="preserved")
        good = [{"n": 0, "head": prior["text"], "instruct": "Quiet, measured."}]
        out = tp.instruct_batch(_client_returning([good]), "m", [prior], self.params)
        self.assertNotIn("instruct_unchecked", out[0])
        self.assertEqual("Quiet, measured.", out[0]["instruct"])
        self.assertEqual("preserved", out[0]["extra"])
        self.assertIs(True, prior["instruct_unchecked"])

    def test_adaptive_split_marks_only_exhausted_leaf(self):
        good = [{"n": 0, "head": self.rows[0]["text"], "instruct": "Quiet, measured."}]
        # Only force the native runtime split decision; calls, validation and
        # leaf fallback remain real. Outer failure followed by one good leaf.
        with patch.object(tp, "get_exhausted_runtime_batch_ranges",
                          side_effect=[[(0, 1), (1, 2)], None]):
            out = tp.instruct_batch(_client_returning([self.bad, good, self.bad]),
                                    "m", self.rows, self.params, max_retries=0)
        self.assertNotIn("instruct_unchecked", out[0])
        self.assertIs(True, out[1].get("instruct_unchecked"))
        self.assertEqual("Quiet, measured.", out[0]["instruct"])
        self.assertEqual(tp.default_instruct(self.rows[1]), out[1]["instruct"])

    def test_native_run_manifest_and_checkpoint_record_exact_fallback_rows(self):
        source = "The room was cold. The lamps flickered. ..."
        segmented = [{"type": "NARRATOR", "text": row["text"]} for row in self.rows]
        segmented.append({"type": "NARRATOR", "text": "..."})
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                tp, "segment_chunk_adaptively", return_value=segmented):
            output = str(Path(tmp) / "book.json")
            out = tp.run_three_pass(_client_returning([self.bad] * 20), "m", source,
                                    self.params, chunk_size=6000, output_path=output)
            manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
            self.assertEqual("complete", manifest["status"])
            self.assertEqual({"count": 2, "entries": [1, 2]}, manifest.get("delivery_review"))
            self.assertIs(True, out[0].get("instruct_unchecked"))
            self.assertIs(True, out[1].get("instruct_unchecked"))
            self.assertNotIn("instruct_unchecked", out[2])
            from generation_checkpoint_deltas import load_generation_delta_checkpoint
            checkpoint = load_generation_delta_checkpoint(tp.three_pass_checkpoint_path(output))
            self.assertEqual([True, True, None],
                             [row.get("instruct_unchecked") for row in checkpoint["annotated"]])

    def test_targeted_retry_calls_only_pass3_with_original_neighbors(self):
        rows = [dict(self.rows[0], instruct="Already approved."),
                dict(self.rows[1], instruct="Neutral, even narration.", instruct_unchecked=True),
                {"speaker": "NARRATOR", "text": "He went outside.", "instruct": "Keep this."}]
        original = copy.deepcopy(rows)
        client = _client_returning([[
            {"n": 0, "head": rows[1]["text"], "instruct": "Soft, hesitant."}]])
        with patch.object(tp, "instruct_batch", wraps=tp.instruct_batch) as instruct, \
                patch.object(tp, "segment_chunk_adaptively", side_effect=AssertionError("pass1 called")), \
                patch.object(tp, "attribute_batch", side_effect=AssertionError("pass2 called")):
            out = tp.retry_delivery_instructions(client, "m", rows, self.params)
        self.assertEqual(original, rows)
        self.assertEqual(original[0], out[0])
        self.assertEqual(original[2], out[2])
        self.assertEqual("Soft, hesitant.", out[1]["instruct"])
        self.assertNotIn("instruct_unchecked", out[1])
        self.assertEqual([rows[1]], instruct.call_args.args[2])
        self.assertEqual([{"previous_context": rows[0], "next_context": rows[2]}],
                         instruct.call_args.kwargs["neighbor_contexts"])

    def test_targeted_retry_exhaustion_retains_warning_and_cancellation_stops_calls(self):
        rows = [dict(self.rows[0], instruct=tp.default_instruct(self.rows[0]),
                     instruct_unchecked=True)]
        original = copy.deepcopy(rows)
        out = tp.retry_delivery_instructions(_client_returning([self.bad] * 10),
                                             "m", rows, self.params)
        self.assertEqual(original, out)
        with patch.object(tp, "instruct_batch", side_effect=AssertionError("called after cancellation")):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                tp.retry_delivery_instructions(None, "m", rows, self.params,
                    cancel_check=lambda: (_ for _ in ()).throw(RuntimeError("cancelled")))
        self.assertEqual(original, rows)

    def test_native_regrouping_preserves_unaffected_audio_and_detaches_changed_rows(self):
        from delivery_review import get_delivery_retry_chunks
        from project import group_into_chunks
        rows = [dict(row, instruct="Neutral, even narration.", instruct_unchecked=True)
                for row in self.rows]
        rows.append({"speaker": "ELENA", "text": "Wait for me.", "instruct": "Firm."})
        chunks = [dict(row, id=i, uid=f"uid-{i}", status="done", audio_path=f"audio-{i}.wav")
                  for i, row in enumerate(group_into_chunks(rows))]
        original = copy.deepcopy(chunks)
        updated = [dict(rows[0], instruct="Quiet."), dict(rows[1], instruct="Tense."), rows[2]]
        out = get_delivery_retry_chunks(rows, updated, chunks)
        self.assertEqual(3, len(out))
        self.assertEqual(dict(chunks[1], id=2), out[2])
        self.assertEqual([None, None], [row["audio_path"] for row in out[:2]])
        self.assertEqual(["pending", "pending"], [row["status"] for row in out[:2]])
        self.assertTrue(all(row["uid"] not in {"uid-0", "uid-1"} for row in out[:2]))
        self.assertEqual(original, chunks)
        for edited in ([dict(chunks[0], text="Editor correction."), chunks[1]],
                       list(reversed(chunks)),
                       [dict(chunks[0], character_style="Warm"), chunks[1]]):
            with self.assertRaisesRegex(ValueError, "Editor"):
                get_delivery_retry_chunks(rows, updated, edited)
        self.assertIsNone(get_delivery_retry_chunks(rows, updated, None))

    def test_source_mapping_retains_native_speech_splits_and_default_output(self):
        from project import group_into_chunks
        entries = [dict(self.rows[0], instruct="Neutral."),
                   {"speaker": "NARRATOR", "text": "..."},
                   {"speaker": "ELENA", "text": "Go. 😀 Come back.", "instruct": "Firm."}]
        original = copy.deepcopy(entries)
        ordinary = group_into_chunks(entries, max_chars=12)
        indexed = group_into_chunks(entries, max_chars=12, include_source_indices=True)
        self.assertEqual(ordinary, [{key: value for key, value in row.items()
                                    if key != "source_entry_indices"} for row in indexed])
        self.assertEqual([0], indexed[0]["source_entry_indices"])
        self.assertTrue(all(row["source_entry_indices"] == [2]
                            for row in indexed if row["speaker"] == "ELENA"))
        self.assertEqual(original, entries)

    def test_reconciliation_refuses_changed_frozen_or_unaffected_rows_without_chunks(self):
        from delivery_review import get_delivery_retry_chunks
        before = [dict(self.rows[0], instruct="Approved."),
                  dict(self.rows[1], instruct="Neutral.", instruct_unchecked=True)]
        for after in ([dict(before[0], instruct="Unexpected."), before[1]],
                      [before[0], dict(before[1], text="Rewritten.")],
                      [before[0], dict(before[1], speaker="ELENA")],
                      before[:1]):
            with self.assertRaisesRegex(ValueError, "frozen or unaffected"):
                get_delivery_retry_chunks(before, after, None)

    def test_native_shared_params_keep_selected_profile_prompts_and_retry_policy(self):
        config = {"llm_mode": "remote", "llm": {"model_name": "stale", "api_retry_limit": 99},
                  "llm_remote": {"model_name": "m", "api_retry_limit": 2,
                                 "on_api_exhaustion": "fail", "retry_max_delay_seconds": 9,
                                 "structured_output": "auto", "provider_extra_body": {"keep": True}},
                  "generation": {"max_tokens": 24000, "temperature": 0.4,
                                 "three_pass_instruct_temperature": 0.12}}
        original = copy.deepcopy(config)
        with patch.object(tp, "resolve_three_pass_prompt", side_effect=[("split", "source"),
                                                                        ("delivery", "rows")]):
            params = tp.get_three_pass_run_params(config, {"context_length": 32768}, "high")
        self.assertEqual(original, config)
        self.assertEqual(24000, params.max_tokens)
        self.assertEqual(24000, params.hard_max_tokens)
        self.assertEqual(32768, params.context_length)
        self.assertEqual(2, params.api_retry_limit)
        self.assertEqual(9, params.retry_max_delay_seconds)
        self.assertEqual("fail", params.on_api_exhaustion)
        self.assertEqual("high", params.reasoning_effort)
        self.assertEqual(0.12, params.instruct_temperature)
        self.assertEqual(("delivery", "rows"), (params.instruct_system_prompt,
                                                 params.instruct_user_prompt_template))
        self.assertEqual({"keep": True}, params.provider_extra_body)

    def test_publication_updates_native_checkpoint_manifest_and_chunks_atomically(self):
        from delivery_review import ensure_delivery_review_snapshot, apply_delivery_retry
        from generation_checkpoint_deltas import load_generation_delta_checkpoint
        from project import ProjectManager, group_into_chunks
        segmented = [{"type": "NARRATOR", "text": row["text"]} for row in self.rows]
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                tp, "segment_chunk_adaptively", return_value=segmented):
            root = Path(tmp)
            output = str(root / "annotated_script.json")
            rows = tp.run_three_pass(_client_returning([self.bad] * 20), "m",
                                    " ".join(row["text"] for row in self.rows),
                                    self.params, chunk_size=6000, output_path=output)
            Path(output).write_text(json.dumps(rows))
            chunks = [dict(row, id=i, uid=f"uid-{i}", status="done", audio_path="prior.wav")
                      for i, row in enumerate(group_into_chunks(rows))]
            ProjectManager(tmp).save_chunks(chunks)
            (root / "prior.wav").write_bytes(b"prior rendered artifact")
            snapshot = ensure_delivery_review_snapshot(tmp)
            after = [dict(row, instruct=f"Validated direction {i}.") for i, row in enumerate(rows)]
            for row in after:
                row.pop("instruct_unchecked")
            result = apply_delivery_retry(tmp, snapshot, after)
            self.assertEqual({"count": 0, "entries": []}, result)
            self.assertEqual(after, json.loads(Path(output).read_text()))
            self.assertEqual(after, load_generation_delta_checkpoint(tp.three_pass_checkpoint_path(output))["annotated"])
            self.assertEqual(result, json.loads(Path(tp.three_pass_manifest_path(output)).read_text())["delivery_review"])
            self.assertEqual([None, None], [row["audio_path"] for row in ProjectManager(tmp).load_chunks()])
            self.assertEqual(b"prior rendered artifact", (root / "prior.wav").read_bytes())
            from book_state_transaction import JOURNAL
            from generation_checkpoint_shards import get_generation_checkpoint_artifacts
            self.assertFalse((root / JOURNAL).exists())
            self.assertEqual([tp.three_pass_checkpoint_path(output)],
                             get_generation_checkpoint_artifacts(tp.three_pass_checkpoint_path(output)))

    def test_stale_book_script_or_editor_and_cancel_refuse_without_publication(self):
        from delivery_review import ensure_delivery_review_snapshot, apply_delivery_retry
        from project import ProjectManager, group_into_chunks
        for changed in ("state.json", "annotated_script.json", "chunks.json", "cancel"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = [dict(self.rows[0], instruct="Neutral.", instruct_unchecked=True)]
                (root / "annotated_script.json").write_text(json.dumps(rows))
                ProjectManager(tmp).save_chunks([dict(group_into_chunks(rows)[0],
                    id=0, uid="retained", status="done", audio_path="old.wav")])
                snapshot = ensure_delivery_review_snapshot(tmp)
                after = [dict(self.rows[0], instruct="Quiet.")]
                if changed == "state.json":
                    (root / changed).write_text('{"active_book_id":"another book"}')
                elif changed == "annotated_script.json":
                    (root / changed).write_text(json.dumps([dict(rows[0], text="Editor correction.")]))
                elif changed == "chunks.json":
                    chunks = json.loads((root / changed).read_text())
                    chunks[0]["instruct"] = "Manual direction."
                    (root / changed).write_text(json.dumps(chunks))
                before = {p.name: p.read_bytes() for p in root.glob("*.json")}
                cancel = (lambda: (_ for _ in ()).throw(RuntimeError("cancelled"))) if changed == "cancel" else None
                with self.assertRaises((ValueError, RuntimeError)):
                    apply_delivery_retry(tmp, snapshot, after, cancel_check=cancel)
                self.assertEqual(before, {p.name: p.read_bytes() for p in root.glob("*.json")})


class DeliveryReviewApiTests(unittest.TestCase):
    def test_actual_get_and_owned_post_retry_clear_only_delivery_marker(self):
        import core
        from routers import script
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from project import ProjectManager, group_into_chunks
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [{"speaker": "NARRATOR", "text": "The room was cold.",
                     "instruct": "Neutral, even narration.", "instruct_unchecked": True}]
            (root / "annotated_script.json").write_text(json.dumps(rows))
            (root / "config.json").write_text(json.dumps({"llm": {"model_name": "m"},
                                                        "generation": {"max_tokens": 500}}))
            ProjectManager(tmp).save_chunks([dict(group_into_chunks(rows)[0], id=0,
                uid="original", status="done", audio_path="old.wav")])
            state = copy.deepcopy(core.process_state)
            for entry in state.values():
                entry["running"] = False
            client = _client_returning([[{"n": 0, "head": rows[0]["text"], "instruct": "Quiet, measured."}]])
            with patch.object(core, "process_state", state), patch.object(script, "process_state", state), \
                    patch.object(core, "_task_claims", {}), patch.object(core, "_gpu_leases", {}), \
                    patch.object(core, "acquire_gpu_lock", return_value=None), \
                    patch.object(core, "llm_is_on_this_gpu", return_value=True), \
                    patch.object(core, "RUN_HISTORY_DIR", str(root / "runs")), \
                    patch.object(core, "DATA_DIR", tmp), patch.object(script, "DATA_DIR", tmp), \
                    patch.object(script, "CONFIG_PATH", str(root / "config.json")), \
                    patch.object(script, "ensure_ideal_settings", return_value=(None, {"context_length": 32768}, "CPU fixture")), \
                    patch("llm_provider.make_run_client", return_value=client), \
                    patch.object(tp, "segment_chunk_adaptively", side_effect=AssertionError("pass1 called")), \
                    patch.object(tp, "attribute_batch", side_effect=AssertionError("pass2 called")):
                app = FastAPI()
                app.add_middleware(core.TaskClaimMiddleware)
                app.include_router(script.router)
                with TestClient(app) as http:
                    review = http.get("/api/annotated_script/delivery_review")
                    self.assertEqual(200, review.status_code)
                    info = review.json()
                    self.assertEqual([1], info["entries"])
                    self.assertEqual(rows[0]["text"], info["rows"][0]["text"])
                    response = http.post("/api/annotated_script/delivery_review/retry",
                                         json={"snapshot": info["snapshot"]})
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertFalse(state["script"]["running"])
                    self.assertEqual({}, core._task_claims)
                    saved = json.loads((root / "annotated_script.json").read_text())
                    self.assertEqual("Quiet, measured.", saved[0]["instruct"])
                    self.assertNotIn("instruct_unchecked", saved[0])
                    receipt = json.loads(Path(tp.three_pass_manifest_path(str(root / "annotated_script.json"))).read_text())
                    self.assertEqual("delivery_retry_only", receipt["status"])
                    record = receipt["delivery_retries"][-1]
                    self.assertEqual([1], record["entries"])
                    self.assertEqual(info["snapshot"], record["source_snapshot"])
                    self.assertEqual("m", record["model_binding"]["primary_model"])
                    self.assertEqual(500, record["configured_params"]["max_tokens"])
                    self.assertEqual(64, len(record["prompt_sha256"]))
                    self.assertEqual(rows[0]["text"], saved[0]["text"])
                    self.assertIsNone(ProjectManager(tmp).load_chunks()[0]["audio_path"])
                    stale = http.post("/api/annotated_script/delivery_review/retry",
                                      json={"snapshot": info["snapshot"]})
                    self.assertEqual(409, stale.status_code)
                    self.assertEqual(409, http.post("/api/annotated_script/delivery_review/cancel",
                         json={"claim_id": response.json()["claim_id"]}).status_code)

    def test_cancelled_blocked_provider_retains_real_claim_until_worker_exits(self):
        import asyncio
        import threading
        import core
        from routers import script
        from fastapi import BackgroundTasks, HTTPException
        from types import SimpleNamespace
        entered, release = threading.Event(), threading.Event()
        rows = [{"speaker": "NARRATOR", "text": "The room was cold.",
                 "instruct": "Neutral.", "instruct_unchecked": True}]
        native_client = _client_returning([[{"n": 0, "head": rows[0]["text"], "instruct": "Quiet."}]])
        native_create = native_client.chat.completions.create

        def create(**kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Controlled provider must be released")
            return native_create(**kwargs)

        native_client.chat.completions.create = create
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / "annotated_script.json"
            script_path.write_text(json.dumps(rows))
            original = script_path.read_bytes()
            (root / "config.json").write_text(json.dumps({"llm": {"model_name": "m"},
                                                        "generation": {"max_tokens": 500}}))
            state = copy.deepcopy(core.process_state)
            for entry in state.values():
                entry["running"] = False
            with patch.object(core, "process_state", state), patch.object(script, "process_state", state), \
                    patch.object(core, "_task_claims", {}), patch.object(core, "_gpu_leases", {}), \
                    patch.object(core, "acquire_gpu_lock", return_value=None), \
                    patch.object(core, "llm_is_on_this_gpu", return_value=True), \
                    patch.object(core, "RUN_HISTORY_DIR", str(root / "runs")), \
                    patch.object(core, "DATA_DIR", tmp), patch.object(script, "DATA_DIR", tmp), \
                    patch.object(script, "CONFIG_PATH", str(root / "config.json")), \
                    patch.object(script, "ensure_ideal_settings", return_value=(None, {"context_length": 32768}, "CPU fixture")), \
                    patch("llm_provider.make_run_client", return_value=native_client):
                review = script.ensure_delivery_review_response()
                tasks = BackgroundTasks()
                started = script.start_delivery_retry(tasks, script.DeliveryRetryRequest(snapshot=review["snapshot"]))
                failures = []

                def worker():
                    try:
                        asyncio.run(tasks())
                    except BaseException as exc:
                        failures.append(exc)

                thread = threading.Thread(target=worker, daemon=True)
                thread.start()
                try:
                    self.assertTrue(entered.wait(2), "native provider call must start")
                    claim = started["claim_id"]
                    self.assertTrue(asyncio.run(script.get_delivery_retry_status(claim))["running"])
                    self.assertFalse(asyncio.run(script.get_delivery_retry_status("wrong-claim"))["running"])
                    with self.assertRaises(HTTPException):
                        asyncio.run(script.cancel_delivery_review(script.DeliveryRetryCancelRequest(claim_id="wrong-claim")))
                    self.assertFalse(state["script"].get("cancel"))
                    asyncio.run(script.cancel_delivery_review(script.DeliveryRetryCancelRequest(claim_id=claim)))
                    self.assertTrue(state["script"]["running"])
                    self.assertEqual(claim, core._task_claims["script"]["id"])
                    with self.assertRaises(HTTPException):
                        core.check_global_gpu_lock("audio")
                    self.assertEqual(original, script_path.read_bytes())
                finally:
                    release.set()
                    thread.join(3)
                self.assertFalse(thread.is_alive(), "worker must exit after controlled provider release")
                self.assertEqual([], failures)
                self.assertFalse(state["script"]["running"])
                self.assertEqual({}, core._task_claims)
                self.assertEqual(original, script_path.read_bytes())
                self.assertFalse(asyncio.run(script.get_delivery_retry_status(started["claim_id"]))["running"])

    def test_failed_publication_rolls_back_script_chunks_manifest_and_checkpoint(self):
        import book_state_transaction as transaction
        from delivery_review import ensure_delivery_review_snapshot, apply_delivery_retry
        from project import ProjectManager, group_into_chunks
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [{"speaker": "NARRATOR", "text": "The room was cold.",
                     "instruct": "Neutral.", "instruct_unchecked": True}]
            output = root / "annotated_script.json"
            output.write_text(json.dumps(rows))
            manifest = Path(tp.three_pass_manifest_path(str(output)))
            manifest.write_text(json.dumps({"status": "complete", "delivery_review": {"count": 1, "entries": [1]}}))
            checkpoint = Path(tp.three_pass_checkpoint_path(str(output)))
            checkpoint.write_text(json.dumps({"annotated": rows, "stage": "done"}))
            ProjectManager(tmp).save_chunks([dict(group_into_chunks(rows)[0], id=0,
                uid="prior", status="done", audio_path="prior.wav")])
            snapshot = ensure_delivery_review_snapshot(tmp)
            before = {path.name: path.read_bytes() for path in root.glob('*.json')}
            original_move = transaction._move
            failures = []

            def fail_one_publish(source, destination):
                if Path(destination) == manifest and Path(source).name.startswith('new-') and not failures:
                    failures.append('injected manifest publish failure')
                    raise OSError(failures[-1])
                return original_move(source, destination)

            with patch.object(transaction, '_move', side_effect=fail_one_publish):
                with self.assertRaisesRegex(OSError, 'manifest publish failure'):
                    apply_delivery_retry(tmp, snapshot, [dict(rows[0], instruct='Quiet.')])
            self.assertEqual(['injected manifest publish failure'], failures)
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.glob('*.json')})
            self.assertFalse((root / transaction.JOURNAL).exists())
            self.assertEqual([], list(root.glob('.book-switch-*')))

    def test_journal_only_change_rejects_stale_retry_and_fresh_retry_keeps_latest_audio(self):
        from chunk_status_journal import ChunkStatusJournal, get_chunk_status_journal_path
        from delivery_review import ensure_delivery_review_snapshot, apply_delivery_retry
        from project import ProjectManager, group_into_chunks
        from utils import file_lock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [{"speaker": "NARRATOR", "text": "The room was cold.",
                     "instruct": "Neutral.", "instruct_unchecked": True},
                    {"speaker": "ELENA", "text": "Wait for me.", "instruct": "Firm."}]
            (root / "annotated_script.json").write_text(json.dumps(rows))
            chunks_path = root / "chunks.json"
            ProjectManager(tmp).save_chunks([dict(row, id=i, uid=f"uid-{i}",
                status="done", audio_path=f"original-{i}.wav")
                for i, row in enumerate(group_into_chunks(rows))])
            original_chunk_bytes = chunks_path.read_bytes()
            journal = ChunkStatusJournal(str(chunks_path), {"active_book_id": None, "book_generation": None})
            with file_lock(str(chunks_path)):
                journal.apply_update("uid-1", {"audio_path": "latest.wav"})
            stale = ensure_delivery_review_snapshot(tmp)
            with file_lock(str(chunks_path)):
                journal.apply_update("uid-1", {"audio_path": "newer.wav"})
            self.assertEqual(original_chunk_bytes, chunks_path.read_bytes())
            after = [dict(rows[0], instruct="Quiet."), rows[1]]
            after[0].pop("instruct_unchecked")
            with self.assertRaisesRegex(ValueError, "changed during delivery retry"):
                apply_delivery_retry(tmp, stale, after)
            fresh = ensure_delivery_review_snapshot(tmp)
            apply_delivery_retry(tmp, fresh, after)
            chunks = ProjectManager(tmp).load_chunks()
            self.assertEqual("uid-1", chunks[1]["uid"])
            self.assertEqual("newer.wav", chunks[1]["audio_path"])
            self.assertEqual("done", chunks[1]["status"])
            self.assertIsNone(chunks[0]["audio_path"])
            self.assertFalse(Path(get_chunk_status_journal_path(str(chunks_path))).exists())
