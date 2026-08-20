"""A refused job must not be reported in the words used for a measurement.

On 2026-08-19 the recheck stage printed eight lines of the form
`recheck FAIL <adapter> (was 0.0342)`. Six of those eight jobs had been refused
by the dirty-tree gate and never ran; two were genuinely measured. The number
in parentheses was the OLD score in every case. Anyone reading that log would
have concluded eight adapters failed a retest - the claim that justifies
rebuilding a dataset.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAIN = os.path.join(REPO, "run_chains", "everything_20260818.sh")


def extract(function_names):
    """-> a runnable snippet holding just the named shell functions."""
    with open(CHAIN, encoding="utf-8") as handle:
        source = handle.read()
    out = []
    for name in function_names:
        start = source.index("%s() {" % name)
        depth, i = 0, start
        while True:
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append(source[start:i + 1])
    return "\n".join(out)


@unittest.skipUnless(os.path.exists(CHAIN), "chain not present")
class RecheckReportingTest(unittest.TestCase):
    """Drives the real shell functions with a stub gpu_job.sh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.experiments = os.path.join(self.tmp.name, "experiments")
        os.makedirs(self.experiments)
        self.log_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(self.log_dir)

    def _run(self, adapters, exit_codes, write_artifact=True):
        """adapters: [(name, old_score)]; exit_codes: {name: rc}."""
        failed_list = os.path.join(self.tmp.name, "failed.tsv")
        with open(failed_list, "w", encoding="utf-8") as handle:
            for name, score in adapters:
                handle.write("%s\tadapter/%s\tdata/%s\t%s\n"
                             % (name, name, name, score))

        # A stub standing in for gpu_job.sh: it writes the artifact and returns
        # the code we want, so every branch can be exercised without a GPU.
        stub = os.path.join(self.tmp.name, "gpu_job.sh")
        codes = " ".join("%s:%d" % (n, c) for n, c in exit_codes.items())
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(
                "#!/usr/bin/bash\n"
                "name=${1#regate2_}\n"
                "for pair in %s; do\n"
                "  if [ \"${pair%%%%:*}\" = \"$name\" ]; then rc=${pair##*:}; fi\n"
                "done\n"
                "if [ \"${rc:-0}\" -eq 0 ] || [ \"${rc:-0}\" -eq 3 ]; then\n"
                "  %s\n"
                "fi\n"
                "exit ${rc:-0}\n"
                % (codes,
                   ("printf '{\"median_ecapa\": 0.6612}' > "
                    "%s/gate_recheck__$name.json" % self.experiments)
                   if write_artifact else "true"))
        os.chmod(stub, 0o755)

        script = "\n".join([
            "set -uo pipefail",
            "REPO=%r" % self.tmp.name,
            "runtime=%r" % self.tmp.name,
            "python=%r" % "python3",
            "STAGE_LOG_DIR=%r" % self.log_dir,
            "FAILED_LIST=%r" % failed_list,
            "stage_note() { echo \"$*\"; }",
            extract(["recheck_one", "recheck_score", "recheck_failures"]),
            "recheck_failures; echo \"RETURN=$?\"",
        ])
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=120, cwd=self.tmp.name)

    def test_a_refused_job_is_not_called_a_failed_adapter(self):
        """rc=5 is gpu_job.sh's dirty-tree refusal - the exact case that
        produced eight verdict-shaped lines for two measurements."""
        result = self._run([("alpha", "0.0342")], {"alpha": 5})
        self.assertIn("NOT MEASURED", result.stdout)
        self.assertNotIn("BELOW", result.stdout)
        self.assertIn("0 of 1 measured", result.stdout)
        self.assertIn("1 never ran", result.stdout)

    def test_a_real_below_threshold_verdict_is_still_reported_as_one(self):
        """rc=3 means it DID measure. Losing that would be the opposite bug."""
        result = self._run([("beta", "0.0342")], {"beta": 3})
        self.assertIn("recheck BELOW beta", result.stdout)
        self.assertNotIn("NOT MEASURED", result.stdout)
        self.assertIn("1 of 1 measured", result.stdout)
        self.assertIn("1 below threshold", result.stdout)

    def test_the_new_score_is_reported_not_the_old_one(self):
        result = self._run([("gamma", "0.0342")], {"gamma": 3})
        line = next(l for l in result.stdout.splitlines() if "gamma" in l)
        self.assertIn("0.6612", line, "the score printed must come from this "
                                      "run's artifact, not the input file")
        self.assertIn("was 0.0342", line, "keep the old score for comparison, "
                                          "but labelled as the old one")

    def test_a_stale_artifact_is_not_read_as_this_runs_measurement(self):
        """The file exists from an earlier night; this run wrote nothing."""
        stale = os.path.join(self.experiments, "gate_recheck__delta.json")
        with open(stale, "w", encoding="utf-8") as handle:
            json.dump({"median_ecapa": 0.99}, handle)
        os.utime(stale, (time.time() - 86400, time.time() - 86400))
        result = self._run([("delta", "0.0342")], {"delta": 5},
                           write_artifact=False)
        self.assertNotIn("0.99", result.stdout)
        self.assertIn("NOT MEASURED", result.stdout)

    def test_the_stage_fails_when_nothing_was_measured(self):
        result = self._run([("a", "0.1"), ("b", "0.2")], {"a": 5, "b": 5})
        self.assertIn("RETURN=1", result.stdout)
        self.assertIn("INCOMPLETE", result.stdout)

    def test_the_stage_passes_when_every_adapter_was_measured(self):
        """Below-threshold on every adapter is a complete stage: it measured
        what it set out to measure."""
        result = self._run([("a", "0.1"), ("b", "0.2")], {"a": 3, "b": 0})
        self.assertIn("RETURN=0", result.stdout)
        self.assertIn("2 of 2 measured", result.stdout)

    def test_the_total_is_counted_not_hard_coded(self):
        """The tally said `of 8` regardless of how many rows it read."""
        result = self._run([("a", "0.1"), ("b", "0.2"), ("c", "0.3")],
                           {"a": 3, "b": 3, "c": 3})
        self.assertIn("3 of 3 measured", result.stdout)
        self.assertNotIn("of 8", result.stdout)

    def test_the_chain_counts_the_stage_in_its_summary(self):
        """Called bare, the return value went nowhere and a night that
        measured nothing still summarised as a pass."""
        with open(CHAIN, encoding="utf-8") as handle:
            source = handle.read()
        block = source[source.index('stage_note "START recheck_failures"'):][:400]
        self.assertIn("STAGE_TOTAL=$((STAGE_TOTAL + 1))", block)
        self.assertIn("STAGE_FAILURES=$((STAGE_FAILURES + 1))", block)
        self.assertRegex(block, r"if\s+recheck_failures;\s*then")


if __name__ == "__main__":
    unittest.main()
