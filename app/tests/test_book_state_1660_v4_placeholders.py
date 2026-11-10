"""Protocol v4: removals and creations publish through a journaled placeholder (#1660).

A removal swaps its target with a same-type workspace placeholder and deletes
only the object whose nonce proves it ours; a creation stages a complete
placeholder in the private workspace before the journal, publishes it at the
target with one no-overwrite rename, then swaps it with the staged
replacement. These tests enumerate every crash point of both directions for
file and directory targets, then prove the next admission reaches a
consistent book without deleting anything that is not ours. Writer races fix
the two consequences that matter: a writer's object is never swapped out and
a placeholder is never deleted without its own proof.
"""
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import book_placeholder
import book_state_transaction as books
import source_upload_publication as publication


class _Crash(BaseException):
    """A crash injected at a protocol seam; not an Exception on purpose."""


POSIX_ONLY = unittest.skipIf(os.name == 'nt', 'POSIX exchange publication')


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _snapshot(root):
    """Book target content only: no locks, journal, workspace or evidence."""
    result = {}
    for path in Path(root).rglob('*'):
        if not path.is_file() or path.name.endswith('.lock'):
            continue
        relative = path.relative_to(root)
        if any(part.startswith('.book-switch-') for part in relative.parts):
            continue
        if relative.name in (books.JOURNAL, books.JOURNAL + '.lock'):
            continue
        if relative.name.startswith(books.JOURNAL + '.new-'):
            continue
        result[relative.as_posix()] = path.read_bytes()
    return result


