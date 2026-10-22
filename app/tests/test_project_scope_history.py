"""Retained history remains immutable and verified; empty browsing creates nothing."""
from pathlib import Path
import tempfile
import unittest
import wave
from audio_take_history import AudioTakeHistory
from project_scope_history import get_scope_history_plan
from take_retention import make_retained_take
from book_state_transaction import get_book_operation_token


class ScopeHistoryTests(unittest.TestCase):
    def test_absent_history_does_not_initialize_archive_or_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            plan = get_scope_history_plan(root, 'a'*64, ['retained'])
            self.assertEqual([], plan['retained_versions'])
            self.assertFalse(plan['manifest']['exists'])
            self.assertEqual([], list(root.iterdir()))

    def test_retained_and_removed_versions_preserve_manifest_and_changed_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); path = root/'selected.wav'
            with wave.open(str(path), 'wb') as out:
                out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000)
                out.writeframes(b'\x01\x00'*800)
            history = AudioTakeHistory(root)
            first = history.capture(path, 'a'*64, 'retained', {'text': 'Synthetic retained line.'})
            history.capture(path, 'a'*64, 'removed', {'text': 'Synthetic removed line.'})
            before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            plan = get_scope_history_plan(root, 'a'*64, ['retained'])
            self.assertEqual([first['version']], [r['version'] for r in plan['retained_versions']])
            self.assertEqual((1, 0), (plan['preserved_other_records'], plan['purged_records']))
            self.assertEqual(1, len(plan['preserved_payloads']))
            self.assertEqual(before, {p.relative_to(root).as_posix(): p.read_bytes()
                                    for p in root.rglob('*') if p.is_file()})
            archived = root/plan['retained_versions'][0]['path']
            raw = archived.read_bytes(); archived.write_bytes(raw[:-2] + b'\x02\x00')
            with self.assertRaisesRegex(ValueError, 'saved manifest'):
                get_scope_history_plan(root, 'a'*64, ['retained'])

    def test_retained_predecessor_slot_is_fenced_to_original_file_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); path = root/'prior.wav'; path.write_bytes(b'prior encoded bytes')
            row = {'uid':'retained', 'text':'Synthetic retained line.', 'audio_path':'prior.wav'}
            row['_retained_take'] = make_retained_take(root, row)
            token = get_book_operation_token(root)
            plan = get_scope_history_plan(root, token, ['retained'], retained_rows=[row])
            self.assertEqual('prior.wav', plan['predecessors'][0]['path'])
            path.write_bytes(b'replacement encoded bytes')
            with self.assertRaisesRegex(ValueError, 'source slot changed'):
                get_scope_history_plan(root, token, ['retained'], retained_rows=[row])
