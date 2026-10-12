"""verbalize_symbols: one cached classification per character (#1082).

WHAT THIS VERIFIES. The drop count is every occurrence, not every distinct
glyph (the goal 5.1 evidence; earlier tests only ever dropped one glyph once);
known-answer renderings for spoken, dropped and kept characters, alone and
repeated, match a straightforward per-character reference; non-str input still
returns plain text; and classification happens once per distinct character per
process. All text is synthetic.
"""
import unittest
from unittest.mock import patch

import speech_text
from speech_policy import get_spoken_symbol, is_unspeakable_character
from speech_text import verbalize_symbols


def reference(text):
    """The pre-#1082 loop, kept as the oracle."""
    out, spoken, dropped = [], [], []
    for ch in text:
        word = get_spoken_symbol(ch)
        if word:
            out.append(f" {word} ")
            spoken.append(ch)
        elif is_unspeakable_character(ch):
            out.append(" ")
            dropped.append(ch)
        else:
            out.append(ch)
    changes = []
    if spoken:
        changes.append({"type": "verbalized_symbol", "symbols": sorted(set(spoken))})
    if dropped:
        changes.append({"type": "dropped_unspeakable", "symbols": sorted(set(dropped)),
                        "count": len(dropped)})
    return "".join(out), changes


class DropCountTest(unittest.TestCase):
    def test_count_is_every_occurrence(self):
        _, changes = verbalize_symbols("A\x01\x01\x01B ⍟ C ⍟")
        drop = next(c for c in changes if c["type"] == "dropped_unspeakable")
        self.assertEqual(["\x01", "⍟"], drop["symbols"])
        self.assertEqual(5, drop["count"])

    def test_many_distinct_dropped_glyphs_are_all_counted(self):
        glyphs = "".join(chr(0xE000 + i) for i in range(2000))  # private use: dropped
        text, changes = verbalize_symbols("a" + glyphs + glyphs + "b")
        drop = next(c for c in changes if c["type"] == "dropped_unspeakable")
        self.assertEqual((2000, 4000), (len(drop["symbols"]), drop["count"]))
        self.assertEqual(reference("a" + glyphs + glyphs + "b"), (text, changes))

    def test_spoken_symbols_are_listed_once_however_often_they_repeat(self):
        text, changes = verbalize_symbols("© me © you ©")
        self.assertEqual([{"type": "verbalized_symbol", "symbols": ["©"]}], changes)
        self.assertEqual(3, text.count("copyright"))


class KnownAnswerTest(unittest.TestCase):
    CASES = ["", "plain prose, no symbols.", "©", "★★★", "A\x01B", "�", "$5 & 10%",
             "漢字かな", "", "† kept", "Tab\tand\nnewline", "© ★ © ★ \x01 é"]

    def test_matches_the_per_character_reference(self):
        for text in self.CASES:
            with self.subTest(text=text):
                self.assertEqual(reference(text), verbalize_symbols(text))

    def test_non_str_input_returns_plain_text(self):
        class Marked(str):
            pass
        for value in (list("A ★ ©"), Marked("A ★ ©")):
            with self.subTest(kind=type(value).__name__):
                text, changes = verbalize_symbols(value)
                self.assertIs(str, type(text))
                self.assertEqual(reference("A ★ ©"), (text, changes))


class ClassificationCacheTest(unittest.TestCase):
    def test_each_distinct_character_is_classified_once(self):
        speech_text._get_symbol_rendering.cache_clear()
        self.addCleanup(speech_text._get_symbol_rendering.cache_clear)
        with patch.object(speech_text, "get_spoken_symbol", wraps=get_spoken_symbol) as lookup:
            for _ in range(3):
                verbalize_symbols("abc ★ abc ©")
        self.assertEqual(len(set("abc ★ abc ©")), lookup.call_count)


if __name__ == "__main__":
    unittest.main()
