"""The untranslated-literal ratchet for markup and JS.

A translation phase cannot convert 1,500 markup strings and 4,000 JS literals in
one commit, so this gate records the number of display-eligible literals that are
still neither keyed nor on a KEEP_ENGLISH allowlist, and fails when any file or
tab pane grows. Every translation phase lowers these numbers; the final phase sets
them to zero and deletes this file.

What "keyed" means here, and nothing more:
  * markup: inside an element carrying data-i18n, or an attribute named by that
    element's data-i18n-attr, or inside data-i18n-skip;
  * JS: the English argument of t('key', 'English') or the third argument of
    createUiMessageNode('tag', 'key', 'English').

The counter is a heuristic (design section 9.1): it includes prompt fragments and
error strings that are never painted. It is a ratchet, not a translation backlog.

Rule 21: the two synthetic fixtures at the bottom prove the counter counts one
unkeyed literal and zero keyed ones, so a counter that silently stopped seeing
anything would fail here.
"""
from html.parser import HTMLParser
import re
from pathlib import Path
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static'
ROOT = STATIC.parent.parent
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
        'meta', 'param', 'source', 'track', 'wbr'}
TEXT_ATTRIBUTES = ('placeholder', 'title', 'aria-label', 'alt')
KEEP_ENGLISH = frozenset({
    'Alexandria', 'Alexandria Audiobook', 'GPU', 'CPU', 'LoRA', 'M4B', 'WAV',
    'MP3', 'API', 'JSON', 'URL', 'OK', 'English', 'Qwen3-TTS', 'NFKC',
    '简体中文', '日本語', 'Español (Latinoamérica)',
    'Pseudo-English (layout test, not a translation)',
})

# Measured 2026-11-10 on the P5 tree by the counters below. A pane or file that
# is absent here fails the test, so a new surface must declare its baseline.
# P4 lowered index.html's setup 411 -> 0 and shell 81 -> 0 and app-settings.js
# 63 -> 53; app-i18n.js rose 23 -> 25 by two key literals in the data-i18n-html
# and safety branches. P5 lowered index.html's script 212 -> 2 (the two left are
# live JS status lines), app-guides.js 154 -> 48 and app-workbench.js 382 -> 324
# (the Dataset Builder surface; the Preparer and system-status sections in that
# file belong to later phases). P6 lowered index.html's voices 75 -> 0 and
# casting 61 -> 0, app-casting.js 262 -> 142, app-casting-research.js 436 -> 217,
# app-pronunciation.js 210 -> 74, app-character-roster.js 110 -> 30 and
# app-core.js 2215 -> 2088 (the voices/casting-only literals; the voice-card
# template, candidate markup and state editor in that file are still English).
# app-help.js's 75 counts two attribute-name literals in the tooltip refresh;
# its 700+ help strings are catalog sources (excluded by help_map_spans, and
# test_ui_help_i18n.py proves every one is keyed in all three locales).
MARKUP_BASELINE = {
    'index.html': {
        'audio': 153, 'casting': 0, 'dataset-builder': 30, 'designer': 19,
        'editor': 207, 'home': 23, 'preparer': 62, 'reports': 17, 'script': 2,
        'setup': 0, 'shell': 0, 'training': 69, 'voicelab': 54, 'voices': 0,
    },
    'readalong-player.html': {'shell': 16},
}

JS_BASELINE = {
    'app-audio-channels.js': 26, 'app-audiobookshelf.js': 88, 'app-calibre.js': 269,
    'app-casting-research.js': 217, 'app-casting.js': 142, 'app-character-roster.js': 30,
    'app-core.js': 2088, 'app-editor-repair.js': 32, 'app-editor-review.js': 193,
    'app-export-quality.js': 38, 'app-guides.js': 48, 'app-help.js': 75,
    'app-i18n.js': 25, 'app-mastering.js': 22, 'app-narration-markup.js': 31,
    'app-project-scope.js': 33, 'app-pronunciation.js': 74,
    'app-publication-validation.js': 2, 'app-publication.js': 45, 'app-readalong.js': 63,
    'app-reports.js': 91, 'app-scripts.js': 329, 'app-settings.js': 53,
    'app-single-voice.js': 96, 'app-sound-assets.js': 39, 'app-sound-cues.js': 111,
    'app-sound-mix-cover.js': 19, 'app-sound-mixes.js': 93, 'app-sound-renditions.js': 148,
    'app-speaker-review.js': 107, 'app-speech-verification.js': 29,
    'app-synthesis-windows.js': 42, 'app-take-history.js': 92, 'app-training.js': 311,
    'app-translated-edition.js': 37, 'app-tts-capabilities.js': 56,
    'app-tts-diagnostics.js': 33, 'app-voicelab.js': 154, 'app-word-timing.js': 68,
    'app-workbench.js': 324, 'app-workspace.js': 135,
}


