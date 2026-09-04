"""`$?` must not be read after a command substitution on the same line.

THE BUG THIS PINS, reproduced exactly:

    $ false; echo "[$(date -Is)] DONE arm rc=$?"
    [2026-09-04T13:31:29-05:00] DONE arm rc=0     <- the command exited 1

`$(date -Is)` runs while the arguments are being built, so `$?` reports
DATE's status, not the command's. The exit code is destroyed before it is
read, and the log records a success that never happened.

On 2026-09-04 the DaisyMiller A/B logged `DONE damaged rc=0` for a
generate_script run that had called sys.exit(1) after chunk 12 failed
validation. The run was then reported, by me, as a completed comparison. It
was diagnosed as a missing `set -e`; `set -e` would not have helped, because
the wrapper never saw a non-zero status to act on.

The fix is one line: capture first, interpolate after.

    cmd ...
    rc=$?
    echo "[$(date -Is)] DONE arm rc=$rc"

This repository is clean today. The test exists so it stays that way - the
four scripts that carried the idiom lived on a cloud box, outside version
control, where nothing could have caught them.
"""
import os
import re
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# A command substitution, then `$?`, on one line. Anchored to `$?` being READ
# rather than assigned, since `rc=$?` on its own line is the correct form.
DESTROYS = re.compile(r"\$\([^)]*\).*\$\?")


def tracked_shell_scripts():
    out = subprocess.run(("git", "-C", REPO, "ls-files", "*.sh"),
                         capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise unittest.SkipTest("git unavailable")
    return [p for p in out.stdout.split() if p]


class ExitCodeSurvives(unittest.TestCase):
    def test_no_tracked_script_reads_status_after_a_substitution(self):
        offenders = []
        for rel in tracked_shell_scripts():
            path = os.path.join(REPO, rel)
            try:
                lines = open(path, encoding="utf-8", errors="replace").read().split("\n")
            except OSError:
                continue
            for n, line in enumerate(lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                if DESTROYS.search(line):
                    offenders.append("%s:%d: %s" % (rel, n, line.strip()[:90]))
        self.assertEqual([], offenders,
                         "a command substitution resets $?; capture it first "
                         "with `rc=$?` on its own line:\n  " + "\n  ".join(offenders))

    def test_the_check_would_catch_the_real_thing(self):
        """Rule 21: a linter that matches nothing passes forever."""
        self.assertTrue(DESTROYS.search('echo "[$(date -Is)] DONE $arm rc=$?"'))
        self.assertTrue(DESTROYS.search('printf "%s %s\\n" "$(hostname)" "$?"'))

    def test_the_correct_form_is_not_flagged(self):
        self.assertIsNone(DESTROYS.search("rc=$?"))
        self.assertIsNone(DESTROYS.search('echo "[$(date -Is)] DONE $arm rc=$rc"'))
        self.assertIsNone(DESTROYS.search('if [ "$?" -ne 0 ]; then'))

    def test_it_finds_scripts_to_check(self):
        self.assertGreater(len(tracked_shell_scripts()), 20)


if __name__ == "__main__":
    unittest.main()