@POSIX_ONLY
class BookStateV4PlaceholderTests(unittest.TestCase):

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    # --- fixtures ---------------------------------------------------------

    def _book(self):
        """A replacement row, a created file, a removed file and a dir."""
        values = {'part-0.json': b'original-0'}
        (self.root / 'part-1.json').write_bytes(b'original-1')
        (self.root / 'chapters').mkdir()
        (self.root / 'chapters' / 'one.wav').write_bytes(b'chapter bytes')
        for name, data in values.items():
            (self.root / name).write_bytes(data)
        return {'replace': {'part-0.json': b'replacement-0'},
                'create': {'brand-new.json': b'created'},
                'remove_file': ['part-1.json'],
                'remove_dir': ['chapters']}

    def _apply(self, values, removals=()):
        with books.ensure_book_state(str(self.root)):
            books.apply_book_state_locked(str(self.root), dict(values), list(removals))

    @staticmethod
    def _stream(data):
        return books.BookStateStream(io.BytesIO(data), len(data), _sha(data))

    def _capture_journals(self):
        records = []
        original = books._save_journal
        def record(root, data, **kwargs):
            result = original(root, data, **kwargs)
            records.append(json.loads(json.dumps(data)))
            return result
        return records, patch.object(books, '_save_journal', side_effect=record)

    def _crash_after(self, original, ordinal, skip=None):
        state = {'count': 0}
        def wrapper(*args, **kwargs):
            if skip is not None and skip(*args, **kwargs):
                return original(*args, **kwargs)
            result = original(*args, **kwargs)
            state['count'] += 1
            if state['count'] == ordinal:
                raise _Crash(f'crash after call {ordinal}')
            return result
        return wrapper

    @staticmethod
    def _skip_probe(source, *args, **kwargs):
        """The capability probe exchanges inside our workspace, not a target."""
        return str(source).startswith('.exchange-probe-')

    def _crash_after_exchange(self, ordinal):
        return self._crash_after(publication.exchange_source_upload_names, ordinal,
                                 skip=self._skip_probe)

    def _disable_forward_recovery(self):
        """Emulate process death instead of the handler's in-process recovery."""
        return patch.object(books, 'recover_book_state_locked', lambda *a, **k: None)

    def _leave_workspace(self):
        """Leave the pre-journal workspace exactly as a killed process would."""
        return (
            patch.object(books, '_delete_unpublished_book_placeholders', lambda *a, **k: None),
            patch.object(books, '_cleanup_unpublished_stream_workspace', lambda *a, **k: None),
        )

    def _crash_apply(self, values, removals=(), *, keep_workspace=False):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(self._disable_forward_recovery())
            if keep_workspace:
                for manager in self._leave_workspace():
                    stack.enter_context(manager)
            with self.assertRaises(_Crash):
                self._apply(values, removals)

    def _assert_recovered(self, expected):
        for _repeat in range(2):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(expected, _snapshot(self.root))
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual([], list(self.root.glob('.book-switch-*')))
        self.assertEqual([], list(self.root.glob('.exchange-probe-*')))
        # A completed recovery proves and deletes its own slots; evidence that
        # had to be quarantined means cleanup refused what it should own.
        self.assertEqual([], list(self.root.glob(books.QUARANTINE_PREFIX + '*')))

    def _journal(self):
        return json.loads((self.root / books.JOURNAL).read_text())

    def _workspace(self):
        return self.root / self._journal()['workspace']

    # --- removals ---------------------------------------------------------

    def test_file_removal_crashes_at_every_step_are_restored(self):
        original = {'part-1.json': b'original-1'}
        points = ('journal', 'exchange', 'delete', 'committed', 'cleanup', 'unlink')
        for point in points:
            with self.subTest(point=point):
                shutil.rmtree(self.root)
                self.root.mkdir()
                (self.root / 'part-1.json').write_bytes(b'original-1')
                expected = ({} if point in ('committed', 'cleanup', 'unlink')
                        else original)
                if point == 'journal':
                    with patch.object(books, '_save_journal',
                                      side_effect=self._crash_after(books._save_journal, 1)):
                        self._crash_apply({}, ['part-1.json'])
                    self.assertEqual('publishing', self._journal()['phase'])
                    self.assertTrue((self._workspace() / 'old-0').exists())
                elif point == 'exchange':
                    with patch.object(publication, 'exchange_source_upload_names',
                                      side_effect=self._crash_after_exchange(1)):
                        self._crash_apply({}, ['part-1.json'])
                    self.assertEqual('publishing', self._journal()['phase'])
                    # T is our placeholder, not absent: readers are fenced and
                    # recovery still owns the name.
                    self.assertTrue(book_placeholder.is_book_placeholder(
                        self.root / 'part-1.json'))
                elif point == 'delete':
                    with patch.object(books, '_delete_book_placeholder_at',
                                      side_effect=self._crash_after(
                                          books._delete_book_placeholder_at, 1)):
                        self._crash_apply({}, ['part-1.json'])
                    self.assertFalse((self.root / 'part-1.json').exists())
                    self.assertTrue((self._workspace() / 'old-0').exists())
                elif point == 'committed':
                    with patch.object(books, '_save_journal',
                                      side_effect=self._crash_after(books._save_journal, 2)):
                        self._crash_apply({}, ['part-1.json'])
                    self.assertEqual('committed', self._journal()['phase'])
                elif point == 'cleanup':
                    with patch.object(books, '_cleanup_validated_byte_workspace',
                                      side_effect=_Crash('crash during cleanup')):
                        self._crash_apply({}, ['part-1.json'])
                    self.assertEqual('committed', self._journal()['phase'])
                else:
                    with patch.object(books, '_unlink_owned_book_journal',
                                      side_effect=_Crash('crash before unlink')):
                        self._crash_apply({}, ['part-1.json'])
                    self.assertEqual('committed', self._journal()['phase'])
                self._assert_recovered(expected)

    def test_directory_removal_crashes_at_every_step_are_restored(self):
        original = {'chapters/one.wav': b'chapter bytes'}
        for point in ('journal', 'exchange', 'delete', 'committed', 'cleanup'):
            with self.subTest(point=point):
                shutil.rmtree(self.root)
                self.root.mkdir()
                (self.root / 'chapters').mkdir()
                (self.root / 'chapters' / 'one.wav').write_bytes(b'chapter bytes')
                expected = ({} if point in ('committed', 'cleanup')
                        else original)
                if point == 'journal':
                    with patch.object(books, '_save_journal',
                                      side_effect=self._crash_after(books._save_journal, 1)):
                        self._crash_apply({}, ['chapters'])
                    # A directory removal's placeholder is itself a directory.
                    self.assertTrue((self._workspace() / 'old-0').is_dir())
                elif point == 'exchange':
                    with patch.object(publication, 'exchange_source_upload_names',
                                      side_effect=self._crash_after_exchange(1)):
                        self._crash_apply({}, ['chapters'])
                    # Our empty directory placeholder is at the target.
                    self.assertTrue((self.root / 'chapters').is_dir())
                    self.assertEqual([], list((self.root / 'chapters').iterdir()))
                    self.assertTrue((self._workspace() / 'old-0' / 'one.wav').exists())
                elif point == 'delete':
                    with patch.object(books, '_delete_book_placeholder_at',
                                      side_effect=self._crash_after(
                                          books._delete_book_placeholder_at, 1)):
                        self._crash_apply({}, ['chapters'])
                    self.assertFalse((self.root / 'chapters').exists())
                elif point == 'committed':
                    with patch.object(books, '_save_journal',
                                      side_effect=self._crash_after(books._save_journal, 2)):
                        self._crash_apply({}, ['chapters'])
                else:
                    with patch.object(books, '_cleanup_validated_byte_workspace',
                                      side_effect=_Crash('crash during cleanup')):
                        self._crash_apply({}, ['chapters'])
                self._assert_recovered(expected)

    def test_removal_placeholder_staged_before_the_journal_is_an_orphan_workspace(self):
        # D0: the staged placeholder exists but no target moved and no journal
        # was published. Recovery must not touch the target, and the orphan
        # workspace is the documented staging leak, never a completed removal.
        (self.root / 'part-1.json').write_bytes(b'original-1')
        with patch.object(books, '_create_book_placeholder',
                          side_effect=self._crash_after(books._create_book_placeholder, 1)):
            self._crash_apply({}, ['part-1.json'], keep_workspace=True)
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual({'part-1.json': b'original-1'}, _snapshot(self.root))
        workspaces = list(self.root.glob('.book-switch-*'))
        self.assertEqual(1, len(workspaces))
        [placeholder] = list(workspaces[0].iterdir())
        self.assertTrue(book_placeholder.is_book_placeholder(placeholder))

    # --- creations --------------------------------------------------------

    def test_creation_crashes_at_every_step_return_the_name_to_absent(self):
        for point in ('journal', 'placeholder', 'exchange', 'committed', 'cleanup'):
            with self.subTest(point=point):
                shutil.rmtree(self.root)
                self.root.mkdir()
                expected = ({'brand-new.json': b'created'}
                            if point in ('committed', 'cleanup') else {})
                if point == 'journal':
                    with patch.object(books, '_save_journal',
                                      side_effect=self._crash_after(books._save_journal, 1)):
                        self._crash_apply({'brand-new.json': b'created'})
                    self.assertEqual('publishing', self._journal()['phase'])
                    self.assertFalse((self.root / 'brand-new.json').exists())
                    self.assertTrue((self._workspace() / 'new-0').exists())
                elif point == 'placeholder':
                    with patch.object(books, '_create_book_placeholder_at',
                                      side_effect=self._crash_after(
                                          books._create_book_placeholder_at, 1)):
                        self._crash_apply({'brand-new.json': b'created'})
                    self.assertTrue(book_placeholder.is_book_placeholder(
                        self.root / 'brand-new.json'))
                    self.assertEqual(b'created', (self._workspace() / 'new-0').read_bytes())
                elif point == 'exchange':
                    with patch.object(publication, 'exchange_source_upload_names',
                                      side_effect=self._crash_after_exchange(1)):
                        self._crash_apply({'brand-new.json': b'created'})
                    self.assertEqual(b'created', (self.root / 'brand-new.json').read_bytes())
                    self.assertTrue(book_placeholder.is_book_placeholder(
                        self._workspace() / 'new-0'))
                elif point == 'committed':
                    with patch.object(books, '_save_journal',
                                      side_effect=self._crash_after(books._save_journal, 2)):
                        self._crash_apply({'brand-new.json': b'created'})
                    self.assertEqual('committed', self._journal()['phase'])
                else:
                    with patch.object(books, '_cleanup_validated_byte_workspace',
                                      side_effect=_Crash('crash during cleanup')):
                        self._crash_apply({'brand-new.json': b'created'})
                self._assert_recovered(expected)

    def test_creation_crash_after_staged_slot_write_before_publish_removes_only_our_slot(self):
        # The complete placeholder is staged in old-0 before the journal. A
        # crash after the journal but before its no-overwrite publish leaves
        # the target absent; recovery deletes only our nonce-proven slot.
        with patch.object(books, '_create_book_placeholder_at',
                          side_effect=_Crash('crash before placeholder publish')):
            self._crash_apply({'brand-new.json': b'created'})
        self.assertEqual('publishing', self._journal()['phase'])
        self.assertFalse((self.root / 'brand-new.json').exists())
        self.assertTrue(book_placeholder.is_book_placeholder(
            self._workspace() / 'old-0'))
        self.assertEqual(b'created', (self._workspace() / 'new-0').read_bytes())
        self._assert_recovered({})

    def test_creation_crash_after_placeholder_publish_recovers_to_absent(self):
        with patch.object(books, '_create_book_placeholder_at',
                          side_effect=self._crash_after(
                              books._create_book_placeholder_at, 1)):
            self._crash_apply({'brand-new.json': b'created'})
        self.assertTrue(book_placeholder.is_book_placeholder(
            self.root / 'brand-new.json'))
        self.assertEqual(2, len(self._journal()['entries'][0]
                                 ['target_placeholder']['identity']))
        self.assertFalse((self._workspace() / 'old-0').exists())
        self.assertEqual(b'created', (self._workspace() / 'new-0').read_bytes())
        self._assert_recovered({})

    def test_creation_placeholder_write_failure_leaves_no_target_or_partial(self):
        # ENOSPC while writing the slot must not leave a short file at the
        # target name; the failed slot is removed and the original error
        # surfaces rather than a cleanup refusal.
        def no_space(descriptor, data):
            raise OSError(errno.ENOSPC, 'No space left on device')
        with patch.object(books, '_write_book_placeholder_bytes', side_effect=no_space):
            with self.assertRaises(OSError) as raised:
                self._apply({'brand-new.json': b'created'})
        self.assertEqual(errno.ENOSPC, raised.exception.errno)
        self.assertIn('No space left on device', str(raised.exception))
        self.assertFalse((self.root / 'brand-new.json').exists())
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual([], list(self.root.glob('.book-switch-*')))

    def test_removal_placeholder_write_failure_surfaces_the_original_error(self):
        # The removal's staged placeholder is ours before its first byte is
        # written, so a failed write is cleaned up and the ENOSPC error is not
        # masked by a refusal to remove an unowned workspace entry.
        (self.root / 'part-1.json').write_bytes(b'original-1')
        def no_space(descriptor, data):
            raise OSError(errno.ENOSPC, 'No space left on device')
        with patch.object(books, '_write_book_placeholder_bytes', side_effect=no_space):
            with self.assertRaises(OSError) as raised:
                self._apply({}, ['part-1.json'])
        self.assertEqual(errno.ENOSPC, raised.exception.errno)
        self.assertIn('No space left on device', str(raised.exception))
        self.assertEqual({'part-1.json': b'original-1'}, _snapshot(self.root))
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual([], list(self.root.glob('.book-switch-*')))

    def test_removal_placeholder_failure_after_complete_write_is_cleaned_up(self):
        # A failure after the placeholder was fully written (the directory
        # fsync, say) must remove our own registered placeholder instead of
        # refusing it as unowned and masking the original error (#1660).
        (self.root / 'part-1.json').write_bytes(b'original-1')
        real = books._create_book_placeholder
        def fail_after_write(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError(errno.EIO, 'fixture placeholder directory fsync failed')
        with patch.object(books, '_create_book_placeholder', side_effect=fail_after_write):
            with self.assertRaises(OSError) as raised:
                self._apply({}, ['part-1.json'])
        self.assertIn('fixture placeholder directory fsync failed', str(raised.exception))
        self.assertEqual({'part-1.json': b'original-1'}, _snapshot(self.root))
        self.assertFalse((self.root / books.JOURNAL).exists())
        self.assertEqual([], list(self.root.glob('.book-switch-*')))

    def test_streamed_mixed_transaction_publishes_and_rolls_back(self):
        # A streamed transaction with a creation and a removal also publishes
        # through placeholders and uses the same cleanup rules (#1660).
        def prepare():
            shutil.rmtree(self.root)
            self.root.mkdir()
            (self.root / 'part-0.json').write_bytes(b'original-0')
            (self.root / 'part-1.json').write_bytes(b'original-1')
        def values():
            return {'part-0.json': self._stream(b'replacement-0'),
                    'brand-new.json': self._stream(b'created')}
        prepare()
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after_exchange(2)):
            with self._disable_forward_recovery():
                with self.assertRaises(_Crash):
                    with books.ensure_book_state(str(self.root)):
                        books.apply_book_state_locked(
                            str(self.root), values(), ['part-1.json'],
                            stream_byte_limit=1 << 20)
        self._assert_recovered({'part-0.json': b'original-0',
                                'part-1.json': b'original-1'})
        prepare()
        with books.ensure_book_state(str(self.root)):
            books.apply_book_state_locked(str(self.root), values(), ['part-1.json'],
                                          stream_byte_limit=1 << 20)
        self.assertEqual({'part-0.json': b'replacement-0',
                          'brand-new.json': b'created'}, _snapshot(self.root))
        self.assertFalse((self.root / 'part-1.json').exists())

    # --- mixed transactions ----------------------------------------------

    def test_mixed_replace_create_and_remove_roll_back_together(self):
        fixture = self._book()
        # Crash after the second row publication: the replacement and the
        # creation are published, the removals are not.
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after(
                              publication.exchange_source_upload_names, 2)):
            self._crash_apply({**fixture['replace'], **fixture['create']},
                              fixture['remove_file'] + fixture['remove_dir'])
        expected = {'part-0.json': b'original-0', 'part-1.json': b'original-1',
                    'chapters/one.wav': b'chapter bytes'}
        self._assert_recovered(expected)

    def test_mixed_replace_create_and_remove_publish_every_direction(self):
        fixture = self._book()
        records, recorder = self._capture_journals()
        with recorder:
            self._apply({**fixture['replace'], **fixture['create']},
                        fixture['remove_file'] + fixture['remove_dir'])
        self.assertEqual({4}, {record['version'] for record in records})
        self.assertEqual([4, 4], [record['version'] for record in records])
        self.assertEqual({'part-0.json': b'replacement-0',
                          'brand-new.json': b'created'}, _snapshot(self.root))
        self.assertFalse((self.root / 'part-1.json').exists())
        self.assertFalse((self.root / 'chapters').exists())
        self.assertEqual([], list(self.root.glob('.book-switch-*')))
        # A removal and a creation both stage their placeholder before the
        # journal, so both journal its identity; only the status of the target
        # (placeheld vs absent) distinguishes the two directions.
        removal = records[0]['entries'][2]
        creation = records[0]['entries'][1]
        self.assertEqual('file', removal['placeholder']['kind'])
        self.assertEqual(32, len(removal['placeholder']['nonce']))
        self.assertIsNotNone(removal['placeholder']['identity'])
        self.assertEqual(2, len(creation['target_placeholder']['identity']))
        self.assertEqual(32, len(creation['target_placeholder']['nonce']))

    def test_capability_probe_runs_inside_the_private_workspace(self):
        # A SIGKILL cannot run the probe's cleanup, so the probe's names must
        # never appear in the user's book root (follow-up (a) of #1880).
        worker = '''
import os,sys
import book_state_transaction as books
import source_upload_publication as publication
real=publication.exchange_source_upload_names
def die(source,*args,**kwargs):
 if str(source).startswith('.exchange-probe-'):
  os._exit(90)
 return real(source,*args,**kwargs)
publication.exchange_source_upload_names=die
with books.ensure_book_state(sys.argv[1]):
 books.apply_book_state_locked(sys.argv[1],{'brand-new.json':b'created'},[])
'''
        result = subprocess.run([sys.executable, '-c', worker, str(self.root)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(90, result.returncode, result.stderr)
        self.assertEqual([], list(self.root.glob('.exchange-probe-*')))
        self.assertFalse((self.root / 'brand-new.json').exists())
        [workspace] = list(self.root.glob('.book-switch-*'))
        self.assertTrue(list(workspace.glob('.exchange-probe-*')))

    # --- writer races -----------------------------------------------------

    def test_writer_creating_the_target_before_our_placeholder_publish_is_never_overwritten(self):
        writer = b'writer got here first'
        real = books._move
        created = []
        def collide(source, destination, **kwargs):
            if (not created and Path(source).name == 'old-0'
                    and Path(destination) == self.root / 'brand-new.json'):
                created.append(destination)
                destination.write_bytes(writer)
            return real(source, destination, **kwargs)
        with patch.object(books, '_move', side_effect=collide):
            with self.assertRaises(ValueError) as raised:
                self._apply({'brand-new.json': b'created'})
        # The no-overwrite rename fails with EEXIST before the target changes;
        # recovery then refuses the writer's object as the operator's decision.
        self.assertEqual([self.root / 'brand-new.json'], created)
        self.assertIsInstance(raised.exception.__cause__, FileExistsError)
        self.assertEqual(writer, (self.root / 'brand-new.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'A book file changed during the switch'):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(writer, (self.root / 'brand-new.json').read_bytes())
        self.assertEqual(b'created', (self._workspace() / 'new-0').read_bytes())
        self.assertTrue(book_placeholder.is_book_placeholder(
            self._workspace() / 'old-0'))
        self.assertEqual([], list(self.root.glob(books.QUARANTINE_PREFIX + '*')))

    def test_writer_writing_into_a_directory_placeholder_is_refused_and_kept(self):
        (self.root / 'chapters').mkdir()
        (self.root / 'chapters' / 'one.wav').write_bytes(b'chapter bytes')
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after_exchange(1)):
            self._crash_apply({}, ['chapters'])
        self.assertEqual([], list((self.root / 'chapters').iterdir()))
        (self.root / 'chapters' / 'writer.txt').write_bytes(b'writer file')
        with self.assertRaisesRegex(ValueError, 'A book file changed during the switch'):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(b'writer file',
                         (self.root / 'chapters' / 'writer.txt').read_bytes())
        self.assertTrue((self._workspace() / 'old-0' / 'one.wav').exists())
        self.assertTrue((self.root / books.JOURNAL).exists())

    def test_writer_deleting_a_directory_placeholder_restores_the_original(self):
        (self.root / 'chapters').mkdir()
        (self.root / 'chapters' / 'one.wav').write_bytes(b'chapter bytes')
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after_exchange(1)):
            self._crash_apply({}, ['chapters'])
        (self.root / 'chapters').rmdir()
        self._assert_recovered({'chapters/one.wav': b'chapter bytes'})

    def test_writer_replacing_our_placeholder_is_never_swapped_out(self):
        # The writer's object is a *valid-format* placeholder with another
        # nonce: only the exact nonce proves ours, so identity and size alone
        # must not let the exchange adopt or delete it (#1660).
        writer = book_placeholder.get_book_placeholder_bytes('b' * 32)
        real = books._exchange
        state = {'done': False}
        def replace_placeholder(source, destination, **kwargs):
            if not state['done']:
                state['done'] = True
                (self.root / 'brand-new.json').write_bytes(writer)
            return real(source, destination, **kwargs)
        with patch.object(books, '_exchange', side_effect=replace_placeholder):
            with self.assertRaises(ValueError):
                self._apply({'brand-new.json': b'created'})
        self.assertEqual(writer, (self.root / 'brand-new.json').read_bytes())
        with self.assertRaisesRegex(ValueError, 'A book file changed during the switch'):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(writer, (self.root / 'brand-new.json').read_bytes())
        self.assertEqual(b'created', (self._workspace() / 'new-0').read_bytes())
        self.assertTrue((self.root / books.JOURNAL).exists())

    def test_edited_removed_original_in_the_slot_is_never_restored(self):
        (self.root / 'part-1.json').write_bytes(b'old-bytes')
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after_exchange(1)):
            self._crash_apply({}, ['part-1.json'])
        slot = self._workspace() / 'old-0'
        info = slot.stat()
        slot.write_bytes(b'NEW-bytes')   # same size, same inode
        os.utime(slot, ns=(info.st_atime_ns, info.st_mtime_ns))
        with self.assertRaisesRegex(ValueError, 'A book file changed during the switch'):
            with books.ensure_book_state(str(self.root)):
                pass
        self.assertEqual(b'NEW-bytes', slot.read_bytes())
        self.assertTrue(book_placeholder.is_book_placeholder(self.root / 'part-1.json'))
        self.assertTrue((self.root / books.JOURNAL).exists())

    def test_writer_deleting_the_placeholder_is_rolled_back_from_the_original(self):
        (self.root / 'part-1.json').write_bytes(b'original-1')
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after_exchange(1)):
            self._crash_apply({}, ['part-1.json'])
        (self.root / 'part-1.json').unlink()   # T=P, old-i=O became T absent
        self._assert_recovered({'part-1.json': b'original-1'})

    # --- operator surfaces ------------------------------------------------

    def test_placeholder_is_reported_and_the_operator_quarantine_keeps_the_writer(self):
        writer = b'writer replaced the placeholder'
        real = books._exchange
        def replace_placeholder(source, destination, **kwargs):
            (self.root / 'brand-new.json').write_bytes(writer)
            return real(source, destination, **kwargs)
        with patch.object(books, '_exchange', side_effect=replace_placeholder):
            with self.assertRaises(ValueError):
                self._apply({'brand-new.json': b'created'})
        self.assertEqual('valid', books.get_book_journal_status(self.root))
        report = books.inspect_book_recovery_state(self.root)
        self.assertEqual(4, report['journal']['version'])
        [entry] = report['entries']
        self.assertIn('target_placeholder', entry['expected'])
        self.assertFalse(entry['current']['placeholder'])   # the writer's file
        import book_state_recovery
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(0, book_state_recovery.main(
                ['--data-dir', str(self.root), 'quarantine', '--accept-current-files']))
        self.assertEqual(writer, (self.root / 'brand-new.json').read_bytes())
        self.assertFalse((self.root / books.JOURNAL).exists())

    def test_inspect_marks_a_target_that_holds_our_placeholder(self):
        (self.root / 'part-1.json').write_bytes(b'original-1')
        with patch.object(publication, 'exchange_source_upload_names',
                          side_effect=self._crash_after_exchange(1)):
            self._crash_apply({}, ['part-1.json'])
        report = books.inspect_book_recovery_state(self.root)
        [entry] = report['entries']
        self.assertTrue(entry['current']['placeholder'])
        self.assertEqual('file', entry['expected']['placeholder']['kind'])


    def test_failed_write_cleanup_never_deletes_a_writer_object_reusing_our_inode(self):
        # After a failed write, cleanup compares the slot name's identity with
        # the object we created. That is only sound while our descriptor is
        # open: once closed, the inode can be freed and handed to a writer who
        # recreates the name (ext4 does this at once), and the check would
        # match the writer's file. Whether reuse happens depends on the
        # filesystem, so pin the property that makes it impossible everywhere:
        # cleanup runs while our descriptor still pins the inode.
        directory_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory_fd)
        ours, observed = [], []
        def disk_full(descriptor, data):
            ours.append(descriptor)
            raise OSError(errno.ENOSPC, 'No space left on device')
        remove = books._remove_book_placeholder_by_identity
        def check_descriptor_still_open(*args, **kwargs):
            try:
                os.fstat(ours[0])
                observed.append('open')
            except OSError:
                observed.append('closed')
            return remove(*args, **kwargs)
        with patch.object(books, '_write_book_placeholder_bytes', side_effect=disk_full), \
                patch.object(books, '_remove_book_placeholder_by_identity',
                             side_effect=check_descriptor_still_open):
            with self.assertRaises(OSError) as raised:
                books._create_book_placeholder(directory_fd, 'old-0', 'file', '0' * 32)
        self.assertEqual(errno.ENOSPC, raised.exception.errno)
        self.assertEqual(['open'], observed)
        self.assertFalse((self.root / 'old-0').exists())   # our partial object is gone
        with self.assertRaises(OSError):
            os.fstat(ours[0])                              # and the descriptor closed

if __name__ == '__main__':
    unittest.main()
