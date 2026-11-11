"""The golden corpus of server refusal text (design section 5.8, Rule 9).

Every ``detail=`` sentence and every ``AppError`` message in app/*.py and
app/routers/*.py is pinned. The harvest normalises both raise shapes to the same
source expression, so converting a site cannot change the corpus while a
rewording cannot pass unnoticed.

The remaining-``detail=`` baseline is a ratchet: each phase converts more sites
and lowers it. It must never grow, which is what stops a new refusal from
bypassing AppError.
"""
import ast
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'api_detail_golden.json'
TOOL = ROOT / 'tools' / 'i18n' / 'extract_api_details.py'
# Measured on the P2 tree after the 30 converted keys. Only ever lower this.
DETAIL_SITES_BASELINE = 830


def load_tool():
    spec = importlib.util.spec_from_file_location('extract_api_details', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GoldenCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tool = load_tool()
        cls.fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
        cls.texts, cls.remaining = cls.tool.harvest(ROOT)

    def test_the_fixture_matches_every_refusal_in_the_tree(self):
        self.assertEqual(self.fixture['strings'], self.texts,
                         'a refusal was reworded or added; run: '
                         'python tools/i18n/extract_api_details.py')

    def test_the_fixture_is_not_empty_and_carries_converted_refusals(self):
        self.assertGreater(len(self.fixture['strings']), 700)
        self.assertIn("'Active book changed; reload voices before saving'",
                      self.fixture['strings'])
        self.assertIn("'Book or voices are busy; please try again.'",
                      self.fixture['strings'])
        self.assertTrue(any(text.startswith("f'Could not complete or confirm {operation}")
                            for text in self.fixture['strings']))

    def test_new_detail_sites_must_use_app_error(self):
        self.assertLessEqual(self.remaining, DETAIL_SITES_BASELINE,
                             'a new HTTPException(detail=...) was added; use AppError and a catalog key')

    def test_the_harvest_cannot_tell_a_converted_site_from_an_unconverted_one(self):
        """A conversion moves the same sentence into a different AST shape."""
        old = ast.parse('raise HTTPException(409, detail="Same refusal.")').body[0]
        new = ast.parse('raise AppError(409, "Same refusal.", key="error.test")').body[0]
        self.assertEqual(ast.unparse(self.tool._refusal_value(old.exc)),
                         ast.unparse(self.tool._refusal_value(new.exc)))

    def test_the_harvest_notices_a_reworded_warning(self):
        """Rule 21: the instrument must reject the change it exists to catch."""
        old = ast.parse('raise AppError(409, "Delete 2 takes? This cannot be undone.", key="k")').body[0]
        new = ast.parse('raise AppError(409, "Delete 2 takes? This might be undone.", key="k")').body[0]
        self.assertNotEqual(ast.unparse(self.tool._refusal_value(old.exc)),
                            ast.unparse(self.tool._refusal_value(new.exc)))

    def test_the_fixture_has_no_meta_or_message_key_leakage(self):
        self.assertEqual({'source', 'regenerate'}, set(self.fixture['_meta']))
        for text in self.fixture['strings']:
            self.assertNotIn('message_key', text)


if __name__ == '__main__':
    unittest.main()
