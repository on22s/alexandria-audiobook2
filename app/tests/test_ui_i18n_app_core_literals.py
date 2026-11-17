"""app-core.js must never show the user a bare English literal (issue #2017).

What this verifies, precisely: for every call to ``showToast``,
``showActionError``, ``getActionErrorMessage`` and ``confirmIfRemote`` in
``app/static/js/app-core.js``, and for every ``.textContent =`` / ``.innerText
=`` assignment, the user-visible text is not written as a raw string literal.
User-facing English lives in ``t('<key>', '<English>', {...})`` so that
``app/static/i18n/ui.*.json`` can translate it; a raw literal is shown to every
locale exactly as typed.

What it does not cover: HTML assembled with ``.innerHTML =`` (a template that
calls ``t()`` inside is invisible to a text scan) and labels set through other
helpers. Those shapes were converted in the same change, but a guard over them
would have to re-implement the template grammar to stay honest, so it is out of
scope here. This guard pins the two shapes that held 175 of the 258 literals
measured on origin/main.

The allowlist is the honest part. It is empty: #2017 converted every literal it
found, and the last two (inside ``autoConfigureSettings()``) were converted by
#2016 when that branch merged. Every entry must name its reason, and a test
below fails when an entry stops being needed, so an exemption cannot be left
behind as dead weight once the code it excused is translated.
"""
import re
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / 'static' / 'js' / 'app-core.js'

HELPERS = ('showToast', 'showActionError', 'getActionErrorMessage', 'confirmIfRemote')
TEXT_TARGETS = ('textContent', 'innerText')

ALLOWED = {
    # Empty by design: every user-visible literal in the scanned shapes goes
    # through t(). Add an entry only with the reason the English must stay raw.
}

PROSE = re.compile(r'[A-Za-z]{2,}')
TRANSLATED = re.compile(r'\bt\(')
TAG = re.compile(r'<[^>]*>')
ENTITY = re.compile(r'&[a-zA-Z]{2,8};')
NEWLINE = chr(10)
QUOTES = "'" + '"' + '`'
REGEX_PREVIOUS = set('(,=:[!&|?{};+-*%~^<>')


def _mask_template(text, index, mask):
    """Mark a template literal, descending into each ${...} interpolation."""
    n = len(text)
    mask[index] = False
    index += 1
    while index < n:
        char = text[index]
        if char == '\\':
            mask[index] = False
            if index + 1 < n:
                mask[index + 1] = False
            index += 2
            continue
        if char == '`':
            mask[index] = False
            return index + 1
        if char == '$' and index + 1 < n and text[index + 1] == '{':
            mask[index] = False
            mask[index + 1] = False
            index = _mask_code(text, index + 2, mask, True)
            if index < n and text[index] == '}':
                mask[index] = False
                index += 1
            continue
        mask[index] = False
        index += 1
    return index


def _mask_code(text, index, mask, stop_at_brace):
    """Walk one code region, clearing every comment, string and regex literal.

    This is the whole reason the scanner is not four lines of regex: a missed
    desync (a nested backtick, a quote inside a regex class) silently drops
    every literal after it, which is exactly the Shape-of-error Rule 21 warns
    about. Returns the index of the closing brace when stop_at_brace is set.
    """
    n = len(text)
    depth = 0
    previous = ''
    while index < n:
        char = text[index]
        if char == '/' and index + 1 < n and text[index + 1] == '/':
            while index < n and text[index] != NEWLINE:
                mask[index] = False
                index += 1
            continue
        if char == '/' and index + 1 < n and text[index + 1] == '*':
            end = text.find('*/', index + 2)
            end = n if end < 0 else end + 2
            while index < end:
                if text[index] != NEWLINE:
                    mask[index] = False
                index += 1
            continue
        if char in "'" + '"':
            quote = char
            mask[index] = False
            index += 1
            while index < n:
                if text[index] == '\\':
                    mask[index] = False
                    if index + 1 < n:
                        mask[index + 1] = False
                    index += 2
                    continue
                mask[index] = False
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            previous = 'x'
            continue
        if char == '`':
            index = _mask_template(text, index, mask)
            previous = 'x'
            continue
        if char == '/' and (previous == '' or previous in REGEX_PREVIOUS):
            mask[index] = True
            index += 1
            in_class = False
            while index < n:
                inner = text[index]
                if inner == '\\':
                    mask[index] = False
                    if index + 1 < n:
                        mask[index + 1] = False
                    index += 2
                    continue
                mask[index] = False
                if inner == '[':
                    in_class = True
                elif inner == ']':
                    in_class = False
                elif inner == '/' and not in_class:
                    index += 1
                    break
                elif inner == NEWLINE:
                    break
                index += 1
            previous = '/'
            continue
        if char in '([{':
            depth += 1
        elif char in ')]}':
            if char == '}' and stop_at_brace and depth == 0:
                return index
            depth -= 1
        if not char.isspace():
            previous = char
        index += 1
    return index


