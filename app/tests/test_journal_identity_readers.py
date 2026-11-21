"""Journal identity must stay coherent across selection, readers and exports."""
import asyncio
import errno
import json
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Event
import shutil
import tempfile
import unittest
import uuid
from unittest.mock import patch

import app as application
import book_state_transaction
import core
from book_state_transaction import apply_book_input_selection
from chunk_status_journal import ChunkStatusJournal, get_chunk_status_journal_path
from export_publication import ExportSourceSnapshot
from fastapi import FastAPI
from fastapi.testclient import TestClient
import project_scope_files
from project import ProjectManager
from routers import editor, script, scripts_library
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

    def full_client(self, root, state=None):
        app = FastAPI()
        app.include_router(editor.router)
        app.include_router(script.router)
        app.include_router(scripts_library.router)
        values = {
            'DATA_DIR': str(root), 'SCRIPT_PATH': str(root / 'annotated_script.json'),
            'VOICE_CONFIG_PATH': str(root / 'voice_config.json'),
            'SCRIPTS_DIR': str(root / 'scripts'), 'CHUNKS_PATH': str(root / 'chunks.json'),
            'UPLOADS_DIR': str(root / 'uploads'),
            'AUDIOBOOK_PATH': str(root / 'audiobook.mp3'),
            'M4B_PATH': str(root / 'audiobook.m4b'),
        }
        patches = []
        for module in (application, core, script, editor, scripts_library):
            for key, value in values.items():
                if hasattr(module, key):
                    patches.append(patch.object(module, key, value))
        manager = ProjectManager(str(root))
        for module in (core, editor, scripts_library):
            if hasattr(module, 'project_manager'):
                patches.append(patch.object(module, 'project_manager', manager))
        patches.append(patch.object(editor, 'process_state',
                        state if state is not None else self.export_state()))
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return TestClient(app, raise_server_exceptions=False)

    def two_books(self, tmp):
        root, book_b = Path(tmp) / "a", Path(tmp) / "b"
        root.mkdir()
        book_b.mkdir()
        self.make_published_book(root, book_id="source")
        self.make_published_book(book_b, book_id="other",
                                 source_bytes=b"A different published source.\n")
        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "other.json").write_bytes((book_b / "annotated_script.json").read_bytes())
        (scripts / "other.voice_config.json").write_bytes(
            (book_b / "voice_config.json").read_bytes())
        return root, book_b

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

    def write_pending_journal(self, root):
        journal = root / ".active_book_transaction.json"
        journal.write_text(json.dumps({"version": 4, "phase": "pending"}))
        return journal

    def switch_to_published_b(self, root, book_b):
        for name in ("state.json", "annotated_script.json", "voice_config.json",
                     BINDING_NAME, "chunks.json"):
            shutil.copyfile(book_b / name, root / name)
        journal_a = Path(get_chunk_status_journal_path(root / "chunks.json"))
        journal_b = Path(get_chunk_status_journal_path(book_b / "chunks.json"))
        if journal_a.exists():
            journal_a.unlink()
        if journal_b.exists():
            shutil.copyfile(journal_b, journal_a)
        state = self.state(root)
        state["book_generation"] = uuid.uuid4().hex
        (root / "state.json").write_text(json.dumps(state))


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


