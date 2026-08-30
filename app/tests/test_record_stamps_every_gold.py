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
        self.assertEqual({"index18"}, set(meta["gold_files"]))

    def test_every_gold_of_a_multi_book_run_is_hashed(self):
        with tempfile.TemporaryDirectory() as d:
            paths = [write_gold(d, "attribution_gold_index18.json", 99),
                     write_gold(d, "attribution_gold_mushoku16.json", 136),
                     write_gold(d, "attribution_gold_owarimonogatari3.json", 162)]
            meta = record(paths, d).meta
        self.assertEqual({"index18", "mushoku16", "owarimonogatari3"},
                         set(meta["gold_files"]),
                         "keyed by book, matching the 34 artifacts that already "
                         "carry this field")
        self.assertEqual(3, len(set(meta["gold_files"].values())),
                         "each gold must hash distinctly")

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
        self.assertEqual(before["index18"], after["index18"],
                         "the untouched gold must not move")
        self.assertNotEqual(before["mushoku16"], after["mushoku16"],
                            "editing the second gold must change the record")

    def test_only_the_record_defines_gold_files(self):
        """One answer to one question (Rule 15).

        Two changes landed on the same day both defining meta["gold_files"]:
        this class as a list of {gold_path, gold_sha256, gold_lines}, and
        distill_eval as a dict of book -> sha256, assigned AFTER the
        constructor so it silently overwrote the first and changed the type.
        Neither side's tests could see the other. A reader of the field would
        have got whichever shape ran last.
        """
        experiments = os.path.join(REPO, "app", "experiments")
        offenders = []
        for name in sorted(os.listdir(experiments)):
            if not name.endswith(".py") or name == "manifest.py":
                continue
            with open(os.path.join(experiments, name), encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if 'meta["gold_files"]' in stripped and "=" in stripped:
                        offenders.append(f"{name}:{n}: {stripped[:70]}")
        self.assertEqual(
            [], offenders,
            "gold_files is built by ExperimentRecord and nowhere else; these "
            "assignments overwrite it, possibly with another shape:\n  "
            + "\n  ".join(offenders))

    def test_declaring_no_gold_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                record([], d)


if __name__ == "__main__":
    unittest.main()
