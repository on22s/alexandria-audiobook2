"""The clip view must actually lay out what it claims to show.

An earlier draft called `asr_clip_view.audio_tag`, which does not exist. Both
renders raised AttributeError - AFTER the first had been reported as working,
because that artifact happened to have no text to lay out and failed later.
"it renders" is a claim about output, so these assert on the output.
"""
import importlib.util
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments", "tts_clip_view.py")


def _mod():
    spec = importlib.util.spec_from_file_location("tcv", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class RowsOf(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_the_asked_text_comes_from_source(self):
        doc = {"rows": [{"wav": "a.wav", "words": 10, "errors": 3,
                         "source": "the asked text", "transcript": "the heard text"}]}
        r = self.m.rows_of(doc)[0]
        self.assertEqual("the asked text", r["reference"])
        self.assertEqual("the heard text", r["hypothesis"])

    def test_detail_is_never_used_as_the_reference_string(self):
        """`detail` is a LIST of error pairs. An earlier draft fell back to it
        as the reference and html.escape raised on a list."""
        doc = {"rows": [{"wav": "a.wav", "words": 10, "errors": 3,
                         "detail": [{"kind": "replace", "expected": "x", "heard": "y"}],
                         "transcript": "heard"}]}
        r = self.m.rows_of(doc)[0]
        self.assertIsInstance(r["reference"], str)
        self.assertEqual([{"kind": "replace", "expected": "x", "heard": "y"}],
                         r["detail"])

    def test_rows_without_audio_or_words_are_dropped(self):
        doc = {"rows": [{"words": 10, "errors": 1},          # no wav
                        {"wav": "a.wav", "words": 0},         # nothing scored
                        {"wav": "b.wav", "words": 4, "errors": 1}]}
        self.assertEqual(1, len(self.m.rows_of(doc)))

    def test_wer_is_errors_over_words(self):
        doc = {"rows": [{"wav": "a.wav", "words": 8, "errors": 2}]}
        self.assertAlmostEqual(0.25, self.m.rows_of(doc)[0]["wer"])


class Rendering(unittest.TestCase):
    def test_the_page_contains_the_text_and_the_error_pairs(self):
        m = _mod()
        import json
        import subprocess
        import sys
        doc = {"rows": [{"wav": "missing.wav", "words": 10, "errors": 5,
                         "source": "ALPHA BETA", "transcript": "ALPHA GAMMA",
                         "detail": [{"kind": "replace", "expected": "BETA",
                                     "heard": "GAMMA"}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            art = os.path.join(tmp, "a.json")
            out = os.path.join(tmp, "v.html")
            json.dump(doc, open(art, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, SCRIPT, "--artifact", art,
                                "--out", out], capture_output=True, text=True,
                               timeout=120)
            self.assertEqual(0, r.returncode, r.stderr[-400:])
            page = open(out, encoding="utf-8").read()
        self.assertIn("ALPHA BETA", page)     # asked
        self.assertIn("ALPHA GAMMA", page)    # heard
        self.assertIn("GAMMA</b>", page)      # the error pair, rendered
        self.assertIn("audio not found", page)


if __name__ == "__main__":
    unittest.main()
