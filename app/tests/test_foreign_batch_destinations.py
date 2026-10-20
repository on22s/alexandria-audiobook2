"""A foreign saver cannot replace a book queued for the batch's backward pass."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from routers import script
from review_script import _checkpoint_path
from tests import test_saved_destination_ownership as fixtures


SAVE_IN_FOREIGN_SERVER = '''import json,sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import scripts_library as library
from utils import file_lock
root=Path(sys.argv[1]);name=sys.argv[2]
for module in (core,library):
 module.DATA_DIR=str(root);module.SCRIPTS_DIR=str(root/'scripts')
 module.SCRIPT_PATH=str(root/'annotated_script.json');module.VOICE_CONFIG_PATH=str(root/'voice_config.json')
with file_lock(root/'scripts/book.json',timeout=0):pass
api=FastAPI();api.include_router(library.router)
with TestClient(api) as client:
 response=client.post('/api/scripts/save',json={'name':name})
 print(json.dumps({'unlocked_book':True,'status':response.status_code}))
'''


class ForeignBatchDestinationTests(unittest.TestCase):
    def save_in_foreign_server(self, root, name):
        result = subprocess.run([sys.executable, '-c', SAVE_IN_FOREIGN_SERVER, str(root), name],
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(0, result.returncode, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def test_forward_reviewed_book_is_reserved_while_foreign_batch_reviews_next_book(self):
        fixture = fixtures.SavedDestinationOwnershipTests()
        with fixture.fixture() as (root, client, states, helper):
            (root / 'scripts/second.json').write_text('[{"speaker":"Second","text":"Second book."}]')
            checkpoint = Path(_checkpoint_path(str(root / 'scripts/book.json')))
            background = fixture.start(('book', 'second'), dedupe_speakers=True,
                                       find_nicknames=True, bidirectional=True)
            observations, snapshots, passes = [], [], []
            def stream(command, *_args, **_kwargs):
                if not any(str(arg).endswith('review_script.py') for arg in command):
                    return 0, []
                source = Path(command[command.index('--input') + 1])
                phase = states['batch_review']['current_pass']
                passes.append((source.stem, phase))
                if source.stem == 'book' and phase == 'fwd':
                    checkpoint.write_bytes(b'forward checkpoint to preserve')
                if source.stem == 'second' and phase == 'fwd':
                    self.assertEqual('done', states['batch_review']['tasks'][0]['status'])
                    self.assertEqual([('book', 'fwd'), ('second', 'fwd')], passes)
                    before = helper.artifacts(root)
                    for name in ('book', 'distinct'):
                        observations.append(self.save_in_foreign_server(root, name))
                    after = {name: data for name, data in helper.artifacts(root).items()
                             if not name.startswith('distinct.')}
                    snapshots.append((before, after))
                return 0, fixtures.CLEAN
            with patch.object(script, '_stream_subprocess_to_logs', side_effect=stream):
                asyncio.run(background())
            self.assertEqual([('book', 'fwd'), ('second', 'fwd'),
                              ('second', 'bwd'), ('book', 'bwd')], passes)
            self.assertEqual([True, True], [row['unlocked_book'] for row in observations])
            self.assertEqual([409, 200], [row['status'] for row in observations])
            self.assertEqual(snapshots[0][0], snapshots[0][1])
            self.assertEqual(b'forward checkpoint to preserve', checkpoint.read_bytes())
            self.assertEqual(200, self.save_in_foreign_server(root, 'book')['status'])
            self.assertFalse(checkpoint.exists())
