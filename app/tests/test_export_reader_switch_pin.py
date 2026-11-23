"""A standard ASGI read paused before descriptor open cannot cross a switch."""
import asyncio
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from book_state_transaction import apply_book_input_selection
from routers import editor
from tests.test_export_book_transition_readers import ExportReaderFixture


class StandardReaderSwitchTests(ExportReaderFixture, unittest.TestCase):
    def setUp(self):
        self.make_root()

    def test_read_paused_before_open_refuses_after_book_switch(self):
        app = FastAPI()
        app.include_router(editor.router)
        real = editor.open_current_export_reader
        ready, release = threading.Event(), threading.Event()

        def paused(root, path, **kwargs):
            ready.set()
            if not release.wait(10):
                raise AssertionError('switch did not release the reader')
            return real(root, path, **kwargs)

        async def run():
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                    base_url='http://fixture') as client:
                task = asyncio.create_task(client.get('/api/audiobook'))
                try:
                    self.assertTrue(await asyncio.to_thread(ready.wait, 10))
                    apply_book_input_selection(self.root,
                        str(self.root / 'new-source.txt'), 'newbook')
                    self.paths['mp3'].write_bytes(b'NEW BOOK BYTES')
                finally:
                    release.set()
                response = await task
                self.assertEqual(409, response.status_code)
                self.assertEqual(b'NEW BOOK BYTES', self.paths['mp3'].read_bytes())

        with patch.object(editor, 'open_current_export_reader', paused):
            asyncio.run(run())