class JournalReadBoundaryFollowupTests(JournalIdentityCases):
    def test_deeply_nested_standalone_snapshot_is_a_controlled_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            Path(get_chunk_status_journal_path(root / "chunks.json")).unlink()
            snapshot = root / "chunks.json"
            nested = b"[" * 500 + b"0" + b"]" * 500
            raw = (b'[{"id":0,"uid":"line-0","status":"done",'
                   b'"audio_path":"line-0.wav","error":' + nested + b'}]')
            snapshot.write_bytes(raw)
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", ProjectManager(tmp)):
                application._recover_startup_state()
            self.assertEqual(raw, snapshot.read_bytes())
            self.assertFalse(list(root.glob("chunks.json.corrupt*")))
            client = self.http_client(root, state=self.export_state())
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [client.get("/api/chunks"),
                             client.get("/api/editor/integrity"),
                             client.post("/api/merge")]
                scheduled.assert_not_called()
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(raw, snapshot.read_bytes())

    def test_pending_transaction_journal_at_entry_refuses_all_export_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            prior = root / "audiobook.m4b"
            prior.write_bytes(b"previous complete M4B export")
            before = {p.name: p.read_bytes() for p in root.iterdir()
                      if p.is_file() and p.name != prior.name}
            journal = self.write_pending_journal(root)
            journal_before = journal.read_bytes()
            client = self.http_client(root, state=self.export_state())
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [client.post("/api/merge_m4b", json={}),
                             client.get("/api/export_chapters/preview")]
                scheduled.assert_not_called()
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(journal_before, journal.read_bytes())
            self.assertEqual(b"previous complete M4B export", prior.read_bytes())
            for name, raw in before.items():
                self.assertEqual(raw, (root / name).read_bytes(), name)

    def test_pending_transaction_journal_appearing_late_refuses_before_schedule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            prior = root / "audiobook.m4b"
            prior.write_bytes(b"previous complete M4B export")
            before = {p.name: p.read_bytes() for p in root.iterdir()
                      if p.is_file() and p.name != prior.name}
            client = self.http_client(root, state=self.export_state())
            def publish_journal(*args, **kwargs):
                self.write_pending_journal(root)
                return []
            with patch.object(editor, "get_unready_export_speakers",
                    side_effect=publish_journal), \
                 patch.object(editor, "schedule_claimed_background_task") as scheduled:
                response = client.post("/api/merge_m4b", json={"require_ready": True})
                scheduled.assert_not_called()
            self.assertEqual(409, response.status_code, response.text)
            journal = root / ".active_book_transaction.json"
            self.assertTrue(journal.exists())
            self.assertEqual(b"previous complete M4B export", prior.read_bytes())
            for name, raw in before.items():
                self.assertEqual(raw, (root / name).read_bytes(), name)

    def test_source_lstat_eio_aborts_selection_and_duplicate_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            real_absent = project_scope_files._confined_absent
            def flaky(path):
                if path.name == source.name:
                    raise OSError(errno.EIO, "injected lstat I/O error")
                return real_absent(path)
            client = self.http_client(root, raise_server_exceptions=False)
            with patch.object(project_scope_files, "_confined_absent", side_effect=flaky):
                response = self.select(client)
            self.assertEqual(500, response.status_code, response.text)
            with patch.object(project_scope_files, "_confined_absent", side_effect=flaky):
                with self.assertRaises(OSError):
                    apply_book_input_selection(str(root), str(source), "source")
            for name, raw in before.items():
                self.assertEqual(raw, (root / name).read_bytes(), name)
            retry = self.select(client)
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertNotIn("script_selection_pending", self.state(root))

    def test_verified_same_source_backfills_legacy_uidless_done_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            Path(get_chunk_status_journal_path(root / "chunks.json")).unlink()
            (root / "chunks.json").write_text(json.dumps([{
                "id": 0, "text": "Synthetic line.", "speaker": "N",
                "status": "done", "audio_path": "line-0.wav"}]))
            client = self.http_client(root)
            response = self.select(client)
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse(list(root.glob("chunks.json.stale-*")))
            saved = json.loads((root / "chunks.json").read_text())
            self.assertEqual(1, len(saved))
            self.assertEqual(12, len(saved[0]["uid"]))
            self.assertEqual("done", saved[0]["status"])
            self.assertEqual("line-0.wav", saved[0]["audio_path"])

    def test_dangling_journal_link_refuses_editor_and_startup_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            snapshot = root / "chunks.json"
            before = snapshot.read_bytes()
            journal = Path(get_chunk_status_journal_path(snapshot))
            journal.unlink()
            journal.symlink_to(root / "missing-target.jsonl")
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", ProjectManager(tmp)):
                application._recover_startup_state()
            self.assertEqual(before, snapshot.read_bytes())
            self.assertTrue(journal.is_symlink())
            client = self.http_client(root, state=self.export_state())
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [client.get("/api/chunks"),
                             client.get("/api/editor/integrity"),
                             client.post("/api/merge")]
                scheduled.assert_not_called()
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, snapshot.read_bytes())


