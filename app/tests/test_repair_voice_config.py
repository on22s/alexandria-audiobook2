"""Tests for the voice-config split repair.

This script rewrites a file the user hand-tunes through the UI, so every way it
could quietly pick the wrong voice is worth a test. The bug it repairs was
itself silent: eight characters in the live book cast in two voices, invisible
because both spellings resolved exactly through `voice_config.get(speaker)`.

  case-insensitive canon    'SUBARU' was in the alias map and resolved; 'Subaru'
                            was not. Case-sensitive matching is the whole
                            reason the split existed.
  same-voice not flagged    Two spellings sharing a voice are harmless.
                            Reporting them would bury the ones that matter.
  type outranks lines       Ranking by line count first gave PUCK an
                            auto-created custom voice over a character LoRA on
                            a 1-vs-0 count.
  disputed surfaced         Where the two rules disagree the answer is
                            arguable and must be visible, not silent.
  losers keep their keys    The script still refers to characters by their
                            original spelling. Deleting a key would send those
                            lines to a fallback - a wrong voice traded for none.
  no mutation of input      Rule 17: apply_merges returns a new dict rather
                            than editing the config it was handed.
"""
import os
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repair_voice_config import (apply_merges, canonical, find_splits,
                                 voice_signature)
import repair_voice_config

LORA = {"type": "lora", "voice": "Ryan", "seed": "-1",
        "adapter_id": "husky_tenor_30s_m_fantasy"}
LORA_OTHER = {"type": "lora", "voice": "Ryan", "seed": "-1",
              "adapter_id": "breathy_alto_50s_f_fantasy"}
CUSTOM = {"type": "custom", "voice": "Aiden", "seed": "-1"}
DESIGN = {"type": "design", "voice": "Ryan", "seed": "-1"}
CLONE = {"type": "clone", "voice": "Ryan", "seed": "-1"}


class EnsembleRepairTests(unittest.TestCase):
    def test_deliberate_ensemble_survives_automatic_custom_with_more_lines(self):
        config = {'Alice': {'type': 'ensemble', 'members': ['ONE', 'TWO']},
                  'ALICE': dict(CUSTOM), 'ONE': dict(CUSTOM), 'TWO': dict(LORA)}
        before = json.dumps(config, sort_keys=True)
        splits = find_splits(config, {}, {'Alice': 5, 'ALICE': 10})
        self.assertEqual('Alice', splits[0]['winner'])
        self.assertFalse(splits[0]['ambiguous'])
        merged = apply_merges(config, splits)
        self.assertEqual(['ONE', 'TWO'], merged['ALICE']['members'])
        self.assertEqual('ensemble', merged['ALICE']['type'])
        self.assertEqual(before, json.dumps(config, sort_keys=True))

    def test_distinct_ensemble_members_are_ambiguous_and_require_override(self):
        config = {'Alice': {'type': 'ensemble', 'members': ['ONE', 'TWO']},
                  'ALICE': {'type': 'ensemble', 'members': ['ONE', 'THREE']}}
        splits = find_splits(config, {}, {'Alice': 5, 'ALICE': 10})
        self.assertEqual(1, len(splits))
        self.assertTrue(splits[0]['ambiguous'])
        self.assertEqual(config, apply_merges(config, splits))
        forced = apply_merges(config, splits, force_ambiguous=True)
        self.assertEqual(['ONE', 'THREE'], forced['Alice']['members'])
        self.assertEqual(['ONE', 'TWO'], config['Alice']['members'])

    def test_ensemble_and_other_top_deliberate_voice_are_ambiguous(self):
        config = {'Alice': {'type': 'ensemble', 'members': ['ONE', 'TWO']},
                  'ALICE': dict(CLONE)}
        splits = find_splits(config, {}, {'ALICE': 10})
        self.assertTrue(splits[0]['ambiguous'])
        self.assertEqual(config, apply_merges(config, splits))

    def test_identical_ensembles_remain_harmless_duplicates(self):
        config = {'Alice': {'type': 'ensemble', 'members': ['ONE', 'TWO']},
                  'ALICE': {'type': 'ensemble', 'members': ['ONE', 'TWO']}}
        self.assertEqual([], find_splits(config, {}, {}))


