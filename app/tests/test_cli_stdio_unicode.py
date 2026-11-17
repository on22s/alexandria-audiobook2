"""CLI stdio must survive non-ASCII LLM output on Windows code pages."""
import io
import json
import sys
import tempfile
import unittest

import core
from utils import configure_cli_stdio


class CliStdioUnicodeTests(unittest.TestCase):
    def test_configure_cli_stdio_makes_unencodable_prints_safe(self):
        raw_out, raw_err = io.BytesIO(), io.BytesIO()
        out = io.TextIOWrapper(raw_out, encoding="cp1252", errors="strict")
        err = io.TextIOWrapper(raw_err, encoding="cp1252", errors="strict")
        original_out, original_err = sys.stdout, sys.stderr
        try:
            sys.stdout, sys.stderr = out, err
            configure_cli_stdio()
            print(json.dumps({"text": "\u2770"}, ensure_ascii=False))
        finally:
            sys.stdout, sys.stderr = original_out, original_err
        out.flush()
        # Written as UTF-8 by the reconfigured stream, and round-trips through
        # JSON exactly as the web app's DIFF_PREVIEW_JSON parser expects.
        self.assertEqual({"text": "\u2770"}, json.loads(raw_out.getvalue().decode("utf-8")))

    def test_subprocess_reader_forces_utf8_even_when_env_requests_cp1252(self):
        state = {"logs": [], "cancel": False, "paused": False}
        with tempfile.TemporaryDirectory() as tmp:
            # Without the forced UTF-8 child env this print raises
            # UnicodeEncodeError under cp1252 and the child exits 1.
            code = "print('DIFF_PREVIEW_JSON: ' + chr(0x2770))"
            rc, lines = core._stream_subprocess_to_logs(
                [sys.executable, "-c", code], tmp, state,
                env={"PYTHONIOENCODING": "cp1252"})
        self.assertEqual(0, rc)
        self.assertTrue(any("\u2770" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
