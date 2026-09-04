"""The manifest that pairs generated audio with the text it was asked to say.

If this pairing is off by one, fluent audio scores as gibberish and the word
error rate becomes a finding rather than a bug. These pin the two decisions
that could shift it.
"""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments",
                      "build_tts_validation_manifest.py")


def _mod():
    spec = importlib.util.spec_from_file_location("mkmanifest", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class JapaneseIsExcludedNotFailed(unittest.TestCase):
    """The local whisper model is small.en. A Japanese line scored by an
    English-only model returns a low number meaning "wrong model", not "bad
    audio" - a fallback that returns a plausible value is the dangerous kind."""

    def setUp(self):
        self.m = _mod()

    def test_kana_is_detected(self):
        for text in ("これは日本語です", "カタカナ", "漢字"):
            self.assertTrue(self.m.CJK.search(text), text)

    def test_plain_english_is_not_flagged(self):
        for text in ("She said nothing at all.",
                     "Onii-chan, stop that!",
                     "The samurai bowed."):
            self.assertIsNone(self.m.CJK.search(text), text)

    def test_romaji_japanese_is_kept_because_english_asr_can_read_it(self):
        """`tsundere` and `pachinko` are Latin script; small.en can attempt
        them. Excluding them would drop exactly the vocabulary goal 5.5 is
        about."""
        self.assertIsNone(self.m.CJK.search("He called her a tsundere."))


if __name__ == "__main__":
    unittest.main()