class TestCanonical(unittest.TestCase):

    def test_alias_map_is_case_insensitive(self):
        # The live bug: the map held 'SUBARU' and the script said 'Subaru'.
        aliases = {"SUBARU": "NATSUKI SUBARU"}
        self.assertEqual(canonical("Subaru", aliases), "NATSUKI SUBARU")
        self.assertEqual(canonical("SUBARU", aliases), "NATSUKI SUBARU")

    def test_unmapped_name_folds_to_upper(self):
        self.assertEqual(canonical("Anastasia", {}), "ANASTASIA")
        self.assertEqual(canonical("ANASTASIA", {}), "ANASTASIA")

    def test_empty_and_missing_are_safe(self):
        self.assertEqual(canonical("", {}), "")
        self.assertEqual(canonical(None, None), "")


class TestFindSplits(unittest.TestCase):

    def test_same_voice_under_two_spellings_is_not_a_split(self):
        # Subaru is duplicated but both point at one voice - not a defect.
        config = {"Subaru": dict(LORA), "NATSUKI SUBARU": dict(LORA)}
        aliases = {"SUBARU": "NATSUKI SUBARU"}
        self.assertEqual(find_splits(config, aliases, {}), [])

    def test_different_voices_under_two_spellings_is_a_split(self):
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        self.assertEqual(len(splits), 1)
        self.assertEqual(splits[0]["canonical"], "ANASTASIA")
        self.assertEqual(splits[0]["winner"], "Anastasia")

    def test_voice_type_outranks_line_count(self):
        # PUCK: the LoRA has zero lines and must still win.
        config = {"Puck": dict(LORA), "PUCK": dict(CUSTOM)}
        splits = find_splits(config, {}, {"PUCK": 1, "Puck": 0})
        self.assertEqual(splits[0]["winner"], "Puck")

    def test_clone_outranks_custom(self):
        config = {"New Voice": dict(CLONE), "NEW VOICE": dict(CUSTOM)}
        splits = find_splits(config, {}, {"NEW VOICE": 1, "New Voice": 0})
        self.assertEqual(splits[0]["winner"], "New Voice")

    def test_disagreement_between_rules_is_flagged(self):
        config = {"Man 2": dict(DESIGN), "MAN 2": dict(CUSTOM)}
        splits = find_splits(config, {}, {"MAN 2": 8, "Man 2": 4})
        self.assertEqual(splits[0]["winner"], "Man 2")
        self.assertTrue(splits[0]["disputed"])
        self.assertIn("more lines", splits[0]["reason"])

    def test_agreement_is_not_flagged_disputed(self):
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        self.assertFalse(splits[0]["disputed"])

    def test_line_count_breaks_ties_within_a_type(self):
        config = {"Man A": dict(CUSTOM), "MAN A": dict(CUSTOM)}
        # Same type but different voices, so still a split.
        config["MAN A"] = {"type": "custom", "voice": "Zed", "seed": "-1"}
        splits = find_splits(config, {}, {"MAN A": 9, "Man A": 1})
        self.assertEqual(splits[0]["winner"], "MAN A")

    def test_non_dict_entries_are_ignored(self):
        # Older configs stored a bare voice name; it must not crash the scan.
        config = {"Legacy": "Ryan", "Anastasia": dict(LORA),
                  "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {})
        self.assertEqual([s["canonical"] for s in splits], ["ANASTASIA"])


class TestAdapterIdIsPartOfIdentity(unittest.TestCase):
    """The field is adapter_id, not adapter. Getting it wrong hid the worst
    split in the book: NATSUKI SUBARU, 412 lines, a male baritone under one
    spelling and a fifty-year-old female alto under the other, reported as
    SAME VOICE because every LoRA entry looks alike on the wrong keys."""

    def test_same_type_different_adapter_is_a_split(self):
        config = {"Subaru": dict(LORA_OTHER), "NATSUKI SUBARU": dict(LORA)}
        splits = find_splits(config, {"SUBARU": "NATSUKI SUBARU"},
                             {"Subaru": 244, "NATSUKI SUBARU": 168})
        self.assertEqual(len(splits), 1)

    def test_identical_adapter_is_not_a_split(self):
        config = {"Subaru": dict(LORA), "NATSUKI SUBARU": dict(LORA)}
        self.assertEqual(
            find_splits(config, {"SUBARU": "NATSUKI SUBARU"}, {}), [])

    def test_signature_reads_adapter_id(self):
        self.assertIn("husky_tenor_30s_m_fantasy", voice_signature(LORA))

    def test_two_deliberate_voices_are_ambiguous(self):
        # No principled winner exists, and line count is a coin flip on the
        # most-heard voice in the book.
        config = {"Subaru": dict(LORA_OTHER), "NATSUKI SUBARU": dict(LORA)}
        splits = find_splits(config, {"SUBARU": "NATSUKI SUBARU"},
                             {"Subaru": 244, "NATSUKI SUBARU": 168})
        self.assertTrue(splits[0]["ambiguous"])

    def test_two_distinct_design_voices_require_explicit_merge_override(self):
        config = {"Anna": {"type": "design", "seed": 10, "description": "Soft voice"},
                  "ANNA": {"type": "design", "seed": 20, "description": "Strong voice"}}
        splits = find_splits(config, {}, {"Anna": 20, "ANNA": 1})
        self.assertEqual(1, len(splits))
        self.assertTrue(splits[0]["ambiguous"])
        self.assertEqual(config, apply_merges(config, splits))
        forced = apply_merges(config, splits, force_ambiguous=True)
        self.assertEqual(config["Anna"], forced["ANNA"])
        self.assertEqual(20, config["ANNA"]["seed"])


    def test_ambiguous_splits_are_not_merged_by_default(self):
        config = {"Subaru": dict(LORA_OTHER), "NATSUKI SUBARU": dict(LORA)}
        splits = find_splits(config, {"SUBARU": "NATSUKI SUBARU"},
                             {"Subaru": 244, "NATSUKI SUBARU": 168})
        merged = apply_merges(config, splits)
        self.assertEqual(merged["NATSUKI SUBARU"]["adapter_id"],
                         "husky_tenor_30s_m_fantasy")

    def test_force_ambiguous_does_merge(self):
        config = {"Subaru": dict(LORA_OTHER), "NATSUKI SUBARU": dict(LORA)}
        splits = find_splits(config, {"SUBARU": "NATSUKI SUBARU"},
                             {"Subaru": 244, "NATSUKI SUBARU": 168})
        merged = apply_merges(config, splits, force_ambiguous=True)
        self.assertEqual(merged["NATSUKI SUBARU"]["adapter_id"],
                         "breathy_alto_50s_f_fantasy")

    def test_lora_versus_custom_is_not_ambiguous(self):
        # A deliberate voice against an auto-created fallback IS decidable.
        config = {"Anna": dict(LORA), "ANNA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anna": 68, "ANNA": 2})
        self.assertFalse(splits[0]["ambiguous"])

    def test_fixed_seed_custom_choices_require_explicit_merge_override(self):
        config = {"Anna": {"type": "custom", "voice": "Aiden", "seed": 0},
                  "ANNA": {"type": "custom", "voice": "Aiden", "seed": "7"}}
        for counts in ({"Anna": 50, "ANNA": 1}, {"Anna": 1, "ANNA": 50}):
            with self.subTest(counts=counts):
                splits = find_splits(config, {}, counts)
                self.assertTrue(splits[0]["ambiguous"])
                self.assertEqual(config, apply_merges(config, splits))
                forced = apply_merges(config, splits, force_ambiguous=True)
                winner = max(counts, key=counts.get)
                self.assertEqual(config[winner], forced["Anna"])
                self.assertEqual(config[winner], forced["ANNA"])
                self.assertEqual(0, config["Anna"]["seed"])
                self.assertEqual("7", config["ANNA"]["seed"])

    def test_fixed_seed_custom_choice_outranks_unseeded_fallback(self):
        config = {"Anna": {"type": "custom", "voice": "Aiden", "seed": "0"},
                  "ANNA": {"type": "custom", "voice": "Aiden", "seed": "-1"}}
        splits = find_splits(config, {}, {"Anna": 1, "ANNA": 99})
        self.assertEqual("Anna", splits[0]["winner"])
        self.assertFalse(splits[0]["ambiguous"])
        self.assertEqual("0", apply_merges(config, splits)["ANNA"]["seed"])
        self.assertEqual("-1", config["ANNA"]["seed"])

    def test_fallback_variant_does_not_hide_conflicting_deliberate_choices(self):
        for first, second in (({**CUSTOM, "seed": 10}, {**CUSTOM, "seed": 20}),
                              (dict(LORA), dict(LORA_OTHER))):
            with self.subTest(first=first, second=second):
                config = {"Anna": first, "ANNA": second, "Ann": dict(CUSTOM)}
                splits = find_splits(config, {"ANN": "ANNA"}, {"Ann": 99})
                self.assertEqual(1, len(splits))
                self.assertTrue(splits[0]["ambiguous"])
                self.assertEqual(config, apply_merges(config, splits))

    def test_cli_preserves_conflicting_fixed_seed_choices_unless_forced(self):
        config = {"Anna": {"type": "custom", "voice": "Aiden", "seed": "0"},
                  "ANNA": {"type": "custom", "voice": "Aiden", "seed": "7"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "voices.json")
            aliases = Path(folder, "aliases.json")
            script = Path(folder, "script.json")
            original = json.dumps(config, indent=2).encode()
            path.write_bytes(original)
            aliases.write_text("{}")
            script.write_text(json.dumps([{"speaker": "Anna"}] + [{"speaker": "ANNA"}] * 20))
            argv = ["repair_voice_config.py", "--config", str(path), "--aliases", str(aliases),
                    "--script", str(script), "--apply"]
            output = io.StringIO()
            with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                repair_voice_config.main()
            self.assertEqual(config, json.loads(path.read_bytes()))
            self.assertIn("AMBIGUOUS", output.getvalue())
            self.assertEqual(original, next(Path(folder).glob("voices.json.bak-*")).read_bytes())
            with patch.object(sys, "argv", argv + ["--force-ambiguous"]), \
                 contextlib.redirect_stdout(io.StringIO()):
                repair_voice_config.main()
            saved = json.loads(path.read_bytes())
            self.assertEqual(config["ANNA"], saved["Anna"])
            self.assertEqual(config["ANNA"], saved["ANNA"])


class TestApplyMerges(unittest.TestCase):

    def test_losers_adopt_the_winner_voice(self):
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        merged = apply_merges(config, splits)
        self.assertEqual(voice_signature(merged["ANASTASIA"]),
                         voice_signature(LORA))

    def test_loser_keys_are_kept_not_deleted(self):
        # The script still says 'ANASTASIA'; dropping the key would send those
        # lines to a fallback voice instead of the right one.
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        self.assertIn("ANASTASIA", apply_merges(config, splits))

    def test_input_config_is_not_mutated(self):
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        apply_merges(config, splits)
        self.assertEqual(config["ANASTASIA"]["type"], "custom")

    def test_merged_entries_are_independent_copies(self):
        # A shared dict would make a later edit to one character silently
        # change another.
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        merged = apply_merges(config, splits)
        merged["ANASTASIA"]["voice"] = "CHANGED"
        self.assertEqual(merged["Anastasia"]["voice"], "Ryan")

    def test_untouched_characters_survive(self):
        config = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM),
                  "NARRATOR": dict(DESIGN)}
        splits = find_splits(config, {}, {"Anastasia": 68, "ANASTASIA": 2})
        merged = apply_merges(config, splits)
        self.assertEqual(voice_signature(merged["NARRATOR"]),
                         voice_signature(DESIGN))


