"""How ruby annotations are narrated: one rule for script extraction and read-aloud.

``<ruby>漢字<rt>かんじ</rt></ruby>`` carries a reading for its base text.
Reading both aloud says the word twice ("漢字かんじ"). A kana reading is how
the author wants the word pronounced, which matters most for names with
unusual readings, so it replaces its base; any other reading (pinyin,
romaji, an English gloss) is dropped and the base is kept. ``<rp>``
fallback parentheses are never spoken.

The EPUB extractor (``routers/script.py``) and the read-aloud mapper
(``epub_readaloud.py``) must agree on this exactly, or every annotated
paragraph fails to synchronize, so both call :func:`get_ruby_spoken_text`.
"""
import unicodedata

RUBY_TAG = "ruby"
RUBY_BASE_TAGS = frozenset({"rb", "rtc"})
RUBY_READING_TAG = "rt"
RUBY_FALLBACK_TAG = "rp"


def _is_kana_reading(text):
    letters = [ch for ch in text if not ch.isspace()]
    return bool(letters) and all(
        unicodedata.name(ch, "").startswith(("HIRAGANA", "KATAKANA")) for ch in letters)


def get_ruby_spoken_text(pairs):
    """Spoken text of one ruby element from its (base, reading) pairs.

    ``reading`` is None for base text with no reading after it.
    """
    spoken = []
    for base, reading in pairs:
        if reading is not None and _is_kana_reading(reading):
            spoken.append(reading.strip())
        else:
            spoken.append(base)
    return "".join(spoken)


class RubyCollector:
    """Accumulates one ruby element's text as (base, reading) pairs.

    Callers feed text with the tag it sits inside; a reading closes the
    base text gathered since the previous reading.
    """

    def __init__(self):
        self.pairs = []
        self._base = []
        self._reading = None

    def start_reading(self):
        self._reading = []

    def end_reading(self):
        if self._reading is not None:
            self.pairs.append(("".join(self._base), "".join(self._reading)))
            self._base, self._reading = [], None

    def add(self, text):
        (self._base if self._reading is None else self._reading).append(text)

    def get_spoken_text(self):
        pairs = list(self.pairs)
        if self._base:
            pairs.append(("".join(self._base), None))
        return get_ruby_spoken_text(pairs)
