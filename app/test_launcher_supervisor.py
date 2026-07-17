"""Phase 8 — behavioral harness for the start.js launcher supervisor.

The existing `test_launcher_contracts_*` test asserts start.js contains the right
*strings*. This complements it by exercising *behavior*: it parses the two
readiness/failure regexes straight out of `start.js` (so it can never drift from
the real launcher), spawns tiny fake "server" processes that emit the exact
signals a Python/uvicorn launch prints, and verifies the supervisor would capture
the URL on success, break visibly on each failure, and — critically — never
mistake healthy output for a failure.

Isolation: the fake servers are `python -c "print(...)"` one-liners. They bind no
ports, import nothing from this app, need no GPU/model, and start no second
Alexandria server — so this is safe to run anywhere the unit suite runs.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

START_JS = Path(__file__).resolve().parent.parent / "start.js"


def _parse_launcher_events(start_js_text):
    """Return the compiled (success, failure) regexes exactly as start.js uses them.

    Parsed from the launcher source so this harness tracks the real patterns; a
    hardcoded copy would silently pass after the launcher changed.
    """
    raw = re.findall(r'event:\s*"([^"]*)"', start_js_text)
    compiled = []
    for literal in raw:
        # JS source doubles every backslash ("\\S"); collapse to one ("\S").
        value = literal.replace("\\\\", "\\")
        match = re.match(r"^/(.*)/([a-z]*)$", value)  # /pattern/flags
        pattern, flags = match.group(1), match.group(2)
        pattern = pattern.replace("\\/", "/")  # \/ is just / for Python re
        compiled.append(re.compile(pattern, re.IGNORECASE if "i" in flags else 0))
    return compiled


def _run_fake_server(*lines, exit_code=0):
    """Spawn a throwaway process that prints the given lines, then exits.

    Emulates a launching Python server's stdout without binding a port or
    importing anything from this project.
    """
    script = "".join(f"print({line!r})\n" for line in lines)
    script += f"import sys; sys.exit({exit_code})"
    result = subprocess.run([sys.executable, "-c", script],
                            capture_output=True, text=True, timeout=30)
    return result.stdout.splitlines()


def _supervise(lines, success_re, failure_re):
    """Mirror start.js's `on` handling: first matching signal wins, per line."""
    for line in lines:
        found = success_re.search(line)
        if found:
            return ("url", found.group(1))
        if failure_re.search(line):
            return ("failed", line)
    return ("exited", None)


HEALTHY_STARTUP = (
    "INFO:     Started server process [12345]",
    "INFO:     Waiting for application startup.",
    "INFO:     Application startup complete.",
    "INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)",
)


class LauncherSupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.success_re, cls.failure_re = _parse_launcher_events(
            START_JS.read_text(encoding="utf-8"))

    def _supervise(self, lines):
        return _supervise(lines, self.success_re, self.failure_re)

    def test_exactly_two_events_success_then_failure(self):
        # start.js must keep one URL-capture signal and one failure signal.
        self.assertTrue(self.success_re.search("http://127.0.0.1:8000"))
        self.assertTrue(self.failure_re.search("ModuleNotFoundError: x"))

    def test_healthy_startup_captures_the_url(self):
        outcome, value = self._supervise(_run_fake_server(*HEALTHY_STARTUP))
        self.assertEqual("url", outcome)
        self.assertEqual("http://127.0.0.1:8000", value)

    def test_dynamic_port_url_is_captured_verbatim(self):
        # The launcher assigns {{port}}; whatever port is printed must be captured.
        lines = _run_fake_server("INFO:     Uvicorn running on http://127.0.0.1:53411")
        self.assertEqual(("url", "http://127.0.0.1:53411"), self._supervise(lines))

    def test_healthy_output_never_trips_the_failure_signal(self):
        # "Application startup complete." must not match "Application startup failed",
        # and no other healthy line may look like a failure.
        for line in HEALTHY_STARTUP:
            self.assertIsNone(self.failure_re.search(line),
                              f"healthy line falsely matched failure: {line!r}")

    def test_module_not_found_breaks_before_any_url(self):
        lines = _run_fake_server(
            "Traceback is not present here, just:",
            "ModuleNotFoundError: No module named 'fastapi'", exit_code=1)
        outcome, line = self._supervise(lines)
        self.assertEqual("failed", outcome)
        self.assertIn("ModuleNotFoundError", line)

    def test_import_error_breaks(self):
        lines = _run_fake_server(
            "ImportError: cannot import name 'foo' from 'bar'", exit_code=1)
        self.assertEqual("failed", self._supervise(lines)[0])

    def test_traceback_breaks(self):
        lines = _run_fake_server(
            "Traceback (most recent call last):",
            '  File "app.py", line 1, in <module>', exit_code=1)
        self.assertEqual("failed", self._supervise(lines)[0])

    def test_application_startup_failed_breaks(self):
        lines = _run_fake_server(
            "ERROR:    Application startup failed. Exiting.", exit_code=3)
        self.assertEqual("failed", self._supervise(lines)[0])

    def test_address_already_in_use_breaks_case_insensitively(self):
        lines = _run_fake_server(
            "[Errno 98] error while attempting to bind on address "
            "('127.0.0.1', 8000): address already in use", exit_code=1)
        self.assertEqual("failed", self._supervise(lines)[0])

    def test_early_clean_exit_yields_no_url_and_no_false_failure(self):
        # A process that exits cleanly without ever printing a URL must not be
        # reported as a failure (nothing matched) — it simply produced no URL.
        lines = _run_fake_server("Booting…", "Nothing to serve, done.", exit_code=0)
        self.assertEqual(("exited", None), self._supervise(lines))

    def test_failure_signal_does_not_match_the_url_line(self):
        # The two signals must be mutually exclusive on a healthy URL line.
        url_line = "INFO:     Uvicorn running on http://127.0.0.1:8000"
        self.assertTrue(self.success_re.search(url_line))
        self.assertIsNone(self.failure_re.search(url_line))


if __name__ == "__main__":
    unittest.main()
