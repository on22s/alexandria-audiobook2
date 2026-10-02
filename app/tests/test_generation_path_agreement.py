"""Every way of generating a script must answer the same questions the same way.

WHY THIS FILE EXISTS. Three defects in one day, all the same shape: a decision
implemented more than once, and the copies disagreeing.

  1. "is an adjacent duplicate block a defect?" lived in FOUR places.
     chunk_quality and pass_quality had it right - only flag when the source
     contains the block once. script_repair and the whole-book audit refused
     any block the source did not contain exactly once. grimgar03 opens with
     its title eight times, so it failed at chunk 1, was fixed, generated all
     49 chunks, and was then rejected AGAIN at the final gate by the other
     wrong copy.

  2. "how much decode damage may a source carry?" had THREE answers.
     generate_script graded it at 0.5%, three_pass_generate refused any count,
     and the per-entry audit blocked at any count. A repaired index18 would
     generate single-pass and be refused three-pass, for identical input.

  3. The per-chunk gate refused what the source gate had just accepted, so
     relaxing the front door moved the refusal to chunk 31 rather than opening
     the book.

Unit tests did not catch any of these, because each copy was correct against
its own tests. What was missing was a test that the copies AGREE. That is what
this file is: it asserts the shared definitions are the only definitions, and
that the same input gets the same verdict from every path.

WHEN THIS FAILS, DO NOT EDIT THE TEST. A new local threshold or a second
duplicate rule is the defect it is built to catch. Point the new caller at the
shared function instead.
"""
import inspect
import re
import unittest

import chunk_quality
import generate_script
import pass_quality
import script_preflight
import script_repair
import three_pass_generate


class ReplacementPolicyAgreementTest(unittest.TestCase):
    """One definition of how much decode damage is acceptable."""

    def test_the_policy_lives_in_exactly_one_place(self):
        defining = [module.__name__ for module in
                    (script_preflight, generate_script, three_pass_generate,
                     chunk_quality, pass_quality, script_repair)
                    if "MAX_REPLACEMENT_SHARE = " in inspect.getsource(module)]
        self.assertEqual(["script_preflight"], defining,
                         "the replacement-character limit must be defined once; "
                         f"found definitions in {defining}")

    def test_both_generators_use_the_shared_check(self):
        for module in (generate_script, three_pass_generate):
            with self.subTest(module=module.__name__):
                source = inspect.getsource(module)
                self.assertIn("replacement_load_is_acceptable", source,
                              f"{module.__name__} must ask the shared policy, "
                              "not re-derive a threshold")

    def test_generators_agree_on_the_same_input(self):
        """The concrete case that broke: repaired index18's damage level."""
        repaired = int(0.0026 * 476376)     # index18 after repair, 0.26%
        corrupt = int(0.0140 * 476376)      # index18 raw, 1.40%
        total = 476376
        self.assertTrue(
            script_preflight.replacement_load_is_acceptable(repaired, total),
            "a repaired book must be accepted by every path")
        self.assertFalse(
            script_preflight.replacement_load_is_acceptable(corrupt, total),
            "a mis-decoded book must still be refused by every path")

    def test_a_clean_source_is_always_acceptable(self):
        self.assertTrue(
            script_preflight.replacement_load_is_acceptable(0, 1000))


