"""Issue #644: quote classification constraints are independent and optional."""
import json
import unittest
from types import SimpleNamespace

from generate_script import LLMGenParams
from pass_quality import validate_segment_quality
import three_pass_generate as tp


QUOTED_TERM = 'That aura would leave something known as a "Mana Trail". It was evidence.'
MERGED_TERM = [{"type": "NARRATOR", "text": QUOTED_TERM}]


class _Client:
    def __init__(self, entries):
        self.calls = []

        def create(**kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(entries)),
                finish_reason="stop")], usage=None)

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


class FidelityGateControlTests(unittest.TestCase):
    def test_quoted_rule_can_be_disabled_without_disabling_other_checks(self):
        strict = validate_segment_quality(QUOTED_TERM, MERGED_TERM)
        self.assertFalse(strict["passed"])
        relaxed = validate_segment_quality(
            QUOTED_TERM, MERGED_TERM, quoted_must_be_spoken=False)
        self.assertTrue(relaxed["passed"], relaxed["findings"])

        dropped = [{"type": "NARRATOR", "text":
                    'That aura would leave something known as a "Mana Trail".'}]
        report = validate_segment_quality(
            QUOTED_TERM, dropped, quoted_must_be_spoken=False)
        self.assertFalse(report["passed"])
        self.assertIn("low_source_token_recall",
                      {finding["code"] for finding in report["findings"]})

    def test_unquoted_rule_can_be_disabled_independently(self):
        source = 'She said "Go." Then the unquoted cry rang out. Haaaaaah!'
        entries = [
            {"type": "NARRATOR", "text": "She said"},
            {"type": "SPOKEN", "text": "Go."},
            {"type": "NARRATOR", "text": "Then the unquoted cry rang out."},
            {"type": "SPOKEN", "text": "Haaaaaah!"},
        ]
        strict = validate_segment_quality(source, entries)
        self.assertFalse(strict["passed"])
        relaxed = validate_segment_quality(
            source, entries, unquoted_must_be_narrator=False)
        self.assertTrue(relaxed["passed"], relaxed["findings"])

    def test_auto_asks_the_model_and_does_not_rewrite_its_quoted_narrator(self):
        client = _Client(MERGED_TERM)
        params = LLMGenParams(
            max_tokens=500, segmentation="auto", quoted_must_be_spoken=False)
        out = tp.segment_chunk_adaptively(client, "m", QUOTED_TERM, params)
        self.assertEqual(MERGED_TERM, out)
        self.assertEqual(1, len(client.calls))
        system = client.calls[0]["messages"][0]["content"]
        self.assertIn("Quoted text is not required to be SPOKEN", system)
        self.assertIn("Unquoted text MUST be NARRATOR", system)

    def test_controls_change_checkpoint_identity_only_when_nondefault(self):
        default = tp.three_pass_fingerprint(
            "text", "m", 3000, LLMGenParams(segmentation="auto"))
        quoted_relaxed = tp.three_pass_fingerprint(
            "text", "m", 3000,
            LLMGenParams(segmentation="auto", quoted_must_be_spoken=False))
        unquoted_relaxed = tp.three_pass_fingerprint(
            "text", "m", 3000,
            LLMGenParams(segmentation="auto", unquoted_must_be_narrator=False))
        self.assertNotEqual(default, quoted_relaxed)
        self.assertNotEqual(default, unquoted_relaxed)
        self.assertNotEqual(quoted_relaxed, unquoted_relaxed)


if __name__ == "__main__":
    unittest.main()
