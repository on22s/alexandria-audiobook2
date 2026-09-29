"""A source-backed repair preview must cover both of its inputs."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from routers import scripts_library


class RepairPreviewTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scripts = Path(self.temp.name, "scripts")
        self.uploads = Path(self.temp.name, "uploads")
        self.scripts.mkdir()
        self.uploads.mkdir()
        self.script = self.scripts / "book.json"
        self.source = self.uploads / "book.txt"
        self.original = json.dumps([
            {"speaker": "NARRATOR", "text": "Take саге.", "instruct": "neutral"}
        ])
        self.script.write_text(self.original, encoding="utf-8")
        self.source.write_text("Take саге.", encoding="utf-8")
        for attr, value in (("SCRIPTS_DIR", self.scripts), ("UPLOADS_DIR", self.uploads)):
            patcher = patch.object(scripts_library, attr, str(value))
            patcher.start()
            self.addCleanup(patcher.stop)

    def preview(self):
        return asyncio.run(scripts_library.preview_deterministic_repair(
            "book", scripts_library.ScriptRepairRequest(source_filename="book.txt")))

    def apply(self, preview):
        return asyncio.run(scripts_library.apply_deterministic_repair(
            "book", scripts_library.ScriptRepairRequest(
                source_filename="book.txt", expected_sha256=preview["sha256"])))

    def test_source_change_refuses_old_preview_without_backup_or_write(self):
        preview = self.preview()
        self.assertEqual(preview["sha256"], self.preview()["sha256"])
        self.source.write_text("Take саге. A new sentence.", encoding="utf-8")
        with self.assertRaises(HTTPException) as raised:
            self.apply(preview)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(self.original, self.script.read_text(encoding="utf-8"))
        self.assertEqual(["book.json"], sorted(p.name for p in self.scripts.iterdir()))
        fresh = self.preview()
        self.assertNotEqual(preview["sha256"], fresh["sha256"])
        result = self.apply(fresh)
        self.assertEqual("repaired", result["status"])
        self.assertEqual("Take care.", json.loads(self.script.read_text())[0]["text"])
        self.assertEqual(self.original, (self.scripts / result["backup"]).read_text())

    def test_script_change_also_refuses_old_preview(self):
        preview = self.preview()
        updated = self.original + "\n"
        self.script.write_text(updated, encoding="utf-8")
        with self.assertRaises(HTTPException) as raised:
            self.apply(preview)
        self.assertEqual(409, raised.exception.status_code)
        self.assertEqual(updated, self.script.read_text(encoding="utf-8"))

    def test_invalid_utf8_source_is_rejected_without_script_mutation(self):
        preview = self.preview()
        self.source.write_bytes(b"\xff")
        with self.assertRaises(HTTPException) as raised:
            self.apply(preview)
        self.assertEqual(422, raised.exception.status_code)
        self.assertEqual(self.original, self.script.read_text(encoding="utf-8"))