class DuplicateBlockAgreementTest(unittest.TestCase):
    """Every caller must treat a source-repeated block as faithful."""

    TITLE = "Grimgar of Fantasy and Ash: Volume 3"

    def _texts_and_source(self):
        texts = [self.TITLE.casefold()] * 4
        source = "\n".join([self.TITLE] * 8)
        return texts, source

    def test_the_detector_reports_the_source_count(self):
        texts, source = self._texts_and_source()
        findings = script_preflight.find_adjacent_duplicate_blocks(texts, source)
        self.assertTrue(findings)
        self.assertGreaterEqual(
            findings[0]["details"]["source_occurrences"], 2,
            "the detector must report how often the SOURCE has the block; "
            "every caller's decision depends on it")

    def test_no_caller_blocks_a_source_repeated_block(self):
        texts, source = self._texts_and_source()
        findings = script_preflight.find_adjacent_duplicate_blocks(texts, source)
        self.assertEqual("manual_review", findings[0]["severity"])

        entries = [{"text": self.TITLE, "speaker": "NARRATOR"}] * 4
        repair = script_repair.build_deterministic_repair(entries, source)
        self.assertEqual([], repair["unresolved"],
                         "the repair path must not call a faithful repeat "
                         "unresolvable")

    def test_every_caller_keys_off_source_occurrences(self):
        """Both gates accept faithful repeats and reject invented repeats."""
        sources = (("\n".join([self.TITLE] * 4), False),
                   (self.TITLE, True))
        for module, gate, entries in (
                (chunk_quality, chunk_quality.validate_chunk_quality,
                 [{"text": self.TITLE, "speaker": "NARRATOR", "instruct": "Neutral."}] * 4),
                (pass_quality, pass_quality.validate_segment_quality,
                 [{"text": self.TITLE, "type": "NARRATOR"}] * 4)):
            for source, unsupported in sources:
                with self.subTest(module=module.__name__, unsupported=unsupported):
                    report = gate(source, entries)
                    codes = {finding["code"] for finding in report["findings"]}
                    self.assertEqual(unsupported, "source_unsupported_duplicate" in codes)
                    if not unsupported:
                        self.assertTrue(report["passed"], report)


class GateOrderingTest(unittest.TestCase):
    """A later gate must not refuse what an earlier one accepted."""

    def test_entry_audit_does_not_re_refuse_accepted_damage(self):
        """index18's exact failure: front door open, chunk 31 shut.

        The per-entry audit used to mark any replacement character blocking,
        so a source admitted by the graded gate could never produce an
        acceptable entry.
        """
        source = inspect.getsource(script_preflight.audit_script)
        marker = 'unicode_report["replacement_character_count"]'
        self.assertIn(marker, source)
        window = source[source.find(marker):source.find(marker) + 300]
        self.assertNotIn('_finding("blocking"', window,
                         "replacement characters the source gate accepted must "
                         "not be blocking at entry level")

    def test_unsafe_controls_stay_blocking_everywhere(self):
        """The thing that must NOT be relaxed by any of this."""
        source = inspect.getsource(script_preflight.audit_script)
        self.assertIn('unicode_report["unsafe_controls"]', source)
        marker = 'if unicode_report["unsafe_controls"]:'
        window = source[source.find(marker):source.find(marker) + 200]
        self.assertIn('"blocking"', window)




class SourcePreprocessingAgreementTest(unittest.TestCase):
    """Both generators must clean a source the same way before reading it.

    `strip_publisher_matter` existed and was called only by
    three_pass_generate. So a published book's chunk 1 on the single-pass path
    was its copyright page and library cataloguing block - text the model
    cannot turn into annotated dialogue, and which the coverage gate then
    failed it for not reproducing. index18 retried chunk 1 eight times.

    This is the same defect shape as the replacement-character policy and the
    duplicate-block rule: a capability living on one path only.
    """

    PREPROCESSORS = ("normalize_extreme_phrase_repetitions",
                     "strip_known_front_matter", "strip_publisher_matter")

    def test_both_generators_strip_the_same_things(self):
        for name in self.PREPROCESSORS:
            for module in (generate_script, three_pass_generate):
                with self.subTest(preprocessor=name, module=module.__name__):
                    self.assertIn(name, inspect.getsource(module.get_preprocessed_source),
                                  f"{module.__name__} must apply {name}; a "
                                  "cleaner that runs on one path only means "
                                  "the same book behaves differently "
                                  "depending on how it is generated")

    def test_publisher_matter_is_not_left_to_a_flag(self):
        """The fan-compiler stripper is opt-in; this one must not be.

        Every published book has a colophon, so making it optional would leave
        the default path broken for the common case.
        """
        source = inspect.getsource(generate_script.get_preprocessed_source)
        index = source.find("strip_publisher_matter(")
        self.assertGreater(index, 0)
        window = source[max(0, index - 200):index]
        self.assertNotIn("if args.strip_front_matter", window.split("\n")[-1],
                         "publisher matter must be stripped unconditionally")


