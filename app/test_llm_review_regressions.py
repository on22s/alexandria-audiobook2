from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import generate_script
import core
import review_script
from lmstudio_settings import get_effective_max_tokens, TokenBudgetError


class LlmReviewTests(unittest.TestCase):
    def test_token_budget_uses_fallback_without_verified_context(self):
        self.assertEqual(4096, get_effective_max_tokens(4096, None, [], 16000))

    def test_token_budget_scales_with_verified_context(self):
        self.assertEqual(16000, get_effective_max_tokens(4096, 98304, [], 16000))

    def test_token_budget_reserves_prompt_space(self):
        messages = [{"role": "user", "content": "x" * 18000}]
        self.assertEqual(1680, get_effective_max_tokens(4096, 8192, messages, 16000))

    def test_token_budget_enforces_task_ceiling(self):
        self.assertEqual(6000, get_effective_max_tokens(2000, 98304, [], 6000))

    def test_token_budget_rejects_prompt_larger_than_context(self):
        with self.assertRaises(TokenBudgetError):
            get_effective_max_tokens(100, 1000, [{"role": "user", "content": "x" * 3000}], 500)

    def test_token_budget_rejects_invalid_context(self):
        with self.assertRaises(ValueError):
            get_effective_max_tokens(100, "not-a-number", [], 500)

    def test_llm_salvage_waits_until_retries_are_exhausted(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop"
            )],
            usage=None,
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: response)
            )
        )
        params = generate_script.LLMGenParams("system", "{text}", 100, 0.1, 1, 0, 0, 0, "")
        complete = [{"type": "narration", "text": "complete"}]
        with patch.object(generate_script, "clean_json_string", return_value="[]"), \
             patch.object(generate_script, "repair_json_array", side_effect=[[], complete]), \
             patch.object(generate_script, "salvage_json_entries", return_value=[{"text": "partial"}]) as salvage:
            result = generate_script.call_llm_for_entries(
                client, "model", "system", "text", params,
                "test_responses.log", "TEST", max_retries=1
            )
        self.assertEqual(result, complete)
        salvage.assert_not_called()

    def test_review_help_does_not_advertise_unimplemented_source_mode(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("review_script.py")),
             "--help"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("--source", result.stdout)


class ReviewDiffAlignmentTests(unittest.TestCase):
    @staticmethod
    def entry(text, speaker="NARRATOR", instruct="neutral"):
        return {"text": text, "speaker": speaker, "instruct": instruct}

    def test_middle_insertion_does_not_shift_later_comparisons(self):
        original = [self.entry("first"), self.entry("second"), self.entry("third")]
        corrected = [
            self.entry("first"), self.entry("inserted", "SUBARU", "urgent"),
            self.entry("second"), self.entry("third"),
        ]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["entries_removed"], 0)
        self.assertEqual(stats["text_changed"], 0)
        self.assertEqual(stats["speaker_changed"], 0)
        self.assertEqual(stats["instruct_changed"], 0)

    def test_middle_removal_does_not_shift_later_comparisons(self):
        original = [self.entry("first"), self.entry("removed"), self.entry("third")]
        corrected = [self.entry("first"), self.entry("third")]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["entries_added"], 0)
        self.assertEqual(stats["entries_removed"], 1)
        self.assertEqual(stats["text_changed"], 0)
        self.assertEqual(stats["speaker_changed"], 0)
        self.assertEqual(stats["instruct_changed"], 0)

    def test_rewrite_and_metadata_changes_remain_visible(self):
        original = [self.entry("old wording"), self.entry("same text")]
        corrected = [
            self.entry("new wording"),
            self.entry("same text", speaker="SUBARU", instruct="quietly"),
        ]
        highlights = {"text": [], "speaker": []}

        stats = review_script.diff_entries(original, corrected, highlights)

        self.assertEqual(stats["text_changed"], 1)
        self.assertEqual(stats["speaker_changed"], 1)
        self.assertEqual(stats["instruct_changed"], 1)
        self.assertEqual(stats["entries_changed"], 2)
        self.assertEqual(highlights["text"][0]["before"], "old wording")
        self.assertEqual(highlights["text"][0]["after"], "new wording")

    def test_insertion_does_not_pair_adjacent_july_report_lines(self):
        spirit_explanation = "That’s the tricky thing about spirit mages."
        old_man_question = "By the way, old man, what is it you’re planning on doing?"
        original = [self.entry(old_man_question), self.entry("following line")]
        corrected = [
            self.entry(spirit_explanation), self.entry(old_man_question),
            self.entry("following line"),
        ]
        highlights = {"text": [], "speaker": []}

        stats = review_script.diff_entries(original, corrected, highlights)

        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["text_changed"], 0)
        self.assertEqual(highlights["text"], [])

    def test_replacement_with_insertion_counts_each_kind_once(self):
        original = [self.entry("first"), self.entry("old"), self.entry("last")]
        corrected = [
            self.entry("first"), self.entry("new"), self.entry("extra"), self.entry("last"),
        ]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["text_changed"], 1)
        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["entries_removed"], 0)

    def test_addition_and_removal_are_counted_when_length_is_unchanged(self):
        original = [self.entry("first"), self.entry("removed"), self.entry("anchor")]
        corrected = [self.entry("inserted"), self.entry("first"), self.entry("anchor")]

        stats = review_script.diff_entries(original, corrected)

        self.assertEqual(stats["entries_added"], 1)
        self.assertEqual(stats["entries_removed"], 1)
        self.assertEqual(stats["text_changed"], 0)

    def test_highlight_pool_cap_is_preserved(self):
        original = [self.entry(f"old {index}") for index in range(510)]
        corrected = [self.entry(f"new {index}") for index in range(510)]
        highlights = {"text": [], "speaker": []}

        stats = review_script.diff_entries(original, corrected, highlights)

        self.assertEqual(stats["text_changed"], 510)
        self.assertEqual(len(highlights["text"]), review_script._MAX_HIGHLIGHT_POOL)

    def test_speaker_change_includes_entry_number_and_neighbor_context(self):
        original = [
            self.entry("before"), self.entry("He said as the cart passed."), self.entry("after"),
        ]
        corrected = [
            self.entry("before"), self.entry("He said as the cart passed.", speaker="KENJI"),
            self.entry("after"),
        ]
        highlights = {"text": [], "speaker": []}

        review_script.diff_entries(original, corrected, highlights, entry_offset=100)

        change = highlights["speaker"][0]
        self.assertEqual(change["entry_number"], 102)
        self.assertEqual(change["context_before"], "before")
        self.assertEqual(change["context_after"], "after")
        self.assertIn("Narrator-to-character", change["manual_review_reason"])

    def test_character_to_character_change_is_not_automatically_flagged(self):
        original = [self.entry("Hello", speaker="MAN")]
        corrected = [self.entry("Hello", speaker="KENJI")]
        highlights = {"text": [], "speaker": []}

        review_script.diff_entries(original, corrected, highlights)

        self.assertNotIn("manual_review_reason", highlights["speaker"][0])

    def test_speaker_markdown_does_not_assert_correction(self):
        highlights = {"text_rewrites": [], "speaker_changes": [{
            "text": "He said as the cart passed.", "before": "NARRATOR", "after": "KENJI",
            "entry_number": 102, "context_before": "Before.", "context_after": "After.",
            "manual_review_reason": "Narrator-to-character changes alter the reading voice.",
        }]}

        markdown = "\n".join(core._markdown_diff_highlights_lines(highlights))

        self.assertIn("Speaker changes to verify", markdown)
        self.assertIn("Entry 102", markdown)
        self.assertIn("Previous", markdown)
        self.assertIn("Manual check recommended", markdown)
        self.assertNotIn("corrected to", markdown)


