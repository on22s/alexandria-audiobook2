"""A finished arm may be reused; a half-finished one may not.

The three-pass arm writes `threepass_manifest.json`, never
`generation_quality.json`, so `_is_reusable` returned False for every
three-pass arm that had ever completed. Invisible until a stage ran long: on
2026-08-21 the light-novel stage hit its 6h cap at chunk 86 of 110 of its third
book, and the resume would have re-run four COMPLETED three-pass arms - about
92 minutes of card time to reproduce files already on disk.

The risk in fixing it is the opposite error: reusing a run that did NOT finish.
Every rejection below is a way a manifest can exist and still not be evidence,
and each is tested separately so a future loosening of one cannot be hidden by
the others passing.
"""
import json
import os
import tempfile
import unittest

from experiments.three_pass_vs_single import _is_reusable

MODEL = "qwen3-14b"


def manifest(status="complete", total=165, done=165, failures=(), model=MODEL):
    return {"status": status,
            "progress": {"chunks_total": total, "chunks_completed": done},
            "diagnostic_failures": list(failures),
            "fingerprint": {"model_name": model, "pipeline": "three_pass"}}


def quality(status="complete", total=82, accepted=82, model=MODEL):
    return {"status": status, "total_chunks": total,
            "accepted_chunk_count": accepted, "model_name": model}


class ReuseTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _arm(self, sibling_suffix=None, record=None, write_output=True):
        out = os.path.join(self.root, "book__arm.json")
        if write_output:
            with open(out, "w", encoding="utf-8") as handle:
                json.dump([{"speaker": "A", "text": "x"}], handle)
        if sibling_suffix is not None:
            with open(out + sibling_suffix, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
        return out

    # --- the three-pass arm, which is what changed ------------------------
    def test_a_complete_manifest_is_reusable(self):
        out = self._arm(".threepass_manifest.json", manifest())
        self.assertTrue(_is_reusable(out, MODEL))

    def test_a_manifest_short_of_its_chunk_count_is_not(self):
        """The 2026-08-21 shape: killed at 86 of 110."""
        out = self._arm(".threepass_manifest.json", manifest(total=110, done=86))
        self.assertFalse(_is_reusable(out, MODEL))

    def test_a_manifest_not_marked_complete_is_not(self):
        out = self._arm(".threepass_manifest.json", manifest(status="running"))
        self.assertFalse(_is_reusable(out, MODEL))

    def test_a_manifest_with_diagnostic_failures_is_not(self):
        out = self._arm(".threepass_manifest.json",
                        manifest(failures=[{"chunk": 12, "why": "unattributable"}]))
        self.assertFalse(_is_reusable(out, MODEL))

    def test_a_manifest_from_another_model_is_not(self):
        out = self._arm(".threepass_manifest.json", manifest(model="magistral-small"))
        self.assertFalse(_is_reusable(out, MODEL))

    def test_the_model_check_is_skipped_when_no_model_is_named(self):
        out = self._arm(".threepass_manifest.json", manifest(model="anything"))
        self.assertTrue(_is_reusable(out, None))

    def test_a_manifest_with_no_chunk_total_is_not_reusable(self):
        out = self._arm(".threepass_manifest.json", manifest(total=None, done=None))
        self.assertFalse(_is_reusable(out, MODEL))

    # --- the single-pass arm must behave exactly as before ----------------
    def test_a_complete_quality_record_is_still_reusable(self):
        out = self._arm(".generation_quality.json", quality())
        self.assertTrue(_is_reusable(out, MODEL))

    def test_an_incomplete_quality_record_is_still_rejected(self):
        out = self._arm(".generation_quality.json", quality(accepted=80))
        self.assertFalse(_is_reusable(out, MODEL))

    def test_a_quality_record_from_another_model_is_still_rejected(self):
        out = self._arm(".generation_quality.json", quality(model="magistral-small"))
        self.assertFalse(_is_reusable(out, MODEL))

    # --- absence of evidence ---------------------------------------------
    def test_output_with_no_sibling_record_is_not_reusable(self):
        """A leftover file proves nothing - the original reason this exists."""
        out = self._arm()
        self.assertFalse(_is_reusable(out, MODEL))

    def test_a_missing_output_is_not_reusable(self):
        out = self._arm(".threepass_manifest.json", manifest(), write_output=False)
        self.assertFalse(_is_reusable(out, MODEL))

    def test_an_unreadable_sibling_is_not_reusable(self):
        out = os.path.join(self.root, "book__arm.json")
        with open(out, "w", encoding="utf-8") as handle:
            handle.write("[]")
        with open(out + ".threepass_manifest.json", "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertFalse(_is_reusable(out, MODEL))

    def test_a_quality_record_wins_when_both_are_present(self):
        """Different arms write different records; a file with both is a bug,
        and taking the stricter historical path is the safe reading."""
        out = self._arm(".generation_quality.json", quality(accepted=1, total=82))
        with open(out + ".threepass_manifest.json", "w", encoding="utf-8") as handle:
            json.dump(manifest(), handle)
        self.assertFalse(_is_reusable(out, MODEL))
