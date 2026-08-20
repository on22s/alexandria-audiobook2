"""The resolver must fix generated conflicts and refuse everything else.

Five pull requests in one morning came back CONFLICTING, every one on the same
derived files. Resolving those by regeneration is safe. Resolving a REAL
disagreement that way silently discards one side - which nearly happened on
2026-08-20, when one branch moved goal 1.2 to Part II while another added
evidence to it in Part I. Taking either side alone would have lost work.

So the refusal is the load-bearing behaviour, and it is tested first.
"""
import os
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "resolve_generated.sh")


@unittest.skipUnless(os.path.exists(SCRIPT), "resolve_generated.sh missing")
class ResolveGeneratedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", self.repo, *args],
                              capture_output=True, text=True, check=check)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def conflicting_merge(self, rel):
        """Make `rel` conflict between main and a side branch."""
        self.write(rel, "base\n")
        self.git("add", "-A"); self.git("commit", "-q", "-m", "base")
        self.git("checkout", "-q", "-b", "side")
        self.write(rel, "side change\n")
        self.git("add", "-A"); self.git("commit", "-q", "-m", "side")
        self.git("checkout", "-q", "main")
        self.write(rel, "main change\n")
        self.git("add", "-A"); self.git("commit", "-q", "-m", "main")
        self.git("merge", "side", check=False)

    def run_script(self):
        return subprocess.run(["bash", SCRIPT], cwd=self.repo,
                              capture_output=True, text=True, timeout=120)

    def test_it_refuses_a_conflict_in_real_code(self):
        """The load-bearing case: regenerating would discard one side."""
        self.conflicting_merge("app/app.py")
        result = self.run_script()
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("REFUSING", result.stdout)
        self.assertIn("app/app.py", result.stdout)

    def test_it_refuses_when_a_real_file_conflicts_alongside_generated_ones(self):
        """A mixed merge must not be half-resolved."""
        self.write("RESULTS_INDEX.md", "base\n")
        self.conflicting_merge("app/tts.py")
        result = self.run_script()
        self.assertEqual(2, result.returncode)
        self.assertIn("app/tts.py", result.stdout)

    def test_it_reports_when_nothing_is_conflicted(self):
        self.write("README.md", "hi\n")
        self.git("add", "-A"); self.git("commit", "-q", "-m", "c")
        result = self.run_script()
        self.assertEqual(0, result.returncode)
        self.assertIn("nothing is conflicted", result.stdout)


if __name__ == "__main__":
    unittest.main()
