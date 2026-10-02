"""Native durable replay, interruption and identity boundaries for chunk deltas."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import chunk_status_journal as journal


class ChunkStatusJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "chunks.json"
        self.rows = [{"uid": "one", "text": "first", "status": "pending"},
                     {"uid": "two", "text": "second", "status": "pending"}]
        self.path.write_text(json.dumps(self.rows))
        self.book = {"active_book_id": "book", "book_generation": "generation"}

    def manager(self):
        return journal.ChunkStatusJournal(self.path, self.book)

    def test_fsynced_uid_updates_replay_and_returns_are_detached(self):
        manager = self.manager()
        original = self.path.read_bytes()
        changes = {"status": "done", "audio_path": "voicelines/one.wav"}
        expected = copy.deepcopy(changes)
        with patch.object(journal.os, "fsync", wraps=os.fsync) as sync:
            returned = manager.apply_update("one", changes)
            self.assertGreaterEqual(sync.call_count, 1)
        changes["status"] = "corrupted caller"
        returned["status"] = "corrupted return"
        rows = self.manager().get_rows()
        self.assertEqual(rows[0], dict(self.rows[0], **expected))
        rows[0]["text"] = "corrupted reader"
        self.assertEqual(manager.get_row("one")["text"], "first")
        self.assertEqual(self.path.read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "SIGKILL native process test requires POSIX")
    def test_native_process_death_retains_committed_transition(self):
        code = """import os,signal,sys,json
from chunk_status_journal import ChunkStatusJournal
m=ChunkStatusJournal(sys.argv[1],json.loads(sys.argv[2]))
m.apply_update('one',{'status':'done','audio_path':'voicelines/one.wav'})
os.kill(os.getpid(),signal.SIGKILL)
"""
        child = subprocess.run([sys.executable, "-c", code, str(self.path), json.dumps(self.book)],
                               capture_output=True, text=True)
        self.assertEqual(child.returncode, -9, child.stderr)
        self.assertEqual(self.manager().get_row("one")["status"], "done")

    def test_torn_record_recovers_committed_prefix_with_warning_and_backup(self):
        manager = self.manager()
        manager.apply_update("one", {"status": "done"})
        with manager.path.open("ab") as handle:
            handle.write(b'{"type":"update","sequence":2')
        torn = manager.path.read_bytes()
        with self.assertLogs(journal.logger, level="WARNING"):
            self.assertEqual(self.manager().get_row("one")["status"], "done")
        self.assertEqual(manager.path.read_bytes(), torn)
        with self.assertLogs(journal.logger, level="WARNING"):
            manager.apply_update("two", {"status": "error", "error": "failed"})
        backup = list(self.path.parent.glob("*.torn-*"))
        self.assertEqual(len(backup), 1)
        self.assertEqual(backup[0].read_bytes(), torn)
        self.assertEqual(self.manager().get_row("two")["status"], "error")

    def test_complete_malformed_record_or_changed_identity_fail_preserving_files(self):
        manager = self.manager()
        manager.apply_update("one", {"status": "done"})
        original = manager.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "identity"):
            journal.ChunkStatusJournal(self.path, {"active_book_id": "another"}).get_rows()
        self.assertEqual(manager.path.read_bytes(), original)
        self.path.write_text(json.dumps(list(reversed(self.rows))))
        with self.assertRaisesRegex(ValueError, "outside"):
            manager.get_rows()
        self.path.write_text(json.dumps(self.rows))
        with manager.path.open("ab") as handle:
            handle.write(b'{"sequence":2,"type":"update","uid":"missing","changes":{"status":"done"}}\n')
        evidence = manager.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "UID"):
            self.manager().get_rows()
        self.assertEqual(manager.path.read_bytes(), evidence)

    def test_compaction_interrupted_before_snapshot_can_retry_without_losing_updates(self):
        manager = self.manager()
        manager.apply_update("one", {"status": "done"})
        rows = manager.get_rows()
        rows[1]["text"] = "edited"
        save = journal.save_adapter_publication_bytes
        def fail_snapshot(path, content, *args, **kwargs):
            if path == str(self.path):
                raise OSError("snapshot write failed")
            return save(path, content, *args, **kwargs)
        with patch.object(journal, "save_adapter_publication_bytes", side_effect=fail_snapshot):
            with self.assertRaisesRegex(OSError, "snapshot write"):
                manager.save_compacted(rows)
        with self.assertLogs(journal.logger, level="WARNING"):
            recovered = self.manager()
            self.assertEqual(recovered.get_row("one")["status"], "done")
            self.assertEqual(recovered.get_row("two")["text"], "second")
        recovered.apply_update("two", {"status": "error"})
        self.assertEqual(self.manager().get_row("two")["status"], "error")
        recovered.save_compacted(recovered.get_rows())
        self.assertFalse(recovered.path.exists())
        self.assertEqual(json.loads(self.path.read_text())[0]["status"], "done")

    def test_compaction_interrupted_after_snapshot_is_not_replayed_twice(self):
        manager = self.manager()
        manager.apply_update("one", {"status": "done"})
        rows = manager.get_rows()
        rows[0]["status"] = "pending"  # a subsequent edit supersedes the old delta
        unlink = Path.unlink
        def fail_journal(path, *args, **kwargs):
            if path == manager.path:
                raise OSError("journal cleanup failed")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_journal):
            with self.assertRaisesRegex(OSError, "cleanup"):
                manager.save_compacted(rows)
        recovered = self.manager()
        self.assertEqual(recovered.get_row("one")["status"], "pending")
        recovered.apply_update("two", {"status": "done"})
        self.assertEqual(self.manager().get_row("one")["status"], "pending")
        self.assertEqual(self.manager().get_row("two")["status"], "done")

    def test_failed_fsync_does_not_report_success_or_keep_unverified_cache(self):
        manager = self.manager()
        manager.ensure_writable()
        with patch.object(journal.os, "fsync", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                manager.apply_update("one", {"status": "done"})
        self.assertIsNone(manager._versions)
        # A complete record may reach disk despite an fsync failure; re-read it.
        self.assertEqual(manager.get_row("one")["status"], "done")


if __name__ == "__main__":
    unittest.main()
