"""Unsafe public CLI arguments must not mutate installed or external bundles."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_promote_adapters import promote_adapters


class PromotionPathComponentTests(unittest.TestCase):
    def test_unsafe_adapter_and_rollback_arguments_preserve_all_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            models, backups, outside = [root / name for name in ('models', 'backups', 'outside')]
            for folder in (models, backups, outside):
                folder.mkdir()
                (folder / 'sentinel').write_bytes(folder.name.encode())
            (models / 'manifest.json').write_bytes(b'[]')
            invalid = ('../outside', str(outside), '.', '..', 'folder/voice', 'folder\\voice', '')
            before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.object(promote_adapters, 'MODELS', str(models)), \
                 patch.object(promote_adapters, 'BACKUPS', str(backups)), \
                 patch.object(promote_adapters, 'shipped_scores', return_value={}):
                for value in invalid:
                    with self.subTest(operation='promote', value=value):
                        with self.assertRaisesRegex(ValueError, 'invalid publication path component'):
                            promote_adapters.promote([value], 'safe-stamp', False)
                    with self.subTest(operation='rollback', value=value):
                        with self.assertRaisesRegex(ValueError, 'invalid publication path component'):
                            promote_adapters.rollback(value)
            after = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            # Kernel lock file is the only intentional admission artifact.
            after.pop('models/manifest.json.lock', None)
            self.assertEqual(before, after)
            self.assertEqual(['sentinel'], sorted(p.name for p in outside.iterdir()))
            self.assertEqual(['sentinel'], sorted(p.name for p in backups.iterdir()))

    def test_unsafe_publication_stamp_refuses_before_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            models = Path(temporary)
            for stamp in ('../outside', '/tmp/outside', '.', '..', 'x\\y', ''):
                with self.subTest(stamp=stamp), patch.object(promote_adapters, 'MODELS', str(models)):
                    with self.assertRaisesRegex(ValueError, 'invalid publication path component'):
                        promote_adapters._apply_publication(['voice'], stamp, 'promotion', lambda _: None)
                    self.assertEqual([], list(models.iterdir()))
