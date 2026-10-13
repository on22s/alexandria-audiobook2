"""Every paid PDNC fixture/context evaluation must remain input-bound."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import pdnc_eval as evaluation
from experiments import pdnc_generalisation as generalisation
from experiments import pdnc_failure_telemetry as telemetry
from experiments.attribution_hybrid import load_rows
from experiments.pdnc_results import get_pdnc_result


def fixture(context="short context", book="Synthetic"):
    return {"book": book, "roster": ["ALICE", "OTHER"], "aliases": [],
            "context_chars": 200 if context == "short context" else 3200,
            "entries": [{"id": "shared-quote", "line": "A synthetic quoted line.",
                         "expected_speaker": "ALICE", "prev_context": context,
                         "next_context": "after", "quote_type": "Explicit",
                         "category": "major"}]}


def write_fixture(root, name, content, **dump_options):
    directory = root / "fixtures"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(content, **dump_options), encoding="utf-8")
    return path


class PdncVariantTests(unittest.TestCase):
    def run_telemetry(self, root, document):
        artifact, output = root / "evaluation.json", root / "telemetry.json"
        artifact.write_text(json.dumps(document))
        argv = ["telemetry", "--artifact", str(artifact), "--out", str(output),
                "--books", "Synthetic", "--arm", "base"]
        with (patch.object(telemetry, "APP", str(root)),
              patch.object(telemetry.sys, "argv", argv),
              patch.object(telemetry, "build_fixture", side_effect=AssertionError("unexpected corpus build")),
              contextlib.redirect_stdout(io.StringIO())):
            telemetry.main()
        return json.loads(output.read_text())

    def run_evaluation(self, root, paths=None):
        output = root / "evaluation.json"
        argv = ["pdnc_eval", "--out", str(output), "--limit", "1", "--batch", "1"]
        if paths is not None:
            argv += ["--fixtures", *(str(path) for path in paths)]
        scales, calls = [], []

        def set_scale(base_url, scale):
            scales.append(scale)
            return scale

        def attribute(client, model, frozen, params, roster, **kwargs):
            contexts = kwargs["neighbor_contexts"]
            calls.append({"scale": scales[-1], "contexts": copy.deepcopy(contexts)})
            return [{"speaker": "ALICE" if scales[-1] == 1
                     or context["previous_context"]["text"] == "short context" else "OTHER"}
                    for context in contexts]

        log = io.StringIO()
        error = None
        with (patch.object(evaluation, "APP", str(root) + os.sep),
              patch.object(evaluation.sys, "argv", argv),
              patch.object(evaluation, "OpenAI", return_value=object()) as client,
              patch.object(evaluation, "set_scale", side_effect=set_scale),
              patch.object(evaluation, "attribute_batch", side_effect=attribute),
              contextlib.redirect_stdout(log)):
            try:
                evaluation.main()
            except Exception as caught:
                error = caught
        document = json.loads(output.read_text()) if output.exists() else None
        return document, calls, scales, client.call_count, log.getvalue(), error

    def test_same_book_variants_keep_both_paired_rows_and_all_pooled_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [write_fixture(root, "short.json", fixture()),
                     write_fixture(root, "long.json", fixture("long context"))]
            before = [path.read_bytes() for path in paths]
            document, calls, scales, _, log, error = self.run_evaluation(root, paths)
            self.assertIsNone(error)
            self.assertEqual(before, [path.read_bytes() for path in paths])
            self.assertEqual(4, len(calls))
            self.assertEqual([0.0, 1.0, 0.0, 1.0], scales)
            self.assertEqual(2, len(document))
            outcomes = {}
            for key, arms in document.items():
                identity = arms["base"]["fixture"]
                self.assertEqual(identity, arms["lora"]["fixture"])
                self.assertEqual("Synthetic", identity["book"])
                self.assertEqual("Synthetic::" + identity["input_sha256"], key)
                source = identity["sources"][0]
                path = next(path for path in paths if source["path"] == os.path.relpath(path, evaluation.REPO))
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), source["sha256"])
                outcomes[source["context_chars"]] = tuple(arms[arm]["correct"] for arm in ("base", "lora"))
                for arm in ("base", "lora"):
                    self.assertEqual(1, arms[arm]["n"])
                    self.assertEqual(["shared-quote"], [row["id"] for row in arms[arm]["rows"]])
                self.assertNotIn("short context", json.dumps(identity))
                self.assertNotIn("long context", json.dumps(identity))
            self.assertEqual({200: (1, 1), 3200: (0, 1)}, outcomes)
            self.assertEqual({key + ":shared-quote" for key in document},
                             {row["id"] for row in load_rows(root / "evaluation.json")})
            self.assertIn("pooled  base 1/2 = 50.0%", log)
            self.assertIn("lora 2/2 = 100.0%", log)

    def test_default_glob_evaluates_and_retains_both_context_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_fixture(root, "attribution_gold_pdnc_synthetic.json", fixture())
            write_fixture(root, "attribution_gold_pdnc_synthetic_w3200.json", fixture("long context"))
            write_fixture(root, "unselected.json", fixture(book="Unselected"))
            document, calls, _, _, _, error = self.run_evaluation(root)
        self.assertIsNone(error)
        self.assertEqual(2, len(document))
        self.assertEqual(4, len(calls))
        self.assertEqual({"Synthetic"}, {arms["base"]["fixture"]["book"] for arms in document.values()})

    def test_identical_admitted_inputs_do_not_pay_twice_and_keep_source_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_fixture(root, "first.json", fixture(), indent=1)
            duplicate = fixture()
            duplicate["context_chars"] = 3200  # declared width alone does not change the actual input
            second = write_fixture(root, "copy.json", duplicate, sort_keys=True)
            document, calls, scales, _, log, error = self.run_evaluation(root, [first, first, second])
            self.assertIsNone(error)
            self.assertEqual(["Synthetic"], list(document))
            self.assertEqual(2, len(calls))
            self.assertEqual([0.0, 1.0], scales)
            sources = document["Synthetic"]["base"]["fixture"]["sources"]
            self.assertEqual(2, len(sources))
            self.assertEqual({hashlib.sha256(path.read_bytes()).hexdigest() for path in (first, second)},
                             {source["sha256"] for source in sources})
            self.assertIn("duplicate", log.lower())

    def test_same_basename_in_two_directories_does_not_collide(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left, right = root / "left", root / "right"
            left.mkdir()
            right.mkdir()
            paths = [write_fixture(left, "same.json", fixture()),
                     write_fixture(right, "same.json", fixture("long context"))]
            document, calls, _, _, _, error = self.run_evaluation(root, paths)
        self.assertIsNone(error)
        self.assertEqual(2, len(document))
        self.assertEqual(4, len(calls))

    def test_variant_content_keys_are_stable_across_order_and_file_formatting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_fixture(root, "first.json", fixture())
            second = write_fixture(root, "second.json", fixture("long context"))
            before, _, _, _, _, error = self.run_evaluation(root, [first, second])
            self.assertIsNone(error)
            first.write_text(json.dumps(fixture(), indent=4, sort_keys=True))
            after, _, _, _, _, error = self.run_evaluation(root, [second, first])
        self.assertIsNone(error)
        self.assertEqual(set(before), set(after))
        for key in before:
            for arm in ("base", "lora"):
                self.assertEqual(before[key][arm]["rows"], after[key][arm]["rows"])

    def test_input_digest_collision_is_refused_before_model_or_scale_work(self):
        get_input = evaluation.get_pdnc_fixture_input

        def collide(content, limit):
            selected, _ = get_input(content, limit)
            return selected, "a" * 64

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [write_fixture(root, "first.json", fixture()),
                     write_fixture(root, "second.json", fixture("long context"))]
            with patch.object(evaluation, "get_pdnc_fixture_input", side_effect=collide):
                document, calls, scales, clients, _, error = self.run_evaluation(root, paths)
        self.assertIsInstance(error, ValueError)
        self.assertIn("collision", str(error))
        self.assertEqual(([], [], 0, None), (calls, scales, clients, document))

    def test_later_invalid_fixture_refuses_before_any_work_or_output_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = fixture("long context")
            del invalid["entries"][0]["next_context"]
            paths = [write_fixture(root, "first.json", fixture()),
                     write_fixture(root, "invalid.json", invalid)]
            (root / "evaluation.json").write_text('{"prior":"keep"}')
            document, calls, scales, clients, _, error = self.run_evaluation(root, paths)
        self.assertIsInstance(error, ValueError)
        self.assertIn("input", str(error))
        self.assertEqual(([], [], 0), (calls, scales, clients))
        self.assertEqual({"prior": "keep"}, document)

    def test_legacy_book_key_and_campaign_reader_stay_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = fixture()
            path = write_fixture(root, "single.json", original)
            document, _, _, _, _, error = self.run_evaluation(root, [path])
        self.assertIsNone(error)
        self.assertEqual(["Synthetic"], list(document))
        self.assertEqual({"base", "lora"}, set(document["Synthetic"]))
        self.assertEqual({"book": "Synthetic", "n": 1, "base": 1, "lora": 1},
                         get_pdnc_result(document, original, 1))
        legacy = copy.deepcopy(document)
        for arm in ("base", "lora"):
            legacy["Synthetic"][arm].pop("fixture", None)
        self.assertEqual(get_pdnc_result(document, original, 1), get_pdnc_result(legacy, original, 1))
        self.assertEqual(legacy["Synthetic"]["base"]["rows"],
                         generalisation.book_rows(legacy, "Synthetic", "base"))

    def test_stamped_results_are_bound_to_context_roster_and_alias_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = fixture()
            path = write_fixture(root, "single.json", original)
            document, _, _, _, _, error = self.run_evaluation(root, [path])
        self.assertIsNone(error)
        before = copy.deepcopy(document)
        for field in ("context", "roster", "aliases"):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                if field == "context":
                    changed["entries"][0]["prev_context"] = "different context"
                elif field == "roster":
                    changed["roster"].reverse()
                else:
                    changed["aliases"] = [["ALICE", "AL"]]
                with self.assertRaisesRegex(ValueError, "input"):
                    get_pdnc_result(document, changed, 1)
        self.assertEqual(before, document)

    def test_disagreeing_arm_provenance_is_refused_by_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = fixture()
            path = write_fixture(root, "single.json", original)
            document, _, _, _, _, error = self.run_evaluation(root, [path])
        self.assertIsNone(error)
        document["Synthetic"]["lora"]["fixture"]["book"] = "AnotherBook"
        with self.assertRaisesRegex(ValueError, "identities disagree"):
            get_pdnc_result(document, original, 1)
        with self.assertRaisesRegex(ValueError, "identities disagree"):
            generalisation.book_rows(document, "Synthetic", "base")

    def test_variant_key_cannot_overwrite_a_literal_legacy_book_before_calls(self):
        original = fixture()
        admitted = {key: original[key] for key in ("book", "entries", "roster", "aliases")}
        digest = hashlib.sha256(json.dumps(admitted, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [write_fixture(root, "first.json", original),
                     write_fixture(root, "second.json", fixture("long context")),
                     write_fixture(root, "literal.json", fixture(book="Synthetic::" + digest))]
            (root / "evaluation.json").write_text('{"prior":"keep"}')
            document, calls, scales, clients, _, error = self.run_evaluation(root, paths)
        self.assertIsInstance(error, ValueError)
        self.assertIn("collision", str(error))
        self.assertEqual(([], [], 0), (calls, scales, clients))
        self.assertEqual({"prior": "keep"}, document)

    def test_generalisation_uses_recorded_book_for_every_variant_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [write_fixture(root, "short.json", fixture(book="PrideAndPrejudice")),
                     write_fixture(root, "long.json", fixture("long context", "PrideAndPrejudice")),
                     write_fixture(root, "unseen.json", fixture(book="UnseenSynthetic"))]
            document, _, _, _, _, error = self.run_evaluation(root, paths)
            self.assertIsNone(error)
            output = root / "generalisation.json"
            with (patch.object(generalisation, "FULL", str(root / "evaluation.json")),
                  patch.object(generalisation.sys, "argv", ["generalisation", "--out", str(output)]),
                  contextlib.redirect_stdout(io.StringIO())):
                generalisation.main()
            result = json.loads(output.read_text())
        self.assertEqual({"books": 1, "n": 2, "accuracy": 50.0}, result["development"])
        self.assertEqual({"books": 1, "n": 1, "accuracy": 100.0}, result["held_out"])
        self.assertEqual({"PrideAndPrejudice", "UnseenSynthetic"}, set(result["per_book"]))
        self.assertEqual(2, len(generalisation.book_rows(document, "PrideAndPrejudice", "base")))

    def test_generalisation_refuses_unprovenanced_variant_key(self):
        row = {"id": "shared", "correct": True}
        for suffix in ("a" * 64, "not-a-digest", ""):
            with self.subTest(suffix=suffix):
                document = {"PrideAndPrejudice::" + suffix:
                            {arm: {"rows": [row]} for arm in ("base", "lora")}}
                with self.assertRaisesRegex(ValueError, "fixture"):
                    generalisation.book_rows(document, "PrideAndPrejudice", "base")

    def test_explicit_null_or_partial_identity_cannot_be_treated_as_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_fixture(root, "single.json", fixture())
            original, _, _, _, _, error = self.run_evaluation(root, [path])
        self.assertIsNone(error)
        for identities in ((None, None), (None, "absent"), (original["Synthetic"]["base"]["fixture"], None)):
            with self.subTest(identities=identities):
                document = copy.deepcopy(original)
                for arm, identity in zip(("base", "lora"), identities):
                    if identity == "absent":
                        document["Synthetic"][arm].pop("fixture")
                    else:
                        document["Synthetic"][arm]["fixture"] = identity
                with self.assertRaisesRegex(ValueError, "identities disagree"):
                    get_pdnc_result(document, fixture(), 1)
                with self.assertRaisesRegex(ValueError, "identities disagree"):
                    generalisation.book_rows(document, "Synthetic", "base")

    def test_telemetry_resolves_exact_context_for_each_evaluated_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [write_fixture(root, "attribution_gold_pdnc_short.json", fixture("ALICE in short context")),
                     write_fixture(root, "attribution_gold_pdnc_long.json", fixture("ALICE in long context"))]
            document, calls, _, _, _, error = self.run_evaluation(root, paths)
            self.assertIsNone(error)
            report = self.run_telemetry(root, document)
        self.assertEqual(4, len(calls))
        self.assertEqual(set(document), set(report["books"]))
        self.assertEqual((2, 2), (report["n"], report["errors"]))
        self.assertEqual({"missed_explicit_context_evidence": 2}, report["failure_classes"])
        contexts = set()
        for key, result in report["books"].items():
            self.assertEqual("Synthetic", result["book"])
            self.assertEqual(document[key]["base"]["fixture"], result["fixture"])
            self.assertEqual((1, 1), (result["n"], result["errors"]))
            contexts.add(result["error_rows"][0]["previous_context"])
        self.assertEqual({"ALICE in short context", "ALICE in long context"}, contexts)

    def test_telemetry_legacy_unique_input_remains_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_fixture(root, "attribution_gold_pdnc_synthetic.json", fixture("ALICE in legacy context"))
            document, _, _, _, _, error = self.run_evaluation(root, [path])
            self.assertIsNone(error)
            for arm in ("base", "lora"):
                document["Synthetic"][arm].pop("fixture")
            report = self.run_telemetry(root, document)
        self.assertEqual(["Synthetic"], list(report["books"]))
        self.assertEqual((1, 1), (report["n"], report["errors"]))
        self.assertEqual("ALICE in legacy context", report["books"]["Synthetic"]["error_rows"][0]["previous_context"])

    def test_telemetry_refuses_ambiguous_legacy_context_before_replacing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_fixture(root, "attribution_gold_pdnc_short.json", fixture())
            write_fixture(root, "attribution_gold_pdnc_long.json", fixture("long context"))
            document, _, _, _, _, error = self.run_evaluation(root, [path])
            self.assertIsNone(error)
            for arm in ("base", "lora"):
                document["Synthetic"][arm].pop("fixture")
            output = root / "telemetry.json"
            output.write_bytes(b"prior report")
            with self.assertRaisesRegex(ValueError, "ambiguous legacy"):
                self.run_telemetry(root, document)
            self.assertEqual(b"prior report", output.read_bytes())

    def test_telemetry_refuses_missing_changed_or_misbound_provenanced_sources(self):
        for change in ("missing", "bytes", "input", "sources", "paired rows"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = write_fixture(root, "single.json", fixture())
                document, _, _, _, _, error = self.run_evaluation(root, [path])
                self.assertIsNone(error)
                if change == "missing":
                    path.unlink()
                elif change == "bytes":
                    path.write_text(json.dumps(fixture(), indent=4))
                elif change == "input":
                    changed = fixture("changed context")
                    path.write_text(json.dumps(changed))
                    for arm in ("base", "lora"):
                        document["Synthetic"][arm]["fixture"]["sources"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                elif change == "sources":
                    for arm in ("base", "lora"):
                        document["Synthetic"][arm]["fixture"]["sources"] = []
                else:
                    document["Synthetic"]["lora"]["rows"][0]["id"] = "another-id"
                output = root / "telemetry.json"
                output.write_bytes(b"prior report")
                with self.assertRaisesRegex(ValueError, "fixture"):
                    self.run_telemetry(root, document)
                self.assertEqual(b"prior report", output.read_bytes())

    def test_telemetry_can_resolve_another_retained_duplicate_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_fixture(root, "first.json", fixture("ALICE in context"))
            second = write_fixture(root, "copy.json", fixture("ALICE in context"), indent=4)
            document, calls, _, _, _, error = self.run_evaluation(root, [first, second])
            self.assertIsNone(error)
            first.unlink()
            report = self.run_telemetry(root, document)
        self.assertEqual(2, len(calls))
        self.assertEqual((1, 1), (report["n"], report["errors"]))
        self.assertEqual("ALICE in context", report["books"]["Synthetic"]["error_rows"][0]["previous_context"])
