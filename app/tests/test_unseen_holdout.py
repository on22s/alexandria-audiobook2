"""The holdout builder must never hand back a clip the adapter trained on.

Every fixture here is a miniature of the real layout: a "trained" dataset zip
whose clips carry audiobook offsets, and source volumes that share those
offsets under different sample numbers - which is exactly how the real data
behaves, since merging a dataset renumbers its samples.
"""
import json
import os
import sys
import pickle
import tempfile
import unittest
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))

from experiments.build_unseen_holdout import (  # noqa: E402
    build, key, read_metadata, voice_similarity, voices_in_book)

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


def _embeddings(path, stems, unlike=()):
    """Write a dedup-style cache: every stem the same voice, except `unlike`.

    The cache value shape is (embeddings, filenames), matching what the real
    dedup run pickles.
    """
    import numpy as np
    rng = np.random.default_rng(11)
    cache = {}
    for stem in stems:
        base = np.zeros(16)
        base[1 if stem in unlike else 0] = 1.0
        cache[f"book/{stem}"] = (base + rng.normal(0, 0.01, size=(20, 16)),
                                 [f"{i}.wav" for i in range(20)])
    with open(path, "wb") as fh:
        pickle.dump(cache, fh)
    return path


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
        self.emb = _embeddings(os.path.join(self.tmp, "emb.pkl"),
                               ["book_vol01", "book_vol02", "book_vol03"])

    def test_no_trained_clip_is_ever_returned(self):
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1,
                    embeddings=self.emb)
        trained = {key(r) for r, _ in read_metadata(self.trained)}
        got = {(round(c["start"], 2), round(c["end"], 2)) for c in doc["clips"]}
        self.assertEqual(trained & got, set(),
                         "a clip the adapter trained on reached the holdout")

    def test_the_whole_contributing_volume_is_dropped(self):
        """Not just the matched clips - a neighbouring clip of a seen passage
        is too close to call unseen."""
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1,
                    embeddings=self.emb)
        self.assertEqual([v for v, _ in doc["volumes_excluded_as_training_data"]],
                         ["book_vol02.zip"])
        self.assertNotIn("book_vol02.zip",
                         {c["source_volume"] for c in doc["clips"]})

    def test_clips_are_deduplicated_across_repeated_listings(self):
        """A zip lists each clip in both metadata files; drawing one twice
        would report two measurements where there is one."""
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=50, seed=1,
                    embeddings=self.emb)
        got = [(c["start"], c["end"]) for c in doc["clips"]]
        self.assertEqual(len(got), len(set(got)))
        self.assertEqual(doc["unseen_pool"], 12)

    def test_it_refuses_when_a_trained_clip_cannot_be_traced(self):
        """If a trained clip belongs to no known volume, that volume is absent
        and its other clips would be sitting in the pool labelled unseen."""
        orphan = os.path.join(self.tmp, "orphan.zip")
        _zip(orphan, self.trained_rows + _rows(7000, 5000.0, 2))
        with self.assertRaises(SystemExit) as ctx:
            build(orphan, self.src, os.path.join(self.tmp, "h2"), 4, 1,
                  embeddings=self.emb)
        self.assertIn("refusing", str(ctx.exception))

    def test_written_audio_matches_the_manifest(self):
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1,
                    embeddings=self.emb)
        with open(os.path.join(out, "val", "metadata.jsonl"),
                  encoding="utf-8") as fh:
            entries = [json.loads(x) for x in fh if x.strip()]
        self.assertEqual(len(entries), doc["written"])
        for e in entries:
            self.assertTrue(os.path.exists(os.path.join(out, e["audio_filepath"])))