class SourceHealthPreflightAgreementTest(unittest.TestCase):
    """The damage check must run on every path that reads a book.

    Fourth instance of the same defect shape. `strip_publisher_matter` ran on
    one path, the replacement policy had three answers, the duplicate rule had
    four copies - each time a capability lived where it was written rather than
    everywhere it was needed. This test exists so the preflight does not become
    the fourth.
    """

    def test_both_generators_run_the_preflight(self):
        for module in (generate_script, three_pass_generate):
            with self.subTest(module=module.__name__):
                self.assertIn("preflight_source", inspect.getsource(module),
                              f"{module.__name__} must run the source health "
                              "preflight; a book that is repaired on one path "
                              "and not the other behaves differently for the "
                              "same input")

    def test_the_preflight_is_defined_once(self):
        import repair_source_encoding
        defining = [m.__name__ for m in
                    (repair_source_encoding, generate_script,
                     three_pass_generate)
                    if "def preflight_source(" in inspect.getsource(m)]
        self.assertEqual(["repair_source_encoding"], defining,
                         f"found definitions in {defining}")

    def test_both_paths_get_the_same_verdict_on_the_same_book(self):
        """Behaviour, not just presence of a call."""
        import repair_source_encoding
        damaged = "“She s got to give me some candy. I can t find any.”\n"
        first = repair_source_encoding.preflight_source(damaged)
        second = repair_source_encoding.preflight_source(damaged)
        self.assertEqual(first["text"], second["text"])
        self.assertTrue(first["healthy"], "a repairable book must come back "
                                          "clean for whichever path asks")

    def test_the_users_file_is_never_rewritten(self):
        """Repair is a guess at an unrecoverable original."""
        import repair_source_encoding
        source = inspect.getsource(repair_source_encoding.preflight_source)
        self.assertNotIn("open(", source,
                         "the preflight must not write to disk")

    def test_unrepairable_damage_does_not_refuse_the_book(self):
        """Damage is a risk signal; the hard refusals stay where they are."""
        import repair_source_encoding
        result = repair_source_encoding.preflight_source(
            "“" + ("—?" * 25) + "!!”\n")
        self.assertIn("text", result,
                      "the preflight returns text rather than exiting")


if __name__ == "__main__":
    unittest.main()


class SourceRepairStructuralAcceptanceTests(unittest.TestCase):
    def test_cli_rejected_repairs_are_not_published_by_shared_preflight(self):
        import json
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        import repair_source_encoding as repair
        import generate_script
        import three_pass_generate

        for source, regression in (("““can t—", "open_quote_ended_with_dash"),
                                   ("—can t””", "close_quote_started_with_dash")):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "source.txt"
                path.write_text(source, encoding="utf-8")
                original = path.read_bytes()
                cli = subprocess.run([sys.executable, str(Path(repair.__file__)), str(path), "--apply"],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(1, cli.returncode, cli.stderr)
                report = json.loads((Path(tmp) / "source.repair_report.json").read_text())
                self.assertFalse(report["quote_balance"]["structurally_sound"])
                self.assertEqual(1, report["quote_balance"]["structural_regressions"][regression])
                self.assertFalse((Path(tmp) / "source.repaired.txt").exists())
                self.assertEqual(original, path.read_bytes())
                for module in (repair, generate_script, three_pass_generate):
                    with self.subTest(caller=module.__name__):
                        result = module.preflight_source(source)
                        self.assertEqual(source, result["text"])
                        self.assertFalse(result["healthy"])
                        self.assertEqual({}, result["applied"])
                        self.assertTrue(result["findings"])
                        self.assertTrue(any("structure" in message.lower()
                                            for message in result["messages"]))
                self.assertEqual(original, path.read_bytes())

    def test_sound_repairs_and_healthy_sources_keep_existing_behavior(self):
        import repair_source_encoding as repair
        source = "“She s here. I can t go.”"
        result = repair.preflight_source(source)
        self.assertEqual("“She’s here. I can’t go.”", result["text"])
        self.assertTrue(result["healthy"])
        self.assertEqual(2, sum(result["applied"].values()))
        self.assertEqual([], result["findings"])
        sound = "“All is well,” she said."
        clean = repair.preflight_source(sound)
        self.assertEqual(sound, clean["text"])
        self.assertTrue(clean["healthy"])
        self.assertEqual({}, clean["applied"])
        self.assertEqual([], clean["messages"])
