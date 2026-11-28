"""Regression test for #2439: an unclosed <textarea>'s trailing RCDATA must not be dropped.

The router module imports optional audio dependencies, so this unit test loads only the
extractor class from its source instead of importing the whole module.
"""

import ast
import pathlib
import unittest
from html import unescape
from html.parser import HTMLParser

_MODULE = pathlib.Path(__file__).resolve().parents[1] / 'routers' / 'script.py'
_TREE = ast.parse(_MODULE.read_text(encoding='utf-8'))
import ruby_text  # noqa: E402 - lightweight module that supplies the extractor's ruby constants

_NAMESPACE = {
    'HTMLParser': HTMLParser,
    'unescape': unescape,
    'RUBY_TAG': ruby_text.RUBY_TAG,
    'RUBY_FALLBACK_TAG': ruby_text.RUBY_FALLBACK_TAG,
    'RUBY_READING_TAG': ruby_text.RUBY_READING_TAG,
    'RubyCollector': ruby_text.RubyCollector,
}
for _node in _TREE.body:
    if isinstance(_node, ast.Assign) and isinstance(_node.value, ast.Constant):
        for _target in _node.targets:
            if isinstance(_target, ast.Name):
                _NAMESPACE[_target.id] = _node.value.value
for _node in _TREE.body:
    if isinstance(_node, ast.ClassDef) and _node.name == '_HTMLTextExtractor':
        exec(compile(ast.Module(body=[_node], type_ignores=[]), str(_MODULE), 'exec'), _NAMESPACE)
        break
_HTMLTextExtractor = _NAMESPACE['_HTMLTextExtractor']


class UnclosedTextareaRcdataTest(unittest.TestCase):
    def test_chunked_feed_does_not_drop_the_rcdata_suffix(self):
        extractor = _HTMLTextExtractor(html_rcdata=True)
        extractor.feed('<html><body><textarea>Hello')
        extractor.feed(' world')
        extractor.feed(' again')
        self.assertEqual(extractor.get_text(), 'Hello world again')

    def test_closed_textarea_is_unchanged(self):
        extractor = _HTMLTextExtractor(html_rcdata=True)
        extractor.feed('<html><body><textarea>Hello world</textarea>')
        self.assertEqual(extractor.get_text(), 'Hello world')


if __name__ == '__main__':
    unittest.main()
