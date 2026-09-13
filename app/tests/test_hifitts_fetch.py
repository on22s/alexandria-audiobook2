"""hifitts_fetch writes a corpus that ljspeech_prepare reads as-is, and
refuses the shapes that would silently corrupt the split-by-book design."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))

from experiments import hifitts_fetch
from experiments import ljspeech_prepare


def _silence(_data):
    return np.zeros(44100, dtype="float32"), 44100


def _row(file, text, speaker="9017"):
    return {"speaker": speaker, "file": file, "text": text,
            "text_normalized": text, "audio": b"fake"}


LINE = "It was a bright cold day in April, and the clocks were striking thirteen."


class ParseFileTest(unittest.TestCase):
    def test_real_paths_yield_speaker_book_and_a_split_safe_id(self):
        for path, speaker, book, clip_id in [
            ("audio/9017_clean/14261/dartagnan03part3_62_dumas_0281.flac",
             "9017", "dartagnan03part3", "dartagnan03part3-62_0281"),
            ("audio/6097_clean/14411/nada_lily_00_haggard_0003.flac",
             "6097", "nada_lily", "nada_lily-00_0003"),
            ("audio/92_clean/7967/wonderfuladventures_04_seacole_0260.flac",
             "92", "wonderfuladventures", "wonderfuladventures-04_0260"),
        ]:
            got = hifitts_fetch.parse_file(path)
            self.assertEqual((speaker, clip_id, book), got, path)
            # The contract downstream relies on: book is the text before '-'.
            self.assertEqual(book, clip_id.split("-")[0])

    def test_a_slug_with_a_dash_is_refused(self):
        with self.assertRaises(ValueError):
            hifitts_fetch.parse_file(
                "audio/9017_clean/1/pride-prejudice_01_austen_0001.flac")

    def test_an_unrecognised_name_is_refused_not_guessed(self):
        with self.assertRaises(ValueError):
            hifitts_fetch.parse_file("audio/9017_clean/1/chapter1.flac")


class WriteRowsTest(unittest.TestCase):
    def test_only_usable_rows_are_written_and_prepare_reads_them_back(self):
        rows = [
            _row("audio/9017_clean/1/dartagnan03part3_62_dumas_0281.flac", LINE),
            _row("audio/9017_clean/2/dartagnan03part3_63_dumas_0002.flac",
                 "Too short."),                                   # below band
            _row("audio/9017_clean/3/twentyyears_01_dumas_0009.flac", LINE + " " + LINE + " " + LINE),  # above band
            _row("audio/9017_clean/3/twentyyears_01_dumas_0010.flac",
                 'She said, "no|never" | twice, and the whole room went quiet at that.'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            kept, per_book, rates = hifitts_fetch.write_rows(
                rows, tmp, 60, 220, decode=_silence)
            self.assertEqual({"dartagnan03part3": 1, "twentyyears": 1},
                             dict(per_book))
            self.assertEqual({44100}, rates)
            self.assertEqual(sorted(os.listdir(os.path.join(tmp, "wavs"))),
                             ["dartagnan03part3-62_0281.wav",
                              "twentyyears-01_0010.wav"])
            back = ljspeech_prepare.load_metadata(tmp)
            self.assertEqual(["dartagnan03part3", "twentyyears"],
                             [r["book"] for r in back])
            # Pipes cannot survive a pipe-delimited file; quotes must.
            self.assertEqual('She said, "no/never" / twice, and the whole room went quiet at that.',
                             back[1]["normalized"])


class PrepareIdentityTest(unittest.TestCase):
    def test_corpus_json_overrides_the_ljspeech_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ljspeech_prepare.LJSPEECH_IDENTITY,
                             ljspeech_prepare.corpus_identity(tmp))
            Path(tmp, "corpus.json").write_text(json.dumps({
                "corpus": "Hi-Fi TTS reader 9017", "licence": "CC BY 4.0",
                "sample_rate_native": 44100, "rows_kept": 3}), encoding="utf-8")
            got = ljspeech_prepare.corpus_identity(tmp)
            self.assertEqual({"corpus": "Hi-Fi TTS reader 9017",
                              "licence": "CC BY 4.0",
                              "sample_rate_native": 44100}, got)


if __name__ == "__main__":
    unittest.main()


class PerBookCapTest(unittest.TestCase):
    def test_cap_counts_across_calls_and_stops_writing_a_full_book(self):
        import collections
        a = [_row(f"audio/9017_clean/1/dartagnan01_01_dumas_{i:04d}.flac", LINE)
             for i in range(3)]
        b = [_row(f"audio/9017_clean/1/dartagnan01_02_dumas_{i:04d}.flac", LINE)
             for i in range(3)] + [_row("audio/9017_clean/2/zarathustra_01_nietzsche_0001.flac", LINE)]
        counts = collections.Counter()
        with tempfile.TemporaryDirectory() as tmp:
            k1, _, _ = hifitts_fetch.write_rows(a, tmp, 60, 220, decode=_silence,
                                                max_per_book=4, per_book=counts)
            k2, _, _ = hifitts_fetch.write_rows(b, tmp, 60, 220, decode=_silence,
                                                max_per_book=4, per_book=counts)
            self.assertEqual(3, len(k1))
            self.assertEqual(2, len(k2), "one more dartagnan01 to reach 4, plus zarathustra")
            self.assertEqual({"dartagnan01": 4, "zarathustra": 1}, dict(counts))
            self.assertEqual(5, len(os.listdir(os.path.join(tmp, "wavs"))))
