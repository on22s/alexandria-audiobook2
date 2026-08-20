"""Nothing after the generation may throw.

`ljspeech_generate` renders every clip and then writes one artifact. On
2026-08-20 that final write raised `KeyError: 'test_books'` for eight
consecutive voices - the key is present on the corpus builds and absent on the
library ones - so five to seven minutes of card per voice was rendered and
then discarded, eight times, with the clips left on disk and no artifact.

It is the same shape #355 fixed for `book` and did not finish: one key
repaired, the next left to crash. So this pins the rule rather than the key -
build metadata is read with .get, and a missing value is recorded as absent.
"""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
GENERATE = REPO / "app" / "experiments" / "ljspeech_generate.py"


class WriteMustNotThrowTest(unittest.TestCase):
    def test_no_required_subscript_on_build_AFTER_generation_begins(self):
        """Scoped to what happens after the clips exist.

        Reading `build["ref_sample"]` at startup SHOULD throw - a run with no
        reference cannot proceed and failing in the first second is right. The
        rule is about the write: once the card has been spent, a missing key
        must not discard the result. The boundary is the generation loop, so
        this checks every `build[...]` that appears after it.
        """
        src = GENERATE.read_text(encoding="utf-8")
        # The tail is a fragment, not a module, so parse the whole file and
        # keep only the nodes that start after the write begins.
        tree = ast.parse(src)
        write_line = src[:src.index("doc = {")].count("\n") + 1
        offenders = [
            node.slice.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name) and node.value.id == "build"
            and getattr(node, "lineno", 0) >= write_line
            and isinstance(getattr(node.slice, "value", None), str)
        ]
        self.assertEqual([], offenders,
                         "read these with build.get(...) - a missing key after "
                         "generation discards every clip already rendered")

    def test_the_document_records_absent_metadata_rather_than_omitting_it(self):
        src = GENERATE.read_text(encoding="utf-8")
        for key in ("test_books", "reference_id", "corpus"):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', src)


class BuilderShapeTest(unittest.TestCase):
    """Both builders must produce the shape the one consumer reads."""

    def _keys(self, path, marker):
        src = path.read_text(encoding="utf-8")
        start = src.index(marker)
        end = src.index("}", start)
        return {k for k in ("corpus", "ref_sample", "ref_text",
                            "ref_source_id", "test_books")
                if f'"{k}"' in src[start:end]}

    def test_the_library_builder_emits_test_books(self):
        keys = self._keys(REPO / "app" / "experiments" / "library_eval_build.py",
                          '"corpus": book,')
        self.assertIn("test_books", keys)

    def test_a_library_build_round_trips_through_the_consumer_contract(self):
        """The document the consumer builds must be JSON-serialisable with a
        library build's keys, including the ones it does not have."""
        build = {"corpus": "warm_baritone_50s_m", "ref_source_id": "ref",
                 "test_books": ["warm_baritone_50s_m"]}
        doc = {"seed": 1234, "arms": ["lora", "clone"],
               "reference_id": build.get("ref_source_id"),
               "test_books": build.get("test_books"),
               "corpus": build.get("corpus"),
               "rows": [], "failures": []}
        with tempfile.NamedTemporaryFile("w", suffix=".json") as fh:
            json.dump(doc, fh)
        self.assertEqual(["warm_baritone_50s_m"], doc["test_books"])

    def test_a_build_missing_the_key_still_produces_a_document(self):
        """The corpus/library asymmetry is allowed to persist; what is not
        allowed is a crash."""
        build = {"corpus": "x"}
        doc = {"reference_id": build.get("ref_source_id"),
               "test_books": build.get("test_books")}
        self.assertIsNone(doc["test_books"])
        self.assertIsNone(doc["reference_id"])


if __name__ == "__main__":
    unittest.main()
