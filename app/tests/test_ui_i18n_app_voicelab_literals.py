"""app-voicelab.js must pass keyed text to every user-visible sink.

#2018 fixed the Voice Lab failure paths that still handed bare English to
showActionError / getActionErrorMessage / copyToClipboard. #2017 asks for the
same guard over app-core.js but had no merged PR when this landed, so this is
the equivalent for app-voicelab.js: the same rule, one file, so the converted
call sites cannot regress.

The rule is deliberately narrow. It does not demand that every string in the
file be keyed - template HTML, element ids, CSS classes, API paths and the
`success`/`warning` toast types are not user copy. It checks the sinks that
render text verbatim: showToast, showActionError, getActionErrorMessage,
confirmIfRemote, setUiMessage, copyToClipboard's subject, and a
textContent/innerText assignment whose whole right-hand side is one literal.
A bare literal in any of those is a message a non-English reader sees in
English.

Rule 21 is why the checker has fixtures: a scanner that silently stopped
recognising a call would pass this file forever.
"""
import json
import re
import unittest
from pathlib import Path

from tests.test_ui_markup_gate import is_display_text

STATIC = Path(__file__).resolve().parent.parent / 'static'
SOURCE = STATIC / 'js' / 'app-voicelab.js'
I18N = STATIC / 'i18n'
LOCALES = ('zh-CN', 'ja', 'es-419')

SINKS = ('showToast', 'showActionError', 'getActionErrorMessage', 'confirmIfRemote',
         'setUiMessage', 'copyToClipboard')
CALL = re.compile(r'\b(?:' + '|'.join(SINKS) + r')\s*\(')
RUNTIME_TEXT = re.compile(r'\.(?:textContent|innerText)\s*=\s*')
KEY_CALL = re.compile(r"(?<![\w$.])t\(\s*'([^']+)'")
OPENERS = {'(': ')', '[': ']', '{': '}'}


def _skip_string(text, index):
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == '\\':
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return len(text)


def _split_arguments(text, open_paren):
    """The top-level arguments of the call whose '(' is at open_paren."""
    arguments, start, depth, index = [], open_paren + 1, 0, open_paren + 1
    while index < len(text):
        character = text[index]
        if character in "'\"`":
            index = _skip_string(text, index)
            continue
        if character in OPENERS:
            depth += 1
        elif character in ')]}':
            if depth == 0:
                arguments.append(text[start:index])
                return arguments
            depth -= 1
        elif character == ',' and depth == 0:
            arguments.append(text[start:index])
            start = index + 1
        index += 1
    return arguments


def _whole_literal(argument):
    """True when the argument is exactly one quoted string, not a call or name."""
    argument = argument.strip()
    if not argument or argument[0] not in "'\"`":
        return False
    return _skip_string(argument, 0) == len(argument)


def bare_literals(text):
    """Every (line, literal) a sink receives as unkeyed display text."""
    findings = []

    def record(index, argument):
        argument = argument.strip()
        if not _whole_literal(argument):
            return
        body = argument[1:-1]
        if is_display_text(body):
            findings.append((text.count('\n', 0, index) + 1, body))

    for match in CALL.finditer(text):
        for argument in _split_arguments(text, match.end() - 1):
            record(match.start(), argument)
    for match in RUNTIME_TEXT.finditer(text):
        end = text.find(';', match.end())
        record(match.start(), text[match.end():end if end >= 0 else len(text)])
    return findings


class CheckerFixtureTests(unittest.TestCase):
    """Rule 21: the scanner is checked on snippets whose answer is known."""

    def test_a_bare_display_literal_is_reported(self):
        self.assertEqual([(1, 'Pipeline start is unconfirmed')],
                         bare_literals("showActionError('Pipeline start is unconfirmed', e, r);"))
        self.assertEqual([(1, 'Saved')], bare_literals("el.textContent = 'Saved';"))

    def test_a_keyed_call_and_its_english_argument_are_not_reported(self):
        self.assertEqual([], bare_literals(
            "showActionError(t('voicelab.start.x', 'Pipeline start is unconfirmed'), e, "
            "t('voicelab.start.x-recovery', 'Check the status before running again.'));"))
        self.assertEqual([], bare_literals("el.textContent = t('voicelab.x', 'Saved text');"))

    def test_variables_types_and_non_copy_literals_are_not_reported(self):
        source = ("showToast(message, 'warning');"
                  "showActionError(action, e, recovery, 'error');"
                  "copyToClipboard(text, subject);"
                  "el.textContent = message;")
        self.assertEqual([], bare_literals(source))

    def test_every_argument_of_a_multi_argument_call_is_checked(self):
        findings = bare_literals(
            "showActionError('Could not fetch diagnostics', e, 'Check the connection.');")
        self.assertEqual([(1, 'Could not fetch diagnostics'), (1, 'Check the connection.')],
                         findings)


class RealFileTests(unittest.TestCase):
    def test_the_voice_lab_sinks_carry_no_bare_display_literal(self):
        findings = bare_literals(SOURCE.read_text(encoding='utf-8'))
        self.assertEqual([], findings, 'wrap these in t() with a voicelab.* key')

    def test_every_key_the_file_uses_exists_in_all_four_catalogs(self):
        keys = set(KEY_CALL.findall(SOURCE.read_text(encoding='utf-8')))
        self.assertGreater(len(keys), 50, 'the file must still key its messages')
        for name in ('ui.en.json',) + tuple(f'ui.{locale}.json' for locale in LOCALES):
            messages = json.loads((I18N / name).read_text(encoding='utf-8'))
            for key in sorted(keys):
                with self.subTest(catalog=name, key=key):
                    self.assertIn(key, messages)

    def test_the_2018_failure_and_recovery_keys_are_used_and_translated(self):
        source = SOURCE.read_text(encoding='utf-8')
        english = json.loads((I18N / 'ui.en.json').read_text(encoding='utf-8'))
        keys = ('voicelab.settings.save-unconfirmed',
                'voicelab.settings.save-unconfirmed-recovery',
                'voicelab.start.settings-invalid',
                'voicelab.start.settings-invalid-recovery',
                'voicelab.start.preflight-unconfirmed',
                'voicelab.start.preflight-unconfirmed-recovery',
                'voicelab.start.preflight-failed',
                'voicelab.start.preflight-failed-recovery',
                'voicelab.start.start-unconfirmed',
                'voicelab.start.start-unconfirmed-recovery',
                'voicelab.diagnostics.fetch-failed',
                'voicelab.diagnostics.fetch-failed-recovery',
                'voicelab.diagnostics.download-failed',
                'voicelab.diagnostics.download-failed-recovery',
                'voicelab.diagnostics.copy-subject')
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", source, 'the Voice Lab failure path must use this key')
                self.assertIn(key, english)
                for locale in LOCALES:
                    messages = json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))
                    self.assertIn(key, messages, f'{locale} is missing {key}')
                    self.assertNotEqual(english[key], messages[key],
                                        f'{locale} left {key} untranslated')


if __name__ == '__main__':
    unittest.main()
