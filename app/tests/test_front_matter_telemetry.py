"""Native source preprocessing through the real CLI before any LLM work."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import generate_script as gs

if os.environ.get('GENERATION_SOURCE'):
    spec = importlib.util.spec_from_file_location('generation_before', os.environ['GENERATION_SOURCE'])
    gs = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gs
    spec.loader.exec_module(gs)


class BeforeModelWork(Exception):
    pass


class FrontMatterTelemetryTests(unittest.TestCase):
    def test_front_only_removal_is_reported_without_modifying_upload(self):
        source = ('Manifesto.\nTranslator introduction.\nContents.\n'
                  'Original Web Novel Chapter - Complete.\nOriginal Translation by A.\n\n'
                  'The gardener waited beside the gate.')
        processed, report = gs.get_preprocessed_source(source)
        self.assertIsNotNone(report['front_matter_removed'])
        self.assertEqual(0, report['publisher_matter']['front_paragraphs'])
        self.assertEqual(0, report['publisher_matter']['back_paragraphs'])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            path.write_text(source, encoding='utf-8')
            before = path.read_bytes()
            captured = io.StringIO()
            with patch.object(sys, 'argv', ['generate_script', str(path)]), \
                 patch.object(gs, 'preflight_source', side_effect=BeforeModelWork) as preflight, \
                 contextlib.redirect_stdout(captured), self.assertRaises(BeforeModelWork):
                gs.main()
            self.assertEqual(processed, preflight.call_args.args[0])
            self.assertEqual(before, path.read_bytes())
        self.assertIn(f"Stripped {report['front_matter_removed']['removed_chars']} characters of known front matter",
                      captured.getvalue())
        self.assertNotIn('Stripped publisher matter', captured.getvalue())
