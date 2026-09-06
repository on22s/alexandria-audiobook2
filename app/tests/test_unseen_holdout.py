"""The holdout builder must never hand back a clip the adapter trained on.

Most tests here pass `verify_clips=False`. That is deliberate and not a
weakening: they exercise the VOLUME-level logic, which needs no audio model,
and the clip-level check is covered by `ClipLevelVoiceGuard` below, which stubs
the embedding call. A test that silently ran without the check because no
interpreter was present would be the fallback this module exists to prevent -
so the flag is explicit at every call site.

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
                    embeddings=self.emb,
                    verify_clips=False)
        trained = {key(r) for r, _ in read_metadata(self.trained)}
        got = {(round(c["start"], 2), round(c["end"], 2)) for c in doc["clips"]}
        self.assertEqual(trained & got, set(),
                         "a clip the adapter trained on reached the holdout")

    def test_the_whole_contributing_volume_is_dropped(self):
        """Not just the matched clips - a neighbouring clip of a seen passage
        is too close to call unseen."""
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1,
                    embeddings=self.emb,
                    verify_clips=False)
        self.assertEqual([v for v, _ in doc["volumes_excluded_as_training_data"]],
                         ["book_vol02.zip"])
        self.assertNotIn("book_vol02.zip",
                         {c["source_volume"] for c in doc["clips"]})

    def test_clips_are_deduplicated_across_repeated_listings(self):
        """A zip lists each clip in both metadata files; drawing one twice
        would report two measurements where there is one."""
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=50, seed=1,
                    embeddings=self.emb,
                    verify_clips=False)
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
                  embeddings=self.emb,
                    verify_clips=False)
        self.assertIn("refusing", str(ctx.exception))

    def test_written_audio_matches_the_manifest(self):
        out = os.path.join(self.tmp, "holdout")
        doc = build(self.trained, self.src, out, lines=12, seed=1,
                    embeddings=self.emb,
                    verify_clips=False)
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
                     12, 1, embeddings=emb,
                     verify_clips=kw.pop("verify_clips", False), **kw)

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
                    12, 1, embeddings=emb, verify_clips=False)
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


class ClipLevelVoiceGuard(unittest.TestCase):
    """The volume guard keeps a volume; a volume holds every character in it.

    THE CASE THAT GOT THROUGH. `crisp_mezzo_30s_f` is char2 of a two-voice
    book. Every one of its candidate volumes passed the volume-level check,
    and 7 of its 20 held-out clips were still the other character - which is
    how one adapter read 0.132 on that holdout and 0.552 on another built from
    the same book. Measured over 68 holdouts and 1,360 clips: 10.7% of clips
    are a different person and 28 holdouts carry at least one.

    The embedding call is stubbed. These tests are about what the builder DOES
    with a verdict, which is the part that was wrong; whether ECAPA can tell
    two people apart is not in question and needs no GPU here.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "book")
        os.makedirs(self.src, exist_ok=True)
        # vol01 is the slice that became the training set; vol02 is unseen.
        _zip(os.path.join(self.src, "book_vol01.zip"), _rows(0, 0.0, 8))
        _zip(os.path.join(self.src, "book_vol02.zip"), _rows(500, 900.0, 12))
        self.trained = os.path.join(self.tmp, "trained.zip")
        _zip(self.trained, _rows(9000, 0.0, 8))
        self.emb = _embeddings(os.path.join(self.tmp, "e.pkl"),
                               ["book_vol01", "book_vol02"])

    def tearDown(self):
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def _build(self, scores, **kw):
        """Run a build whose clip verifier returns `scores` in order."""
        import experiments.build_unseen_holdout as m
        seq = list(scores)

        def fake(pairs, python_bin):
            # one score per candidate, repeated once per anchor
            per = max(1, len(pairs) // max(1, len(seq)))
            return [seq[i // per] for i in range(len(pairs))], None

        real = m._ecapa
        m._ecapa = fake
        try:
            return m.build(self.trained, self.src,
                           os.path.join(self.tmp, "h"), kw.pop("lines", 4), 1,
                           embeddings=self.emb, sibling_python=sys.executable,
                           clip_anchors=1, **kw)
        finally:
            m._ecapa = real

    def test_a_clip_of_another_character_is_dropped(self):
        doc = self._build([0.9] * 6 + [0.02] * 6, lines=4)
        self.assertEqual(len(doc["clips"]), 4)
        for c in doc["clips"]:
            self.assertGreaterEqual(c["voice_similarity"], 0.30)
        self.assertGreater(doc["clips_rejected_wrong_voice"], 0,
                           "the foreign clips were not counted as rejected")

    def test_the_threshold_is_the_trough_not_the_gate(self):
        """0.45 sat on the rising edge of the REAL mode and cost 62 good clips."""
        doc = self._build([0.35] * 12, lines=4)
        self.assertEqual(len(doc["clips"]), 4,
                         "clips at 0.35 are the trained voice and must be kept")
        with self.assertRaises(SystemExit) as ctx:
            self._build([0.35] * 12, lines=4, min_clip_voice=0.45)
        self.assertIn("min-clip-voice", str(ctx.exception),
                      "the refusal must name the clip filter, not the volume "
                      "guard - they need different fixes")

    def test_it_refuses_rather_than_skipping_verification(self):
        """No silent pass-through: the dangerous fallback returns a plausible value."""
        with self.assertRaises(SystemExit) as ctx:
            build(self.trained, self.src, os.path.join(self.tmp, "h9"), 4, 1,
                  embeddings=self.emb, sibling_python=None)
        self.assertIn("Refusing", str(ctx.exception))

    def test_an_unverified_holdout_says_so_in_the_artifact(self):
        doc = build(self.trained, self.src, os.path.join(self.tmp, "h10"), 4, 1,
                    embeddings=self.emb, verify_clips=False)
        self.assertFalse(doc["clips_verified"])
        self.assertIsNone(doc["min_clip_voice"])
        for c in doc["clips"]:
            self.assertIsNone(c["voice_similarity"],
                              "an unverified clip must not carry a score")

    def test_the_written_order_is_shuffled(self):
        """The gate reads the FIRST --lines entries, so order must be random.

        If clips were written in pool order they would cluster by volume and
        by offset, and the gate would read a systematically skewed sample
        rather than a random one.
        """
        doc = self._build([0.9] * 12, lines=8)
        starts = [float(c["start"]) for c in doc["clips"]]
        self.assertNotEqual(starts, sorted(starts),
                            "clips were written in offset order")

    def test_a_short_holdout_is_refused_not_quietly_written(self):
        """velvety_mezzo_30s_f_gothic wrote SIX clips and nobody was told.

        Its book is about 90% another voice: 54 of 60 candidates were
        rejected, 6 were written, and the identity gate scored a median over 6
        lines while every other adapter used 12. That is a different
        measurement wearing the same name, and it was visible nowhere. Empty
        was already refused; short was not.
        """
        with self.assertRaises(SystemExit) as ctx:
            self._build([0.9] * 3 + [0.01] * 9, lines=8)
        msg = str(ctx.exception)
        self.assertIn("survived the voice check", msg)
        self.assertIn("not comparable", msg,
                      "the refusal must say WHY a short holdout is a problem")

    def test_min_lines_can_be_lowered_deliberately(self):
        """Refusing must be overridable, or a real short holdout is unbuildable."""
        doc = self._build([0.9] * 3 + [0.01] * 9, lines=8, min_lines=2)
        self.assertEqual(len(doc["clips"]), 3)
        self.assertEqual(doc["clips_written_short_by"], 5,
                         "the shortfall must be recorded, not just tolerated")

    def test_a_full_holdout_records_no_shortfall(self):
        doc = self._build([0.9] * 12, lines=4)
        self.assertEqual(doc["clips_written_short_by"], 0)
