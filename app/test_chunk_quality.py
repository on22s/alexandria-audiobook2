import copy
from pathlib import Path
import tempfile
import unittest

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
    def test_final_gate_requires_whole_book_quality_and_zero_blockers(self):
        self.assertTrue(generate_script.passes_final_generation_gate(
            {"passed": True}, {"counts": {"blocking": 0}}))
        self.assertFalse(generate_script.passes_final_generation_gate(
            {"passed": False}, {"counts": {"blocking": 0}}))
        self.assertFalse(generate_script.passes_final_generation_gate(
            {"passed": True}, {"counts": {"blocking": 1}}))

    def test_quality_manifest_summarizes_chunks_without_copying_entries(self):
        accepted = [{"chunk_number": 1, "source_sha256": "source",
                     "entries": [_entry("Text")], "quality": {"passed": True}}]
        manifest = generate_script.build_generation_quality_manifest(
            "verified", {"source_sha256": "book"}, accepted, [])
        self.assertEqual(1, manifest["accepted_chunk_count"])
        self.assertEqual(1, manifest["chunks"][0]["entry_count"])
        self.assertNotIn("entries", manifest["chunks"][0])

    def test_known_source_corruption_normalizes_with_location_evidence(self):
        original = "First line.\nTake саге now."

        normalized, changes = normalize_known_source_corruptions(original)

        self.assertEqual("First line.\nTake care now.", normalized)
        self.assertEqual("First line.\nTake саге now.", original)
        self.assertEqual((2, 6, "саге", "care"),
                         (changes[0]["line"], changes[0]["column"],
                          changes[0]["before"], changes[0]["after"]))

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


if __name__ == "__main__":
    unittest.main()
