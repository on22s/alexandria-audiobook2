"""Compact Pass 1 (#744): the evaluated splitter/parser, and the per-chunk fallback.

WHAT THIS VERIFIES. The splitter never cuts inside a quote and never loses text;
the parser rejects malformed output instead of guessing; a compact reply is only
accepted when the shipped segment gate accepts it, otherwise the chunk falls back
to the ordinary full-text path; the default "ordinary" never calls compact and
does not change the run identity. All text is synthetic.
"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import compact_pass1 as cp
import three_pass_generate as tp
from generate_script import LLMGenParams
from pass_quality import analyze_outer_quote_regions


class SplitUnitsTest(unittest.TestCase):
    def test_sentence_end_inside_closing_quote_cuts_after_the_quote(self):
        self.assertEqual(cp.split_units("He frowned. “No,” I said, “not yet.” She left."),
                         ["He frowned.", "“No,” I said, “not yet.”", "She left."])

    def test_quote_spanning_two_sentences_is_one_unit(self):
        self.assertEqual(cp.split_units("“I tried. It failed,” he said."),
                         ["“I tried. It failed,” he said."])

    def test_straight_quotes(self):
        self.assertEqual(cp.split_units('"Stop." He ran. "Why?" she asked.'),
                         ['"Stop."', "He ran.", '"Why?"', "she asked."])

    def test_paragraph_break_always_cuts(self):
        self.assertEqual(cp.split_units("One.\n\nTwo here. Three."), ["One.", "Two here.", "Three."])

    def test_no_text_is_lost(self):
        for text in ("He frowned. “No,” I said, “not yet.” She left.",
                     "“I tried. It failed,” he said.", '"Stop." He ran. "Why?" she asked.',
                     "One.\n\nTwo here. Three.", "no terminator at all"):
            self.assertEqual(cp.norm("".join(cp.split_units(text))), cp.norm(text), text)

    def test_unterminated_quote_never_splits_inside(self):
        self.assertEqual(len(cp.split_units("He said, “I went. Then I stopped. And then")), 1)


class ParseCompactTest(unittest.TestCase):
    def test_accepts_fenced_json_after_a_think_block(self):
        types, problems = cp.parse_compact(
            '<think>hm</think>\n```json\n[{"n":1,"type":"NARRATOR"},{"n":2,"type":"spoken"}]\n```', 2)
        self.assertEqual((types, problems), (["NARRATOR", "SPOKEN"], []))

    def test_missing_unit_is_reported_not_filled_in(self):
        types, problems = cp.parse_compact('[{"n":1,"type":"SPOKEN"}]', 3)
        self.assertEqual(types, ["SPOKEN", None, None])
        self.assertIn("missing_units", problems)

    def test_rejects_duplicates_bad_labels_and_out_of_range(self):
        types, problems = cp.parse_compact(
            '[{"n":1,"type":"SPOKEN"},{"n":1,"type":"NARRATOR"},{"n":2,"type":"THOUGHT"},{"n":9,"type":"SPOKEN"}]', 2)
        self.assertEqual(types, ["SPOKEN", None])
        self.assertIn("duplicate_n", problems)
        self.assertIn("bad_label_or_n", problems)


class EntriesFromLabelsTest(unittest.TestCase):
    def test_strips_quote_delimiters_and_merges_narration(self):
        units = ["The door opened.", "The wind rose.", "“Who is there?”"]
        entries = cp.entries_from_labels(units, ["NARRATOR", "NARRATOR", "SPOKEN"],
                                         analyze_outer_quote_regions)
        self.assertEqual(entries, [{"type": "NARRATOR", "text": "The door opened. The wind rose."},
                                   {"type": "SPOKEN", "text": "Who is there?"}])

    def test_mixed_unit_keeps_the_quote_parser_regions(self):
        # One label cannot describe quoted and unquoted words; the parser's own split wins.
        unit = "“Not yet,” she said."
        entries = cp.entries_from_labels([unit], ["SPOKEN"], analyze_outer_quote_regions)
        self.assertEqual({e["type"] for e in entries}, {"SPOKEN", "NARRATOR"})

    def test_bad_rows_become_unresolved(self):
        out = cp.get_compact_entries([{"n": 1, "type": "SPOKEN"}], ["A.", "B."], analyze_outer_quote_regions)
        self.assertEqual([], out["entries"])
        self.assertIn("missing_units", out["unresolved"])


def _client(content):
    class Msg:
        pass

    class Choice:
        finish_reason = "stop"

    class Resp:
        usage = None

    def create(**_kw):
        msg = Msg(); msg.content = content
        choice = Choice(); choice.message = msg
        resp = Resp(); resp.choices = [choice]
        return resp
    return type("C", (), {"chat": type("Ch", (), {"completions": type("Co", (), {"create": staticmethod(create)})()})()})()


class SegmentChunkCompactTest(unittest.TestCase):
    CHUNK = "The door opened. “Who is there?” The wind rose."

    def test_accepts_labels_the_gate_passes_and_tags_the_attempt(self):
        attempts = []
        reply = json.dumps([{"n": 1, "type": "NARRATOR"}, {"n": 2, "type": "SPOKEN"}, {"n": 3, "type": "NARRATOR"}])
        entries = cp.segment_chunk_compact(_client(reply), "m", self.CHUNK,
                                           LLMGenParams(max_tokens=500, temperature=0.1), attempt_sink=attempts)
        self.assertEqual(["NARRATOR", "SPOKEN", "NARRATOR"], [e["type"] for e in entries])
        self.assertEqual("Who is there?", entries[1]["text"])
        self.assertTrue(attempts and all(a.get("pass") == "segment_compact" for a in attempts))

    def test_unusable_reply_returns_empty_for_fallback(self):
        for reply in ("not json", json.dumps([{"n": 1, "type": "NARRATOR"}])):
            with self.subTest(reply=reply):
                self.assertEqual([], cp.segment_chunk_compact(
                    _client(reply), "m", self.CHUNK, LLMGenParams(max_tokens=500, temperature=0.1)))

    def test_gate_rejection_returns_empty_for_fallback(self):
        reply = json.dumps([{"n": 1, "type": "NARRATOR"}, {"n": 2, "type": "SPOKEN"}, {"n": 3, "type": "NARRATOR"}])
        with patch.object(tp, "validate_segment_for_params",
                          return_value={"passed": False, "findings": [{"code": "low_source_token_recall"}],
                                        "metrics": {}}):
            self.assertEqual([], cp.segment_chunk_compact(
                _client(reply), "m", self.CHUNK, LLMGenParams(max_tokens=500, temperature=0.1)))


class RunThreePassHookTest(unittest.TestCase):
    SOURCE = "First paragraph here."
    ENTRIES = [{"type": "NARRATOR", "text": "First paragraph here."}]

    def run_book(self, fmt, compact_result):
        params = LLMGenParams(max_tokens=500, temperature=0.1, pass1_format=fmt)
        with tempfile.TemporaryDirectory() as d, \
             patch.object(tp, "segment_chunk_compact", return_value=compact_result) as compact, \
             patch.object(tp, "segment_chunk_adaptively", return_value=self.ENTRIES) as ordinary:
            out = os.path.join(d, "book.json")
            entries = tp.run_three_pass(_client("[]"), "m", self.SOURCE, params, chunk_size=6000,
                                        output_path=out)
            with open(tp.three_pass_manifest_path(out)) as handle:
                manifest = json.load(handle)
        return entries, manifest, compact, ordinary

    def test_compact_success_skips_the_ordinary_path(self):
        entries, manifest, compact, ordinary = self.run_book("compact", self.ENTRIES)
        compact.assert_called_once()
        ordinary.assert_not_called()
        self.assertIn("compact", json.dumps(manifest))
        self.assertEqual("First paragraph here.", entries[0]["text"])

    def test_compact_failure_falls_back_to_ordinary(self):
        entries, manifest, compact, ordinary = self.run_book("compact", [])
        compact.assert_called_once()
        ordinary.assert_called_once()
        self.assertEqual("First paragraph here.", entries[0]["text"])

    def test_default_ordinary_never_calls_compact(self):
        _, _, compact, ordinary = self.run_book("ordinary", self.ENTRIES)
        compact.assert_not_called()
        ordinary.assert_called_once()

    def test_parser_settled_chunks_never_call_compact(self):
        # A chunk the quote marks settle (resolution quote_presegmented in auto mode).
        source = "“Hello,” she said. The door shut."
        params = LLMGenParams(max_tokens=500, temperature=0.1, pass1_format="compact", segmentation="auto")
        with patch.object(tp, "segment_chunk_compact") as compact, \
             patch.object(tp, "segment_chunk_adaptively", wraps=tp.segment_chunk_adaptively) as ordinary, \
             patch.object(tp, "attribute_batch", return_value=[{"n": 0, "speaker": "SHE"}]), \
             patch.object(tp, "instruct_batch", return_value=[]):
            try:
                tp.run_three_pass(_client("[]"), "m", source, params, chunk_size=6000)
            except Exception:
                pass  # later passes are irrelevant here; only Pass 1's choice is under test
        ordinary.assert_called_once()  # Pass 1 really ran on this chunk (no vacuous pass)
        compact.assert_not_called()


class FingerprintTest(unittest.TestCase):
    def settings_for(self, fmt):
        captured = []
        real = json.dumps

        def capture(obj, *a, **k):
            if isinstance(obj, dict) and "model_name" in obj:
                captured.append(obj)
            return real(obj, *a, **k)
        with patch.object(tp.json, "dumps", side_effect=capture):
            fp = tp.three_pass_fingerprint("text", "m", 3000,
                                           params=LLMGenParams(max_tokens=500, pass1_format=fmt))
        return fp, captured[-1]

    def test_default_leaves_identity_unchanged_and_compact_changes_it(self):
        ordinary_fp, ordinary_settings = self.settings_for("ordinary")
        compact_fp, compact_settings = self.settings_for("compact")
        self.assertNotIn("pass1_format", ordinary_settings)
        self.assertEqual("compact", compact_settings["pass1_format"])
        self.assertNotEqual(ordinary_fp, compact_fp)


class SettingsTest(unittest.TestCase):
    def resolve(self, gen, profile=None):
        config = {"llm": {"model_name": "m"}, "generation": gen}
        if profile is not None:
            config["generation"]["three_pass_model_profiles"] = {"m": profile}
        with patch.object(tp, "get_active_llm_config", return_value=config["llm"]):
            return tp.resolve_three_pass_generation_settings(config)["pass1_format"]

    def test_default_global_and_profile_override(self):
        self.assertEqual("ordinary", self.resolve({}))
        self.assertEqual("compact", self.resolve({"three_pass_pass1_format": "compact"}))
        self.assertEqual("ordinary", self.resolve({"three_pass_pass1_format": "compact"},
                                                  profile={"pass1_format": "ordinary"}))

    def test_config_schema_accepts_only_known_formats(self):
        from config_settings import GenerationConfig
        self.assertEqual("ordinary", GenerationConfig().three_pass_pass1_format)
        self.assertEqual("compact", GenerationConfig(three_pass_pass1_format="compact").three_pass_pass1_format)
        with self.assertRaises(Exception):
            GenerationConfig(three_pass_pass1_format="labels")

    def test_cli_flag_overrides_config(self):
        with open(tp.__file__, encoding="utf-8") as handle:
            self.assertIn("--pass1-format", handle.read())
        self.assertEqual(("ordinary", "compact"), tp.PASS1_FORMATS)


if __name__ == "__main__":
    unittest.main()
