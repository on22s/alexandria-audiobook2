"""Unit tests for the translation preview builder; synthetic text only."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import translated_edition

PAIR = {"from_code": "en", "to_code": "fr", "from_name": "English",
        "to_name": "French", "package_version": "1.0"}


def get_book(root, rows, source_lines=None):
    (root / "state.json").write_text(json.dumps(
        {"active_book_id": "fixture", "book_generation": "one",
         "input_file_path": str(root / "source.txt")}), encoding="utf-8")
    (root / "annotated_script.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "voice_config.json").write_text(
        json.dumps({"ALICE": {"voice": "Aiden"}}), encoding="utf-8")
    (root / "config.json").write_text(
        json.dumps({"tts_language": "english"}), encoding="utf-8")
    (root / "source.txt").write_text(
        "\n".join(source_lines if source_lines is not None
                  else [row["text"] for row in rows]), encoding="utf-8")


def get_bytes(root, names=("annotated_script.json", "source.txt", "state.json",
                           "voice_config.json", "config.json")):
    return {name: (root / name).read_bytes() for name in names}


def get_fake_argos(packages):
    """A fake argostranslate that records any index/download access."""
    calls = []
    package = ModuleType("argostranslate.package")
    package.get_installed_packages = lambda: list(packages)
    package.update_package_index = lambda: calls.append("index")
    package.get_available_packages = lambda: calls.append("index") or []
    package.download = lambda *args: calls.append("download")
    parent = ModuleType("argostranslate")
    parent.package = package
    return parent, package, calls


class FakePackage:
    def __init__(self, from_code, to_code, package_type="translate", version="9.9"):
        self.from_code = from_code
        self.to_code = to_code
        self.from_name = from_code.upper()
        self.to_name = to_code.upper()
        self.package_version = version
        self.type = package_type


class InstalledPairsTests(unittest.TestCase):
    def test_unavailable_without_package(self):
        with patch.dict(sys.modules, {"argostranslate": None,
                                      "argostranslate.package": None}):
            result = translated_edition.get_installed_pairs()
        self.assertFalse(result["available"])
        self.assertEqual("unavailable", result["reason"])
        self.assertEqual([], result["pairs"])

    def test_reports_only_direct_translate_packages(self):
        fake, package, calls = get_fake_argos([
            FakePackage("en", "fr"), FakePackage("fr", "en"),
            FakePackage("en", "fr", package_type="sbd"), FakePackage("de", "de")])
        with patch.dict(sys.modules, {"argostranslate": fake,
                                      "argostranslate.package": package}):
            result = translated_edition.get_installed_pairs()
        self.assertTrue(result["available"])
        pairs = [(p["from_code"], p["to_code"]) for p in result["pairs"]]
        self.assertEqual([("en", "fr"), ("fr", "en")], pairs)
        self.assertEqual([], calls)

    def test_argos_translator_refuses_without_download(self):
        fake, package, calls = get_fake_argos([FakePackage("en", "fr")])
        with patch.dict(sys.modules, {"argostranslate": fake,
                                      "argostranslate.package": package}):
            with self.assertRaisesRegex(ValueError, "not installed"):
                translated_edition.build_argos_translator("en", "de")
        self.assertEqual([], calls)

    def test_argos_translator_runtime_without_translate_is_unavailable(self):
        fake, package, calls = get_fake_argos([FakePackage("en", "fr")])
        with patch.dict(sys.modules, {"argostranslate": fake,
                                      "argostranslate.package": package,
                                      "argostranslate.translate": None}):
            with self.assertRaisesRegex(ValueError, "runtime is unavailable"):
                translated_edition.build_argos_translator("en", "fr")
        self.assertEqual([], calls)

    def _fake_argos_with_device(self, packages, device):
        fake, package, calls = get_fake_argos(packages)
        translate_module = ModuleType("argostranslate.translate")
        translate_module.translate = lambda text, from_code, to_code: "FR: " + text
        fake.translate = translate_module
        settings_module = ModuleType("argostranslate.settings")
        settings_module.device = device
        fake.settings = settings_module
        return fake, package, translate_module, settings_module, calls

    def test_argos_translator_refuses_non_cpu_device(self):
        # Upstream settings.device defaults to "cpu" but a user can set
        # ARGOS_DEVICE_TYPE=cuda, which would run GPU inference outside the
        # GPU lock. The builder must refuse it before any translate call.
        fake, package, translate_module, settings_module, calls = \
            self._fake_argos_with_device([FakePackage("en", "fr")], "cuda")
        with patch.dict(sys.modules, {"argostranslate": fake,
                                      "argostranslate.package": package,
                                      "argostranslate.translate": translate_module,
                                      "argostranslate.settings": settings_module}):
            with self.assertRaisesRegex(ValueError, "device is not CPU"):
                translated_edition.build_argos_translator("en", "fr")
        self.assertEqual([], calls)

    def test_argos_translator_runs_on_cpu_device(self):
        fake, package, translate_module, settings_module, calls = \
            self._fake_argos_with_device([FakePackage("en", "fr")], "cpu")
        with patch.dict(sys.modules, {"argostranslate": fake,
                                      "argostranslate.package": package,
                                      "argostranslate.translate": translate_module,
                                      "argostranslate.settings": settings_module}):
            translate = translated_edition.build_argos_translator("en", "fr")
        self.assertEqual("FR: Synthetic line.", translate("Synthetic line."))


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def build(self, rows, translate_fn, **kwargs):
        get_book(self.root, rows)
        return translated_edition.build_proposal(
            str(self.root), source_language="en", target_language="fr",
            translate_fn=translate_fn, installed_pairs=[PAIR], **kwargs)

    def test_complete_proposal_saved_and_bytes_unchanged(self):
        rows = [{"speaker": "ALICE", "text": "The small town woke slowly."},
                {"speaker": "BOB", "text": "The baker opened the wooden door."}]
        get_book(self.root, rows)
        before = get_bytes(self.root)
        proposal = translated_edition.build_proposal(
            str(self.root), source_language="en", target_language="fr",
            translate_fn=lambda text: "FR: " + text, installed_pairs=[PAIR],
            runtime_version="2.5.0")
        self.assertEqual("complete", proposal["status"])
        self.assertEqual("proposal, not approved", proposal["label"])
        self.assertEqual("en", proposal["source_language"])
        self.assertEqual("fr", proposal["target_language"])
        self.assertEqual(64, len(proposal["source_script_sha256"]))
        self.assertEqual(2, proposal["excerpt"]["row_count"])
        self.assertEqual(["ALICE", "BOB"], [r["speaker"] for r in proposal["rows"]])
        saved = (self.root / "translation_proposals" / (proposal["proposal_id"] + ".json"))
        self.assertTrue(saved.is_file())
        saved_proposal = json.loads(saved.read_text())
        self.assertEqual(proposal["proposal_id"], saved_proposal["proposal_id"])
        # Provenance is in the saved artifact, not only in the HTTP response.
        self.assertEqual("1.0", saved_proposal["package_version"])
        self.assertEqual("2.5.0", saved_proposal["runtime_version"])
        self.assertEqual(before, get_bytes(self.root))

    def test_unsupported_pair_refused(self):
        get_book(self.root, [{"speaker": "ALICE", "text": "Synthetic line."}])
        with self.assertRaisesRegex(ValueError, "not installed"):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="de",
                translate_fn=lambda text: text, installed_pairs=[PAIR])

    def test_argos_translator_unavailable_refused(self):
        get_book(self.root, [{"speaker": "ALICE", "text": "Synthetic line."}])
        with patch.dict(sys.modules, {"argostranslate": None,
                                      "argostranslate.package": None}):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                translated_edition.build_argos_translator("en", "fr")

    def test_partial_failure_is_never_complete(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line one."},
                {"speaker": "BOB", "text": "Synthetic line two."},
                {"speaker": "ALICE", "text": "Synthetic line three."}]
        calls = []

        def flaky(text):
            calls.append(text)
            if "two" in text:
                raise RuntimeError("synthetic translator failure")
            return "FR: " + text

        proposal = self.build(rows, flaky)
        self.assertEqual("partial", proposal["status"])
        self.assertEqual("failed", proposal["rows"][1]["status"])
        self.assertIn("synthetic translator failure", proposal["rows"][1]["error"])
        self.assertEqual("translated", proposal["rows"][0]["status"])
        self.assertEqual("translated", proposal["rows"][2]["status"])

    def test_cancellation_marks_remaining_rows(self):
        rows = [{"speaker": "ALICE", "text": f"Synthetic line {index}."} for index in range(5)]
        seen = []

        def cancel_after_two():
            seen.append(1)
            return len(seen) > 2

        proposal = self.build(rows, lambda text: "FR: " + text,
                              cancel_check=cancel_after_two)
        self.assertEqual("cancelled", proposal["status"])
        self.assertEqual(["translated", "translated", "cancelled", "cancelled", "cancelled"],
                         [row["status"] for row in proposal["rows"]])

    def test_control_tags_and_markup_unsupported(self):
        rows = [{"speaker": "ALICE", "text": "The door creaked [pause] softly."},
                {"speaker": "BOB", "text": "A <b>light</b> flickered."},
                {"speaker": "ALICE", "text": "The window stayed shut."}]
        called = []
        proposal = self.build(rows, lambda text: called.append(text) or "FR: " + text)
        self.assertEqual("partial", proposal["status"])
        self.assertEqual("unsupported", proposal["rows"][0]["status"])
        self.assertEqual("unsupported", proposal["rows"][1]["status"])
        self.assertEqual(rows[0]["text"], proposal["rows"][0]["translated_text"])
        self.assertEqual(rows[1]["text"], proposal["rows"][1]["translated_text"])
        self.assertEqual(["The window stayed shut."], called)

    def test_empty_source_row_skipped(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line."},
                {"speaker": "BOB", "text": "   "}]
        called = []
        proposal = self.build(rows, lambda text: called.append(text) or "FR: " + text)
        self.assertEqual("partial", proposal["status"])
        self.assertEqual("skipped", proposal["rows"][1]["status"])
        self.assertEqual(["Synthetic line."], called)

    def test_known_bad_translations_are_flagged(self):
        rows = [{"speaker": "ALICE", "text": "The train arrived at 42 minutes."},
                {"speaker": "BOB", "text": "The bridge lights were bright."},
                {"speaker": "ALICE", "text": "The garden gate was locked."}]

        def known_bad(text):
            if "42" in text:
                return "Le train est arrive."
            if "bridge" in text:
                return ""
            return text

        proposal = self.build(rows, known_bad)
        self.assertEqual("partial", proposal["status"])
        self.assertEqual(["number_changed"], proposal["rows"][0]["flags"])
        self.assertEqual(["empty_translation"], proposal["rows"][1]["flags"])
        self.assertEqual(["copied_source"], proposal["rows"][2]["flags"])
        self.assertTrue(all(row["status"] == "translated" for row in proposal["rows"]))

    def test_number_punctuation_is_not_a_number_change(self):
        # "He had 5." must not flag number_changed against "Er hatte 5":
        # the old pattern swallowed the sentence period into the number.
        rows = [{"speaker": "ALICE", "text": "He had 5."},
                {"speaker": "BOB", "text": "Er hatte 5"}]
        mapping = {"He had 5.": "Er hatte 5", "Er hatte 5": "He had 5."}
        proposal = self.build(rows, lambda text: mapping[text])
        self.assertEqual("complete", proposal["status"])
        self.assertEqual([], proposal["rows"][0]["flags"])
        self.assertEqual([], proposal["rows"][1]["flags"])

    def test_changed_number_still_flagged_with_punctuation(self):
        rows = [{"speaker": "ALICE", "text": "He had 5."},
                {"speaker": "BOB", "text": "The fare was 1,000 coins."}]
        mapping = {"He had 5.": "Er hatte 6.", "The fare was 1,000 coins.": "Die Fahrt kostete 1,000 Muenzen."}
        proposal = self.build(rows, lambda text: mapping[text])
        self.assertEqual("partial", proposal["status"])
        self.assertEqual(["number_changed"], proposal["rows"][0]["flags"])
        self.assertEqual([], proposal["rows"][1]["flags"])

    def test_stale_source_detected(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line."}]
        proposal = self.build(rows, lambda text: "FR: " + text)
        self.assertFalse(translated_edition.is_proposal_stale(str(self.root), proposal))
        (self.root / "annotated_script.json").write_text(
            json.dumps([{"speaker": "ALICE", "text": "Synthetic line changed."}]),
            encoding="utf-8")
        self.assertTrue(translated_edition.is_proposal_stale(str(self.root), proposal))

    def test_excerpt_bound(self):
        rows = [{"speaker": "ALICE", "text": f"Synthetic line {index}."} for index in range(40)]
        proposal = self.build(rows, lambda text: "FR: " + text)
        self.assertEqual(25, proposal["excerpt"]["row_count"])
        self.assertEqual(40, proposal["excerpt"]["total_rows"])
        self.assertEqual(25, len(proposal["rows"]))
        self.assertEqual(25, proposal["row_limit"])
        # A translated prefix of a longer Script is not a complete edition.
        self.assertEqual("excerpt_complete", proposal["status"])

    def test_missing_script_fails_loud(self):
        (self.root / "state.json").write_text(json.dumps(
            {"active_book_id": "fixture"}), encoding="utf-8")
        with self.assertRaises(ValueError):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="fr",
                translate_fn=lambda text: text, installed_pairs=[PAIR])

    def test_script_changed_mid_build_is_not_saved(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line one."},
                {"speaker": "BOB", "text": "Synthetic line two."}]
        get_book(self.root, rows)

        def mutate(text):
            if text == "Synthetic line two.":
                (self.root / "annotated_script.json").write_text(
                    json.dumps([{"speaker": "ALICE", "text": "Changed synthetic line."}]),
                    encoding="utf-8")
            return "FR: " + text

        with self.assertRaisesRegex(ValueError, "changed"):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="fr",
                translate_fn=mutate, installed_pairs=[PAIR])
        self.assertFalse((self.root / "translation_proposals").exists())

    def test_book_switch_mid_build_is_not_saved(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line one."},
                {"speaker": "BOB", "text": "Synthetic line two."}]
        get_book(self.root, rows)

        def switch_book(text):
            # Same script bytes, different book identity: the SHA check alone
            # cannot see this, the book operation token must.
            (self.root / "state.json").write_text(json.dumps(
                {"active_book_id": "other", "book_generation": "two",
                 "input_file_path": str(self.root / "source.txt")}), encoding="utf-8")
            return "FR: " + text

        with self.assertRaisesRegex(ValueError, "changed"):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="fr",
                translate_fn=switch_book, installed_pairs=[PAIR])
        self.assertFalse((self.root / "translation_proposals").exists())

    def test_non_text_row_is_refused_without_saving(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line."},
                {"speaker": "BOB", "text": None}]
        get_book(self.root, rows, source_lines=["Synthetic line.", "placeholder"])
        with self.assertRaisesRegex(ValueError, "need inspection"):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="fr",
                translate_fn=lambda text: "FR: " + text, installed_pairs=[PAIR])
        self.assertFalse((self.root / "translation_proposals").exists())

    def test_non_text_row_beyond_the_excerpt_is_still_refused(self):
        # The whole-Script refusal is deliberate (see _get_script_rows): a
        # bad row outside the 25-row excerpt still means a corrupt Script.
        rows = [{"speaker": "ALICE", "text": f"Synthetic line {index}."}
                for index in range(26)]
        rows.append({"speaker": "BOB", "text": None})
        get_book(self.root, rows,
                 source_lines=[row["text"] or "placeholder" for row in rows])
        with self.assertRaisesRegex(ValueError, "need inspection"):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="fr",
                translate_fn=lambda text: "FR: " + text, installed_pairs=[PAIR])
        self.assertFalse((self.root / "translation_proposals").exists())

    def test_latest_proposal_returns_most_recent_for_current_book(self):
        first = self.build([{"speaker": "ALICE", "text": "Synthetic line one."}],
                           lambda text: "FR: " + text)
        second = self.build([{"speaker": "ALICE", "text": "Synthetic line two."}],
                            lambda text: "FR: " + text)
        result = translated_edition.get_latest_proposal(str(self.root))
        self.assertEqual([], result["malformed_proposals"])
        self.assertEqual(second["proposal_id"], result["proposal"]["proposal_id"])
        self.assertNotEqual(first["proposal_id"], result["proposal"]["proposal_id"])

    def test_latest_proposal_empty_when_none_saved(self):
        get_book(self.root, [{"speaker": "ALICE", "text": "Synthetic line."}])
        result = translated_edition.get_latest_proposal(str(self.root))
        self.assertIsNone(result["proposal"])
        self.assertEqual([], result["malformed_proposals"])

    def test_latest_proposal_never_returns_another_books_proposal(self):
        proposal = self.build([{"speaker": "ALICE", "text": "Synthetic line."}],
                              lambda text: "FR: " + text)
        (self.root / "state.json").write_text(json.dumps(
            {"active_book_id": "other", "book_generation": "two",
             "input_file_path": str(self.root / "source.txt")}), encoding="utf-8")
        result = translated_edition.get_latest_proposal(str(self.root))
        self.assertIsNone(result["proposal"])
        self.assertEqual([], result["malformed_proposals"])
        # The file is still on disk, refused like get_proposal refuses it.
        self.assertTrue((self.root / "translation_proposals"
                         / (proposal["proposal_id"] + ".json")).is_file())

    def test_latest_proposal_reports_malformed_without_hiding_a_good_one(self):
        good = self.build([{"speaker": "ALICE", "text": "Synthetic line."}],
                          lambda text: "FR: " + text)
        proposals_dir = self.root / "translation_proposals"
        (proposals_dir / ("f" * 32 + ".json")).write_text("not json", encoding="utf-8")
        (proposals_dir / ("e" * 32 + ".json")).write_text(
            json.dumps({"kind": "something_else"}), encoding="utf-8")
        result = translated_edition.get_latest_proposal(str(self.root))
        self.assertEqual(good["proposal_id"], result["proposal"]["proposal_id"])
        self.assertEqual(sorted(["f" * 32 + ".json", "e" * 32 + ".json"]),
                         sorted(result["malformed_proposals"]))

    def test_get_proposal_rejects_bad_or_traversing_ids(self):
        proposal = self.build([{"speaker": "ALICE", "text": "Synthetic line."}],
                              lambda text: "FR: " + text)
        for bad in ("../../state", "../" + proposal["proposal_id"], "A" * 32,
                    "g" * 32, proposal["proposal_id"][:-1], ""):
            with self.assertRaisesRegex(ValueError, "Invalid proposal id"):
                translated_edition.get_proposal(str(self.root), bad)
        self.assertEqual(proposal["proposal_id"], translated_edition.get_proposal(
            str(self.root), proposal["proposal_id"])["proposal_id"])

    def test_proposal_from_another_book_is_refused(self):
        proposal = self.build([{"speaker": "ALICE", "text": "Synthetic line."}],
                              lambda text: "FR: " + text)
        (self.root / "state.json").write_text(json.dumps(
            {"active_book_id": "other", "book_generation": "two",
             "input_file_path": str(self.root / "source.txt")}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different book"):
            translated_edition.get_proposal(str(self.root), proposal["proposal_id"])

    def test_proposal_with_no_book_id_is_never_the_current_book(self):
        # None must never compare equal to a real book id.
        proposal = self.build([{"speaker": "ALICE", "text": "Synthetic line."}],
                              lambda text: "FR: " + text)
        saved = self.root / "translation_proposals" / (proposal["proposal_id"] + ".json")
        payload = json.loads(saved.read_text())
        payload["book_id"] = None
        saved.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different book"):
            translated_edition.get_proposal(str(self.root), proposal["proposal_id"])

    def test_busy_book_during_stale_recheck_is_a_refusal_not_stale(self):
        proposal = self.build([{"speaker": "ALICE", "text": "Synthetic line."}],
                              lambda text: "FR: " + text)
        with patch.object(translated_edition, "ensure_book_state",
                          side_effect=TimeoutError("fixture lock busy")):
            with self.assertRaisesRegex(ValueError, "busy"):
                translated_edition.is_proposal_stale(str(self.root), proposal)

    def test_busy_book_is_reported_as_a_refusal(self):
        get_book(self.root, [{"speaker": "ALICE", "text": "Synthetic line."}])
        with patch.object(translated_edition, "ensure_book_state",
                          side_effect=TimeoutError("fixture lock busy")):
            with self.assertRaisesRegex(ValueError, "busy"):
                translated_edition.build_proposal(
                    str(self.root), source_language="en", target_language="fr",
                    translate_fn=lambda text: "FR: " + text, installed_pairs=[PAIR])
        self.assertFalse((self.root / "translation_proposals").exists())

    def test_script_deleted_mid_build_is_a_refusal_not_a_crash(self):
        rows = [{"speaker": "ALICE", "text": "Synthetic line one."},
                {"speaker": "BOB", "text": "Synthetic line two."}]
        get_book(self.root, rows)

        def delete_script(text):
            (self.root / "annotated_script.json").unlink()
            return "FR: " + text

        with self.assertRaisesRegex(ValueError, "changed during preview"):
            translated_edition.build_proposal(
                str(self.root), source_language="en", target_language="fr",
                translate_fn=delete_script, installed_pairs=[PAIR])
        self.assertFalse((self.root / "translation_proposals").exists())

    def test_late_cancel_is_saved_as_cancelled_never_complete(self):
        cancelled = []

        def translate_last_row_then_cancel(text):
            if "two" in text:
                cancelled.append(True)
            return "FR: " + text

        proposal = self.build(
            [{"speaker": "ALICE", "text": "Synthetic line one."},
             {"speaker": "BOB", "text": "Synthetic line two."}],
            translate_last_row_then_cancel,
            cancel_check=lambda: bool(cancelled))
        # Every row translated, but the cancel arrived before publication:
        # the saved status must be cancelled, not complete.
        self.assertEqual("cancelled", proposal["status"])
        self.assertEqual(2, proposal["counts"]["translated"])
        saved = (self.root / "translation_proposals"
                 / (proposal["proposal_id"] + ".json"))
        self.assertEqual("cancelled", json.loads(saved.read_text())["status"])


if __name__ == "__main__":
    unittest.main()
