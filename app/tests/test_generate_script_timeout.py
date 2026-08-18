"""Every LLM client here must have a finite timeout.

On 2026-08-18 the unseen_books run held the GPU for two hours having used two
SECONDS of CPU. One request never returned, generate_script's client had no
timeout, and the job slept while the queue waited behind it and the deadline
passed. core._make_llm_client had defaulted to 60s the whole time - two ways
to build the same client, disagreeing (Rule 15).

The test is on the SOURCE of the constructor call rather than a live client
because building one requires config and a server; what must never come back
is a bare OpenAI(base_url, api_key) with nothing bounding it.
"""
import os
import re
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class LlmClientTimeoutTest(unittest.TestCase):
    def _sources(self):
        for name in ("generate_script.py", "review_script.py", "core.py",
                     "find_nicknames.py", "generate_personas.py"):
            path = os.path.join(APP, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as fh:
                    yield name, fh.read()

    @staticmethod
    def _calls(source):
        """Every OpenAI( ... ) construction, to its BALANCED closing paren.

        The first version of this used a regex that stopped at the first ")",
        which lands inside `api_key=cfg.get("api_key", "local")` - so it
        reported core.py, the one module that had always passed a timeout, and
        cleared nobody. An instrument that mis-scores the case you already know
        is not measuring the thing you asked it about (Rule 21).
        """
        out = []
        for start in (m.end() for m in re.finditer(r"\bOpenAI\(", source)):
            depth, i = 1, start
            while i < len(source) and depth:
                depth += (source[i] == "(") - (source[i] == ")")
                i += 1
            out.append(source[start:i - 1])
        return out

    def test_the_scanner_sees_a_timeout_hidden_behind_a_nested_call(self):
        """Guards the bug above: keep the scanner honest on a known case."""
        sample = 'OpenAI(base_url=u, api_key=cfg.get("api_key", "local"), timeout=5)'
        self.assertTrue(any("timeout" in c for c in self._calls(sample)))

    def test_the_scanner_still_catches_a_client_with_no_timeout(self):
        sample = 'OpenAI(base_url=u, api_key=cfg.get("api_key", "local"))'
        self.assertFalse(any("timeout" in c for c in self._calls(sample)))

    def test_no_openai_client_is_built_without_a_timeout(self):
        offenders = [f"{name}: OpenAI({call[:60]}...)"
                     for name, source in self._sources()
                     for call in self._calls(source)
                     if "timeout" not in call]
        self.assertEqual([], offenders,
                         "an LLM client with no timeout can hang forever, "
                         "holding the GPU and the queue behind it")

    def test_every_caller_shares_one_definition_of_how_long_is_too_long(self):
        # generate_script had none while core had 60s - two answers to one
        # question, which is how the hang went unnoticed (Rule 15).
        for name, source in self._sources():
            for call in self._calls(source):
                if "timeout=" in call and name != "core.py":
                    self.assertIn("llm_timeout_seconds", call,
                                  f"{name} sets its own timeout instead of "
                                  f"using the shared one")
