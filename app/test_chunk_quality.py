import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chunk_quality import validate_chunk_quality
import generate_script
from source_normalization import normalize_known_source_corruptions


def _entry(text, speaker="NARRATOR", instruct="Read naturally."):
    return {"speaker": speaker, "text": text, "instruct": instruct}


def generate_script_test_client(entry_lists):
    from types import SimpleNamespace
    import json
    responses = iter(entry_lists)

    def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(responses))),
            finish_reason="stop")], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


class ChunkQualityTests(unittest.TestCase):
    def test_adaptive_split_recombines_and_carries_context(self):
        source = ("First section " + "word " * 200 + ".\n\n" +
                  "Second section " + "word " * 200 + ".")
        first, second = generate_script.split_failed_chunk(source)
        first_entries = [_entry(first)]
        second_entries = [_entry(second)]
        with patch.object(generate_script, "process_chunk",
                          side_effect=[[], first_entries, second_entries]) as process:
            entries, split = generate_script.process_chunk_adaptively(
                object(), "model", source, 1, 1, generate_script.LLMGenParams(),
                previous_entries=[_entry("Earlier")])
        self.assertTrue(split)
        self.assertEqual(first_entries + second_entries, entries)
        self.assertEqual([_entry("Earlier")] + first_entries,
                         process.call_args_list[2].kwargs["previous_entries"])

    def test_adaptive_split_refuses_short_or_boundaryless_chunk(self):
        self.assertEqual([], generate_script.split_failed_chunk("short text"))
        self.assertEqual([], generate_script.split_failed_chunk("x" * 2000))

    def test_retry_action_splits_only_repeated_severe_truncation(self):
        severe = {"metrics": {"output_source_ratio": 0.2}, "findings": [
            {"code": "low_source_token_recall"},
            {"code": "low_ordered_trigram_recall"},
        ]}
        mild = {"metrics": {"output_source_ratio": 0.8}, "findings": [
            {"code": "low_source_token_recall"},
            {"code": "low_ordered_trigram_recall"},
        ]}

        self.assertEqual("retry", generate_script.get_chunk_retry_action(
            severe, 1, allow_early_split=True))
        self.assertEqual("split", generate_script.get_chunk_retry_action(
            severe, 2, allow_early_split=True))
        self.assertEqual("retry", generate_script.get_chunk_retry_action(
            severe, 2, allow_early_split=False))
        self.assertEqual("retry", generate_script.get_chunk_retry_action(
            mild, 2, allow_early_split=True))

    def test_adaptive_split_still_attempts_second_half_after_first_half_fails(self):
        # Regression: a real overnight batch run lost 71/78 remaining chunks of
        # one book because the first split half failed and the second half was
        # never even attempted, even though live reproduction testing showed
        # this content has a real (measured ~40%) per-attempt success rate --
        # one unlucky sample on half 1 must not forfeit half 2's independent
        # chance. The whole chunk is still reported as failed (checkpoint/
        # resume requires every accepted chunk to be gapless), but part 2 must
        # have been genuinely tried.
        source = ("First section " + "word " * 200 + ".\n\n" +
                  "Second section " + "word " * 200 + ".")
        with patch.object(generate_script, "process_chunk",
                          side_effect=[[], [], [_entry("second, tried anyway")]]) as process:
            entries, split = generate_script.process_chunk_adaptively(
                object(), "model", source, 1, 1, generate_script.LLMGenParams(),
                previous_entries=[_entry("Earlier")])
        self.assertEqual([], entries)
        self.assertTrue(split)
        # original + part 1 + part 2 -- part 2 must have been called.
        self.assertEqual(3, process.call_count)
        self.assertEqual([_entry("Earlier")],
                         process.call_args_list[2].kwargs["previous_entries"])

    def test_adaptive_split_fails_cleanly_when_both_halves_fail(self):
        source = ("First section " + "word " * 200 + ".\n\n" +
                  "Second section " + "word " * 200 + ".")
        with patch.object(generate_script, "process_chunk",
                          side_effect=[[], [], []]) as process:
            entries, split = generate_script.process_chunk_adaptively(
                object(), "model", source, 1, 1, generate_script.LLMGenParams())
        self.assertEqual([], entries)
        self.assertTrue(split)
        self.assertEqual(3, process.call_count)

    def test_book_request_preflight_uses_real_chunks_and_parallel_slots(self):
        report = generate_script.build_book_request_preflight(
            ["short text", "x" * 6000], "system", "{context}\n{chunk}",
            10000, 16384, 2)
        self.assertEqual(2, report["chunk_count"])
        self.assertEqual(8192, report["per_slot_context"])
        self.assertGreater(report["worst_predicted_tokens"], report["average_predicted_tokens"])
        self.assertEqual(report["worst_predicted_tokens"] * 3,
                         report["required_total_context"]["3"])

    def test_book_request_preflight_reports_context_miss(self):
        report = generate_script.build_book_request_preflight(
            ["x" * 6000], "s", "{context}{chunk}", 10000, 8192, 2)
        self.assertFalse(report["predicted_fits"])

    def test_final_gate_requires_whole_book_quality_and_zero_blockers(self):
        self.assertTrue(generate_script.passes_final_generation_gate(
            {"passed": True}, {"counts": {"blocking": 0}}))
        self.assertFalse(generate_script.passes_final_generation_gate(
            {"passed": False}, {"counts": {"blocking": 0}}))
        self.assertFalse(generate_script.passes_final_generation_gate(
            {"passed": True}, {"counts": {"blocking": 1}}))
        self.assertFalse(generate_script.passes_final_generation_gate(
            {"passed": True}, {"counts": {"blocking": 0}}, [{"reason": "unsafe"}]))

    def test_final_repair_removes_unique_duplicate_across_chunk_boundary(self):
        first = _entry("A sufficiently long first source line.")
        second = _entry("A sufficiently long second source line.")
        source = f"{first['text']} {second['text']}"

        repaired = generate_script.build_final_generation_repair(
            [first, second, copy.deepcopy(first), copy.deepcopy(second)], source)

        self.assertEqual([first, second], repaired["entries"])
        self.assertEqual("adjacent_duplicate_block", repaired["changes"][0]["type"])
        self.assertEqual([], repaired["unresolved"])

    def test_quality_manifest_summarizes_chunks_without_copying_entries(self):
        accepted = [{"chunk_number": 1, "source_sha256": "source",
                     "entries": [_entry("Text")], "quality": {"passed": True}}]
        manifest = generate_script.build_generation_quality_manifest(
            "verified", {"source_sha256": "book"}, accepted, [])
        self.assertEqual(1, manifest["accepted_chunk_count"])
        self.assertEqual(1, manifest["chunks"][0]["entry_count"])
        self.assertNotIn("entries", manifest["chunks"][0])
        self.assertEqual([], manifest["chunks"][0]["attempts"])

    def test_adaptive_split_labels_full_and_split_attempt_telemetry(self):
        source = ("First section " + "word " * 200 + ".\n\n" +
                  "Second section " + "word " * 200 + ".")
        attempts = []

        def process(*_args, **kwargs):
            attempt = {"attempt": 1}
            kwargs["attempt_observer"](attempt)
            attempt["outcome"] = "quality_rejected"
            return []

        with patch.object(generate_script, "process_chunk", side_effect=process):
            generate_script.process_chunk_adaptively(
                object(), "model", source, 1, 1, generate_script.LLMGenParams(),
                attempt_observer=attempts.append)

        self.assertEqual(["full", "split", "split"],
                         [attempt["phase"] for attempt in attempts])
        self.assertEqual([1, 2], [attempt["split_part"] for attempt in attempts[1:]])
        self.assertTrue(all(attempt["outcome"] == "quality_rejected"
                            for attempt in attempts))

    def test_known_source_corruption_normalizes_with_location_evidence(self):
        original = "First line.\nTake саге now."

        normalized, changes = normalize_known_source_corruptions(original)

        self.assertEqual("First line.\nTake care now.", normalized)
        self.assertEqual("First line.\nTake саге now.", original)
        self.assertEqual((2, 6, "саге", "care"),
                         (changes[0]["line"], changes[0]["column"],
                          changes[0]["before"], changes[0]["after"]))

    def test_known_source_nap_corruption_is_normalized(self):
        normalized, changes = normalize_known_source_corruptions(
            "She intended to пар until their arrival.")

        self.assertEqual("She intended to nap until their arrival.", normalized)
        self.assertEqual("пар", changes[0]["before"])
        self.assertEqual("nap", changes[0]["after"])

    def test_generation_repairs_empty_silence_before_quality_acceptance(self):
        source = "First spoken line. Following spoken line."
        response_entries = [
            _entry("First spoken line."),
            _entry("", instruct="A beat of silence."),
            _entry("Following spoken line."),
        ]
        client = generate_script_test_client([response_entries])
        params = generate_script.LLMGenParams("system", "{chunk}")

        result = generate_script.process_chunk(client, "model", source, 1, 1, params)

        self.assertEqual(2, len(result))
        self.assertEqual(1000, result[0]["pause_after"])

    def test_generation_checkpoint_roundtrip_requires_validated_matching_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, "book.json"))
            params = generate_script.LLMGenParams("system", "{chunk}")
            chunks = ["First source chunk.", "Second source chunk."]
            fingerprint = generate_script.get_generation_fingerprint(
                "\n".join(chunks), chunks, "model", "http://local", params, 6000)
            accepted = [{
                "chunk_number": 1,
                "source_sha256": fingerprint["chunk_sha256"][0],
                "entries": [_entry(chunks[0])],
                "quality": {"passed": True},
            }]

            generate_script.save_generation_checkpoint(output, fingerprint, accepted)

            self.assertEqual(accepted, generate_script.load_generation_checkpoint(output, fingerprint))
            changed = dict(fingerprint, source_sha256="changed")
            self.assertEqual([], generate_script.load_generation_checkpoint(output, changed))
            generate_script.clear_generation_checkpoint(output)
            self.assertFalse(Path(generate_script.get_generation_checkpoint_path(output)).exists())

    def test_generation_checkpoint_rejects_unvalidated_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, "book.json"))
            params = generate_script.LLMGenParams("system", "{chunk}")
            fingerprint = generate_script.get_generation_fingerprint(
                "source", ["source"], "model", "http://local", params, 6000)
            rejected = [{"chunk_number": 1,
                         "source_sha256": fingerprint["chunk_sha256"][0],
                         "entries": [_entry("source")], "quality": {"passed": False}}]
            generate_script.save_generation_checkpoint(output, fingerprint, rejected)

            self.assertEqual([], generate_script.load_generation_checkpoint(output, fingerprint))

    def test_complete_resegmented_response_passes_without_mutation(self):
        source = "One complete sentence appears here. Another sentence follows it."
        entries = [_entry("One complete sentence appears here."),
                   _entry("Another sentence follows it.")]
        original = copy.deepcopy(entries)

        report = validate_chunk_quality(source, entries)

        self.assertTrue(report["passed"])
        self.assertEqual(1.0, report["metrics"]["source_token_recall"])
        self.assertEqual(original, entries)

    def test_volume_10_style_early_stop_fails_all_coverage_signals(self):
        source = " ".join(f"sourceword{index}" for index in range(1000))
        entries = [_entry(" ".join(f"sourceword{index}" for index in range(100)))]

        report = validate_chunk_quality(source, entries)

        self.assertFalse(report["passed"])
        self.assertEqual(
            {"low_source_token_recall", "low_ordered_trigram_recall", "output_source_ratio"},
            {finding["code"] for finding in report["findings"]},
        )

    def test_boundary_allows_lowest_calibrated_intact_coverage(self):
        source_words = [f"word{index}" for index in range(100)]
        source = " ".join(source_words)
        entries = [_entry(" ".join(source_words[:93]))]

        report = validate_chunk_quality(source, entries)

        self.assertTrue(report["passed"])

    def test_malformed_and_empty_entries_fail_structural_checks(self):
        report = validate_chunk_quality("A spoken line.", [
            {"speaker": "NARRATOR", "text": ""}, "bad entry",
        ])

        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("missing_fields", codes)
        self.assertIn("empty_text", codes)
        self.assertIn("invalid_entry", codes)

    def test_source_cyrillic_is_reported_but_only_new_cyrillic_blocks(self):
        copied = validate_chunk_quality("Take саге now.", [_entry("Take саге now.")])
        introduced = validate_chunk_quality("Take care now.", [_entry("Take саге now.")])

        self.assertTrue(copied["passed"])
        self.assertEqual(["а", "г", "е", "с"], copied["source_cyrillic"])
        self.assertIn("unsupported_cyrillic", {item["code"] for item in introduced["findings"]})

    def test_source_unsupported_adjacent_block_is_explicitly_reported(self):
        block = [_entry("A sufficiently long first line."),
                 _entry("A sufficiently long second line.")]
        source = "A sufficiently long first line. A sufficiently long second line."

        report = validate_chunk_quality(source, block + block)

        self.assertIn("source_unsupported_duplicate",
                      {item["code"] for item in report["findings"]})

    def test_complete_japanese_text_uses_character_units(self):
        source = "彼はありがとうと言った"
        report = validate_chunk_quality(source, [_entry(source)])
        self.assertTrue(report["passed"])
        self.assertGreater(report["metrics"]["source_tokens"], 3)

    def test_missing_japanese_span_fails_recall(self):
        source = "彼はありがとうと言った"
        report = validate_chunk_quality(source, [_entry("彼は言った")])
        self.assertFalse(report["passed"])
        self.assertIn("low_source_token_recall", {item["code"] for item in report["findings"]})

    def test_introduced_hiragana_reports_codepoint(self):
        report = validate_chunk_quality("Subaru saw a cat", [_entry("Subあru saw a cat")])
        finding = next(item for item in report["findings"]
                       if item["code"] == "unsupported_unicode_character")
        self.assertEqual("U+3042", finding["characters"][0]["codepoint"])

    def test_canonically_equivalent_accents_compare_equal(self):
        report = validate_chunk_quality("Caf\u00e9", [_entry("Cafe\u0301")])
        self.assertTrue(report["passed"])


if __name__ == "__main__":
    unittest.main()