class ExportAdmissionIdentityTests(JournalIdentityCases):
    def test_completed_switch_after_admission_refuses_export_stages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "a"
            book_b = Path(tmp) / "b"
            root.mkdir()
            book_b.mkdir()
            self.make_published_book(root, book_id="source")
            self.make_published_book(book_b, book_id="other",
                source_bytes=b"A different published source.\n")
            (root / "voice_config.json").write_text(json.dumps({"N": {"ready": False}}))
            (book_b / "voice_config.json").write_text(json.dumps({"N": {"ready": True}}))
            prior = root / "audiobook.m4b"
            prior.write_bytes(b"previous complete M4B export")
            untouched = root / "audacity_export.zip"
            untouched.write_bytes(b"previous complete Audacity export")

            def publish_b(*args, **kwargs):
                self.switch_to_published_b(root, book_b)
                return []

            client = self.http_client(root, state=self.export_state())
            reservations = ("schedule_claimed_background_task", "reserve_background_task",
                            "register_claimed_background_task")
            with patch.object(editor, "get_unready_export_speakers", side_effect=publish_b):
                with ExitStack() as stack:
                    reserved = [stack.enter_context(patch.object(editor, name))
                                for name in reservations]
                    m4b = client.post("/api/merge_m4b", json={"require_ready": True})
                    preview = client.get("/api/export_chapters/preview",
                                         params={"require_ready": "true"})
            for response in (m4b, preview):
                self.assertEqual(409, response.status_code, response.text)
            for mock in reserved:
                mock.assert_not_called()
            self.assertEqual(b"previous complete M4B export", prior.read_bytes())
            self.assertEqual(b"previous complete Audacity export", untouched.read_bytes())
            self.assertEqual([], list(root.glob("*.pending.*")))
            self.assertEqual([], list(root.glob("chapter_exports/*")))

    def test_lstat_eio_on_every_confined_artifact_aborts_both_entry_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            journal = Path(get_chunk_status_journal_path(root / "chunks.json"))
            before = {p.name: p.read_bytes() for p in root.iterdir() if p.is_file()}
            real_absent = project_scope_files._confined_absent
            for name in ("source.txt", "annotated_script.json", "chunks.json", journal.name):
                with self.subTest(artifact=name):
                    def flaky(path, target=name):
                        if path.name == target:
                            raise OSError(errno.EIO, "injected lstat I/O error")
                        return real_absent(path)
                    client = self.http_client(root, raise_server_exceptions=False)
                    with patch.object(project_scope_files, "_confined_absent", side_effect=flaky):
                        response = self.select(client)
                    self.assertEqual(500, response.status_code, response.text)
                    with patch.object(project_scope_files, "_confined_absent", side_effect=flaky):
                        with self.assertRaises(OSError):
                            apply_book_input_selection(str(root), str(source), "source")
                    for path_name, raw in before.items():
                        self.assertEqual(raw, (root / path_name).read_bytes(), path_name)
            retry = self.select(client)
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertNotIn("script_selection_pending", self.state(root))

    def test_decoder_time_depth_is_a_controlled_conflict_with_recovery_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            Path(get_chunk_status_journal_path(root / "chunks.json")).unlink()
            snapshot = root / "chunks.json"
            raw = b"[" * 2000 + b"0" + b"]" * 2000
            snapshot.write_bytes(raw)
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", ProjectManager(tmp)):
                application._recover_startup_state()
            self.assertEqual(raw, snapshot.read_bytes())
            self.assertFalse(list(root.glob("chunks.json.corrupt*")))
            client = self.http_client(root, state=self.export_state())
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [client.get("/api/chunks"),
                             client.get("/api/editor/integrity"),
                             client.post("/api/merge")]
                scheduled.assert_not_called()
            for response in responses:
                self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(raw, snapshot.read_bytes())
            snapshot.write_bytes(b"{corrupt saved rows")
            regenerated = ProjectManager(str(root)).load_chunks()
            self.assertEqual(1, len(regenerated))
            self.assertEqual("pending", regenerated[0]["status"])
            self.assertEqual(1, len(list(root.glob("chunks.json.corrupt*"))))