class TestRepairPersistence(unittest.TestCase):
    def test_apply_rejects_ui_change_after_report_without_writing_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "voice_config.json")
            original = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
            with open(path, "w", encoding="utf-8") as target:
                json.dump(original, target)
            real_find_splits = repair_voice_config.find_splits

            def intervening_save(config, aliases, counts):
                splits = real_find_splits(config, aliases, counts)
                with open(path, "w", encoding="utf-8") as target:
                    json.dump({**original, "NEW": dict(CLONE)}, target)
                return splits

            argv = ["repair_voice_config.py", "--config", path,
                    "--aliases", os.path.join(directory, "missing.json"),
                    "--script", os.path.join(directory, "missing_chunks.json"),
                    "--apply"]
            with patch.object(sys, "argv", argv), \
                 patch.object(repair_voice_config, "find_splits",
                              side_effect=intervening_save):
                with self.assertRaisesRegex(RuntimeError, "changed since the repair report"):
                    repair_voice_config.main()
            with open(path, encoding="utf-8") as source:
                saved = json.load(source)
            self.assertEqual(dict(CLONE), saved["NEW"])
            self.assertEqual(dict(CUSTOM), saved["ANASTASIA"])
            self.assertEqual(["voice_config.json", "voice_config.json.lock"], sorted(os.listdir(directory)))
            from tests.test_support import assert_file_lock_released
            assert_file_lock_released(path)

    def test_apply_uses_atomic_write_and_keeps_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "voice_config.json")
            original = {"Anastasia": dict(LORA), "ANASTASIA": dict(CUSTOM)}
            with open(path, "w", encoding="utf-8") as target:
                json.dump(original, target)
            argv = ["repair_voice_config.py", "--config", path,
                    "--aliases", os.path.join(directory, "missing.json"),
                    "--script", os.path.join(directory, "missing_chunks.json"),
                    "--apply"]
            with patch.object(sys, "argv", argv):
                repair_voice_config.main()
            with open(path, encoding="utf-8") as source:
                saved = json.load(source)
            self.assertEqual(dict(LORA), saved["ANASTASIA"])
            backups = [name for name in os.listdir(directory) if ".bak-" in name]
            self.assertEqual(1, len(backups))
            with open(os.path.join(directory, backups[0]), encoding="utf-8") as source:
                self.assertEqual(original, json.load(source))