def code_mask(text):
    """True at every character that is real JS code (not comment or string)."""
    mask = [True] * len(text)
    _mask_code(text, 0, mask, False)
    return mask


def read_literal(text, index):
    """Read a JS string or template literal starting at index. Returns (raw, end)."""
    quote = text[index]
    position = index + 1
    chunks = []
    while position < len(text):
        char = text[position]
        if char == '\\':
            chunks.append(text[position:position + 2])
            position += 2
            continue
        if char == quote:
            return ''.join(chunks), position + 1
        chunks.append(char)
        position += 1
    return None, position


def visible_text(raw):
    """A literal's text with inline markup and entities removed."""
    return ENTITY.sub('', TAG.sub('', raw))


def has_prose(raw):
    """True when a literal carries words, not just a symbol or a value."""
    text = visible_text(raw).strip()
    if not text:
        return False
    if re.fullmatch(r'[a-z0-9_/#.-]+', text) and '..' not in text and not text.endswith('.'):
        return False
    return PROSE.search(text) is not None


def _translated_template(source, index, raw):
    """A template literal whose wording already comes from t(<key>, <English>)."""
    return source[index] == '`' and TRANSLATED.search(raw) is not None


# Helpers whose RECOVERY argument (the third, after the error object) is also
# shown to the user; the scan checks it as well as the first argument (#2017).
RECOVERY_HELPERS = ('showActionError', 'getActionErrorMessage')


def argument_starts(text, index):
    """Start offsets of each top-level argument of the call whose '(' ends at index."""
    starts = [index]
    depth = 0
    position = index
    while position < len(text):
        char = text[position]
        if char in QUOTES:
            _raw, position = read_literal(text, position)
            continue
        if char in '([{':
            depth += 1
        elif char in ')]}':
            if depth == 0:
                return starts
            depth -= 1
        elif char == ',' and depth == 0:
            starts.append(position + 1)
        position += 1
    return starts


def line_of(text, index):
    return text.count(NEWLINE, 0, index) + 1


def scan(source):
    """Every (line, site, literal) in source that shows bare English text."""
    mask = code_mask(source)
    findings = []
    for name in HELPERS:
        for match in re.finditer(r'(?<![\w.])' + name + r'\s*\(', source):
            if not mask[match.start()]:
                continue
            index = match.end()
            while index < len(source) and source[index].isspace():
                index += 1
            if index >= len(source) or source[index] not in QUOTES:
                continue
            raw, _end = read_literal(source, index)
            if raw is not None and has_prose(raw) and not _translated_template(source, index, raw):
                findings.append((line_of(source, match.start()), name, raw))
    for name in RECOVERY_HELPERS:
        for match in re.finditer(r'(?<![\w.])' + name + r'\s*\(', source):
            if not mask[match.start()]:
                continue
            starts = argument_starts(source, match.end())
            if len(starts) < 3:
                continue
            index = starts[2]
            while index < len(source) and source[index].isspace():
                index += 1
            if index >= len(source) or source[index] not in QUOTES:
                continue
            raw, _end = read_literal(source, index)
            if raw is not None and has_prose(raw) and not _translated_template(source, index, raw):
                findings.append((line_of(source, match.start()), name + ' recovery', raw))
    for target in TEXT_TARGETS:
        for match in re.finditer(r'\.\s*' + target + r'\s*=(?!=)\s*', source):
            if not mask[match.start()]:
                continue
            index = match.end()
            if index >= len(source) or source[index] not in QUOTES:
                continue
            raw, _end = read_literal(source, index)
            if raw is not None and has_prose(raw) and not _translated_template(source, index, raw):
                findings.append((line_of(source, match.start()), '.' + target, raw))
    findings.sort()
    return findings


def core_source():
    return CORE.read_text(encoding='utf-8')


