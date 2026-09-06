"""A chain must not report success over a crash.

WHAT HAPPENED. `restrict_candidates_20260906.sh` piped its run through
`tail -40` and then read `rc=$?`, which is the status of TAIL, not of the
python. tail always succeeds. On 2026-09-06 the run died instantly with
`RuntimeError: no llama.cpp /props at http://127.0.0.1:8090/v1`, and the chain
printed `RUN rc=0` and `ALL DONE`, and gpu_job.sh logged the job as OK. The
queue's record said an experiment had completed when nothing had run.

The repository's chains already carry the warning "never write rc=$? after a
command substitution". This is the PIPELINE variant of the same trap, and it
was written into a new chain by someone who had read that warning.

This scans every chain rather than pinning one file, because the next chain to
be written is the one at risk.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PIPE_THEN_RC = re.compile(
    r"\|\s*(?:tail|head|grep|tee|sed|awk)\b[^\n]*\n\s*(?:local\s+)?rc=\$\?")


def chains():
    out = []
    for folder in (os.path.join(REPO, "ab_test_runtime"), REPO):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if name.endswith(".sh"):
                out.append(os.path.join(folder, name))
    return out


class ChainExitCodes(unittest.TestCase):

    def test_no_chain_reads_rc_after_a_pipeline(self):
        offenders = []
        for path in chains():
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for match in PIPE_THEN_RC.finditer(text):
                line = text[:match.start()].count("\n") + 2
                offenders.append(f"{os.path.relpath(path, REPO)}:{line}")
        self.assertEqual([], offenders,
                         "rc=$? after a pipeline reads the LAST command's "
                         "status, not the one that matters. Use "
                         "${PIPESTATUS[0]}:\n  " + "\n  ".join(offenders))

    def test_the_pattern_would_catch_the_original_bug(self):
        """The fixture is the exact text that shipped, so the rule cannot
        quietly stop matching."""
        broken = ('"$PY" -u app/experiments/two_stage_attribution.py \\\n'
                  '    --out "$ART" 2>&1 | tail -40\n'
                  'rc=$?\n')
        self.assertTrue(PIPE_THEN_RC.search(broken),
                        "the detector must match the form that shipped")

    def test_the_fixed_form_is_not_flagged(self):
        fixed = ('"$PY" -u thing.py 2>&1 | tail -40\n'
                 'rc=${PIPESTATUS[0]}\n')
        self.assertIsNone(PIPE_THEN_RC.search(fixed))

    def test_a_plain_command_then_rc_is_not_flagged(self):
        """rc=$? is correct when nothing was piped; the rule must not overreach."""
        plain = '"$PY" -u thing.py > log 2>&1\nrc=$?\n'
        self.assertIsNone(PIPE_THEN_RC.search(plain))


if __name__ == "__main__":
    unittest.main()