if __name__ == "__main__":
    unittest.main()


class RepairAliasShapeTests(unittest.TestCase):
    def test_invalid_alias_shapes_raise_value_error_without_publishing_changes(self):
        import repair_voice_config as repair
        invalid_values = ([], ["BOB"], "BOB", 0, False,
                          {"Bob": None}, {"Bob": 7}, {"Bob": []}, {"Bob": ""})
        for aliases in invalid_values:
            with self.subTest(aliases=aliases), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / "voices.json"
                aliases_path = root / "aliases.json"
                script = root / "script.json"
                config.write_text(json.dumps({"Bob": {"type": "lora", "adapter_id": "deliberate"},
                                              "BOB": {"type": "custom", "voice": "Ryan", "seed": -1}}))
                aliases_path.write_text(json.dumps(aliases))
                script.write_text("[]")
                originals = {p.name: p.read_bytes() for p in (config, aliases_path, script)}
                args = ["repair_voice_config", "--config", str(config), "--aliases", str(aliases_path),
                        "--script", str(script), "--apply"]
                with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(ValueError, "alias registry"):
                        repair.main()
                self.assertEqual(set(originals), {p.name for p in root.iterdir()})
                for path in (config, aliases_path, script):
                    self.assertEqual(originals[path.name], path.read_bytes())

    def test_valid_map_and_missing_aliases_keep_the_existing_case_contract(self):
        import copy
        aliases = {"Bob": "Robert"}
        before = copy.deepcopy(aliases)
        self.assertEqual("ROBERT", canonical("bOB", aliases))
        self.assertEqual("ANN", canonical("Ann", {}))
        self.assertEqual("ANN", canonical("Ann", None))
        self.assertEqual("", canonical(None, None))
        self.assertEqual(before, aliases)


