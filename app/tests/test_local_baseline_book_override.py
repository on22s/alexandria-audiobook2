"""The corrupt-copy guard must follow the books actually being run.

`local_four_book_baseline_20260906.sh` refuses when index18's input holds
U+FFFD replacement characters - the corrupt copy has 6,662 of them and no quote
marks at all, and a run against it measures nothing. That guard was
unconditional, which was correct while the chain always ran four books.

Making BOOKS overridable puts it at risk in both directions, so both are
pinned here: a subset that includes index18 must still be refused, and a subset
that excludes it must not be refused for the state of a file it never reads.
Weakening a safety net to make a run possible is the failure this guards
against (Rule 9); so is leaving it in a form that blocks a legitimate run.

The chain itself is executed, not a copy of its logic - the point is whether
THIS file behaves, and a test that reimplements the case statement would pass
while the file did the wrong thing.
"""
import http.server
import os
import shutil
import subprocess
import tempfile
import threading
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAIN = os.path.join(REPO, "ab_test_runtime", "local_four_book_baseline_20260906.sh")
BOOKS = ["grimgar03", "index18", "mushoku16", "owarimonogatari3"]


@unittest.skipUnless(os.path.exists(CHAIN), "chain not present")
class LocalBaselineBookOverrideTest(unittest.TestCase):

    def _gold(self, corrupt_index18):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "inputs"))
        os.makedirs(os.path.join(root, "checkpoints"))
        for book in BOOKS:
            text = "clean text"
            if book == "index18" and corrupt_index18:
                text = "broken �� text"
            with open(os.path.join(root, "inputs", book + ".txt"), "w",
                      encoding="utf-8") as handle:
                handle.write(text)
            with open(os.path.join(root, "checkpoints",
                                   book + "__three_pass.json.threepass_checkpoint.json"),
                      "w", encoding="utf-8") as handle:
                handle.write("{}")
        return root

    def _server(self):
        """A stub answering /props, because the chain checks the endpoint is
        alive BEFORE it reaches the guards under test."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):                        # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        return "http://127.0.0.1:%d/v1" % server.server_address[1]

    def _run(self, gold, books=None):
        """Run the chain far enough to reach its guards. The endpoint is a stub
        and the books are fixtures, so only the guards decide the outcome."""
        env = dict(os.environ)
        env["LN_BASE_URL"] = self._server()
        # A tag that cannot collide with a real artifact, so the skip-on-exists
        # branch never fires and the guards are what we are measuring.
        env["LN_TAG"] = "unit-test-does-not-exist-%d" % os.getpid()
        env["LN_GOLD"] = gold
        if books is not None:
            env["LN_BOOKS"] = books
        proc = subprocess.run(["bash", CHAIN], env=env, cwd=REPO,
                              capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    def test_a_subset_containing_index18_is_still_refused(self):
        gold = self._gold(corrupt_index18=True)
        code, out = self._run(gold, "index18 mushoku16")
        self.assertNotEqual(0, code)
        self.assertIn("replacement characters", out)

    def test_a_subset_without_index18_is_not_refused_for_it(self):
        # THE CASE THE CONDITION EXISTS FOR. index18 on disk is corrupt and
        # irrelevant: owarimonogatari3 never reads it.
        gold = self._gold(corrupt_index18=True)
        code, out = self._run(gold, "owarimonogatari3")
        self.assertNotIn("replacement characters", out)

    def test_the_default_run_still_checks_index18(self):
        # No override at all must behave exactly as before this was made
        # overridable.
        gold = self._gold(corrupt_index18=True)
        code, out = self._run(gold)
        self.assertNotEqual(0, code)
        self.assertIn("replacement characters", out)


if __name__ == "__main__":
    unittest.main()
