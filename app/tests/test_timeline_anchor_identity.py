"""#2160: stable timeline anchors across routes, batches, scope and drift."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

import tts
import voice_drift
from project import ProjectManager
from project_scope_voices import get_scope_voice_plan
from routers import editor, voices as voices_router
from book_state_transaction import get_book_operation_token
from chunk_status_journal import get_chunk_status_journal_path
from timeline_anchors import build_uid_index, full_uid_index_context, get_anchor_uid, resolve_timeline_anchors
from tests import test_voice_followups_733 as v733
from tests import test_drift_voice_states as drift_tests


def style_config(point):
    return {"A": {"type": "custom", "voice": "Ryan", "character_style": "young",
                  "style_timeline": [point]}}


def make_rows(count=4):
    return [{"id": index, "uid": f"u{index}", "speaker": "A", "text": f"line {index}",
             "instruct": "soft"} for index in range(count)]


def style_at(voices, row, index):
    return tts.get_chunk_voice_inputs(voices, row, index)["A"]["character_style"]


class ResolverTests(unittest.TestCase):
    def test_sparse_batch_uses_absolute_book_index(self):
        voices = style_config({"from_index": 4, "from_uid": "u4", "character_style": "aged"})
        rows = [{"index": 2, "uid": "u2", "speaker": "A", "text": "two"},
                {"index": 4, "uid": "u4", "speaker": "A", "text": "four"}]
        full = [{"uid": f"u{index}"} for index in range(6)]
        full_index = build_uid_index(full)
        self.assertEqual((2, 4), (full_index["u2"], full_index["u4"]))
        with full_uid_index_context(build_uid_index(full)):
            resolved = resolve_timeline_anchors(voices, rows, sparse=True)
        self.assertEqual("young", style_at(resolved, rows[0], 2))
        self.assertEqual("aged", style_at(resolved, rows[1], 4))
        self.assertEqual("u4", get_anchor_uid(full, 4))

    def test_duplicate_uid_is_ambiguous_and_falls_back_to_recorded_index(self):
        rows = make_rows()
        rows[2]["uid"] = "u0"
        voices = style_config({"from_index": 2, "from_uid": "u0", "character_style": "aged"})
        self.assertNotIn("u0", build_uid_index(rows))
        resolved = resolve_timeline_anchors(voices, rows)
        self.assertEqual("aged", style_at(resolved, rows[2], 2))
        self.assertEqual("young", style_at(resolved, rows[1], 1))

    def test_missing_and_regenerated_uid_keep_legacy_index(self):
        voices = style_config({"from_index": 2, "from_uid": "old", "character_style": "aged"})
        full = resolve_timeline_anchors(voices, make_rows())
        sparse = resolve_timeline_anchors(voices, [{"index": 3, "uid": "fresh", "speaker": "A"}], sparse=True)
        for resolved in (full, sparse):
            self.assertEqual(voices["A"]["style_timeline"], resolved["A"]["style_timeline"])
            self.assertEqual("aged", style_at(resolved, make_rows()[3], 3))

    def test_legacy_null_point_does_not_raise_for_unrelated_speaker(self):
        voices = {"A": {"style_timeline": [None,
                        {"from_index": None, "character_style": "aged"}]},
                  "B": {"type": "custom", "voice": "Ryan"}}
        resolved = resolve_timeline_anchors(voices, make_rows())
        self.assertEqual(voices, resolved)
        self.assertEqual("Ryan", tts.get_chunk_voice_inputs(resolved, {"speaker": "B"}, 0)["B"]["voice"])
        with self.assertRaises(ValueError):
            tts.get_chunk_voice_inputs(resolved, {"speaker": "A"}, 0)


class DispatchTests(unittest.TestCase):
    def test_local_and_external_batch_share_absolute_anchor(self):
        voices = style_config({"from_index": 4, "from_uid": "u4", "character_style": "aged"})
        chunks = [{"index": 2, "uid": "u2", "speaker": "A", "text": "two", "instruct": "soft"},
                  {"index": 4, "uid": "u4", "speaker": "A", "text": "four", "instruct": "soft"}]
        from tests.test_batch_style_timeline import get_engine
        seen = []
        def local(rows, config, output, seed):
            seen.append(("local", [p["from_index"] for p in config["A"]["style_timeline"]]))
            return {"completed": [row["index"] for row in rows], "failed": []}
        def external(rows, config, output, kind, seed):
            seen.append(("external", [p["from_index"] for p in config["A"]["style_timeline"]]))
            return {"completed": [row["index"] for row in rows], "failed": []}
        full = [{"uid": f"u{index}"} for index in range(6)]
        with tempfile.TemporaryDirectory() as directory, \
                full_uid_index_context(build_uid_index(full)):
            engine = get_engine()
            with patch.object(engine, "get_voice_backend", return_value="local"), \
                 patch.object(engine, "_local_batch_custom", local):
                engine.generate_batch(chunks, voices, directory)
            external_engine = get_engine()
            with patch.object(external_engine, "get_voice_backend", return_value="external"), \
                 patch.object(external_engine, "_external_batch", external):
                external_engine.generate_batch(chunks, voices, directory)
        self.assertEqual([("local", [4]), ("external", [4])], seen)

    def test_direct_batch_uses_recorded_position_for_duplicate_uid(self):
        voices = style_config({"from_index": 4, "from_uid": "shared", "character_style": "aged"})
        chunks = [{"index": index, "uid": "shared", "speaker": "A", "text": f"line {index}"}
                  for index in (2, 4)]
        from tests.test_batch_style_timeline import get_engine
        with tempfile.TemporaryDirectory() as directory:
            for backend, method in (("local", "_local_batch_custom"), ("external", "_external_batch")):
                for order in (chunks, chunks[::-1]):
                    with self.subTest(backend=backend, first=order[0]["index"]):
                        engine = get_engine()
                        seen = {}
                        def render(rows, config, *_args):
                            seen.update({row["index"]: style_at(config, row, row["index"]) for row in rows})
                            return {"completed": [row["index"] for row in rows], "failed": []}
                        with patch.object(engine, "get_voice_backend", return_value=backend), \
                             patch.object(engine, method, side_effect=render):
                            result = engine.generate_batch(order, voices, directory)
                        self.assertEqual([], result["failed"])
                        self.assertEqual({2: "young", 4: "aged"}, seen)


class RouteTests(unittest.TestCase):
    def fixture(self, directory, stack, config=None):
        root, manager, client, path, stored, rows = v733.VoiceTimelineAudioTests.fixture(
            self, directory, stack)
        if config is not None:
            path.write_text(json.dumps(config))
        return root, manager, client, path, stored, rows

    def test_delete_removes_only_one_point_after_insert(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "first"},
                {"from_index": 3, "from_uid": rows[3]["uid"], "character_style": "second"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.insert_chunk(0)
            response = client.delete(f"/api/voices/A/style_timeline/3?from_uid={rows[3]['uid']}")
            self.assertEqual(200, response.status_code, response.text)
            points = response.json()["style_timeline"]
            self.assertEqual([rows[2]["uid"]], [point["from_uid"] for point in points])

    def test_editor_insert_delete_and_render_preserve_content_voice_and_prior_audio(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, rows = self.fixture(directory, stack)
            client.app.include_router(editor.router)
            stack.enter_context(patch.object(editor, "SCRIPT_PATH", manager.script_path))
            stack.enter_context(patch.object(editor, "project_manager", manager))
            stack.enter_context(patch.object(editor, "check_global_gpu_lock"))
            stack.enter_context(patch.object(editor, "_deleted_chunk_receipts", {}))
            token = get_book_operation_token(str(root))
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged",
                "expected_book_token": token})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("pending", manager.get_saved_chunks()[2]["status"])
            seen = []
            def render(text, _instruct, speaker, config, output):
                seen.append((text, config[speaker]["character_style"]))
                sf.write(output, np.full(240, .2), 24000)
                return True
            manager.engine = SimpleNamespace(generate_voice=render)
            self.assertTrue(manager.generate_chunk_audio(2)[0])
            before = manager.get_saved_chunks()[2]
            original = (root / before["audio_path"]).read_bytes()
            voice_bytes = path.read_bytes()
            response = client.post("/api/chunks/0/insert", params={
                "expected_book_token": token, "expected_uid": rows[0]["uid"]})
            self.assertEqual(200, response.status_code, response.text)
            after = manager.get_saved_chunks()[3]
            self.assertEqual((before["uid"], "done", before["audio_path"]),
                             (after["uid"], after["status"], after["audio_path"]))
            self.assertEqual(original, (root / after["audio_path"]).read_bytes())
            self.assertTrue(manager.generate_chunk_audio(3)[0])
            for uid in (rows[0]["uid"], manager.get_saved_chunks()[1]["uid"]):
                response = client.delete("/api/chunks/0", params={
                    "expected_book_token": token, "expected_uid": uid})
                self.assertEqual(200, response.status_code, response.text)
            after_delete = manager.get_saved_chunks()[1]
            self.assertEqual((before["uid"], "done"), (after_delete["uid"], after_delete["status"]))
            self.assertEqual(original, (root / after_delete["audio_path"]).read_bytes())
            self.assertTrue(manager.generate_chunk_audio(1)[0])
            self.assertEqual([("Changed.", "aged")] * 3, seen)
            self.assertEqual(voice_bytes, path.read_bytes())

    def test_clear_timeline_invalidates_moved_anchor_audio(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "versions": {
                "different": {"type": "custom", "voice": "Serena"}},
                "version_timeline": [{"from_index": 2, "from_uid": rows[2]["uid"],
                                      "version_id": "different"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.delete_chunk(0)
            self.assertEqual("done", manager.load_chunks()[1]["status"])
            response = client.delete("/api/voices/A/version_timeline")
            self.assertEqual(200, response.status_code, response.text)
            moved = manager.load_chunks()[1]
            self.assertEqual("pending", moved["status"])
            self.assertIsNone(moved["audio_path"])

    def test_guarded_save_refuses_mismatched_uid(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            body = copy.deepcopy(snapshot["config"])
            body["A"]["style_timeline"] = [{"from_index": 12, "from_uid": rows[1]["uid"],
                                            "character_style": "aged"}]
            refused_mismatch = client.post("/api/voice_config/save", json={"voices": body,
                "revision": snapshot["revision"], "book_token": snapshot["book_token"]})
            self.assertEqual(400, refused_mismatch.status_code, refused_mismatch.text)
            fresh = client.get("/api/voice_config/snapshot").json()
            body = copy.deepcopy(fresh["config"])
            body["A"]["style_timeline"] = [{"from_index": 12, "character_style": "aged"}]
            refused = client.post("/api/voice_config/save", json={"voices": body,
                "revision": fresh["revision"], "book_token": fresh["book_token"]})
            self.assertEqual(400, refused.status_code, refused.text)

    def test_stale_explicit_uid_is_refused(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            manager.delete_chunk(0)
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 0, "from_uid": rows[0]["uid"], "character_style": "aged"})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_damaged_authoritative_rows_refuse_reads_and_echoes_without_writes(self):
        for damage in ("missing_snapshot", "invalid_snapshot", "wrong_shape", "invalid_journal", "dangling_snapshot"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                _root, manager, client, path, config, rows = self.fixture(directory, stack)
                point = {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}
                config["A"]["style_timeline"] = [point]
                path.write_text(json.dumps(config))
                snapshot = client.get("/api/voice_config/snapshot").json()
                chunks = Path(manager.chunks_path)
                journal = Path(get_chunk_status_journal_path(chunks))
                if damage == "missing_snapshot":
                    chunks.unlink()
                    journal.write_bytes(b'{}\n')
                elif damage == "invalid_journal":
                    journal.write_bytes(b'{}\n')
                elif damage == "dangling_snapshot":
                    chunks.unlink()
                    chunks.symlink_to(chunks.with_name("absent.json"))
                else:
                    chunks.write_text("[" if damage == "invalid_snapshot" else "{}")
                def saved_bytes():
                    return {p.name: p.readlink() if p.is_symlink() else p.read_bytes()
                            for p in (path, chunks, journal) if p.is_symlink() or p.exists()}
                before = saved_bytes()
                responses = [
                    client.get("/api/voices/A/state_timeline"),
                    client.get("/api/voice_config/snapshot"),
                    client.post("/api/voices/A/style_timeline", json=point),
                    client.post("/api/voice_config/save", json={
                        "voices": snapshot["config"], "revision": snapshot["revision"],
                        "book_token": snapshot["book_token"]}),
                ]
                for response in responses:
                    self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, saved_bytes())

    def test_empty_chunk_snapshot_refuses_new_point(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            manager.save_chunks([])
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 20, "character_style": "aged"})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_rejected_state_and_version_requests_never_backfill_legacy_chunks(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            chunks = Path(manager.chunks_path)
            for row in rows:
                row.pop("uid")
            chunks.write_text(json.dumps(rows))
            for speaker in ("A", "B"):
                with self.subTest(speaker=speaker):
                    malformed = copy.deepcopy(config)
                    malformed[speaker]["versions"] = []
                    path.write_text(json.dumps(malformed))
                    before = (path.read_bytes(), chunks.read_bytes())
                    response = client.get("/api/voices/A/state_timeline")
                    self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            path.write_text(json.dumps(config))
            before = (path.read_bytes(), chunks.read_bytes())
            response = client.post("/api/voices/A/version_timeline", json={
                "points": [{"from_index": 2, "version_id": "unknown"}]})
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_guarded_snapshot_echo_keeps_nullable_and_distinct_point_records(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            config["A"].update(
                style_timeline=[{"from_index": "002", "character_style": "aged"},
                                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}],
                version_timeline=[None, {"from_index": "002", "version_id": "different"},
                                  {"from_index": 2, "from_uid": rows[2]["uid"], "version_id": "different"}])
            config["B"]["style_timeline"] = None
            config["C"] = None
            path.write_text(json.dumps(config, indent=4) + "\n")
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            snapshot = client.get("/api/voice_config/snapshot").json()
            self.assertIsNone(snapshot["config"]["B"]["style_timeline"])
            self.assertIsNone(snapshot["config"]["A"]["version_timeline"][0])
            request = {"voices": snapshot["config"], "revision": snapshot["revision"],
                       "book_token": snapshot["book_token"]}
            response = client.post("/api/voice_config/save", json=request)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(snapshot["revision"], response.json()["revision"])
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            request["voices"]["A"] = None
            response = client.post("/api/voice_config/save", json=request)
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_dedicated_echoes_keep_bytes_and_reject_stale_book_tokens(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, config, rows = self.fixture(directory, stack)
            style = {"from_index": "002", "from_uid": rows[2]["uid"], "character_style": "aged"}
            version = {"from_index": "002", "from_uid": rows[2]["uid"], "version_id": "different"}
            config["A"].update(style_timeline=[style], version_timeline=[version])
            path.write_text(json.dumps(config, indent=4) + "\n")
            manager.insert_chunk(0)
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            for route, request in (("style_timeline", style), ("version_timeline", {"points": [version]})):
                with self.subTest(route=route):
                    url = f"/api/voices/A/{route}"
                    self.assertEqual(409, client.post(url, json={**request, "book_token": "0" * 64}).status_code)
                    response = client.post(url, json=request)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            old_token = get_book_operation_token(str(root))
            (root / "state.json").write_text('{"active_book_id":"book","book_generation":"two"}')
            response = client.post("/api/voices/A/style_timeline", json={
                **style, "expected_book_token": old_token})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_style_echo_waits_for_chunk_edit_and_preserves_the_insert(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, config, rows = self.fixture(directory, stack)
            point = {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}
            config["A"]["style_timeline"] = [point]
            path.write_text(json.dumps(config, indent=4))
            before = path.read_bytes()
            entered = Event()
            mutate = voices_router._mutate_book_voice_entry
            def notify(*args, **kwargs):
                entered.set()
                return mutate(*args, **kwargs)
            with ThreadPoolExecutor(max_workers=1) as pool, \
                 patch.object(voices_router, "_mutate_book_voice_entry", side_effect=notify):
                with manager._chunks_lock:
                    pending = pool.submit(client.post, "/api/voices/A/style_timeline", json=point)
                    self.assertTrue(entered.wait(5))
                    modified = rows[:1] + [{"uid": "inserted", "speaker": "A", "text": ""}] + rows[1:]
                    for index, row in enumerate(modified):
                        row["id"] = index
                    manager._save_chunks_locked(modified, uids_backfilled=True)
                    inserted_bytes = Path(manager.chunks_path).read_bytes()
                response = pending.result(timeout=5)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(3, response.json()["style_timeline"][0]["display_index"])
            self.assertEqual(inserted_bytes, Path(manager.chunks_path).read_bytes())
            self.assertEqual(before, path.read_bytes())

    def test_persona_version_echo_uses_moved_boundary_and_refuses_a_different_uid(self):
        from speaker_traits import get_persona_state_targets
        from tests.test_state_review_fixes import two_state_script
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            script = two_state_script()
            (root / "annotated_script.json").write_text(json.dumps(script))
            manager.save_chunks([{**row, "id": index, "uid": f"u{index}"}
                                 for index, row in enumerate(script)])
            target = get_persona_state_targets(script)["MIRA"][1]
            index = target["from_entry"]
            version_id = target["version_id"]
            point = {"from_index": index, "from_uid": f"u{index}", "version_id": version_id}
            path.write_text(json.dumps({"MIRA": {"type": "custom", "voice": "Ryan",
                "versions": {version_id: {"type": "custom", "voice": "Serena", "persona_state": target}},
                "version_timeline": [point]}}))
            manager.insert_chunk(0)
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            route = "/api/voices/MIRA/version_timeline"
            response = client.post(route, json={"points": [point]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))
            response = client.post(route, json={"points": [{**point, "from_uid": f"u{index + 1}"}]})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))

    def test_invalid_supplied_uids_are_rejected_consistently(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _config, _rows = self.fixture(directory, stack)
            snapshot = client.get("/api/voice_config/snapshot").json()
            chunks = Path(manager.chunks_path)
            before = (path.read_bytes(), chunks.read_bytes())
            for uid in (" ", "a" * 129, 7):
                with self.subTest(uid=uid):
                    style = {"from_index": 2, "from_uid": uid, "character_style": "aged"}
                    version = {"from_index": 2, "from_uid": uid, "version_id": "different"}
                    for response in (
                        client.post("/api/voices/A/style_timeline", json=style),
                        client.post("/api/voices/A/version_timeline", json={"points": [version]}),
                        client.post("/api/voice_config/save", json={
                            "voices": {"A": {"style_timeline": [style]}}, "revision": snapshot["revision"],
                            "book_token": snapshot["book_token"]}),
                    ):
                        self.assertEqual(400, response.status_code, response.text)
                    if isinstance(uid, str):
                        response = client.delete("/api/voices/A/style_timeline/2", params={"from_uid": uid})
                        self.assertEqual(400, response.status_code, response.text)
                    self.assertEqual(before, (path.read_bytes(), chunks.read_bytes()))


class ScopeAndDriftTests(unittest.TestCase):
    def test_scope_from_full_book_keeps_active_anchor_without_orphan(self):
        rows = make_rows(5)
        voices = style_config({"from_index": 0, "from_uid": "u0", "character_style": "aged"})
        voices["A"]["type"] = "custom"
        plan = get_scope_voice_plan(voices, rows, 2)
        point = plan["voices"]["A"]["style_timeline"][0]
        self.assertEqual({"from_index": 0, "character_style": "aged"}, point)
        self.assertEqual("aged", style_at(plan["voices"], rows[2], 0))

    def test_drift_groups_follow_moved_anchor(self):
        helper = drift_tests.DriftVoiceStateTests()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drift_tests.save_pcm(root / "young.wav", 11)
            drift_tests.save_pcm(root / "adult.wav", 22)
            config = {"Hero": {"type": "clone", "ref_audio": "young.wav",
                "versions": {"adult": {"type": "clone", "ref_audio": "adult.wav"}},
                "version_timeline": [{"from_index": 1, "from_uid": "u1",
                                      "version_id": "adult"}]}}
            rows = []
            for index, uid in enumerate(("u0", "other", "u1", "u2")):
                path = root / f"line-{index}.wav"
                drift_tests.save_pcm(path, 100 + index)
                rows.append({"id": index, "uid": uid, "speaker": "Hero",
                             "text": f"line {index}", "status": "done", "audio_path": path.name})
            groups, keys = voice_drift.get_drift_voice_groups(rows, config)
            self.assertEqual(keys[2], keys[3])
            self.assertEqual(keys[0], keys[1])
            self.assertNotEqual(keys[1], keys[2])


class IdentityContractTests(unittest.TestCase):
    def fixture(self, directory, stack, config=None):
        return v733.VoiceTimelineAudioTests.fixture(self, directory, stack)

    def test_blank_then_replacement_keep_the_neighbouring_point(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "first"},
                {"from_index": 3, "from_uid": rows[3]["uid"], "character_style": "second"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.insert_chunk(0)
            blank = client.post("/api/voices/A/style_timeline", json={
                "from_index": 3, "from_uid": rows[2]["uid"], "character_style": ""})
            self.assertEqual(200, blank.status_code, blank.text)
            self.assertEqual([rows[3]["uid"]],
                             [p["from_uid"] for p in blank.json()["style_timeline"]])
            replace = client.post("/api/voices/A/style_timeline", json={
                "from_index": 3, "from_uid": rows[2]["uid"], "character_style": "replaced"})
            self.assertEqual(200, replace.status_code, replace.text)
            points = replace.json()["style_timeline"]
            self.assertEqual({rows[2]["uid"], rows[3]["uid"]}, {p["from_uid"] for p in points})
            self.assertEqual("replaced", next(p["character_style"] for p in points
                                              if p["from_uid"] == rows[2]["uid"]))

    def test_delete_unknown_uid_never_falls_back_to_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "first"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            response = client.delete("/api/voices/A/style_timeline/0?from_uid=missing")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(config["A"]["style_timeline"], json.loads(path.read_text())["A"]["style_timeline"])
            self.assertEqual(config["A"]["style_timeline"], json.loads(path.read_text())["A"]["style_timeline"])

    def test_legacy_points_sharing_an_index_are_all_removed(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "character_style": "first"},
                {"from_index": 2, "character_style": "second"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            response = client.delete("/api/voices/A/style_timeline/2")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual([], response.json()["style_timeline"])

    def test_dedicated_post_refuses_out_of_range_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, _path, _stored, _rows = self.fixture(directory, stack)
            response = client.post("/api/voices/A/style_timeline", json={
                "from_index": 12, "from_uid": "u2", "character_style": "aged"})
            self.assertEqual(409, response.status_code, response.text)

    def test_sparse_batch_uses_full_book_after_anchor_moves_outside_batch(self):
        import contextlib
        import io
        import sys
        from tests.test_batch_style_timeline import get_engine, get_torch
        voices = style_config({"from_index": 4, "from_uid": "u4", "character_style": "aged"})
        seen = []
        def render(**kwargs):
            seen.extend(kwargs["instruct"])
            return [np.full(400, 0.1, dtype="float32") for _ in kwargs["text"]], 16000
        engine = get_engine()
        engine._init_local_custom = Mock(return_value=SimpleNamespace(generate_custom_voice=render))
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(sys.modules, {"torch": get_torch()}), \
                contextlib.redirect_stdout(io.StringIO()):
            manager = ProjectManager(directory)
            uids = ["u0", "u1", "u2", "u3", "fresh", "u4"]
            rows = [{"id": index, "uid": uid, "speaker": "A", "text": f"line {index}",
                     "instruct": "soft", "status": "pending"} for index, uid in enumerate(uids)]
            manager.save_chunks(rows)
            Path(manager.voice_config_path).write_text(json.dumps(voices))
            with patch.object(manager, "get_engine", return_value=engine):
                result = manager.generate_chunks_batch([2, 4], batch_size=4, batch_seed=0,
                                                       batch_group_by_type=False)
            self.assertEqual([], result["failed"])
        self.assertEqual(["young soft", "young soft"], seen[:2])

    def test_state_get_to_insert_to_apply_keeps_stable_identity(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            for index, row in enumerate(rows):
                row.pop("uid", None)
            manager.save_chunks(rows)
            before = Path(manager.chunks_path).read_bytes()
            state = client.get("/api/voices/A/state_timeline")
            self.assertEqual(200, state.status_code, state.text)
            materialized = json.loads(Path(manager.chunks_path).read_text())
            self.assertTrue(all(row.get("uid") for row in materialized))
            self.assertNotEqual(before, Path(manager.chunks_path).read_bytes())
            target_uid = materialized[2]["uid"]
            manager.insert_chunk(0)
            point = {"from_index": 3, "from_uid": target_uid, "version_id": "different"}
            response = client.post("/api/voices/A/version_timeline", json={"points": [point]})
            self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_text())["A"]["version_timeline"][0]
            self.assertEqual(target_uid, saved["from_uid"])
            self.assertEqual(3, saved["from_index"])
            self.assertEqual(3, resolve_timeline_anchors(
                json.loads(path.read_text()), json.loads(Path(manager.chunks_path).read_text())
            )["A"]["version_timeline"][0]["from_index"])

    def test_snapshot_reports_current_line_without_rewriting_stored_index(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            _root, manager, client, path, _stored, rows = self.fixture(directory, stack)
            config = {"A": {"type": "custom", "voice": "Ryan", "style_timeline": [
                {"from_index": 2, "from_uid": rows[2]["uid"], "character_style": "aged"}]},
                "B": {"type": "custom", "voice": "Ryan"}}
            path.write_text(json.dumps(config))
            manager.insert_chunk(0)
            snapshot = client.get("/api/voice_config/snapshot").json()
            point = snapshot["config"]["A"]["style_timeline"][0]
            self.assertEqual(2, point["from_index"])
            self.assertEqual(3, point["display_index"])
            self.assertEqual(2, json.loads(path.read_text())["A"]["style_timeline"][0]["from_index"])


if __name__ == "__main__":
    unittest.main()
