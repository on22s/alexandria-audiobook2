import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class PreparerLoggingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, previous)
        self.logger = logging.getLogger('alexandria')
        original_handlers = self.logger.handlers[:]
        original_level = self.logger.level
        self.logger.handlers = []
        self.logger.setLevel(logging.WARNING)
        def restore():
            for handler in self.logger.handlers:
                handler.close()
            self.logger.handlers = original_handlers
            self.logger.setLevel(original_level)
        self.addCleanup(restore)
        sys.path.insert(0, str(ROOT))
        self.addCleanup(sys.path.remove, str(ROOT))

    def load(self):
        spec = importlib.util.spec_from_file_location('preparer_logging_fixture', ROOT / 'alexandria_preparer_rocm_compatible.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_repeated_library_loads_do_not_create_logs_or_handlers(self):
        self.load()
        self.load()
        self.assertFalse(Path('logs').exists())
        self.assertEqual([], self.logger.handlers)
        self.assertEqual(logging.WARNING, self.logger.level)

    def test_explicit_setup_survives_reload_and_writes_once(self):
        foreign = logging.NullHandler()
        self.logger.addHandler(foreign)
        first = self.load()
        path = first.ensure_preparer_logging()
        handlers = self.logger.handlers[:]
        second = self.load()
        self.assertEqual(path, second.ensure_preparer_logging())
        self.assertEqual(handlers, self.logger.handlers)
        self.assertEqual(3, len(handlers))
        self.assertIn(foreign, handlers)
        self.logger.info('unique lifecycle message')
        for handler in handlers:
            handler.flush()
        self.assertEqual(1, Path(path).read_text().count('unique lifecycle message'))
        self.assertEqual(1, len(list(Path('logs').glob('*.log'))))

    def test_file_setup_error_preserves_foreign_handlers_and_retry(self):
        foreign = logging.NullHandler()
        self.logger.addHandler(foreign)
        module = self.load()
        with patch.object(module.logging, 'FileHandler', side_effect=OSError('fixture read-only')):
            with self.assertRaisesRegex(OSError, 'fixture read-only'):
                module.ensure_preparer_logging()
        self.assertEqual([foreign], self.logger.handlers)
        path = module.ensure_preparer_logging()
        self.assertTrue(Path(path).is_file())
        self.assertEqual(3, len(self.logger.handlers))