def is_display_text(text):
    stripped = text.strip()
    if not stripped or not re.search(r'[A-Za-z\u3400-\u9fff]', stripped):
        return False
    if re.fullmatch(r'[\W_]+', stripped, re.UNICODE):
        return False
    if stripped in KEEP_ENGLISH:
        return False
    words = re.findall(r'[A-Za-z]{2,}|[\u3400-\u9fff]+', stripped)
    if len(words) >= 2:
        return True
    if not words:
        return False
    return stripped[0].isupper() or stripped[-1] in '.!?:'


class LiteralInventory(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.counts = {}
        self.raw = False

    def _pane(self):
        for tag, pane in reversed(self.stack):
            if pane is not None:
                return pane
        return 'shell'

    def _count(self, pane):
        self.counts[pane] = self.counts.get(pane, 0) + 1

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        inherited = self._pane() if self.stack else 'shell'
        pane = inherited
        if attributes.get('id', '').endswith('-tab') and 'tab-content' in attributes.get('class', ''):
            pane = attributes['id'][:-4]
        skip = inherited == 'skip' or 'data-i18n-skip' in attributes
        keyed = skip or 'data-i18n' in attributes or 'data-i18n-html' in attributes
        if tag in {'script', 'style'}:
            self.raw = True
            keyed = True
        attrs_keyed = set()
        for entry in (attributes.get('data-i18n-attr') or '').split(','):
            attribute, _, _ = entry.partition(':')
            if attribute.strip():
                attrs_keyed.add(attribute.strip())
        if not keyed:
            for attribute in TEXT_ATTRIBUTES:
                value = attributes.get(attribute)
                if value is not None and attribute not in attrs_keyed and is_display_text(value):
                    self._count(pane)
        if tag not in VOID:
            self.stack.append((tag, 'skip' if (skip or keyed) else pane))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.raw = False
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        if self.raw:
            return
        if is_display_text(data) and self._pane() != 'skip':
            self._count(self._pane())


def count_markup(path):
    inventory = LiteralInventory()
    inventory.feed(path.read_text(encoding='utf-8'))
    return inventory.counts


def _literal_at(text, index):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] not in "'\"":
        return None
    quote = text[index]
    start = index
    index += 1
    while index < len(text):
        if text[index] == '\\':
            index += 2
            continue
        if text[index] == quote:
            return start, index + 1
        index += 1
    return None


def _arguments(text, open_paren):
    spans = []
    index = open_paren
    for _ in range(3):
        span = _literal_at(text, index)
        if span is None:
            break
        spans.append(span)
        index = span[1]
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == ',':
            index += 1
            continue
        break
    return spans


HELP_SOURCE = 'app-help.js'
HELP_MAP_START = 'const FIELD_HELP = {'
HELP_MAP_END = '/** The tooltip text'


def help_map_spans(path):
    """String spans inside app-help.js's help maps.

    Every string there is either a map id or an English help value, and every
    value is a catalog source that test_ui_help_i18n.py proves is keyed, so the
    ratchet must not count it as an unkeyed display literal. A literal added
    outside the maps still counts, which is what keeps this a ratchet.
    """
    text = path.read_text(encoding='utf-8')
    start = text.index(HELP_MAP_START)
    end = text.index(HELP_MAP_END, start)
    return {(match.start(), match.end()) for match in STRING.finditer(text, start, end)}


def keyed_spans(text):
    spans = set()
    for match in re.finditer(r"\bt\s*\(", text):
        arguments = _arguments(text, match.end())
        if len(arguments) >= 2:
            spans.add(arguments[0])
            spans.add(arguments[1])
    for match in re.finditer(r"\bcreateUiMessageNode\s*\(", text):
        arguments = _arguments(text, match.end())
        if len(arguments) >= 3:
            spans.add(arguments[1])
            spans.add(arguments[2])
    # formatUiSafetyText('key', 'English', ...) is the string-sink form of the
    # same authored safety message, so its first two arguments are keyed too.
    for match in re.finditer(r"\bformatUiSafetyText\s*\(", text):
        arguments = _arguments(text, match.end())
        if len(arguments) >= 2:
            spans.add(arguments[0])
            spans.add(arguments[1])
    return spans


STRING = re.compile(r"""'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`""", re.S)


def count_js(path, excluded=()):
    text = path.read_text(encoding='utf-8')
    keyed = keyed_spans(text) | set(excluded)
    count = 0
    for match in STRING.finditer(text):
        if (match.start(), match.end()) in keyed:
            continue
        body = match.group(0)[1:-1]
        if not is_display_text(body):
            continue
        if body.startswith(('/', 'http://', 'https://', '.', '#', '[')):
            continue
        if re.search(r'[\w-]+\s*:', body) and ' ' not in body.strip():
            continue
        count += 1
    return count