class ReviewFailureReportingTests(unittest.TestCase):
    def test_failed_section_uses_human_entry_range_and_stable_ratio(self):
        section = review_script.get_failed_section(
            batch=3, zero_based_start=50, length=25,
            category="text_length_mismatch", word_ratio=0.912345,
        )

        self.assertEqual(section, {
            "batch": 3,
            "entry_start": 51,
            "entry_end": 75,
            "category": "text_length_mismatch",
            "word_ratio": 0.9123,
        })

    def test_failed_sections_parser_and_markdown_explain_safe_retry(self):
        lines = [
            'FAILED_SECTIONS_JSON: {"sections":[{"batch":3,"entry_start":51,'
            '"entry_end":75,"category":"text_length_mismatch","word_ratio":0.91}],'
            '"original_entries_preserved":true,"checkpoint_retained":true,'
            '"retry_from_batch":3}'
        ]

        failures = core._extract_failed_sections(lines)
        markdown = core._markdown_failed_sections_lines(failures)

        self.assertEqual(failures["sections"][0]["entry_start"], 51)
        self.assertTrue(any("entries 51–75" in line for line in markdown))
        self.assertTrue(any("original entries" in line for line in markdown))
        self.assertTrue(any("single-book review" in line for line in markdown))

    def test_malformed_failed_sections_are_not_reported(self):
        self.assertEqual(
            core._extract_failed_sections(['FAILED_SECTIONS_JSON: {"sections":"bad"}']),
            {"sections": []},
        )


class ReviewSummaryTests(unittest.TestCase):
    def test_incomplete_summary_is_deterministic_and_does_not_call_llm(self):
        stats = {"total_changes": 12, "batches_failed": 1, "batches_skipped_vram": 0}
        with patch.object(core, "_llm_summarize_report") as summarize:
            lines = core._insert_llm_summary(["Report"], 1, stats, incomplete=True)

        summarize.assert_not_called()
        self.assertIn("This review was incomplete", lines[4])
        self.assertIn("1 section(s) failed", lines[4])
        self.assertIn("12 change(s)", lines[4])

    def test_unsupported_quality_claim_uses_deterministic_fallback(self):
        stats = {"total_changes": 3}
        with patch.object(
                core, "_llm_summarize_report",
                return_value="Everything looks great and all issues were fixed."):
            lines = core._insert_llm_summary(["Report"], 1, stats)

        self.assertIn("without recorded failed or skipped sections", lines[4])
        self.assertNotIn("Everything looks great", lines[4])

    def test_evidence_bound_llm_summary_is_kept_for_complete_run(self):
        summary = "The pass reported three text changes; inspect the examples below."
        with patch.object(core, "_llm_summarize_report", return_value=summary):
            lines = core._insert_llm_summary(["Report"], 1, {"total_changes": 3})

        self.assertEqual(lines[4], summary)


class ReviewChangeDensityTests(unittest.TestCase):
    def test_high_change_density_warns_with_separate_structural_counts(self):
        stats = {
            "entries_before": 2615, "entries_changed": 613,
            "entries_added": 8, "entries_removed": 1,
        }

        lines = core._markdown_change_density_lines(stats)

        self.assertEqual(len(lines), 1)
        self.assertIn("613 of 2615", lines[0])
        self.assertIn("23.4%", lines[0])
        self.assertIn("+8/-1", lines[0])
        self.assertIn("before generating audio", lines[0])

    def test_low_density_and_small_scripts_do_not_warn(self):
        self.assertEqual(core._markdown_change_density_lines({
            "entries_before": 1000, "entries_changed": 199,
        }), [])
        self.assertEqual(core._markdown_change_density_lines({
            "entries_before": 99, "entries_changed": 99,
        }), [])
