"""Pass 1 asks again when a clause is dropped, then warns (#1921). Synthetic text only.

On the reported book the model dropped an eight-word narration clause from a
chunk of about a thousand words; 90% recall let it through silently. What these
verify:
- the pass-1 gate reports a missing run of distinct words, and does not report
  a squashed repetition ("hello hello hello ...") or a complete conversion;
- such a report is retried with the missing words named in the feedback;
- a model that restores the clause on retry is accepted with the full text;
- a model that never restores it is accepted after the retries, as before this
  check existed, with a visible Warning line - never a failed run;
- reports that also lose general recall are still not near misses.
"""
import contextlib
import io
import json
from types import SimpleNamespace
import unittest

import three_pass_generate as tp
from chunk_quality import is_segment_near_miss
from generate_script import LLMGenParams, _build_retry_feedback_message
from pass_quality import validate_segment_quality

PROSE = ("The morning bell rang across the courtyard and every student hurried toward the "
         "great hall, where the instructors were already waiting in a long and silent line. ")
CLAUSE = "The old guard was a man who could only ever whisper"
SOURCE = PROSE * 4 + CLAUSE + ". " + PROSE * 4
DROPPED = PROSE * 4 + PROSE * 4


def narration(text):
    return [{"type": "NARRATOR", "text": text.strip()}]


def codes(report):
    return {finding["code"] for finding in report["findings"]}


class MissingSourceRunGateTests(unittest.TestCase):
    def test_dropped_clause_is_reported_and_retry_names_it(self):
        report = validate_segment_quality(SOURCE, narration(DROPPED))
        self.assertGreaterEqual(report["metrics"]["source_token_recall"], 0.9)
        self.assertEqual({"missing_source_run"}, codes(report))
        self.assertTrue(is_segment_near_miss(report))
        message = _build_retry_feedback_message(report)
        self.assertIn("left out part of the source text", message)
        self.assertIn("could only ever whisper", message)

    def test_complete_and_squashed_repetition_are_not_reported(self):
        self.assertTrue(validate_segment_quality(SOURCE, narration(SOURCE))["passed"])
        source = PROSE * 3 + "[" + " ".join(["Hello"] * 19) + "?] " + PROSE * 3
        squashed = PROSE * 3 + "Hello Hello Hello...? " + PROSE * 3
        self.assertNotIn("missing_source_run",
                         codes(validate_segment_quality(source, narration(squashed))))

    def test_general_loss_is_not_a_near_miss(self):
        report = validate_segment_quality(SOURCE, narration(PROSE * 2))
        self.assertIn("low_source_token_recall", codes(report))
        self.assertFalse(is_segment_near_miss(report))


def scripted_client(replies):
    calls = []

    def create(**kwargs):
        calls.append(kwargs["messages"][-1]["content"])
        text = replies[min(len(calls), len(replies)) - 1]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(narration(text))), finish_reason="stop")],
            usage=None)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


class MissingSourceRunRetryTests(unittest.TestCase):
    def run_pass_one(self, replies):
        client, calls = scripted_client(replies)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            resolutions = []
            entries = tp.segment_chunk_adaptively(
                client, "m", SOURCE,
                LLMGenParams(max_tokens=2000, temperature=0.1, segmentation="llm"),
                resolution_sink=resolutions)
        return entries, calls, out.getvalue()

    def test_restored_on_retry_is_accepted_whole(self):
        entries, calls, log = self.run_pass_one([DROPPED, SOURCE])
        self.assertEqual(2, len(calls))
        self.assertIn(CLAUSE.lower().split()[2], " ".join(e["text"] for e in entries).lower())
        self.assertIn("could only ever whisper", calls[1].lower())
        self.assertNotIn("Warning: Step 1 accepted", log)

    def test_never_restored_is_accepted_with_a_warning(self):
        entries, calls, log = self.run_pass_one([DROPPED])
        self.assertGreater(len(calls), 1)
        # One round of retries, no adaptive split: a split down to the floor
        # could cost dozens of calls for the same warning.
        self.assertLessEqual(len(calls), 5)
        self.assertNotIn("Adaptive split", log)
        self.assertTrue(entries, "a run that succeeded before this check must not fail")
        self.assertIn("Warning: Step 1 accepted this chunk with about 11 source words", log)

    def test_complete_reordering_outranks_a_dropped_run(self):
        """Two near misses in one call: a complete answer that only reordered
        words (trigram 0.85) is kept over a later one that dropped the clause,
        although the latter has the higher trigram recall."""
        import re
        sentences = [s for s in re.split(r"(?<=\.) ", SOURCE) if s]
        for i in range(0, len(sentences), 4):
            words = sentences[i].split()
            words[1], words[2] = words[2], words[1]
            words[-2], words[-3] = words[-3], words[-2]
            words[5], words[6] = words[6], words[5]
            sentences[i] = " ".join(words)
        reordered = " ".join(sentences)
        self.assertEqual({"low_ordered_trigram_recall"},
                         codes(validate_segment_quality(SOURCE, narration(reordered))))
        client, calls = scripted_client([reordered, DROPPED])
        sink = []
        with contextlib.redirect_stdout(io.StringIO()):
            tp.segment_chunk(client, "m", SOURCE,
                             LLMGenParams(max_tokens=2000, temperature=0.1, segmentation="llm"),
                             max_retries=1, near_miss_sink=sink)
        self.assertEqual(2, len(calls))
        self.assertEqual({"low_ordered_trigram_recall"}, codes(sink[0][1]))
        self.assertIn("guard old was a who man", " ".join(e["text"] for e in sink[0][0]))


class CharacterScriptTests(unittest.TestCase):
    """CJK, kana, Hangul and Thai are tokenised one character per token."""
    SENTENCE = "他缓缓站了起来，然后转身离开了房间。"
    CLAUSE = ("城门外的守卫是一个只会低声"
              "说话的老人，他从不大声喊叫。")

    def test_a_short_rewording_is_not_a_missing_run(self):
        source = self.SENTENCE * 6
        output = self.SENTENCE * 5 + self.SENTENCE[7:]
        self.assertNotIn("missing_source_run", codes(validate_segment_quality(source, narration(output))))

    def test_a_dropped_clause_is_reported_in_characters(self):
        source = self.SENTENCE * 20 + self.CLAUSE + self.SENTENCE * 20
        report = validate_segment_quality(source, narration(self.SENTENCE * 40))
        self.assertIn("missing_source_run", codes(report))
        self.assertEqual({"characters"}, {span["unit"] for span in report["missing_source_spans"]})
        self.assertIn("source characters still missing", tp.get_missing_run_warning(report))


if __name__ == "__main__":
    unittest.main()
