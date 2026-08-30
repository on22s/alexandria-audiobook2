"""A multi-book run must record every gold it read, not just the first.

The nine cloud evaluations of 2026-08-28/29 scored 383 rows across three books
- owarimonogatari3 (162), mushoku16 (133), index18 (88) - and stamped
`gold_path: attribution_gold_index18.json` with a correct hash. The hash was
right and verified 88 of 383 rows; a change to the other two books' gold would
have left no trace. ExperimentRecord could not have done better, because it
took a single path.

The singular fields keep their old meaning deliberately: narration_signal.py
and length_bins.py both select artifacts with
`os.path.basename(meta["gold_path"]) != goldfile`, so changing that field to a
list would have made both quietly match nothing.
"""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.manifest import ExperimentRecord  # noqa: E402

ENV = {"loaded": True, "context_length": 32768, "parallel": 1}


def write_gold(directory, name, n):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"entries": [{"id": f"{name}-{i}"} for i in range(n)]}, handle)
    return path


def record(gold, repo):
    return ExperimentRecord("t", repo, "m", "http://x/v1", gold,
                            {"temperature": 0.0}, environment=ENV)


class RecordStampsEveryGoldTest(unittest.TestCase):

    def test_a_single_path_still_produces_the_singular_fields(self):
        with tempfile.TemporaryDirectory() as d:
            g = write_gold(d, "attribution_gold_index18.json", 99)
            meta = record(g, d).meta
        self.assertEqual("attribution_gold_index18.json",
                         os.path.basename(meta["gold_path"]))
        self.assertEqual(99, meta["gold_lines"])
        self.assertEqual(1, len(meta["gold_files"]))

    def test_every_gold_of_a_multi_book_run_is_hashed(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [write_gold(d, "attribution_gold_index18.json", 99),
                     write_gold(d, "attribution_gold_mushoku16.json", 136),
                     write_gold(d, "attribution_gold_owarimonogatari3.json", 162)]
            meta = record(paths, d).meta
        self.assertEqual(3, len(meta["gold_files"]))
        self.assertEqual([99, 136, 162], [g["gold_lines"] for g in meta["gold_files"]])
        self.assertEqual(3, len({g["gold_sha256"] for g in meta["gold_files"]}),
                         "each gold must hash distinctly")
        self.assertEqual(397, sum(g["gold_lines"] for g in meta["gold_files"]))

    def test_the_consumers_selector_still_matches(self):
        """narration_signal and length_bins both key off this exact expression."""
        with tempfile.TemporaryDirectory() as d:
            paths = [write_gold(d, "attribution_gold_index18.json", 99),
                     write_gold(d, "attribution_gold_mushoku16.json", 136)]
            meta = record(paths, d).meta
        self.assertEqual("attribution_gold_index18.json",
                         os.path.basename(str(meta.get("gold_path", ""))))

    def test_a_changed_second_gold_changes_the_record(self):
        """The property the old shape lacked: the failure it could not see."""
        with tempfile.TemporaryDirectory() as d:
            first = write_gold(d, "attribution_gold_index18.json", 99)
            second = write_gold(d, "attribution_gold_mushoku16.json", 136)
            before = record([first, second], d).meta["gold_files"]
            write_gold(d, "attribution_gold_mushoku16.json", 135)   # gold edited
            after = record([first, second], d).meta["gold_files"]
        self.assertEqual(before[0], after[0], "the untouched gold must not move")
        self.assertNotEqual(before[1]["gold_sha256"], after[1]["gold_sha256"],
                            "editing the second gold must change the record")

    def test_declaring_no_gold_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                record([], d)


if __name__ == "__main__":
    unittest.main()
