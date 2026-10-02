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

START_JS = Path(__file__).resolve().parent.parent.parent / "start.js"


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
        self.assertTrue(self.success_re.search("INFO: Uvicorn running on http://127.0.0.1:8000"))
        self.assertTrue(self.failure_re.search("ModuleNotFoundError: x"))

    def test_dependency_urls_before_bind_do_not_become_app_url(self):
        lines = _run_fake_server(
            "WARNING: documentation at http://docs.example.test/tts",
            "Downloading from http://models.example.test/weights.bin",
            *HEALTHY_STARTUP)
        self.assertEqual(("url", "http://127.0.0.1:8000"), self._supervise(lines))

    def test_documentation_url_before_startup_failure_is_not_ready(self):
        lines = _run_fake_server("Help: http://docs.example.test/install",
                                 "ERROR: Application startup failed. Exiting.", exit_code=3)
        self.assertEqual("failed", self._supervise(lines)[0])

    def test_healthy_startup_captures_the_url(self):
        outcome, value = self._supervise(_run_fake_server(*HEALTHY_STARTUP))
        self.assertEqual("url", outcome)
        self.assertEqual("http://127.0.0.1:8000", value)

    def test_dynamic_port_url_is_captured_verbatim(self):
        # The launcher assigns {{port}}; whatever port is printed must be captured.
        lines = _run_fake_server("INFO:     Uvicorn running on http://127.0.0.1:53411")
        self.assertEqual(("url", "http://127.0.0.1:53411"), self._supervise(lines))

    def test_real_uvicorn_bind_announcement_matches_actual_js_event(self):
        import selectors
        import time
        from urllib.request import urlopen
        script = """
import uvicorn
print('WARNING: help at http://docs.example.test/tts', flush=True)
async def app(scope, receive, send):
    await send({'type':'http.response.start','status':200,'headers':[]})
    await send({'type':'http.response.body','body':b'fixture-ready'})
uvicorn.run(app, host='127.0.0.1', port=0, lifespan='off', access_log=False, use_colors=False)
"""
        process = subprocess.Popen([sys.executable, '-u', '-c', script], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        lines = []
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if not selector.select(timeout=0.1):
                    continue
                line = process.stdout.readline()
                if not line:
                    break
                lines.append(line)
                if 'Uvicorn running on ' in line:
                    break
            outcome, url = self._supervise(lines)
            self.assertEqual('url', outcome, lines)
            self.assertTrue(url.startswith('http://127.0.0.1:'), url)
            with urlopen(url, timeout=3) as response:
                self.assertEqual(b'fixture-ready', response.read())
            node = r"""
const launcher = require(process.argv[1]);
const event = launcher.run.find(step => step.method === 'shell.run').params.on[0].event;
const match = event.match(/^\/(.*)\/([a-z]*)$/);
const expression = new RegExp(match[1], match[2]);
for (const line of JSON.parse(process.argv[2])) {
  const result = expression.exec(line);
  if (result) { console.log(result[1]); break; }
}
"""
            import json
            captured = subprocess.check_output(['node', '-e', node, str(START_JS), json.dumps(lines)],
                                               text=True, timeout=5).strip()
            self.assertEqual(url, captured)
        finally:
            selector.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()

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


_RESET_ARTIFACT_FIXTURE = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const launcher = require(process.argv[1]);
const root = fs.mkdtempSync(path.join(os.tmpdir(), 'alexandria-reset-fixture-'));
try {
  const activeFiles = ['annotated_script.json', 'voice_config.json', 'character_aliases.json', 'chunks.json', 'state.json'];
  for (const name of activeFiles) {
    fs.writeFileSync(path.join(root, name), JSON.stringify({fromBook: 'old book'}));
  }
  fs.mkdirSync(path.join(root, 'scripts'));
  const library = path.join(root, 'scripts', 'saved-book.json');
  const libraryBytes = Buffer.from('{"keep":"saved book"}');
  fs.writeFileSync(library, libraryBytes);
  const cast = path.join(root, 'voice_library.json');
  fs.writeFileSync(cast, '{"keep":"reusable cast"}');
  const castBytes = fs.readFileSync(cast);
  for (const step of launcher.run) {
    if (step.method === 'script.stop' || (step.method === 'stop_writers' && step.uri === 'launcher_lifecycle.js')) {
      // Lifecycle is stubbed; this fixture checks configured file cleanup.
    } else {
      assert.equal(step.method, 'fs.rm');
      const target = path.resolve(root, step.params.path);
      assert.ok(target.startsWith(root + path.sep));
      fs.rmSync(target, {recursive: true, force: true});
    }
  }
  for (const name of activeFiles) {
    assert.equal(fs.existsSync(path.join(root, name)), false, name + ' survived reset');
  }
  assert.deepEqual(fs.readFileSync(library), libraryBytes);
  assert.deepEqual(fs.readFileSync(cast), castBytes);
  console.log(JSON.stringify({activeFilesRemoved: activeFiles, savedBookPreserved: true, castPreserved: true, note: 'Configured deletion steps executed with Node fs only in a temporary fixture; Shutdown step mocked in this artifact-only fixture; awaited lifecycle is exercised separately.'}));
} finally {
  fs.rmSync(root, {recursive: true, force: true});
}
"""

class LauncherResetArtifactTests(unittest.TestCase):
    def test_reset_removes_previous_book_aliases_and_preserves_saved_library(self):
        import json
        result = subprocess.run(
            ["node", "-e", _RESET_ARTIFACT_FIXTURE, str(START_JS.with_name("reset.js"))],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        artifact = json.loads(result.stdout)
        self.assertIn("character_aliases.json", artifact["activeFilesRemoved"])
        self.assertTrue(artifact["savedBookPreserved"])
        self.assertTrue(artifact["castPreserved"])
