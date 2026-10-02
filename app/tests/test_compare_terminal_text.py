import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import alexandria_compare as compare


def without_style(text):
    for style in (compare.RED, compare.GREEN, compare.YELLOW, compare.CYAN,
                  compare.BOLD, compare.DIM, compare.RESET):
        text = text.replace(style, '')
    return text


class CompareTerminalTextTests(unittest.TestCase):
    def test_colored_diff_escapes_payload_before_adding_generated_colors(self):
        attacks = ('\x1b[2J', '\x1b]52;c;payload\x07', '\rhidden', '\bhidden',
                   '\x9b2J', '\u202ehidden', '\tcolumn', '\nline')
        for attack in attacks:
            with self.subTest(attack=repr(attack)):
                source = ['safe', attack]; before = source.copy()
                a, b = compare.color_diff(source, ['safe', 'replacement'])
                self.assertNotIn(attack, without_style(a))
                self.assertIn(compare.RED, a)
                self.assertIn(compare.GREEN, b)
                self.assertEqual(before, source)

    def test_actual_review_displays_controls_visibly_but_keeps_saved_metadata(self):
        attack = '\x1b]52;c;payload\x07\r\b\x9b2J\u202e'
        entries = [{'text': attack + ' *hello* ... world',
                    'audio_filepath': attack + '.wav', 'start': 0, 'end': 1}]
        before = copy.deepcopy(entries)
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root / ('metadata' + attack + '.jsonl')
            source.write_text(json.dumps(entries[0]) + '\n')
            original_bytes = source.read_bytes()
            output = root / 'corrected.jsonl'; log = root / 'review.jsonl'
            capture = io.StringIO()
            words = [attack, 'hello', 'world']
            with patch.object(compare, 'find_best_match', return_value=(0, 3, 1)), \
                    patch('builtins.input', return_value='k'), contextlib.redirect_stdout(capture):
                compare.run(entries, words, words, {}, 0, .9, True,
                            str(source), str(output), log)
            displayed = without_style(capture.getvalue())
            for control in ('\x1b', '\x07', '\r', '\b', '\x9b', '\u202e'):
                self.assertNotIn(control, displayed)
            self.assertIn('\\x1b]52;c;payload\\x07', displayed)
            self.assertEqual(entries, compare.load_jsonl(output))
            self.assertEqual(original_bytes, source.read_bytes())
            self.assertEqual(before, entries)
            self.assertEqual('keep', json.loads(log.read_text().splitlines()[0])['action'])
            self.assertFalse(compare.checkpoint_path(str(source)).exists())
            self.assertFalse(compare.checkpoint_journal_path(str(source)).exists())

    def test_normal_unicode_diff_and_generated_styles_are_unchanged(self):
        a, b = compare.color_diff(['Café', '猫', '👩\u200d💻'], ['Café', '猫', 'bonjour'])
        self.assertEqual('Café 猫 ' + compare.RED + '👩\u200d💻' + compare.RESET, a)
        self.assertEqual('Café 猫 ' + compare.GREEN + 'bonjour' + compare.RESET, b)

    def test_checkpoint_warning_and_recovery_refusal_escape_owned_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'book\x1b]52;c;payload\x07.jsonl')
            checkpoint = compare.checkpoint_path(path)
            checkpoint.write_text('{partial')
            capture = io.StringIO()
            with contextlib.redirect_stdout(capture):
                self.assertEqual({'decisions': {}, 'cursor': 0}, compare.load_checkpoint(path))
            self.assertNotIn('\x1b', capture.getvalue())
            self.assertIn('\\x1b]52;c;payload\\x07', capture.getvalue())
            checkpoint.unlink()
            journal = compare.checkpoint_journal_path(path); journal.write_text('{partial')
            with self.assertRaises(SystemExit) as error:
                compare.load_checkpoint(path)
            self.assertNotIn('\x1b', str(error.exception))
            self.assertIn('missing', str(error.exception))
            self.assertEqual('{partial', journal.read_text())