class ExportBoundaryFinalTests(JournalIdentityCases):
    def test_export_and_editor_refuse_unreadable_chunk_files_without_dropping_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _rows, _identity = self.make_published_book(tmp)
            snapshot = root / "chunks.json"
            journal = Path(get_chunk_status_journal_path(snapshot))
            watched = (snapshot, journal, root / "line-0.wav", root / "state.json",
                       root / BINDING_NAME, root / "annotated_script.json", source)
            before = {path: path.read_bytes() for path in watched}
            manager = ProjectManager(str(root))
            client = self.http_client(root, manager=manager, state=self.export_state())
            preview_url = "/api/export_chapters/preview"
            preview_before = client.get(preview_url)
            self.assertEqual(200, preview_before.status_code, preview_before.text)
            self.assertEqual(1, len(preview_before.json()["chapters"]))
            listed = client.get("/api/chunks")
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual("done", listed.json()[0]["status"])

            real_lstat, real_path_lstat = os.lstat, Path.lstat
            for failed_path in (snapshot, journal):
                with self.subTest(path=failed_path.name):
                    def refuse_faulted_path(path):
                        if os.fspath(path) == str(failed_path):
                            raise OSError(errno.EIO, "injected chunk lookup failure")

                    def fail_lookup(path, *args, **kwargs):
                        refuse_faulted_path(path)
                        return real_lstat(path, *args, **kwargs)

                    def fail_path_lookup(path):
                        refuse_faulted_path(path)
                        return real_path_lstat(path)

                    with patch.object(os, "lstat", side_effect=fail_lookup), \
                         patch.object(Path, "lstat", new=fail_path_lookup), \
                         patch.object(editor, "schedule_claimed_background_task") as scheduled, \
                         patch.object(application, "project_manager", manager):
                        responses = [
                            client.get(preview_url),
                            client.post("/api/export_audacity"),
                            client.get("/api/chunks"),
                        ]
                        self.assertEqual(0, application.reset_stuck_chunks())
                        scheduled.assert_not_called()
                    for response in responses:
                        self.assertEqual(409, response.status_code, response.text)
                        self.assertNotIn(tmp, response.text)
                    self.assertEqual(before, {path: path.read_bytes() for path in watched})

            preview_after = client.get(preview_url)
            self.assertEqual(200, preview_after.status_code, preview_after.text)
            self.assertEqual(preview_before.json(), preview_after.json())

    def test_duplicate_upload_fresh_verification_storage_error_aborts_route(self):
        for number in (errno.EIO, getattr(errno, "ESTALE", errno.EINVAL)):
            with self.subTest(errno=number), tempfile.TemporaryDirectory() as tmp:
                root, source, _rows, _identity = self.make_published_book(tmp)
                watched = [root / "state.json", root / "chunks.json",
                           Path(get_chunk_status_journal_path(root / "chunks.json")),
                           root / BINDING_NAME, root / "annotated_script.json",
                           root / "line-0.wav", source]
                before = {path: path.read_bytes() for path in watched}
                client = self.http_client(root, raise_server_exceptions=False)
                real_capture = script.capture_scope_file

                def fail_capture(root_path, relative, **kwargs):
                    if relative == source.name:
                        raise OSError(number, "injected fresh verification failure")
                    return real_capture(root_path, relative, **kwargs)

                with patch.object(script, "capture_scope_file", side_effect=fail_capture):
                    response = client.post("/api/upload",
                        files={"file": ("again.txt", self.SOURCE, "text/plain")})
                self.assertEqual(500, response.status_code, response.text)
                self.assertEqual(before, {path: path.read_bytes() for path in watched})
                self.assertEqual([source], sorted(source.parent.iterdir()))
                retry = client.post("/api/upload",
                    files={"file": ("again.txt", self.SOURCE, "text/plain")})
                self.assertEqual(200, retry.status_code, retry.text)
                self.assertTrue(retry.json().get("reused"))

    def test_readiness_validates_admission_before_reporting_other_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, book_b = self.two_books(tmp)
            (root / "voice_config.json").write_text(json.dumps({"N": {"ready": True}}))
            (book_b / "voice_config.json").write_text(json.dumps({"N": {"ready": False}}))
            routes = (
                ("post", "/api/merge_m4b", {"json": {"require_ready": True}}),
                ("post", "/api/export_chapters", {"json": {"require_ready": True}}),
                ("get", "/api/export_chapters/preview", {"params": {"require_ready": "true"}}),
            )
            for method, path, options in routes:
                with self.subTest(route=path):
                    def switch_and_unready(*args, **kwargs):
                        self.switch_to_published_b(root, book_b)
                        return ["N"]
                    client = self.http_client(root, state=self.export_state())
                    with patch.object(editor, "get_unready_export_speakers",
                            side_effect=switch_and_unready):
                        response = getattr(client, method)(path, **options)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL,
                                     response.json()["detail"])

    def test_scheduler_boundary_switch_refuses_all_three_exports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, book_b = self.two_books(tmp)
            real_claim = core.claim_gpu_task
            def switch_at_claim(*args, **kwargs):
                self.switch_to_published_b(root, book_b)
                return real_claim(*args, **kwargs)
            routes = (
                ("post", "/api/export_audacity", {}),
                ("post", "/api/merge_m4b", {"json": {}}),
                ("post", "/api/export_chapters", {"json": {}}),
            )
            for method, path, options in routes:
                with self.subTest(route=path):
                    before_claims = set(core._task_claims)
                    client = self.http_client(root, state=self.export_state())
                    with patch.object(core, "claim_gpu_task", side_effect=switch_at_claim), \
                         patch.object(core, "register_claimed_background_task") as registered:
                        response = getattr(client, method)(path, **options)
                    self.assertEqual(409, response.status_code, response.text)
                    registered.assert_not_called()
                    self.assertEqual(before_claims, set(core._task_claims))

    def test_preview_guards_identity_after_consuming_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, book_b = self.two_books(tmp)
            client = self.http_client(root, state=self.export_state())
            real_preview = editor.project_manager.preview_chapter_filenames
            def switch_during_preview(*args, **kwargs):
                self.switch_to_published_b(root, book_b)
                return real_preview(*args, **kwargs)
            with patch.object(editor.project_manager, "preview_chapter_filenames",
                    side_effect=switch_during_preview):
                response = client.get("/api/export_chapters/preview")
            self.assertEqual(409, response.status_code, response.text)


