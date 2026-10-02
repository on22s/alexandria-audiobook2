import builtins
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import scripts_library
from tests.test_support import assert_directory_payload_names


class RepairSnapshotTests(unittest.TestCase):
    def request_preview(self, client, family):
        url = f'/api/scripts/book/repair/{family}/preview'
        if family == 'deterministic':
            return client.post(url, json={'source_filename': 'book.txt'})
        return client.get(url)

    def test_real_previews_use_one_snapshot_when_script_replaced_after_first_read(self):
        original = [{'speaker': 'NARRATOR', 'text': 'Take саге.', 'instruct': ' Calm. '},
                    {'speaker': 'ALICE', 'text': 'Come here.', 'instruct': ' Neutral. '},
                    {'speaker': 'SUBARU', 'text': 'Subaru looked toward the door.', 'instruct': 'Calm.'}]
        replacement = [{'speaker': 'BOB', 'text': 'A different version.', 'instruct': 'neutral'}]
        api = FastAPI()
        api.include_router(scripts_library.router)
        for family in ('deterministic', 'speakers', 'content'):
            with self.subTest(family=family), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                root = Path(tmp)
                script = root / 'book.json'
                source = root / 'book.txt'
                source.write_text('Take саге. Come here.', encoding='utf-8')
                first = json.dumps(original).encode()
                second = json.dumps(replacement).encode()
                script.write_bytes(first)
                reads = []
                @contextmanager
                def replacing_open(path, mode='r', *args, **kwargs):
                    with builtins.open(path, mode, *args, **kwargs) as handle:
                        if os.fspath(path) == str(script) and mode == 'rb':
                            def read(*read_args):
                                captured = handle.read(*read_args)
                                reads.append(captured)
                                temp = root / 'replacement.tmp'
                                temp.write_bytes(second)
                                os.replace(temp, script)
                                return captured
                            yield SimpleNamespace(read=read)
                        else:
                            yield handle
                with patch.object(scripts_library, 'SCRIPTS_DIR', str(root)), \
                     patch.object(scripts_library, 'UPLOADS_DIR', str(root)):
                    expected = self.request_preview(client, family)
                    self.assertEqual(200, expected.status_code, expected.text)
                    with patch.object(scripts_library, 'open', side_effect=replacing_open, create=True):
                        actual = self.request_preview(client, family)
                    self.assertEqual(200, actual.status_code, actual.text)
                    self.assertEqual(expected.json(), actual.json())
                    self.assertEqual([first], reads)
                    self.assertEqual(second, script.read_bytes())
                    body = {'expected_sha256': actual.json()['sha256']}
                    if family == 'deterministic':
                        body['source_filename'] = 'book.txt'
                    applied = client.post(f'/api/scripts/book/repair/{family}/apply', json=body)
                    self.assertEqual(409, applied.status_code, applied.text)
                    self.assertEqual(second, script.read_bytes())
                    assert_directory_payload_names(self, root, ['book.json', 'book.txt'], lock_targets=[root / '.active_book_transaction.json', script, *scripts_library._get_saved_book_companions(str(script))])

    def test_invalid_script_encodings_json_and_nonarrays_keep_422_and_bytes(self):
        api = FastAPI()
        api.include_router(scripts_library.router)
        for payload in (b'\xff', b'{', b'null', b'{}', b'"text"', b'\xef\xbb\xbf[]'):
            for family in ('deterministic', 'speakers', 'content'):
                with self.subTest(payload=payload, family=family), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                    root = Path(tmp)
                    script = root / 'book.json'
                    script.write_bytes(payload)
                    (root / 'book.txt').write_text('source')
                    with patch.object(scripts_library, 'SCRIPTS_DIR', str(root)), \
                         patch.object(scripts_library, 'UPLOADS_DIR', str(root)):
                        result = self.request_preview(client, family)
                    self.assertEqual(422, result.status_code, result.text)
                    self.assertEqual(payload, script.read_bytes())
                    assert_directory_payload_names(self, root, ['book.json', 'book.txt'], lock_targets=[root / '.active_book_transaction.json', script, *scripts_library._get_saved_book_companions(str(script))])
