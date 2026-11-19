"""One authority for the review-context limits and defaults.

The SPA, the HTTP routes and the CLI must agree on each mode's cap and default.
The JS table is the SPA's single source; this test pins it to the Python
constants so a future edit cannot silently make the field disagree with the
server (issue #2091).
"""
import json
import re
from pathlib import Path
import unittest

import review_script

APP = Path(__file__).resolve().parent.parent


class ReviewContextLimitTests(unittest.TestCase):
    def test_js_context_table_matches_the_python_constants(self):
        source = (APP / 'static/js/app-core.js').read_text(encoding='utf-8')
        match = re.search(r'const REVIEW_CONTEXT_MODES = \{(.*?)\n        \};', source, re.S)
        self.assertIsNotNone(match, 'a single JS limit/default table must exist')
        body = match.group(1)

        def number(mode, field):
            found = re.search(rf'\b{mode}:\s*\{{[^}}]*?{field}:\s*(\d+)', body)
            self.assertIsNotNone(found, f'{mode}.{field} must be declared in the table')
            return int(found.group(1))

        self.assertEqual(review_script.CONTEXT_ENTRY_MAX, number('entries', 'limit'))
        self.assertEqual(review_script.CONTEXT_CHAR_MAX, number('chars', 'limit'))
        self.assertEqual(review_script.DEFAULT_CONTEXT_ENTRIES, number('entries', 'default'))
        self.assertEqual(review_script.DEFAULT_CONTEXT_CHARS, number('chars', 'default'))

    def test_html_field_starts_at_the_entries_default(self):
        html = (APP / 'static/index.html').read_text(encoding='utf-8')
        field = re.search(r'<input[^>]*id="review-context-window"[^>]*>', html)
        self.assertIsNotNone(field)
        self.assertIn(f'max="{review_script.CONTEXT_ENTRY_MAX}"', field.group(0))
        self.assertIn(f'value="{review_script.DEFAULT_CONTEXT_ENTRIES}"', field.group(0))

    def test_mode_labels_are_translated_and_the_default_matches_english(self):
        keys = ('script.review.contextual.entries', 'script.review.contextual.chars')
        catalogs = {}
        for locale in ('en', 'es-419', 'ja', 'zh-CN'):
            catalogs[locale] = json.loads(
                (APP / 'static/i18n' / f'ui.{locale}.json').read_text(encoding='utf-8'))
            for key in keys:
                self.assertIsInstance(catalogs[locale].get(key), str, f'{locale}: {key}')
                self.assertTrue(catalogs[locale][key].strip(), f'{locale}: {key}')
        self.assertNotEqual(catalogs['en'][keys[0]], catalogs['en'][keys[1]],
                            'the two modes need distinct, unit-bearing labels')
        html = (APP / 'static/index.html').read_text(encoding='utf-8')
        self.assertIn('>' + catalogs['en'][keys[0]] + '</span>', html)


if __name__ == '__main__':
    unittest.main()