class ExportLateBoundaryTests(JournalIdentityCases):
    def test_real_published_load_at_reservation_boundary_returns_409(self):
        routes = (("/api/export_audacity", {}, True),
                  ("/api/merge_m4b", {}, False),
                  ("/api/export_chapters", {"chapters": [0]}, False))
        for path, payload, at_require in routes:
            with self.subTest(route=path), tempfile.TemporaryDirectory() as tmp:
                root, _book_b = self.two_books(tmp)
                client = self.full_client(root, state=self.export_state())
                before_claims = set(core._task_claims)
                real_require = editor._require_export_chunks
                real_ready = editor.validate_export_readiness
                def do_load():
                    result = client.post("/api/scripts/load", json={"name": "other"})
                    self.assertEqual(200, result.status_code, result.text)
                if at_require:
                    def after_require():
                        value = real_require()
                        do_load()
                        return value
                    hook = patch.object(editor, "_require_export_chunks", after_require)
                else:
                    async def after_ready(*args, **kwargs):
                        await real_ready(*args, **kwargs)
                        do_load()
                    hook = patch.object(editor, "validate_export_readiness", after_ready)
                with hook, patch.object(core, "register_claimed_background_task") as registered:
                    response = client.post(path, json=payload)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL,
                                 response.json()["detail"])
                registered.assert_not_called()
                self.assertEqual(before_claims, set(core._task_claims))

    def test_late_pending_journal_at_admission_returns_stable_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            client = self.full_client(root, state=self.export_state())
            journal = root / ".active_book_transaction.json"
            raw = b'{"version":4,"phase":"pending"}'
            async def ready_then_pending(*args, **kwargs):
                journal.write_bytes(raw)
            with patch.object(editor, "validate_export_readiness", ready_then_pending), \
                 patch.object(core, "register_claimed_background_task") as registered:
                preview = client.get("/api/export_chapters/preview")
                started = client.post("/api/export_chapters", json={"chapters": [0]})
                registered.assert_not_called()
            real_require = editor._require_export_chunks
            def require_then_pending():
                value = real_require()
                journal.write_bytes(raw)
                return value
            with patch.object(editor, "_require_export_chunks", require_then_pending), \
                 patch.object(core, "register_claimed_background_task") as registered:
                audacity = client.post("/api/export_audacity")
                registered.assert_not_called()
            for response in (preview, started, audacity):
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL,
                                 response.json()["detail"])
            self.assertEqual(raw, journal.read_bytes())

    def test_reservation_and_load_lock_order_do_not_deadlock(self):
        """An export and a saved-book load contend without blocking or leaking a claim."""
        with tempfile.TemporaryDirectory() as tmp:
            root, _book_b = self.two_books(tmp)
            client = self.full_client(root, state=self.export_state())
            before_claims = set(core._task_claims)
            real_schedule = editor.schedule_claimed_background_task
            real_ensure = scripts_library.ensure_book_state
            at_schedule, at_book, allow = Event(), Event(), Event()
            def hold_before_claim(*args, **kwargs):
                at_schedule.set()
                self.assertTrue(allow.wait(3))
                return real_schedule(*args, **kwargs)
            @contextmanager
            def observe_order(*args, **kwargs):
                # On integrated main the load releases the GPU mutex before
                # waiting on book admission, so only the book wait is observed.
                at_book.set()
                with real_ensure(*args, **kwargs) as admitted:
                    yield admitted
            with ThreadPoolExecutor(2) as pool, \
                 patch.object(editor, "schedule_claimed_background_task", hold_before_claim), \
                 patch.object(scripts_library, "ensure_book_state", observe_order):
                exporting = pool.submit(client.post, "/api/export_audacity", json={})
                self.assertTrue(at_schedule.wait(3))
                switching = pool.submit(client.post, "/api/scripts/load",
                                        json={"name": "other"})
                self.assertTrue(at_book.wait(3))
                allow.set()
                switch_result = switching.result(timeout=5)
                export_result = exporting.result(timeout=5)
            self.assertIn(switch_result.status_code, (200, 409), switch_result.text)
            # Integrated main lets the load win and cleanly refuses the export
            # ("Task admission or startup recovery is in progress"); the
            # pre-integration tree instead let the export win. Both are valid
            # mutual-exclusion outcomes - neither deadlocks nor leaks a claim.
            self.assertIn(export_result.status_code, (200, 400, 409), export_result.text)
            self.assertEqual(before_claims, set(core._task_claims))