class SharedRepairIdentityTests(unittest.TestCase):
    def test_generation_equivalent_identities_report_a_split_and_keep_voice_rules(self):
        import copy
        from speaker_identity import resolve_speaker_label
        for names, aliases in ((['Bri-chan', 'Bri chan'], {}),
                               (['José', 'José'], {}),
                               (['BRI', 'Bri chan'], {'Bri-chan': 'BRI'})):
            with self.subTest(names=names):
                config = {names[0]: dict(LORA), names[1]: dict(CUSTOM), 'Other': dict(CLONE)}
                prior = copy.deepcopy((config, aliases))
                if not aliases:
                    self.assertIsNotNone(resolve_speaker_label(names[1], [names[0]]))
                else:
                    self.assertEqual('BRI', canonical(names[1], aliases))
                splits = find_splits(config, aliases, {names[0]: 1, names[1]: 4})
                self.assertEqual(1, len(splits), splits)
                split = splits[0]
                self.assertEqual(set(names), set(split['keys']))
                self.assertEqual(names[0], split['winner'])
                self.assertEqual(min(canonical(name, aliases) for name in names), split['canonical'])
                merged = apply_merges(config, splits)
                self.assertEqual(LORA, merged[names[1]])
                self.assertEqual(CLONE, merged['Other'])
                self.assertEqual(prior, (config, aliases))
                self.assertEqual(set(config), set(merged))
                config[names[1]] = dict(LORA_OTHER)
                ambiguous = find_splits(config, aliases, {})
                self.assertTrue(ambiguous[0]['ambiguous'])
                self.assertEqual(config, apply_merges(config, ambiguous))

    def test_distinct_and_empty_normalized_identities_do_not_merge(self):
        for names in (['Alice', 'Bob'], ['Alice', 'Alicia'], ['Bri_chan', 'Bri chan'], ['!', '?']):
            with self.subTest(names=names):
                self.assertEqual([], find_splits({names[0]: dict(LORA), names[1]: dict(CUSTOM)}, {}, {}))

    def test_actual_cli_dry_run_and_apply_preserve_raw_keys_and_backup(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'voices.json'
            aliases = root / 'aliases.json'
            script = root / 'script.json'
            original = {'Bri-chan': dict(LORA), 'Bri chan': dict(CUSTOM), 'Other': dict(CLONE)}
            config.write_text(json.dumps(original))
            aliases.write_text('{}')
            script.write_text(json.dumps([{'speaker': 'Bri-chan'}, {'speaker': 'Bri chan'}]))
            before = config.read_bytes()
            args = [sys.executable, repair_voice_config.__file__, '--config', str(config),
                    '--aliases', str(aliases), '--script', str(script)]
            report = subprocess.run(args, capture_output=True, text=True, timeout=10)
            self.assertEqual(0, report.returncode, report.stderr)
            self.assertIn('1 characters cast in more than one voice', report.stdout)
            self.assertEqual(before, config.read_bytes())
            self.assertEqual([], list(root.glob('voices.json.bak-*')))
            applied = subprocess.run(args + ['--apply'], capture_output=True, text=True, timeout=10)
            self.assertEqual(0, applied.returncode, applied.stderr)
            updated = json.loads(config.read_text())
            self.assertEqual({**original, 'Bri chan': dict(LORA)}, updated)
            backups = list(root.glob('voices.json.bak-*'))
            self.assertEqual(1, len(backups))
            self.assertEqual(before, backups[0].read_bytes())
            self.assertEqual('{}', aliases.read_text())
