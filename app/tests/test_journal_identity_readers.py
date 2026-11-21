"""Journal identity must stay coherent across selection and every reader."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as application
from book_state_transaction import apply_book_input_selection
from chunk_status_journal import ChunkStatusJournal, get_chunk_status_journal_path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from project import ProjectManager
from routers import editor


class JournalIdentityCases(unittest.TestCase):
    def make_book(self, root, *, generation="old", book_id="book"):
        root = Path(root)
        rows = [{"id": 0, "uid": "line-0", "text": "synthetic line", "speaker": "N",
                 "status": "done", "audio_path": None}]
        (root / "chunks.json").write_bytes(json.dumps(rows, indent=2).encode())
        (root / "annotated_script.json").write_bytes(b"[]")
        (root / "voice_config.json").write_bytes(b"{}")
        source = root / "source.txt"
        source.write_bytes(b"synthetic source")
        identity = {"active_book_id": book_id, "book_generation": generation}
        (root / "state.json").write_bytes(json.dumps({**identity,
            "input_file_path": str(source)}).encode())
        ChunkStatusJournal(root / "chunks.json", identity).ensure_writable()
        return root, source, rows, identity

    def stale_journal(self, root):
        """An already-committed journal from a previous generation."""
        root = Path(root)
        state = json.loads((root / "state.json").read_text())
        journal = ChunkStatusJournal(root / "chunks.json",
            {"active_book_id": state["active_book_id"],
             "book_generation": state["book_generation"]})
        journal.ensure_writable()
        state["book_generation"] = "new-generation"
        (root / "state.json").write_text(json.dumps(state))
        return get_chunk_status_journal_path(root / "chunks.json")

    def client_with(self, root, manager, state):
        app = FastAPI()
        app.include_router(editor.router)
        patches = (
            patch.object(editor, "project_manager", manager),
            patch.object(editor, "SCRIPT_PATH", str(root / "annotated_script.json")),
            patch.object(editor, "DATA_DIR", str(root)),
            patch.object(editor, "process_state", state),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return TestClient(app)

    def clear_pending(self, root):
        path = root / "state.json"
        state = json.loads(path.read_text())
        state.pop("script_selection_pending", None)
        path.write_text(json.dumps(state))


class SelectionJournalIdentityTests(JournalIdentityCases):
    def test_same_source_selection_keeps_validated_progress_and_chunks_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            ChunkStatusJournal(root / "chunks.json", identity).apply_update(
                "line-0", {"status": "generating"})
            apply_book_input_selection(tmp, str(source), "book")
            self.clear_pending(root)
            self.assertFalse((root / "chunks.json.status.jsonl").exists())
            self.assertEqual("generating", ProjectManager(tmp).load_chunks()[0]["status"])
            client = self.client_with(root, ProjectManager(tmp), {})
            response = client.get("/api/chunks")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("generating", response.json()[0]["status"])

    def test_other_source_selection_retires_old_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            apply_book_input_selection(tmp, str(root / "other.txt"), "other-book")
            self.assertFalse((root / "chunks.json").exists())
            self.assertFalse((root / "chunks.json.status.jsonl").exists())

    def test_selection_over_already_stale_journal_preserves_raw_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            journal_path = Path(self.stale_journal(root))
            raw = journal_path.read_bytes()
            apply_book_input_selection(tmp, str(source), "book")
            self.clear_pending(root)
            self.assertFalse(journal_path.exists())
            backups = list(root.glob(journal_path.name + ".stale-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(raw, backups[0].read_bytes())
            response = self.client_with(root, ProjectManager(tmp), {}).get("/api/chunks")
            self.assertEqual(200, response.status_code, response.text)

    def test_torn_tail_is_backed_up_and_journal_retired_with_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            path = Path(get_chunk_status_journal_path(root / "chunks.json"))
            torn = path.read_bytes() + b'{"type":"update","sequence":1,'
            path.write_bytes(torn)
            apply_book_input_selection(tmp, str(source), "book")
            self.assertFalse(path.exists())
            self.assertEqual("done", json.loads((root / "chunks.json").read_text())[0]["status"])
            backups = list(root.glob(path.name + ".torn-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(torn, backups[0].read_bytes())


class StaleJournalReaderTests(JournalIdentityCases):
    def test_startup_recovery_survives_a_stale_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            journal_path = self.stale_journal(root)
            before = Path(journal_path).read_bytes()
            manager = ProjectManager(tmp)
            with patch.object(application, "DATA_DIR", tmp), \
                 patch.object(application, "project_manager", manager):
                application._recover_startup_state()
            self.assertEqual(before, Path(journal_path).read_bytes())
            self.assertEqual("done", json.loads((root / "chunks.json").read_text())[0]["status"])

    def test_editor_integrity_and_merge_report_actionable_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            self.stale_journal(root)
            before = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
            state = {name: {"running": False, "logs": [], "result": None}
                     for name in ("audio", "audacity_export", "m4b_export", "chapter_export")}
            client = self.client_with(root, ProjectManager(tmp), state)
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                integrity = client.get("/api/editor/integrity")
                merge = client.post("/api/merge")
                scheduled.assert_not_called()
            self.assertEqual(409, integrity.status_code, integrity.text)
            self.assertEqual(editor.CHUNK_SNAPSHOT_CONFLICT_DETAIL, integrity.json()["detail"])
            self.assertEqual(409, merge.status_code, merge.text)
            for path, raw in before.items():
                if path.exists():
                    self.assertEqual(raw, path.read_bytes())

    def test_exports_refuse_before_enqueue_and_preview_reports_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, rows, identity = self.make_book(tmp)
            self.stale_journal(root)
            state = {name: {"running": False, "logs": [], "result": None}
                     for name in ("audio", "audacity_export", "m4b_export", "chapter_export")}
            client = self.client_with(root, ProjectManager(tmp), state)
            with patch.object(editor, "schedule_claimed_background_task") as scheduled:
                responses = [
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


if __name__ == "__main__":
    unittest.main()
