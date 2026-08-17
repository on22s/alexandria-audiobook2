"""One source for "which LLM endpoint is this run using?" (goal 6.2).

WHAT THIS COST. On 2026-08-06 a run dialled a dead endpoint for an hour while
a working server sat idle, because `config["llm"]` is a MIRROR of the active
profile rather than a source. `/api/config` copies the profile named by
`llm_mode` into `llm` and refuses to save a disagreement - but anything
writing config.json outside that endpoint (a benchmark caching concurrency
into `llm_local`, a hand edit, a script) updates one and not the other.

The rule then existed in two hand-written spellings, in two files, each with
a comment citing Rule 15. These tests pin the single implementation and the
agreement it is supposed to guarantee.
"""
import re
import unittest
from pathlib import Path

from lmstudio_settings import get_active_llm_config

APP = Path(__file__).resolve().parent.parent

LOCAL = {"base_url": "http://127.0.0.1:8090/v1", "model_name": "qwen3-14b"}
REMOTE = {"base_url": "http://10.0.0.5:1234/v1", "model_name": "llama-3.3-70b"}


class ActiveProfileTests(unittest.TestCase):

    def test_the_toggle_decides_which_profile_is_active(self):
        config = {"llm_mode": "remote", "llm_local": LOCAL, "llm_remote": REMOTE,
                  "llm": LOCAL}
        self.assertEqual(REMOTE, get_active_llm_config(config))

    def test_a_stale_mirror_does_not_win_over_the_toggle(self):
        """The 2026-08-06 failure, as a test.

        `llm` still points at the endpoint that was active before the toggle
        moved. Reading the mirror dials the dead one; reading the toggle's
        profile dials the live one.
        """
        dead = {"base_url": "http://127.0.0.1:9999/v1", "model_name": "gone"}
        config = {"llm_mode": "local", "llm_local": LOCAL, "llm": dead}
        self.assertEqual(LOCAL, get_active_llm_config(config),
                         "the profile named by llm_mode is the source; llm is "
                         "a mirror that can go stale")

    def test_a_config_predating_the_toggle_still_resolves(self):
        """Migration: only `llm` exists, no llm_local/llm_remote yet."""
        self.assertEqual(LOCAL, get_active_llm_config({"llm": LOCAL}))
        self.assertEqual(
            LOCAL, get_active_llm_config({"llm_mode": "local", "llm": LOCAL}))

    def test_remote_mode_with_no_remote_profile_falls_back_not_empty(self):
        config = {"llm_mode": "remote", "llm": REMOTE}
        self.assertEqual(REMOTE, get_active_llm_config(config))

    def test_an_empty_profile_is_not_treated_as_configured(self):
        config = {"llm_mode": "remote", "llm_remote": {}, "llm": LOCAL}
        self.assertEqual(LOCAL, get_active_llm_config(config))

    def test_garbage_never_raises(self):
        for bad in (None, [], "config", 7, {"llm_mode": "local", "llm_local": "x"}):
            self.assertEqual({}, get_active_llm_config(bad)
                             if not isinstance(bad, dict) else
                             get_active_llm_config(bad) or {})


class CopiesAgreeTests(unittest.TestCase):
    """Goal 6.2's target: each parallel definition gets a test asserting the
    copies agree. These are the two spellings that existed before, kept here
    as the reference behaviour the single implementation must reproduce."""

    @staticmethod
    def _attribution_adapter_spelling(config):
        mode = config.get("llm_mode", "local")
        return config.get(f"llm_{mode}") or config.get("llm") or {}

    @staticmethod
    def _repair_source_spelling(config):
        mode = config.get("llm_mode", "local")
        return (config.get("llm_remote" if mode == "remote" else "llm_local")
                or config.get("llm") or {})

    def test_both_former_spellings_agree_with_the_single_implementation(self):
        configs = [
            {"llm_mode": "local", "llm_local": LOCAL, "llm_remote": REMOTE, "llm": LOCAL},
            {"llm_mode": "remote", "llm_local": LOCAL, "llm_remote": REMOTE, "llm": REMOTE},
            {"llm_mode": "remote", "llm_local": LOCAL, "llm_remote": None, "llm": LOCAL},
            {"llm_mode": "local", "llm": LOCAL},
            {"llm_mode": "remote", "llm": REMOTE},
            {"llm": LOCAL},
            {"llm_mode": "local", "llm_local": {}, "llm": LOCAL},
        ]
        for config in configs:
            with self.subTest(config=config):
                resolved = get_active_llm_config(config)
                self.assertEqual(self._attribution_adapter_spelling(config), resolved)
                self.assertEqual(self._repair_source_spelling(config), resolved)


class SingleImplementationTests(unittest.TestCase):
    """Same shape as test_generation_path_agreement's
    `test_the_policy_lives_in_exactly_one_place`: two copies of one decision
    will drift, so assert there is only one."""

    # mode-aware resolution written out by hand: llm_<mode> or llm
    HAND_ROLLED = re.compile(
        r"""get\(\s*f?["']llm_(\{mode\}|local|remote)["']"""
        r"""[^\n]*\bor\b[^\n]*get\(\s*["']llm["']""")

    # benchmark_runner._get_llm_benchmark_target resolves an explicitly NAMED
    # target ("local" / "thunder") so it can measure both endpoints regardless
    # of the toggle. That is a different question from "what is active", and
    # routing it through get_active_llm_config would collapse both targets
    # onto whichever one the toggle happens to name.
    EXEMPT = {"benchmark_runner.py"}

    def test_the_active_profile_rule_lives_in_one_place(self):
        offenders = []
        for path in sorted(APP.rglob("*.py")):
            if path.name.startswith("test_") or path.name == "lmstudio_settings.py":
                continue
            if path.name in self.EXEMPT:
                continue
            if "env" in path.parts or "site-packages" in path.parts:
                continue
            for number, line in enumerate(
                    path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if self.HAND_ROLLED.search(line):
                    offenders.append(f"{path.relative_to(APP)}:{number}: {line.strip()}")
        self.assertEqual([], offenders,
                         "resolve the active LLM profile with "
                         "lmstudio_settings.get_active_llm_config instead of "
                         "spelling the rule out again:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
