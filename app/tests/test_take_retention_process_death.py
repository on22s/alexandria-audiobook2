"""Kill real writers at durable boundaries, then retry from newly loaded state."""
import copy
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest

from tests import test_automatic_take_history as automatic_cases
from audio_take_history import AudioTakeHistory
from project import ProjectManager
from take_retention import KEY


WORKER = r'''
import copy, os, sys
from pathlib import Path
from unittest.mock import patch
from audio_take_history import AudioTakeHistory
from project import ProjectManager
import project
from take_retention import KEY
root, boundary, journal = Path(sys.argv[1]), sys.argv[2], sys.argv[3] == '1'
manager = ProjectManager(str(root))
if journal:
    active = manager._get_status_journal_locked()
    active.ensure_writable()
    manager._active_journal_identity = copy.deepcopy(active.book_identity)
saved = AudioTakeHistory._save
def save(self, document):
    result = saved(self, document)
    if document['records']:
        state = document['records'][-1]['state']
        if boundary == 'reservation' and state == 'pending': os._exit(91)
        if boundary == 'ready-copy' and state == 'ready': os._exit(92)
    return result
update = manager._update_chunk_fields_by_uid
def commit(*args, **kwargs):
    result = update(*args, **kwargs)
    if result is not None:
        if boundary == 'chunk-hold' and kwargs.get(KEY, {}).get('archive'): os._exit(93)
        if boundary == 'selected-metadata' and kwargs.get('status') == 'done': os._exit(95)
    return result
publish = project.publish_audio_output
def install(source, target, *args, **kwargs):
    result = publish(source, target, *args, **kwargs)
    if boundary == 'slot-install' and '.slot-' in str(target): os._exit(94)
    return result
with patch.object(AudioTakeHistory, '_save', save), \
        patch.object(manager, '_update_chunk_fields_by_uid', side_effect=commit), \
        patch('project.publish_audio_output', side_effect=install), \
        patch('project._export_audio_segment', side_effect=RuntimeError('force actual WAV fallback')):
    captured = manager.load_chunks()[0]
    manager._update_generation_fields(captured, status='generating')
    manager._publish_generated_chunk_audio(captured, str(root/'fresh.wav'), 'NARRATOR')
raise AssertionError('Requested durable boundary was never reached')
'''


class TakeRetentionProcessDeathTests(unittest.TestCase):
    fixture = automatic_cases.AutomaticTakeHistoryTests.fixture
    policy = automatic_cases.AutomaticTakeHistoryTests.policy

    def test_actual_process_death_preserves_original_take_and_allows_explicit_retry(self):
        boundaries = [('reservation', 91), ('ready-copy', 92), ('chunk-hold', 93),
                      ('slot-install', 94), ('selected-metadata', 95)]
        for journal in (False, True):
            for boundary, code in boundaries:
                with self.subTest(journal=journal, boundary=boundary), \
                        self.fixture(journal, enabled=False) as (root, manager, previous, fresh):
                    original = previous.read_bytes()
                    slot = root / f"voicelines/voiceline_line_{hashlib.sha256(b'line').hexdigest()[:16]}.slot-0.wav"
                    previous.rename(slot)
                    rows = manager.load_chunks(); rows[0]['audio_path'] = slot.relative_to(root).as_posix()
                    manager.save_chunks(rows); self.policy(manager, True)
                    if journal:
                        active = manager._get_status_journal_locked(); active.ensure_writable()
                        manager._active_journal_identity = copy.deepcopy(active.book_identity)
                    manager.update_chunk(0, {'text':'Edited after the saved performance.'})
                    child = subprocess.run([sys.executable, '-c', WORKER, str(root), boundary, str(int(journal))],
                        capture_output=True, text=True, timeout=30, cwd=Path(__file__).resolve().parent.parent)
                    self.assertEqual(child.returncode, code, child.stdout + child.stderr)
                    # Load a new manager: no cache from the dead writer may act as proof.
                    recovered = ProjectManager(str(root))
                    if journal:
                        active = recovered._get_status_journal_locked(); active.ensure_writable()
                        recovered._active_journal_identity = copy.deepcopy(active.book_identity)
                    row = recovered.load_chunks()[0]
                    history = AudioTakeHistory(root); listing = history.list()
                    self.assertEqual(listing['total'], 1)
                    record = listing['records'][0]
                    if boundary == 'reservation':
                        self.assertEqual(record['state'], 'pending')
                        self.assertEqual(listing['usage']['bytes'], len(original))
                        self.assertEqual(slot.read_bytes(), original)
                        self.assertIsNone(row[KEY]['archive'])
                        history.change(record['version'], record['sha256'], 'purge', lambda path: False,
                            book_token=record['book_token'], uid='line')
                    else:
                        self.assertEqual(record['state'], 'ready')
                        archived = root / f"take_history/{record['version']}.wav"
                        self.assertEqual(archived.read_bytes(), original)
                        if boundary == 'selected-metadata':
                            self.assertEqual(row['status'], 'done'); self.assertIsNone(row[KEY])
                            self.assertNotEqual(row['audio_revision'], 'original-revision')
                            self.assertEqual((root/row['audio_path']).read_bytes(), fresh.read_bytes())
                            continue
                        if boundary in ('chunk-hold', 'slot-install'):
                            self.assertEqual(row[KEY]['archive']['version'], record['version'])
                            # The old slot may be reused after interruption. A retry
                            # must verify its committed archive, not relabel these bytes.
                            slot.write_bytes(fresh.read_bytes())
                        else:
                            self.assertIsNone(row[KEY]['archive'])
                            self.assertEqual(slot.read_bytes(), original)
                    result = recovered._publish_generated_chunk_audio(row, str(fresh), 'NARRATOR')
                    completed = ProjectManager(str(root)).load_chunks()[0]
                    self.assertIsNone(completed[KEY]); self.assertEqual(completed['status'], 'done')
                    self.assertEqual((root/result).read_bytes(), fresh.read_bytes())
                    final = history.list(); self.assertEqual(final['total'], 1)
                    archived = root / f"take_history/{final['records'][0]['version']}.wav"
                    self.assertEqual(archived.read_bytes(), original)
