"""One serving harness, with every flag the experiments actually use.

WHY THIS TEST EXISTS. For weeks there were three divergent copies of
`app/experiments/lora_serving_eval.py`:

  - main: had the #670 roster fallback and guards, but NO --prompt-variant and
    NO --window-limit, so it could not run any michel2_full or nine-novel cell
  - the probe worktrees: had those flags, but not the #670 fallback, so they
    resolved an EMPTY roster against regenerated fixtures and would score every
    row against no candidates while reporting a plausible accuracy
  - the Thunder boxes: a third variant with neither

That is Rule 15's "two independently-maintained checks will drift" applied to
the measurement instrument itself, and it cost real time: a cell was launched
from the live tree on 2026-09-26 and died in 45s on `unrecognized arguments`,
because the flags every recorded invocation uses were only in a worktree.

These tests pin the union so a future edit cannot quietly drop half of it.
Every default here must preserve the pre-merge behaviour of main, because a
changed default silently rewrites what every existing arm measured.
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SOURCE = os.path.join(REPO, "app", "experiments", "lora_serving_eval.py")


class OneServingHarnessTest(unittest.TestCase):
    # (flag, expected default as it appears in the source)
    EXPERIMENT_FLAGS = (
        ("--prompt-variant", 'default="default"'),
        ("--window-limit", "default=0"),
        ("--temperature", "default=0.0"),
        ("--roster-mode", 'default="full"'),
        ("--surround-chars", "default=2000"),
        ("--api-key-env", "default=None"),
        ("--provider-extra-body", "default=None"),
    )
    GUARD_FLAGS = (
        ("--min-roster", "default=1"),
        ("--max-hours", "default=0.0"),
    )

    def setUp(self):
        with open(SOURCE, encoding="utf-8") as fh:
            self.src = fh.read()

    def test_every_flag_the_experiments_use_is_present(self):
        for flag, _ in self.EXPERIMENT_FLAGS + self.GUARD_FLAGS:
            self.assertIn(
                f'"{flag}"', self.src,
                f"{flag} is missing; a recorded invocation uses it and the run "
                f"will die on 'unrecognized arguments'")

    def test_defaults_preserve_pre_merge_behaviour(self):
        """A changed default rewrites what every existing arm measured."""
        for flag, default in self.EXPERIMENT_FLAGS + self.GUARD_FLAGS:
            tail = self.src.split(f'"{flag}"', 1)[1][:200]
            self.assertIn(
                default, tail,
                f"{flag} no longer defaults to {default}; that silently changes "
                f"the instrument for every arm that does not pass it")

    def test_keep_traces_is_opt_in(self):
        tail = self.src.split('"--keep-traces"', 1)[1][:160]
        self.assertIn("store_true", tail)

    def test_the_670_guards_survived_the_merge(self):
        """The flags came from one copy and these from another; both must land."""
        self.assertIn('gold.get("roster") or []', self.src)
        self.assertIn("if len(roster) < args.min_roster:", self.src)
        self.assertIn("raise SystemExit", self.src)
        self.assertIn("if k == projected_at", self.src)
        self.assertIn("STOPPING: projected", self.src)
        self.assertTrue(
            any(l.startswith("def _sha256_file(") for l in self.src.splitlines()),
            "_sha256_file must be a real top-level function, not just a call "
            "site -- an earlier merge inserted the call without the def")

    def test_prompt_variants_import_resolves_in_this_tree(self):
        """attribution_prompt_variants lives at app/ here and app/experiments/
        in the probe checkouts. Importing by bare name is what works here, and
        getting it wrong makes --help itself fail."""
        self.assertIn("from attribution_prompt_variants import", self.src)
        self.assertNotIn("from experiments.attribution_prompt_variants", self.src)

    def test_help_actually_runs(self):
        """The cheapest end-to-end check: a broken import makes --help empty,
        which is exactly how the bad merge presented."""
        out = subprocess.run(
            [sys.executable, os.path.join("experiments", "lora_serving_eval.py"),
             "--help"],
            cwd=os.path.join(REPO, "app"), capture_output=True, text=True)
        self.assertEqual(0, out.returncode, out.stderr[-2000:])
        for flag, _ in self.EXPERIMENT_FLAGS + self.GUARD_FLAGS:
            self.assertIn(flag, out.stdout, f"{flag} absent from --help")


if __name__ == "__main__":
    unittest.main()