class SameVoiceGuard(unittest.TestCase):
    """A candidate volume must be shown to be the SAME VOICE, not merely from
    the same book.

    The guard this replaced counted how many datasets dedup produced and
    refused above one. That tracks contiguous per-actor blocks rather than
    people: Waking Gods is a cast production whose actors alternate, so every
    volume held all of them, the volumes resembled each other, and it passed as
    a single voice - while 16 of its 17 volumes sat at 0.326-0.859 similarity
    to the trained one.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "book")
        os.makedirs(self.src)
        self.trained = os.path.join(self.tmp, "trained.zip")
        _zip(self.trained, _rows(9000, 200.0, 6))
        _zip(os.path.join(self.src, "book_vol01.zip"), _rows(0, 0.0, 6))
        _zip(os.path.join(self.src, "book_vol02.zip"), _rows(500, 200.0, 6))
        _zip(os.path.join(self.src, "book_vol03.zip"), _rows(800, 900.0, 6))
        self.stems = ["book_vol01", "book_vol02", "book_vol03"]

    def _build(self, unlike=(), **kw):
        emb = _embeddings(os.path.join(self.tmp, "e.pkl"), self.stems, unlike)
        return build(self.trained, self.src, os.path.join(self.tmp, "h"),
                     12, 1, embeddings=emb, **kw)

    def test_a_different_voice_is_dropped_from_the_pool(self):
        doc = self._build(unlike=["book_vol03"])
        self.assertEqual(doc["volumes_dropped_wrong_voice"],
                         ["book_vol03.zip"])
        self.assertNotIn("book_vol03.zip",
                         {c["source_volume"] for c in doc["clips"]})

    def test_the_same_voice_is_kept(self):
        doc = self._build()
        self.assertEqual(doc["volumes_dropped_wrong_voice"], [])
        self.assertIn("book_vol03.zip",
                      {c["source_volume"] for c in doc["clips"]})

    def test_every_similarity_is_recorded_so_the_threshold_can_be_rejudged(self):
        doc = self._build(unlike=["book_vol03"])
        sims = doc["volume_similarity_to_trained"]
        self.assertIn("book_vol03.zip", sims)
        self.assertLess(sims["book_vol03.zip"], 0.85)
        self.assertGreater(sims["book_vol01.zip"], 0.85)

    def test_a_volume_that_was_never_embedded_is_dropped_not_assumed(self):
        """Unknown is neither same nor different. Treating it as same would
        put an unverified voice into the evidence."""
        emb = _embeddings(os.path.join(self.tmp, "part.pkl"),
                          ["book_vol02", "book_vol03"])
        doc = build(self.trained, self.src, os.path.join(self.tmp, "h4"),
                    12, 1, embeddings=emb)
        self.assertEqual(doc["volumes_dropped_unjudged"], ["book_vol01.zip"])
        self.assertNotIn("book_vol01.zip",
                         {c["source_volume"] for c in doc["clips"]})

    def test_it_refuses_with_no_embeddings_at_all(self):
        with self.assertRaises(SystemExit) as ctx:
            build(self.trained, self.src, os.path.join(self.tmp, "h5"), 4, 1,
                  embeddings=None)
        self.assertIn("refusing", str(ctx.exception))

    def test_it_refuses_when_the_trained_volume_was_never_embedded(self):
        emb = _embeddings(os.path.join(self.tmp, "no_trained.pkl"),
                          ["book_vol01", "book_vol03"])
        with self.assertRaises(SystemExit) as ctx:
            build(self.trained, self.src, os.path.join(self.tmp, "h6"), 4, 1,
                  embeddings=emb)
        self.assertIn("never embedded", str(ctx.exception))

    def test_the_threshold_is_honoured(self):
        doc = self._build(unlike=["book_vol03"], min_voice_similarity=-1.0)
        self.assertEqual(doc["volumes_dropped_wrong_voice"], [],
                         "a permissive threshold must actually permit")

    def test_unknown_similarity_is_none_rather_than_zero(self):
        self.assertIsNone(voice_similarity({}, "a", "b"))


if __name__ == "__main__":
    unittest.main()