if __name__ == "__main__":
    unittest.main()


class StorageAdmissionBoundaryTests(JournalIdentityCases):
    @contextmanager
    def refuse_version_read(self, path):
        read_stat, read_lstat, read_path_lstat = os.stat, os.lstat, Path.lstat

        def fail_lookup(read):
            def lookup(candidate, *args, **kwargs):
                if not isinstance(candidate, int) and os.fspath(candidate) == str(path):
                    raise OSError(errno.EIO, "injected version lookup failure")
                return read(candidate, *args, **kwargs)
            return lookup

        with patch.object(os, "stat", side_effect=fail_lookup(read_stat)), \
             patch.object(os, "lstat", side_effect=fail_lookup(read_lstat)), \
             patch.object(Path, "lstat", new=fail_lookup(read_path_lstat)):
            yield

    def test_selection_state_read_failure_preserves_published_book_and_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            state = root / "state.json"
            snapshot = root / "chunks.json"
            journal = Path(get_chunk_status_journal_path(snapshot))
            watched = (state, snapshot, journal, root / BINDING_NAME, root / "line-0.wav")
            before = {path: path.read_bytes() for path in watched}
            client = self.http_client(root, raise_server_exceptions=False)
            import builtins
            faults = []

            def fail_read(read):
                def open_file(path, *args, **kwargs):
                    if (not faults and not isinstance(path, int)
                            and os.fspath(path) == str(state)):
                        faults.append(path)
                        raise OSError(errno.EIO, "injected selection state read failure")
                    return read(path, *args, **kwargs)
                return open_file

            with patch.object(builtins, "open", side_effect=fail_read(builtins.open)), \
                 patch.object(os, "open", side_effect=fail_read(os.open)):
                response = self.select(client)
            self.assertTrue(faults)
            self.assertEqual(500, response.status_code, response.text)
            self.assertEqual(before, {path: path.read_bytes() for path in watched})
            retry = self.select(client)
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertNotIn("script_selection_pending", self.state(root))
            row, = client.get("/api/chunks").json()
            self.assertEqual(("done", "line-0.wav"), (row["status"], row["audio_path"]))

    def test_unreadable_pending_marker_refuses_fresh_and_reused_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            client = self.http_client(root, state=self.export_state())
            snapshot = root / "chunks.json"
            journal = Path(get_chunk_status_journal_path(snapshot))
            with book_state_transaction.ensure_book_state(root, recover=False):
                pending = self.write_pending_journal(root)
                with self.refuse_version_read(pending), self.assertRaises(OSError):
                    with book_state_transaction.reuse_book_state(root, recover=False):
                        self.fail("unreadable transaction marker authorized reuse")
            watched = (pending, snapshot, journal, root / "state.json", root / "line-0.wav")
            before = {path: path.read_bytes() for path in watched}
            with self.refuse_version_read(pending), \
                 patch.object(editor, "schedule_claimed_background_task") as scheduled:
                preview = client.get("/api/export_chapters/preview")
                start = client.post("/api/export_audacity")
                scheduled.assert_not_called()
            for response in (preview, start):
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL, response.json()["detail"])
            self.assertEqual(before, {path: path.read_bytes() for path in watched})
            pending.unlink()  # Finish the synthetic transaction before retrying.
            retry = client.get("/api/export_chapters/preview")
            self.assertEqual(200, retry.status_code, retry.text)
            self.assertEqual(1, len(retry.json()["chapters"]))

    def test_unknown_snapshot_version_never_authorizes_cached_legacy_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            snapshot = root / "chunks.json"
            journal = Path(get_chunk_status_journal_path(snapshot))
            manager = ProjectManager(str(root))
            rows = manager.load_chunks()
            journal.unlink()
            snapshot.write_text(json.dumps(rows), encoding="utf-8")
            client = self.http_client(root, manager=manager)
            self.assertEqual("line-0", client.get("/api/chunks").json()[0]["uid"])

            legacy = dict(rows[0])
            legacy.pop("uid")
            with self.refuse_version_read(snapshot):
                warm = client.get("/api/chunks")
                snapshot.write_text(json.dumps([legacy]), encoding="utf-8")
                replaced = client.get("/api/chunks")
            for response in (warm, replaced):
                self.assertEqual(409, response.status_code, response.text)
            self.assertEqual([legacy], json.loads(snapshot.read_bytes()))
            retry = client.get("/api/chunks")
            self.assertEqual(200, retry.status_code, retry.text)
            repaired, = retry.json()
            self.assertTrue(repaired["uid"])
            self.assertEqual(("done", "line-0.wav"), (repaired["status"], repaired["audio_path"]))
            self.assertEqual(repaired["uid"], client.get("/api/chunks").json()[0]["uid"])

    def test_legacy_missing_and_unhashable_uids_backfill_without_discarding_audio(self):
        for uid in (None, [], {}):
            with self.subTest(uid=uid), tempfile.TemporaryDirectory() as tmp:
                root, _source, _rows, _identity = self.make_published_book(tmp)
                snapshot = root / "chunks.json"
                journal = Path(get_chunk_status_journal_path(snapshot))
                rows = ProjectManager(str(root)).load_chunks()
                journal.unlink()
                if uid is None:
                    rows[0].pop("uid")
                else:
                    rows[0]["uid"] = uid
                snapshot.write_text(json.dumps(rows), encoding="utf-8")
                client = self.http_client(root, raise_server_exceptions=False)
                selected = self.select(client)
                self.assertEqual(200, selected.status_code, selected.text)
                repaired, = ProjectManager(str(root)).load_chunks()
                self.assertIsInstance(repaired["uid"], str)
                self.assertTrue(repaired["uid"])
                self.assertEqual(("done", "line-0.wav"),
                                 (repaired["status"], repaired["audio_path"]))

    def test_export_readiness_second_identity_read_refuses_storage_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, _rows, _identity = self.make_published_book(tmp)
            snapshot = root / "chunks.json"
            journal = Path(get_chunk_status_journal_path(snapshot))
            watched = (root / "state.json", snapshot, journal, root / "line-0.wav")
            before = {path: path.read_bytes() for path in watched}
            client = self.http_client(root, state=self.export_state(), raise_server_exceptions=False)
            real_token = editor.get_book_operation_token
            routes = (
                ("get", "/api/export_chapters/preview", {}),
                ("post", "/api/export_chapters", {"json": {"chapters": [0]}}),
                ("post", "/api/merge_m4b", {"json": {}}),
            )
            for method, path, kwargs in routes:
                with self.subTest(route=path):
                    calls = []

                    def fail_readiness(*args, **options):
                        calls.append(args)
                        if len(calls) == 2:
                            raise OSError(getattr(errno, "ESTALE", errno.EIO),
                                          "injected readiness identity read failure")
                        return real_token(*args, **options)

                    with patch.object(editor, "get_book_operation_token",
                                      side_effect=fail_readiness), \
                         patch.object(editor, "schedule_claimed_background_task") as scheduled:
                        response = getattr(client, method)(path, **kwargs)
                        scheduled.assert_not_called()
                    self.assertEqual(2, len(calls))
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL, response.json()["detail"])
                    self.assertEqual(before, {path: path.read_bytes() for path in watched})
            self.assertEqual(200, client.get("/api/export_chapters/preview").status_code)
