"""#2019: app-training.js hands no bare English to a message sink.

The Training tab ships in four locales. Its failure and recovery paths used to
pass English straight to showToast / showActionError / getActionErrorMessage, so
a Chinese, Japanese or Spanish reader met the one kind of text a failure message
must never be in: the sentence telling them what to check. Eighteen call sites
were doing it (issue #2019) while sibling lines in the same file were already
keyed.

Each of those strings is now a training.* key, and this is the guard that keeps
it that way. The scanner lives in tests/ui_i18n_literals.py so the app-core.js
(#2017) and app-voicelab.js (#2018) guards can point at the same copy rather
than writing a second and then a third (Rule 15).

Rule 21: the fixtures below prove the scanner still discriminates - it must
reject a bare action, a bare recovery, both arms of a literal ternary and a
textContent literal, and it must accept the keyed forms, a variable, an empty
string and a numeric template. One test injects a literal back into the real
file, so a scanner that quietly stopped reading it fails here instead of
passing everything.
"""
import unittest
from pathlib import Path

from tests.ui_i18n_literals import bare_message_literals

STATIC = Path(__file__).resolve().parent.parent / 'static'
SOURCE = STATIC / 'js' / 'app-training.js'


def found(source):
    return [(finding.sink, finding.literal) for finding in bare_message_literals(source)]


class ScannerFixtureTests(unittest.TestCase):
    """The scanner is checked on cases whose answer is already known."""

    def test_a_bare_action_and_recovery_are_both_reported(self):
        self.assertEqual(
            [('showActionError', 'Promotion failed'),
             ('showActionError', 'Refresh the adapter list before promoting again.')],
            found("showActionError('Promotion failed', e, 'Refresh the adapter list before promoting again.');"))

    def test_both_arms_of_a_literal_ternary_are_reported(self):
        self.assertEqual(
            [('showActionError', 'Upload error'), ('showActionError', 'Dataset upload refused')],
            found("showActionError(refused ? 'Dataset upload refused' : 'Upload error', e, recovery());"))

    def test_a_keyed_call_is_accepted(self):
        self.assertEqual([], found(
            "showActionError(t('training.promote.failed', 'Promotion failed'), e, "
            "t('training.promote.failed-recovery', 'Refresh the adapter list before promoting again.'));"))

    def test_a_ternary_of_keyed_calls_is_accepted(self):
        self.assertEqual([], found(
            "showToast(ready ? t('a.ready', 'Ready to start.') : t('a.wait', 'Waiting for a slot.'), 'warning');"))

    def test_a_variable_empty_string_and_numeric_template_are_accepted(self):
        self.assertEqual([], found(
            "showToast(message);\nstatus.textContent = '';\nepoch.innerText = `${epoch}/${maxEpoch}`;\n"
            "getActionErrorMessage(action, e, recovery);"))

    def test_a_textContent_literal_is_reported_but_a_keyed_one_is_not(self):
        self.assertEqual([('textContent', 'Could not load adapters.')],
                         found("status.textContent = 'Could not load adapters.';\n"))
        self.assertEqual([], found(
            "status.textContent = t('training.adapter.load-failed', 'Could not load adapters.');\n"))

    def test_a_key_that_reads_like_copy_is_not_reported(self):
        self.assertEqual([], found(
            "showToast(t('training.compare.unavailable-recovery', 'Reopen Compare.'), 'error');"))


class AppTrainingLiteralTests(unittest.TestCase):
    def test_the_file_hands_no_bare_english_to_a_message_sink(self):
        findings = bare_message_literals(SOURCE.read_text(encoding='utf-8'))
        self.assertEqual([], [(finding.line, finding.sink, finding.literal) for finding in findings],
                         'a message sink in app-training.js was handed bare English; '
                         'wrap it in t() with a training.* key')

    def test_a_reintroduced_literal_in_the_real_file_is_caught(self):
        """The guard reads this file: prove it on the file it is guarding."""
        source = SOURCE.read_text(encoding='utf-8')
        keyed = "t('training.train.cancel-failed', 'Failed to cancel training')"
        self.assertIn(keyed, source)
        self.assertEqual([('showActionError', 'Failed to cancel training')],
                         found(source.replace(keyed, "'Failed to cancel training'")))

    def test_every_converted_recovery_string_is_a_catalog_key(self):
        """A converted site must use a keyed call, not an inline literal."""
        source = SOURCE.read_text(encoding='utf-8')
        for key in ('training.train.cancel-failed-recovery',
                    'training.compare.unavailable-recovery',
                    'training.history.unavailable-recovery',
                    'training.test.failed-recovery',
                    'training.adapter.download-failed-recovery'):
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", source)


if __name__ == '__main__':
    unittest.main()
