"""A book that failed must not let the run report DONE.

grimgar06 died on 2026-08-19 with rc=1 ("chunk 29/70 failed validation after
retries"), wrote no output, and the chain printed UNSEEN BOOKS DONE and exited
0 - so gpu_job.sh logged OK and the driver counted the stage as a pass. One of
four books produced nothing and every layer above it said success.
"""
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAIN = os.path.join(REPO, "run_chains", "unseen_books_20260819b.sh")


@unittest.skipUnless(os.path.exists(CHAIN), "chain not present")
class UnseenBooksReportingTest(unittest.TestCase):
    """Runs the real loop with a stub generator, so exit codes are exercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _harness(self, outcomes):
        """outcomes: {book: "ok"|"fail"|"empty"} -> (returncode, stdout)."""
        out_dir = os.path.join(self.tmp.name, "out")
        log_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(out_dir)
        os.makedirs(log_dir)
        with open(CHAIN, encoding="utf-8") as handle:
            source = handle.read()

        def block(marker, opener):
            start = source.index(opener)
            depth, i = 0, start
            while True:
                if source[i] == "{":
                    depth += 1
                elif source[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return source[start:i + 1]
                i += 1

        loop_start = source.index("failed_books=0;")
        loop_end = source.index('echo "UNSEEN BOOKS DONE')
        tail_end = source.index("\n", loop_end)
        body = source[loop_start:tail_end]

        script = "\n".join([
            "set -uo pipefail",
            "OUT=%r; L=%r; IN=%r; PY=python3" % (out_dir, log_dir, self.tmp.name),
            block("book_complete", "book_complete() {"),
            # Stub the generator: writes a complete book, an empty one, or fails.
            "generate() { case $1 in %s esac; }" % " ".join(
                "%s) %s;;" % (book, {
                    "ok": "python3 -c \"import json,sys;json.dump({'entries':[{'i':n} for n in range(60)]},open(sys.argv[1],'w'))\" $2; return 0",
                    "fail": "return 1",
                    "empty": "return 0",
                }[state]) for book, state in outcomes.items()),
            body.replace(
                'timeout 43200 "$PY" -u generate_script.py "$IN/$book.txt" \\\n'
                '        --output "$OUT/$book.json" > "$L/unseen_$book.log" 2>&1',
                'generate "$book" "$OUT/$book.json" > "$L/unseen_$book.log" 2>&1'),
        ])
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, timeout=120)
        return result

    def test_a_failed_book_makes_the_run_fail(self):
        result = self._harness({"mushoku18": "ok", "grimgar06": "fail",
                                "mushoku23": "ok", "arc4_volume10wn": "ok"})
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("INCOMPLETE", result.stdout)
        self.assertIn("grimgar06", result.stdout)
        self.assertNotIn("UNSEEN BOOKS DONE", result.stdout)

    def test_a_book_that_exits_zero_but_writes_nothing_also_fails(self):
        """rc=0 is not proof of an artifact - check the file too."""
        result = self._harness({"mushoku18": "ok", "grimgar06": "empty",
                                "mushoku23": "ok", "arc4_volume10wn": "ok"})
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("grimgar06", result.stdout)

    def test_all_books_succeeding_reports_done(self):
        result = self._harness({b: "ok" for b in
                                ("mushoku18", "grimgar06", "mushoku23",
                                 "arc4_volume10wn")})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("UNSEEN BOOKS DONE", result.stdout)
        self.assertNotIn("INCOMPLETE", result.stdout)


if __name__ == "__main__":
    unittest.main()
