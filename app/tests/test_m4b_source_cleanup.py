"""An owned temporary cover cleanup failure must not change export status."""
from contextlib import ExitStack
import hashlib
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import m4b_encode
from tests import test_m4b_source_publication as source_fixture
from tts import ExportCancelled


class M4bSourceCleanupTests(unittest.TestCase):
    def test_cleanup_oserror_preserves_success_original_error_and_cancellation(self):
        fixture = source_fixture.M4bSourcePublicationTests()
        original_rmtree = shutil.rmtree
        for fault in ('success', 'exception', 'cancel', 'encoder_failure'):
            with self.subTest(fault=fault), fixture.fixture() as (root, manager, row):
                previous = fixture.prior(root, manager)
                choice = fixture.selection(root)
                remaining = []
                callbacks = []
                original_error = ValueError('synthetic original encoder failure')

                cleanup_error = PermissionError('synthetic cover cleanup failure')
                def remove_tree(path, *args, **kwargs):
                    owned = Path(path)
                    if owned.parent == root and owned.name.startswith('.m4b-source-cover-'):
                        remaining.append(owned)
                        raise cleanup_error
                    return original_rmtree(path, *args, **kwargs)

                try:
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(shutil, 'rmtree', side_effect=remove_tree))
                        if fault != 'success':
                            side_effect = original_error if fault == 'exception' else (
                                ExportCancelled() if fault == 'cancel' else None)
                            stack.enter_context(patch.object(m4b_encode, 'encode_m4b',
                                side_effect=side_effect, return_value=(1, 'synthetic encoder failure')))
                        with self.assertLogs('AlexandriaUI', level='WARNING') as logs:
                            if fault == 'exception':
                                with self.assertRaises(ValueError) as raised:
                                    manager.merge_m4b(source_cover=choice)
                                self.assertIs(original_error, raised.exception)
                            else:
                                result = manager.merge_m4b(source_cover=choice,
                                    export_quality_callback=callbacks.append)
                                expected = {'success': (True, 'audiobook.m4b'),
                                    'cancel': (False, 'Export cancelled'),
                                    'encoder_failure': (False, 'FFmpeg failed (exit 1)')}[fault]
                                self.assertEqual(expected, result)
                        self.assertEqual(1, len(logs.records))
                        self.assertEqual('M4B source-cover temporary files could not be removed.',
                                         logs.records[0].getMessage())
                        self.assertIs(cleanup_error, logs.records[0].exc_info[1])
                    self.assertEqual(1, len(remaining))
                    self.assertEqual(remaining, list(root.glob('.m4b-source-cover-*')))
                    self.assertEqual(root, remaining[0].parent)
                    covers = list(remaining[0].iterdir())
                    self.assertEqual(1, len(covers))
                    self.assertEqual(row['cover']['sha256'],
                                     hashlib.sha256(covers[0].read_bytes()).hexdigest())
                    self.assertFalse(list(root.glob('*.pending.*')))
                    self.assertFalse(list(root.glob('_m4b_*')))
                    current = (root / 'audiobook.m4b').read_bytes()
                    if fault == 'success':
                        self.assertNotEqual(previous, current)
                        self.assertEqual(1, len(callbacks))
                        self.assertTrue(any(s.get('disposition', {}).get('attached_pic')
                                            for s in fixture.probe(root)['streams']))
                    else:
                        self.assertEqual(previous, current)
                        self.assertEqual([], callbacks)
                finally:
                    for path in remaining:
                        shutil.rmtree(path)

    def test_non_oserror_cleanup_failure_still_surfaces_after_owned_cleanup(self):
        fixture = source_fixture.M4bSourcePublicationTests()
        original_rmtree = shutil.rmtree
        error = RuntimeError('synthetic cleanup programming failure')
        with fixture.fixture() as (root, manager, _):
            previous = fixture.prior(root, manager)
            choice = fixture.selection(root)
            def remove_tree(path, *args, **kwargs):
                result = original_rmtree(path, *args, **kwargs)
                owned = Path(path)
                if owned.parent == root and owned.name.startswith('.m4b-source-cover-'):
                    raise error
                return result
            with patch.object(shutil, 'rmtree', side_effect=remove_tree):
                with self.assertRaises(RuntimeError) as raised:
                    manager.merge_m4b(source_cover=choice)
            self.assertIs(error, raised.exception)
            self.assertNotEqual(previous, (root / 'audiobook.m4b').read_bytes())
            fixture.assert_clean(root)
