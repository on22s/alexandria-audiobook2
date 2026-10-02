"""Repair commands must not write over their input or each other's output."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import repair_source_encoding
import repair_source_llm


class SourceRepairPathSafetyTests(unittest.TestCase):
    def test_both_commands_reject_input_as_output_or_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("café", encoding="utf-8")
            for command in (repair_source_encoding, repair_source_llm):
                for option in ("--out", "--report"):
                    with self.subTest(command=command.__name__, option=option):
                        argv = [command.__name__, str(source), "--apply", option, str(source)]
                        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
                            command.main()
                        self.assertEqual(2, raised.exception.code)
                        self.assertEqual("café", source.read_text(encoding="utf-8"))

    def test_both_commands_reject_output_report_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            target = Path(directory) / "result.txt"
            source.write_text("café", encoding="utf-8")
            for command in (repair_source_encoding, repair_source_llm):
                with self.subTest(command=command.__name__):
                    argv = [command.__name__, str(source), "--out", str(target),
                            "--report", str(target)]
                    with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
                        command.main()
                    self.assertEqual(2, raised.exception.code)
                    self.assertFalse(target.exists())

    def test_symlink_and_hardlink_to_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "book.txt"
            source.write_text("café", encoding="utf-8")
            symlink = Path(directory) / "symlink.txt"
            hardlink = Path(directory) / "hardlink.txt"
            symlink.symlink_to(source)
            os.link(source, hardlink)
            for command in (repair_source_encoding, repair_source_llm):
                for alias in (symlink, hardlink):
                    with self.subTest(command=command.__name__, alias=alias.name):
                        argv = [command.__name__, str(source), "--apply", "--out", str(alias)]
                        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as raised:
                            command.main()
                        self.assertEqual(2, raised.exception.code)
                        self.assertEqual("café", source.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
