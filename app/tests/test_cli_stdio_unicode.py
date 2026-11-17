"""CLI stdio must survive non-ASCII LLM output on Windows code pages."""
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import core
from utils import (CLI_STDIO_ENCODING, CLI_STDIO_ERRORS, configure_cli_stdio,
                   escape_unsafe_display_text, force_utf8_child_env)


class CliStdioUnicodeTests(unittest.TestCase):
    def test_configure_cli_stdio_makes_both_streams_safe(self):
        raw_out, raw_err = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
        err = io.TextIOWrapper(raw_err, encoding="cp1252", errors="strict")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            configure_cli_stdio()
            print("\u2770")
            print("\u2771", file=sys.stderr)
            out.flush()
            err.flush()
        # Raw characters, not JSON escapes or replacement chars, reach the
        # parent reader as UTF-8 on both streams.
        self.assertEqual("\u2770" + os.linesep, raw_out.getvalue().decode("utf-8"))
        self.assertEqual("\u2771" + os.linesep, raw_err.getvalue().decode("utf-8"))

    def test_configure_cli_stdio_tolerates_streams_that_cannot_reconfigure(self):
        class BrokenReconfigure:
            def reconfigure(self, **_kwargs):
                raise OSError("pipe reader closed")

        original_out, original_err = sys.stdout, sys.stderr
        # StringIO has no reconfigure; the custom stream raises OSError from it.
        # Neither may abort CLI startup.
        try:
            sys.stdout, sys.stderr = io.StringIO(), BrokenReconfigure()
            configure_cli_stdio()
        finally:
            sys.stdout, sys.stderr = original_out, original_err

    def test_subprocess_reader_round_trips_diff_preview_exactly(self):
        state = {"logs": [], "cancel": False, "paused": False}
        payload = {"text": "\u2770"}
        code = ("import json; print('DIFF_PREVIEW_JSON: ' + "
                "json.dumps({'text': chr(0x2770)}, ensure_ascii=False))")
        with tempfile.TemporaryDirectory() as tmp:
            # Without the shared child env this print raises UnicodeEncodeError
            # under cp1252 and the child exits 1.
            rc, lines = core._stream_subprocess_to_logs(
                [sys.executable, "-c", code], tmp, state,
                env={"PYTHONIOENCODING": "cp1252"})
        expected = "DIFF_PREVIEW_JSON: " + json.dumps(payload, ensure_ascii=False)
        self.assertEqual(0, rc)
        self.assertEqual([expected], lines)
        self.assertEqual(payload, json.loads(lines[0].split(": ", 1)[1]))

    def test_force_utf8_child_env_applies_one_shared_policy(self):
        env = force_utf8_child_env({"KEEP": "1", "PYTHONIOENCODING": "cp1252"})
        self.assertEqual(f"{CLI_STDIO_ENCODING}:{CLI_STDIO_ERRORS}",
                         env["PYTHONIOENCODING"])
        self.assertEqual("1", env["PYTHONUTF8"])
        self.assertEqual("1", env["KEEP"])

    def test_only_python_children_are_decoded_as_utf8(self):
        self.assertTrue(core._is_python_command([sys.executable, "-c", "pass"]))
        self.assertFalse(core._is_python_command(["ffmpeg", "-i", "input.wav"]))

    def test_display_controls_are_escaped_but_normal_unicode_is_preserved(self):
        self.assertEqual("a\\u202eb", escape_unsafe_display_text("a\u202eb"))
        self.assertEqual("a\\u200bb", escape_unsafe_display_text("a\u200bb"))
        self.assertEqual("a\u2770b", escape_unsafe_display_text("a\u2770b"))

    def test_every_cli_entry_point_configures_stdio(self):
        app_dir = Path(__file__).resolve().parent.parent
        entry_points = ("review_script.py", "generate_script.py", "find_nicknames.py",
                        "three_pass_generate.py", "cast_list.py")
        for name in entry_points:
            with self.subTest(entry_point=name):
                tree = ast.parse((app_dir / name).read_text(encoding="utf-8"))
                guards = [node for node in tree.body
                          if isinstance(node, ast.If)
                          and isinstance(node.test, ast.Compare)
                          and "__main__" in ast.dump(node.test)]
                self.assertTrue(guards, name)
                calls = [node for guard in guards for node in ast.walk(guard)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id in ("configure_cli_stdio", "_startup_stdio")]
                self.assertTrue(calls, name)


if __name__ == "__main__":
    unittest.main()
