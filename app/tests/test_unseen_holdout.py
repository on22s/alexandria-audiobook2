"""The holdout builder must never hand back a clip the adapter trained on.

Every fixture here is a miniature of the real layout: a "trained" dataset zip
whose clips carry audiobook offsets, and source volumes that share those
offsets under different sample numbers - which is exactly how the real data
behaves, since merging a dataset renumbers its samples.
"""
import json
import os
import sys
import tempfile
import unittest
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.build_unseen_holdout import build, key, read_metadata  # noqa: E402

WAV = (b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00"
       b"\x44\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00")


def _zip(path, rows, prefix="train"):
    with zipfile.ZipFile(path, "w") as zf:
        lines = []
        for r in rows:
            member = f"{prefix}/{r['name']}.wav"
            zf.writestr(member, WAV)
            lines.append(json.dumps({"audio_filepath": member,
                                     "text": r.get("text", "line"),
                                     "duration": r["end"] - r["start"],
                                     "start": r["start"], "end": r["end"]}))
        body = "\n".join(lines) + "\n"
        # Real zips repeat the listing at the root as well as in the split.
        zf.writestr(f"{prefix}/metadata.jsonl", body)
        zf.writestr("metadata.jsonl", body)


def _rows(start_id, start_t, n):
    return [{"name": f"sample_{start_id + i:04d}",
             "start": start_t + i * 10.0, "end": start_t + i * 10.0 + 5.0}
            for i in range(n)]


class UnseenHoldout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "book")
        os.makedirs(self.src)
        # vol02 is the slice that became the training set, renumbered.
        self.trained_rows = _rows(9000, 200.0, 6)
        self.trained = os.path.join(self.tmp, "trained.zip")
        _zip(self.trained, self.trained_rows)
        _zip(os.path.join(self.src, "book_vol01.zip"), _rows(0, 0.0, 6))
        _zip(os.path.join(self.src, "book_vol02.zip"), _rows(500, 200.0, 6))
        _zip(os.path.join(self.src, "book_vol03.zip"), _rows(800, 900.0, 6))

    def test_no_trained_clip_is_ever_returned(self):
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1)
        trained = {key(r) for r, _ in read_metadata(self.trained)}
        got = {(round(c["start"], 2), round(c["end"], 2)) for c in doc["clips"]}
        self.assertEqual(trained & got, set(),
                         "a clip the adapter trained on reached the holdout")

    def test_the_whole_contributing_volume_is_dropped(self):
        """Not just the matched clips - a neighbouring clip of a seen passage
        is too close to call unseen."""
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1)
        self.assertEqual([v for v, _ in doc["volumes_excluded_as_training_data"]],
                         ["book_vol02.zip"])
        self.assertNotIn("book_vol02.zip",
                         {c["source_volume"] for c in doc["clips"]})

    def test_clips_are_deduplicated_across_repeated_listings(self):
        """A zip lists each clip in both metadata files; drawing one twice
        would report two measurements where there is one."""
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=50, seed=1)
        got = [(c["start"], c["end"]) for c in doc["clips"]]
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(doc["unseen_pool"], 12)

    def test_it_refuses_when_a_trained_clip_cannot_be_traced(self):
        """If a trained clip belongs to no known volume, that volume is absent
        and its other clips would be sitting in the pool labelled unseen."""
        orphan = os.path.join(self.tmp, "orphan.zip")
        _zip(orphan, self.trained_rows + _rows(7000, 5000.0, 2))
        with self.assertRaises(SystemExit) as ctx:
            build(orphan, self.src, os.path.join(self.tmp, "h2"), 4, 1)
        self.assertIn("refusing", str(ctx.exception))

    def test_written_audio_matches_the_manifest(self):
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1)
        with open(os.path.join(out, "val", "metadata.jsonl"),
                  encoding="utf-8") as fh:
            entries = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len(entries), doc["written"])
        for e in entries:
            self.assertTrue(os.path.exists(os.path.join(out, e["audio_filepath"])))


if __name__ == "__main__":
    unittest.main()