class CounterFixtureTests(unittest.TestCase):
    """Rule 21: the counter must see one unkeyed literal and no keyed ones."""

    def test_markup_counter_counts_only_unkeyed_display_text(self):
        source = STATIC / 'index.html'
        counts = count_markup(source)
        self.assertGreater(sum(counts.values()), 0)
        fixture = LiteralInventory()
        fixture.feed('<div id="w-tab" class="tab-content"><p>Unkeyed sentence</p>'
                     '<p data-i18n="a.b">Keyed sentence</p>'
                     '<input placeholder="Unkeyed placeholder" data-i18n-attr="title:a.b" title="keyed">'
                     '<span data-i18n-skip>Brand name</span>'
                     '<script>var x = "not visible";</script></div>')
        self.assertEqual({'w': 2}, fixture.counts)

    def test_js_counter_ignores_the_english_argument_of_t_and_create(self):
        text = ("const a=t('setup.finder.label','Find a setting');"
                "const b=createUiMessageNode('strong','setup.preview.saved','Saved');"
                "const c='Leave this literal alone';")
        keyed = keyed_spans(text)
        seen = [(m.start(), m.end()) for m in STRING.finditer(text)]
        unkeyed = [span for span in seen if span not in keyed and is_display_text(text[span[0] + 1:span[1] - 1])]
        self.assertEqual(1, len(unkeyed))
        self.assertEqual("'Leave this literal alone'", text[unkeyed[0][0]:unkeyed[0][1]])

    def test_the_help_map_exclusion_stops_at_the_maps(self):
        """The maps are catalog sources; a literal after them is still counted."""
        path = STATIC / 'js' / HELP_SOURCE
        text = path.read_text(encoding='utf-8')
        spans = help_map_spans(path)
        self.assertGreater(len(spans), 700)
        self.assertTrue(any(text[start:end] == "'roster-import'" for start, end in spans))
        later = STRING.search(text, text.index(HELP_MAP_END))
        self.assertIsNotNone(later)
        self.assertNotIn((later.start(), later.end()), spans)


class RatchetTests(unittest.TestCase):
    def test_markup_baseline_covers_every_page_and_never_grows(self):
        pages = {'index.html', 'readalong-player.html'}
        self.assertEqual(pages, set(MARKUP_BASELINE), 'every page must declare a baseline')
        for name, baseline in MARKUP_BASELINE.items():
            with self.subTest(page=name):
                counts = count_markup(STATIC / name)
                for pane in sorted(set(counts) - set(baseline)):
                    self.fail(f'{name} has an undeclared pane {pane!r} with {counts[pane]} unkeyed literals')
                for pane, limit in baseline.items():
                    self.assertLessEqual(counts.get(pane, 0), limit,
                                         f'{name} pane {pane!r} grew from {limit} to {counts.get(pane, 0)} '
                                         f'unkeyed display literals; key the new text or justify it in KEEP_ENGLISH')

    def test_js_baseline_covers_every_file_and_never_grows(self):
        files = {path.name for path in (STATIC / 'js').glob('*.js')}
        self.assertEqual(files, set(JS_BASELINE), 'every JS file must declare a baseline')
        for name, limit in JS_BASELINE.items():
            with self.subTest(file=name):
                path = STATIC / 'js' / name
                excluded = help_map_spans(path) if name == HELP_SOURCE else ()
                count = count_js(path, excluded)
                self.assertLessEqual(count, limit,
                                     f'{name} grew from {limit} to {count} unkeyed display literals; '
                                     f'wrap keyed text in t() or add a reviewed KEEP_ENGLISH entry')

    def test_the_p1_surfaces_are_actually_keyed(self):
        """The ratchet only helps if the strings this phase migrated are keyed."""
        markup = (STATIC / 'index.html').read_text(encoding='utf-8')
        for key in ('setup.finder.label', 'setup.finder.placeholder', 'setup.finder.clear',
                    'setup.finder.hint', 'setup.preview.action', 'setup.preview.title',
                    'setup.preview.close', 'setup.preview.defaults'):
            with self.subTest(key=key):
                if key == 'setup.finder.placeholder':
                    self.assertIn(f'data-i18n-attr="placeholder:{key}"', markup)
                else:
                    self.assertIn(f'data-i18n="{key}"', markup)
        settings = (STATIC / 'js' / 'app-settings.js').read_text(encoding='utf-8')
        for key in ('setup.finder.none', 'setup.finder.one', 'setup.finder.many',
                    'setup.finder.limited', 'setup.finder.opened', 'setup.preview.saved',
                    'setup.preview.would_save', 'setup.locale.storage_notice'):
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", settings)


if __name__ == '__main__':
    unittest.main()
