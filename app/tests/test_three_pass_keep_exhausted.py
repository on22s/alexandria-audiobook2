"""on_exhaustion='keep': one line that runs out of retries no longer aborts the book.

Both fixtures are the stage-0 lines that stopped a DeepSeek run on 2026-09-28,
each refused by a check while the model's answer matched PDNC gold:
The Invisible Man's Silas Durgan (named once, so the two-mention rule refused
him) and Hard Times' "the Hands," (a quoted nickname pass 1 took for speech;
the model said nobody speaks it). Audit: ab_test_runtime/gutenberg_stage0/
rejection_audit.json.
"""
import io
import contextlib
import json
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
from generate_script import LLMGenParams

DURGAN_LINE = "if he chooses to show enself at\nfairs he’d make his fortune in no time,"
# Long enough for the attestation gate to apply (MIN_SOURCE_FOR_ATTESTATION),
# and Silas Durgan is written once, as in the novel.
DURGAN_SOURCE = ("Mr. Fearenside talked. " * 300 + "Another school of opinion followed "
                 "Mr. Fearenside; as, for instance, Silas\nDurgan, who was heard to "
                 'assert that "' + DURGAN_LINE + '" and so on. ' + "Mr. Hall nodded. " * 100)


def _client_always(attribution, instruct=None, segment=None):
    """Answers every attribution request with `attribution`, every other with
    `segment` first (when given) and then `instruct`."""
    calls = {"attribute": 0, "other": 0}

    def create(**kwargs):
        schema = ((kwargs.get("response_format") or {}).get("json_schema") or {}).get("name")
        if schema == "speaker_attribution":
            calls["attribute"] += 1
            payload = attribution
        else:
            calls["other"] += 1
            payload = segment if (segment is not None and calls["other"] == 1) else instruct
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)),
            finish_reason="stop")], usage=None)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    return client, calls


def _params():
    return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                        max_tokens=500, temperature=0.0)


def _attribute(frozen, answer, mode, source_text=None, keep_scope="line", cast=None):
    client, calls = _client_always(answer)
    with contextlib.redirect_stdout(io.StringIO()):
        out = tp.attribute_batch(client, "m", frozen, _params(), roster=[],
                                 max_retries=1, on_exhaustion=mode,
                                 source_text=source_text, keep_scope=keep_scope, cast=cast)
    _attribute.calls = calls["attribute"]
    return out


class KeepExhaustedAnswerTests(unittest.TestCase):
    def test_once_named_speaker_is_refused_then_kept_flagged(self):
        frozen = [{"type": "SPOKEN", "text": DURGAN_LINE}]
        answer = [{"n": 0, "speaker": "SILAS DURGAN"}]
        with self.assertRaises(tp.PassExhausted):   # the check does refuse him
            _attribute(frozen, answer, "fail", DURGAN_SOURCE)
        out = _attribute(frozen, answer, "keep", DURGAN_SOURCE)
        self.assertEqual("SILAS DURGAN", out[0]["speaker"])
        self.assertEqual(["speaker_not_in_source"], out[0]["attribution_unchecked"])
        self.assertEqual(DURGAN_LINE, out[0]["text"])          # text stays frozen

    def test_quoted_nickname_keeps_the_answer_that_nobody_speaks(self):
        out = _attribute([{"type": "SPOKEN", "text": "the Hands,"}],
                         [{"n": 0, "speaker": "NARRATOR"}], "keep")
        self.assertEqual("NARRATOR", out[0]["speaker"])
        self.assertEqual(["spoken_not_named"], out[0]["attribution_unchecked"])

    def test_renamed_narration_is_not_kept(self):
        """narrator_renamed was right 29 of 29 in the audit: the model had put
        the speaker on a speech tag. 'keep' must not accept that answer."""
        out = _attribute([{"type": "NARRATOR", "text": "exclaimed Lucy."}],
                         [{"n": 0, "speaker": "LUCY"}], "keep")
        self.assertEqual("NARRATOR", out[0]["speaker"])
        self.assertEqual(["narrator_renamed"], out[0]["attribution_unchecked"])

    def test_unparseable_answer_gets_the_fallback_label(self):
        out = _attribute([{"type": "SPOKEN", "text": "Tell me."}], "not json at all", "keep")
        self.assertEqual("UNKNOWN", out[0]["speaker"])
        self.assertTrue(out[0]["attribution_unchecked"])

    def test_multi_entry_batch_still_raises_so_the_caller_subdivides(self):
        frozen = [{"type": "SPOKEN", "text": "the Hands,"},
                  {"type": "SPOKEN", "text": "Scarlet Coat"}]
        with self.assertRaises(tp.PassExhausted):
            _attribute(frozen, [{"n": 0, "speaker": "NARRATOR"},
                                {"n": 1, "speaker": "NARRATOR"}], "keep")

    def test_kept_names_stay_off_both_roster_gates(self):
        """A kept name bypassed a check, so it must not reach the roster that
        every later batch is told to trust - on the incremental path or on a
        resume's rebuild, which must agree."""
        source = "Silas Durgan came. Silas Durgan went. Silas Durgan stayed. " * 200
        kept = {"text": "x", "speaker": "SILAS DURGAN",
                "attribution_unchecked": ["speaker_not_in_source"]}
        checked = {"text": "y", "speaker": "SILAS DURGAN"}
        self.assertEqual([], tp.build_roster([kept], source))
        self.assertEqual([], tp.attested_new_speakers([kept], set(), source))
        self.assertEqual(["SILAS DURGAN"], tp.build_roster([checked], source))
        self.assertEqual(["SILAS DURGAN"], tp.attested_new_speakers([checked], set(), source))

    def test_keep_resumes_a_fail_checkpoint(self):
        """The app moved from 'fail' to 'keep'; a new identity would restart
        every half-generated book from pass 1."""
        fp = lambda mode: tp.three_pass_fingerprint("text", "m", 6000, on_exhaustion=mode)
        self.assertEqual(fp("fail"), fp("keep"))
        self.assertNotEqual(fp("fail"), fp("fallback"))

    def test_whole_book_completes_and_the_manifest_counts_the_kept_line(self):
        source = 'The workers of Coketown, generically called "the Hands," lived there.'
        client, calls = _client_always(
            attribution=[{"n": 0, "speaker": "NARRATOR"}],
            instruct=[{"n": 0, "instruct": "Plain."}, {"n": 1, "instruct": "Plain."},
                      {"n": 2, "instruct": "Plain."}])
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "book.json")
            with contextlib.redirect_stdout(io.StringIO()):
                entries = tp.run_three_pass(
                    client, "m", source,
                    LLMGenParams(max_tokens=500, temperature=0.0, segmentation="auto"),
                    chunk_size=6000, on_exhaustion="keep", output_path=out_path)
            manifest = json.load(open(tp.three_pass_manifest_path(out_path)))
        hands = [e for e in entries if e["text"] == "the Hands,"]
        self.assertEqual(1, len(hands))
        self.assertEqual(["spoken_not_named"], hands[0]["attribution_unchecked"])
        self.assertGreater(calls["attribute"], 1)                # it was retried first
        self.assertEqual(1, manifest["passes"]["attribute"]["unchecked_entries"])


