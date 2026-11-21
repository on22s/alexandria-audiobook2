"""Journal identity must stay coherent across selection, readers and exports."""
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as application
import book_state_transaction
from book_state_transaction import apply_book_input_selection
from chunk_status_journal import ChunkStatusJournal, get_chunk_status_journal_path
from export_publication import ExportSourceSnapshot
from fastapi import FastAPI
from fastapi.testclient import TestClient
import project_scope_files
from project import ProjectManager
from routers import editor, script
from script_selection_binding import (BINDING_NAME, capture_generation_selection,
    publish_generated_script)


class JournalIdentityCases(unittest.TestCase):
    SOURCE = b"A synthetic source used only for regression fixtures.\n"

    def make_published_book(self, root, *, source_bytes=None, book_id="source",
                            audio_path="line-0.wav"):
        root = Path(root)
        uploads = root / "uploads"
        uploads.mkdir()
        source = uploads / "source.txt"
        source.write_bytes(self.SOURCE if source_bytes is None else source_bytes)
        script_bytes = b'[{"speaker":"N","text":"Synthetic line."}]'
        (root / "annotated_script.json").write_bytes(script_bytes)
        (root / "voice_config.json").write_bytes(b"{}")
        (root / "state.json").write_text(json.dumps({
            "active_book_id": book_id, "book_generation": "generation-a",
            "input_file_path": str(source)}))
        selected = capture_generation_selection(str(root), str(source))
        state = json.loads((root / "state.json").read_text())
        state["script_generation_run_id"] = "f" * 32
        state["script_generation_base_sha256"] = __import__("hashlib").sha256(script_bytes).hexdigest()
        (root / "state.json").write_text(json.dumps(state))
        publish_generated_script(str(root), selected["book_token"], str(source),
            selected["source_sha256"], script_bytes, expected_run_id="f" * 32)
        import wave
        with wave.open(str(root / audio_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 80)
        rows = [{"id": 0, "uid": "line-0", "text": "Synthetic line.",
                 "speaker": "N", "status": "generating", "audio_path": None}]
        (root / "chunks.json").write_text(json.dumps(rows, indent=2))
        identity = {"active_book_id": book_id,
                    "book_generation": json.loads((root / "state.json").read_text())["book_generation"]}
        ChunkStatusJournal(root / "chunks.json", identity).apply_update(
            "line-0", {"status": "done", "audio_path": audio_path})
        return root, source, rows, identity

    def http_client(self, root, manager=None, state=None, *, raise_server_exceptions=True):
        app = FastAPI()
        app.include_router(editor.router)
        app.include_router(script.router)
        uploads = root / "uploads"
        patches = (
            patch.object(editor, "project_manager", manager or ProjectManager(str(root))),
            patch.object(editor, "SCRIPT_PATH", str(root / "annotated_script.json")),
            patch.object(editor, "DATA_DIR", str(root)),
            patch.object(editor, "process_state", state if state is not None else {}),
            patch.object(script, "UPLOADS_DIR", str(uploads)),
            patch.object(script, "DATA_DIR", str(root)),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return TestClient(app, raise_server_exceptions=raise_server_exceptions)

    def select(self, client, filename="source.txt"):
        return client.post("/api/uploads/select", json={"filename": filename})

    def state(self, root):
        return json.loads((root / "state.json").read_text())

    def export_state(self):
        return {name: {"running": False, "logs": [], "result": None}
                for name in ("audio", "audacity_export", "m4b_export", "chapter_export")}

    def deeply_nested_journal(self, root, depth=2000):
        path = Path(get_chunk_status_journal_path(root / "chunks.json"))
        nested = b"[" * depth + b"0" + b"]" * depth
        header, _ = path.read_bytes().split(b"\n", 1)
        path.write_bytes(header + b'\n{"type":"update","sequence":1,"uid":"line-0",'
            b'"changes":{"error":' + nested + b'}}\n')


class SelectionJournalIdentityTests(JournalIdentityCases):
    def test_same_bytes_upload_preserves_verified_binding_and_retained_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_published_book(tmp)
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertNotIn("script_selection_pending", self.state(root))
            saved = json.loads((root / "chunks.json").read_text())
            self.assertEqual([("line-0", "line-0.wav", "done")],
                [(row["uid"], row["audio_path"], row["status"]) for row in saved])
            self.assertEqual(saved, ProjectManager(str(root)).load_chunks())
            listed = client.get("/api/chunks")
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual(("line-0", "done"),
                (listed.json()[0]["uid"], listed.json()[0]["status"]))

    def test_changed_bytes_under_same_filename_never_reuse_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            source.write_bytes(b"Different bytes under the same filename.\n")
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(self.state(root)["script_selection_pending"])
            self.assertFalse((root / "chunks.json").exists())
            backups = list(root.glob("chunks.json.status.jsonl.stale-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(b"Different bytes under the same filename.\n", source.read_bytes())

    def test_retained_stale_pair_replays_the_saved_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            snapshot_before = (root / "chunks.json").read_bytes()
            journal_before = journal.read_bytes()
            state = self.state(root)
            state["book_generation"] = "foreign-generation"
            (root / "state.json").write_text(json.dumps(state))
            client = self.http_client(root)
            self.assertEqual(200, self.select(client).status_code)
            snapshot_backup = list(root.glob("chunks.json.stale-*"))
            journal_backup = list(root.glob(journal.name + ".stale-*"))
            self.assertEqual(1, len(snapshot_backup))
            self.assertEqual(1, len(journal_backup))
            self.assertEqual(snapshot_backup[0].name.split(".stale-")[1],
                             journal_backup[0].name.split(".stale-")[1])
            self.assertEqual(snapshot_before, snapshot_backup[0].read_bytes())
            self.assertEqual(journal_before, journal_backup[0].read_bytes())
            replayed = ChunkStatusJournal(root / "chunks.json", identity).get_rows_from_bytes(
                snapshot_before, journal_before)
            self.assertEqual("done", replayed[0]["status"])
            self.assertEqual("line-0.wav", replayed[0]["audio_path"])

    def test_transient_io_error_refuses_without_deleting_valid_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            before = {path: path.read_bytes() for path in (root / "state.json",
                root / "chunks.json", journal)}
            real_open = __import__("audio_take_history")._open_regular
            def fail_journal(path, *args, **kwargs):
                if Path(path).name == journal.name:
                    raise OSError(errno.EIO, "injected transient I/O error")
                return real_open(path, *args, **kwargs)
            client = self.http_client(root, raise_server_exceptions=False)
            with patch("project_scope_files._open_regular", side_effect=fail_journal):
                response = self.select(client)
            self.assertEqual(500, response.status_code)
            self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_missing_snapshot_retains_orphan_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            journal_before = journal.read_bytes()
            (root / "chunks.json").unlink()
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse(journal.exists())
            backups = list(root.glob(journal.name + ".stale-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(journal_before, backups[0].read_bytes())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs unavailable")
    def test_fifo_snapshot_and_journal_refuse_promptly_and_release_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            saved = (root / "chunks.json").read_bytes(), journal.read_bytes()
            for path in (root / "chunks.json", journal):
                path.unlink()
                os.mkfifo(path)
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(409, response.status_code, response.text)
            for path in (root / "chunks.json", journal):
                path.unlink()
            (root / "chunks.json").write_bytes(saved[0])
            journal.write_bytes(saved[1])
            apply_book_input_selection(str(root), str(source), "source")
            self.assertTrue(self.state(root)["book_generation"])


class StaleJournalReaderTests(JournalIdentityCases):
    def test_startup_lifespan_survives_deeply_nested_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            self.deeply_nested_journal(root)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            before = journal.read_bytes()
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", ProjectManager(tmp)):
                application._recover_startup_state()
            self.assertEqual(before, journal.read_bytes())

    def test_deeply_nested_journal_reports_actionable_conflict_on_all_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            self.deeply_nested_journal(root)
            before = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
            state = self.export_state()
            client = self.http_client(root, state=state)
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [
                    client.get("/api/editor/integrity"),
                    client.post("/api/merge"),
                    client.post("/api/export_audacity"),
                    client.post("/api/merge_m4b", json={}),
                    client.post("/api/export_chapters", json={}),
                    client.get("/api/export_chapters/preview"),
                ]
                scheduled.assert_not_called()
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL,
                                 response.json()["detail"])
            for name in ("audacity_export", "m4b_export", "chapter_export"):
                self.assertIsNone(state[name]["result"])
            for path, raw in before.items():
                if path.exists():
                    self.assertEqual(raw, path.read_bytes())

    def test_export_preflight_is_read_only_and_distinguishes_absent_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            journal.unlink()
            (root / "chunks.json").write_bytes(b"{corrupt saved rows")
            before = (root / "chunks.json").read_bytes()
            state = self.export_state()
            client = self.http_client(root, state=state)
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                response = client.post("/api/export_audacity")
                scheduled.assert_not_called()
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, (root / "chunks.json").read_bytes())
            (root / "chunks.json").unlink()
            state["audacity_export"]["running"] = False
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                allowed = client.post("/api/export_audacity")
                scheduled.assert_called_once()
            self.assertEqual(200, allowed.status_code, allowed.text)
            self.assertFalse((root / "chunks.json").exists())

    def test_export_capture_enforces_published_selection_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            prior = root / "audacity_export.zip"
            prior.write_bytes(b"previous complete Audacity export")
            client = self.http_client(root)
            source.write_bytes(b"Different source bytes.")
            self.assertEqual(200, self.select(client).status_code)
            manager = ProjectManager(str(root))
            with self.assertRaisesRegex(ValueError, "no published script"):
                ExportSourceSnapshot(manager)
            with self.assertRaisesRegex(ValueError, "no published script"):
                manager.export_audacity()
            self.assertEqual(b"previous complete Audacity export", prior.read_bytes())

    def test_merge_m4b_preflights_journal_before_optional_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            state_data = self.state(root)
            state_data["book_generation"] = "foreign"
            (root / "state.json").write_text(json.dumps(state_data))
            (root / "voice_config.json").write_text(json.dumps({"N": {"ready": False}}))
            before = journal.read_bytes()
            state = self.export_state()
            client = self.http_client(root, state=state)
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                response = client.post("/api/merge_m4b", json={"require_ready": True})
                scheduled.assert_not_called()
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL, response.json()["detail"])
            self.assertEqual(before, journal.read_bytes())


class JournalRecoveryBoundaryTests(JournalIdentityCases):
    def test_same_source_torn_replay_retains_the_exact_pair_under_one_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, identity = self.make_published_book(tmp)
            snapshot = root / "chunks.json"
            journal = Path(get_chunk_status_journal_path(snapshot))
            snapshot_before = snapshot.read_bytes()
            journal_before = journal.read_bytes()
            journal.write_bytes(journal_before + b'{"type":"update","sequence":2,')
            torn_before = journal.read_bytes()
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(200, response.status_code, response.text)
            replayed = json.loads(snapshot.read_text())
            self.assertEqual([("line-0", "line-0.wav", "done")],
                [(row["uid"], row["audio_path"], row["status"]) for row in replayed])
            self.assertFalse(journal.exists())
            snapshot_copies = list(root.glob("chunks.json.torn-*"))
            journal_copies = list(root.glob(journal.name + ".torn-*"))
            self.assertEqual(1, len(snapshot_copies))
            self.assertEqual(1, len(journal_copies))
            self.assertEqual(snapshot_copies[0].name.split(".torn-")[1],
                             journal_copies[0].name.split(".torn-")[1])
            self.assertEqual(snapshot_before, snapshot_copies[0].read_bytes())
            self.assertEqual(torn_before, journal_copies[0].read_bytes())
            restored = ChunkStatusJournal(snapshot, identity).get_rows_from_bytes(
                snapshot_before, torn_before)
            self.assertEqual(("done", "line-0.wav"),
                             (restored[0]["status"], restored[0]["audio_path"]))

    def test_changed_source_retires_a_standalone_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            snapshot_before = (root / "chunks.json").read_bytes()
            journal.unlink()
            source.write_bytes(b"Different source bytes.")
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse((root / "chunks.json").exists())
            backups = list(root.glob("chunks.json.stale-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(snapshot_before, backups[0].read_bytes())

    def test_transient_source_io_error_keeps_state_unchanged_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            before = (root / "state.json").read_bytes()
            client = self.http_client(root, raise_server_exceptions=False)
            real = book_state_transaction._selection_source_digest
            calls = {"count": 0}
            def flaky(path, data_dir):
                calls["count"] += 1
                if calls["count"] == 1:
                    raise OSError(errno.EIO, "injected transient source I/O error")
                return real(path, data_dir)
            with patch.object(book_state_transaction, "_selection_source_digest", side_effect=flaky):
                response = self.select(client)
            self.assertEqual(500, response.status_code, response.text)
            self.assertEqual(before, (root / "state.json").read_bytes())
            retry = self.select(client)
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertNotIn("script_selection_pending", self.state(root))

    def test_transient_script_io_error_keeps_state_unchanged_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            before = (root / "state.json").read_bytes()
            binding_before = (root / BINDING_NAME).read_bytes()
            client = self.http_client(root, raise_server_exceptions=False)
            real = project_scope_files.capture_scope_file
            failed = {"count": 0}
            def flaky(root_path, relative, **kwargs):
                if relative == "annotated_script.json" and not failed["count"]:
                    failed["count"] = 1
                    raise OSError(errno.EIO, "injected transient script I/O error")
                return real(root_path, relative, **kwargs)
            with patch.object(project_scope_files, "capture_scope_file", side_effect=flaky):
                response = self.select(client)
            self.assertEqual(500, response.status_code, response.text)
            self.assertEqual(before, (root / "state.json").read_bytes())
            self.assertEqual(binding_before, (root / BINDING_NAME).read_bytes())
            retry = self.select(client)
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertNotIn("script_selection_pending", self.state(root))

    def test_integrity_and_merge_are_fenced_after_a_source_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            client = self.http_client(root, state=self.export_state())
            source.write_bytes(b"Different source bytes.")
            self.assertEqual(200, self.select(client).status_code)
            self.assertTrue(self.state(root)["script_selection_pending"])
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [client.get("/api/editor/integrity"), client.post("/api/merge")]
                scheduled.assert_not_called()
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
                self.assertIn("no published script", response.json()["detail"])

    def test_orphan_journal_is_an_export_conflict_not_an_absent_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            before = journal.read_bytes()
            (root / "chunks.json").unlink()
            state = self.export_state()
            client = self.http_client(root, state=state)
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                response = client.post("/api/export_audacity")
                scheduled.assert_not_called()
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL, response.json()["detail"])
            self.assertEqual(before, journal.read_bytes())
            self.assertFalse((root / "chunks.json").exists())

    def test_export_preflight_does_not_recover_a_pending_book_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            before = {p.name: p.read_bytes() for p in root.iterdir()
                      if p.is_file() and not p.name.endswith(".lock")}
            state = self.export_state()
            client = self.http_client(root, state=state)
            with patch.object(book_state_transaction, "recover_book_state_locked",
                    side_effect=AssertionError("recovery must not run during preflight")):
                with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                    response = client.post("/api/export_audacity")
                    scheduled.assert_called_once()
            self.assertEqual(200, response.status_code, response.text)
            for name, raw in before.items():
                self.assertEqual(raw, (root / name).read_bytes(), name)

    def test_nonregular_snapshot_is_refused_without_regenerating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            journal.unlink()
            snapshot = root / "chunks.json"
            snapshot.unlink()
            snapshot.mkdir()
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", ProjectManager(tmp)):
                application._recover_startup_state()
            self.assertTrue(snapshot.is_dir())
            client = self.http_client(root, state=self.export_state())
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                response = client.post("/api/export_audacity")
                scheduled.assert_not_called()
            self.assertEqual(409, response.status_code, response.text)
            self.assertTrue(snapshot.is_dir())

    def test_decodable_deeply_nested_record_is_refused_before_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            self.deeply_nested_journal(root, depth=500)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            raw = journal.read_bytes()
            json.loads(raw.split(b"\n")[1])
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", ProjectManager(tmp)):
                application._recover_startup_state()
            self.assertEqual(raw, journal.read_bytes())
            client = self.http_client(root, state=self.export_state())
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                response = client.post("/api/export_audacity")
                scheduled.assert_not_called()
            self.assertEqual(409, response.status_code, response.text)


if __name__ == "__main__":
    unittest.main()