class ScannerFixtureTests(unittest.TestCase):
    """Rule 21: the scanner is checked on sources whose answer is known."""

    def test_a_bare_literal_in_each_helper_is_reported(self):
        source = ("showToast('Saved the thing.');\n"
                  "showActionError('Hardware detection failed', e, 'Recovery text.');\n"
                  "getActionErrorMessage('Could not save', e, 'Recovery.');\n"
                  "confirmIfRemote('Send this book?', 'It costs money.');\n")
        found = [(site, raw) for _line, site, raw in scan(source)]
        self.assertIn(('showToast', 'Saved the thing.'), found)
        self.assertIn(('showActionError', 'Hardware detection failed'), found)
        self.assertIn(('getActionErrorMessage', 'Could not save'), found)
        self.assertIn(('confirmIfRemote', 'Send this book?'), found)
        self.assertIn(('showActionError recovery', 'Recovery text.'), found)
        self.assertIn(('getActionErrorMessage recovery', 'Recovery.'), found)
        self.assertEqual(6, len(found))

    def test_a_bare_recovery_argument_is_reported_even_when_the_action_is_translated(self):
        source = ("showActionError(t('core.save.failed', 'Could not save'), err, 'Try again later.');\n"
                  "getActionErrorMessage(t('core.load.failed', 'Could not load'), err, `Reload ${name} first.`);\n"
                  "showActionError(t('a', 'A'), fn(x, y), t('b', 'Translated recovery.'));\n"
                  "showActionError(t('a', 'A'), err);\n")
        found = [(site, raw) for _line, site, raw in scan(source)]
        self.assertEqual([('showActionError recovery', 'Try again later.'),
                          ('getActionErrorMessage recovery', 'Reload ${name} first.')], found)

    def test_translated_and_passed_through_text_is_not_reported(self):
        source = ("showToast(t('core.saved', 'Saved the thing.'));\n"
                  "showActionError(getActionErrorMessage(t('k', 'Could not save'), e, t('k2', 'Try again.')));\n"
                  "showToast(error.message);\n"
                  "confirmIfRemote(t('core.remote.title', 'Send this book?'));\n"
                  "showToast(`<b>${t('k', 'Reused existing copy')}</b>`);\n")
        self.assertEqual([], scan(source))

    def test_an_untranslated_template_with_interpolation_is_reported(self):
        found = scan("showToast(`Reused existing copy: ${file}`);\n")
        self.assertEqual(['Reused existing copy: ${file}'], [raw for _line, _site, raw in found])

    def test_a_text_content_assignment_is_reported(self):
        found = scan("el.textContent = 'Loaded: file.txt';\n")
        self.assertEqual(['.textContent'], [site for _line, site, _raw in found])
        self.assertEqual([], scan("el.textContent = t('setup.config.loaded', 'Loaded: {file}', {file});\n"))
        self.assertEqual([], scan("el.textContent = '';\n"))
        self.assertEqual([], scan("el.textContent = String(count);\n"))

    def test_html_markup_alone_is_not_prose_but_a_label_beside_it_is(self):
        self.assertEqual([], scan("el.textContent = '<i class=\"fas fa-check\"></i>';\n"))
        found = scan("el.textContent = '<i class=\"fas fa-magic\"></i>Auto-Configure';\n")
        self.assertEqual(['<i class="fas fa-magic"></i>Auto-Configure'],
                         [raw for _line, _site, raw in found])

    def test_comments_and_nested_templates_do_not_desync_the_scan(self):
        source = ("// showToast('only in a comment');\n"
                  "const note = \"showToast('only in a string')\";\n"
                  "const row = `<a class=\"${flag ? 'b' : `c-${d}`}\">${t('k', 'Label')}</a>`;\n"
                  "showToast('Real literal after the nested template.');\n")
        found = [raw for _line, _site, raw in scan(source)]
        self.assertEqual(['Real literal after the nested template.'], found)


class AppCoreLiteralTests(unittest.TestCase):
    def test_app_core_js_has_no_bare_english_messages(self):
        findings = scan(core_source())
        unexplained = [row for row in findings if row[2] not in ALLOWED]
        self.assertEqual([], unexplained,
                         'app-core.js shows raw English instead of t(<key>, <English>): '
                         + repr(unexplained[:20]))

    def test_every_allowlist_entry_is_still_needed_and_says_why(self):
        findings = {raw for _line, _site, raw in scan(core_source())}
        for literal, reason in ALLOWED.items():
            with self.subTest(literal=literal):
                self.assertTrue(reason.strip(), 'an allowlist entry must state its reason')
                self.assertIn(literal, findings,
                              'this allowlist entry is no longer needed; delete it')


if __name__ == '__main__':
    unittest.main()
