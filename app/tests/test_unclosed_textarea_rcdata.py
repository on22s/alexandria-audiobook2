"""Regression test for #2439: an unclosed <textarea>'s trailing RCDATA must not be dropped.

Python 3.14's ``html.parser`` holds a short trailing ``feed()`` in
``self._pending`` until ``close()``. The extractor's feed-only text accessors
never call ``close()``, so ``_finalize_pending_rcdata`` must drain that buffer
too or the last chunk of an unclosed ``<textarea>`` is silently lost.
"""

import unittest

from routers import script


class UnclosedTextareaRcdataTest(unittest.TestCase):
    def test_chunked_feed_does_not_drop_the_rcdata_suffix(self):
        # The three-call shape matters on 3.14: the second feed makes no
        # progress, so the parser waits for the buffer to double and the
        # third, smaller feed lands in _pending instead of rawdata.
        extractor = script._HTMLTextExtractor(html_rcdata=True)
        extractor.feed('<html><body><textarea>Hello')
        extractor.feed(' world')
        extractor.feed(' again')
        self.assertEqual(extractor.get_text(), 'Hello world again')

    def test_buffered_pending_tail_is_finalized(self):
        # CI runs 3.10, which has no _pending, so reproduce 3.14's buffered
        # state explicitly. Without the drain in _finalize_pending_rcdata the
        # tail is dropped here on every interpreter.
        extractor = script._HTMLTextExtractor(html_rcdata=True)
        extractor.feed('<html><body><textarea>Hello world')
        self.assertEqual(extractor.rawdata, 'Hello world')
        extractor._pending = [' again']
        if hasattr(extractor, '_pending_len'):
            extractor._pending_len = len(' again')
        self.assertEqual(extractor.get_text(), 'Hello world again')
        self.assertEqual(extractor._pending, [])
        self.assertEqual(getattr(extractor, '_pending_len', 0), 0)

    def test_closed_textarea_is_unchanged(self):
        extractor = script._HTMLTextExtractor(html_rcdata=True)
        extractor.feed('<html><body><textarea>Hello world</textarea>')
        self.assertEqual(extractor.get_text(), 'Hello world')


if __name__ == '__main__':
    unittest.main()