if __name__ == "__main__":
    unittest.main()


# A three-line batch from The Invisible Man: Mr. Hall is attested, Silas Durgan
# is written once (refused by speaker_not_in_source), the tag is narration.
HALL_LINE = "Mr. Hall, we shall see."
BATCH = [{"type": "SPOKEN", "text": HALL_LINE},
         {"type": "SPOKEN", "text": DURGAN_LINE},
         {"type": "NARRATOR", "text": "said he."}]
BATCH_SOURCE = DURGAN_SOURCE + '"' + HALL_LINE + '" said he. '


class BatchKeepScopeTests(unittest.TestCase):
    """--pass2-keep-scope batch (#668): a batch whose ONLY failures are keepable
    is kept whole, flagging just the refused lines, instead of being halved
    down to them - one rejected label otherwise costs a halving per level."""

    ANSWER = [{"n": 0, "speaker": "MR. HALL"}, {"n": 1, "speaker": "SILAS DURGAN"},
              {"n": 2, "speaker": "NARRATOR"}]

    def test_line_scope_still_raises_so_the_caller_subdivides(self):
        with self.assertRaises(tp.PassExhausted):
            _attribute(BATCH, self.ANSWER, "keep", BATCH_SOURCE)

    def test_batch_scope_keeps_the_batch_and_flags_only_the_refused_line(self):
        out = _attribute(BATCH, self.ANSWER, "keep", BATCH_SOURCE, keep_scope="batch")
        self.assertEqual(2, _attribute.calls)                   # max_retries=1: no subdivision
        self.assertEqual(["MR. HALL", "SILAS DURGAN", "NARRATOR"], [e["speaker"] for e in out])
        self.assertNotIn("attribution_unchecked", out[0])
        self.assertEqual(["speaker_not_in_source"], out[1]["attribution_unchecked"])
        self.assertNotIn("attribution_unchecked", out[2])
        self.assertEqual([e["text"] for e in BATCH], [e["text"] for e in out])   # text frozen

    def test_batch_scope_still_subdivides_a_mixed_failure(self):
        """narrator_renamed is not keepable (right 29 of 29 in the audit), so a
        batch that also renamed narration must still be split, not kept."""
        renamed = self.ANSWER[:2] + [{"n": 2, "speaker": "MR. HALL"}]
        with self.assertRaises(tp.PassExhausted):
            _attribute(BATCH, renamed, "keep", BATCH_SOURCE, keep_scope="batch")

    def test_batch_scope_still_subdivides_a_misaligned_answer(self):
        with self.assertRaises(tp.PassExhausted):
            _attribute(BATCH, self.ANSWER[:2], "keep", BATCH_SOURCE, keep_scope="batch")

    def test_batch_scope_needs_keep(self):
        with self.assertRaises(tp.PassExhausted):
            _attribute(BATCH, self.ANSWER, "fail", BATCH_SOURCE, keep_scope="batch")

    def test_a_kept_cast_alias_is_folded_like_an_accepted_one(self):
        """One voice per character on both paths: GRIFFIN is THE STRANGER
        whether the batch passed or was kept."""
        cast = {"alias_to_name": {"GRIFFIN": "THE STRANGER"}, "known_names": frozenset()}
        answer = [{"n": 0, "speaker": "GRIFFIN"}, {"n": 1, "speaker": "SILAS DURGAN"},
                  {"n": 2, "speaker": "NARRATOR"}]
        out = _attribute(BATCH, answer, "keep", BATCH_SOURCE, keep_scope="batch", cast=cast)
        self.assertEqual("THE STRANGER", out[0]["speaker"])

    def test_the_kept_label_stays_off_the_roster(self):
        out = _attribute(BATCH, self.ANSWER, "keep", BATCH_SOURCE, keep_scope="batch")
        self.assertNotIn("SILAS DURGAN", tp.attested_new_speakers(out, set(), BATCH_SOURCE))

    def test_scope_does_not_change_the_checkpoint_identity(self):
        """A run may resume under either scope: everything either one saves is
        valid output and anything kept is flagged."""
        import inspect
        self.assertNotIn("keep_scope", inspect.signature(tp.three_pass_fingerprint).parameters)

