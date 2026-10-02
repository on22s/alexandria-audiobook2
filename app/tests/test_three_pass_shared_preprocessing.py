"""Actual CLI prepared text, fingerprints, publication and input refusal."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import generate_script as gs
import three_pass_generate as tp

if os.environ.get('THREE_PASS_PREPROCESS_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_preprocess_saved', os.environ['THREE_PASS_PREPROCESS_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


if os.environ.get('SCRIPT_REPAIR_SOURCE'):
    spec = importlib.util.spec_from_file_location('repair_saved', os.environ['SCRIPT_REPAIR_SOURCE'])
    repair_saved = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repair_saved)
    gs.build_deterministic_repair = repair_saved.build_deterministic_repair
    tp.build_deterministic_repair = repair_saved.build_deterministic_repair


class SharedPreprocessingCliTests(unittest.TestCase):
    def run_cli(self, module, raw):
        expected, _ = gs.get_preprocessed_source(raw, strip_front_matter=True)
        calls = []
        def create(**kwargs):
            calls.append(kwargs)
            if module is gs:
                payload = [{'speaker':'NARRATOR','text':expected,'instruct':'Neutral.'}]
            else:
                prompt = kwargs['messages'][-1]['content']
                match = re.search(r'\[\{"n":', prompt)
                if not match:
                    raise AssertionError('Expected delivery request only: ' + prompt)
                rows = json.JSONDecoder().raw_decode(prompt[match.start():])[0]
                payload = [{'n':entry['n'], 'head':' '.join(entry['text'].split()[:3]), 'instruct':'Neutral.'} for entry in rows]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        config = {'llm_mode':'local', 'llm_local':{'model_name':'fixture','structured_output':'off'},
                  'generation':{'max_tokens':500,'three_pass_segmentation':'quotes'}}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as transcript:
            root = Path(tmp); source = root / 'book.txt'; source.write_text(raw, encoding='utf-8'); before = source.read_bytes()
            output = root / 'out.json'
            with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR':tmp}), \
                 patch.object(module.sys, 'argv', ['generator',str(source),'--output',str(output),'--strip-front-matter']), \
                 patch.object(module,'load_app_config',return_value=config), \
                 patch.object(module,'ensure_ideal_settings',return_value=(False, {'context_length':32768}, 'fixture')), \
                 patch.object(module,'make_run_client',return_value=client), \
                 patch.object(tp,'DEFAULT_MODEL_PROFILES_PATH',str(Path(__file__).parent.parent / 'three_pass_model_profiles.json')):
                try:
                    module.main()
                except SystemExit as exc:
                    raise AssertionError(transcript.getvalue()) from exc
            entries = json.loads(output.read_text())
            suffix = '.generation_quality.json' if module is gs else '.threepass_manifest.json'
            manifest = json.loads(Path(str(output)+suffix).read_text())
            self.assertEqual(before, source.read_bytes())
        self.assertTrue(calls)
        return entries, manifest, expected

    def test_homoglyph_repair_reaches_both_actual_cli_outputs_and_source_fingerprints(self):
        raw = ('The patient gardener carefully watered the tall flowers beside the old stone wall. ' * 5
               + 'Аlice stood beside the door and watched the morning light.')
        single, single_manifest, expected = self.run_cli(gs, raw)
        triple, triple_manifest, _ = self.run_cli(tp, raw)
        self.assertIn('Alice', expected)
        self.assertNotIn('Аlice', expected)
        self.assertEqual(expected, ' '.join(entry['text'] for entry in single))
        self.assertEqual(expected, ' '.join(entry['text'] for entry in triple))
        self.assertEqual('complete', single_manifest['status'])
        self.assertEqual('complete', triple_manifest['status'])
        # Fingerprints are created from the actual prepared source, not raw input.
        import hashlib
        digest = hashlib.sha256(expected.encode('utf-8')).hexdigest()
        self.assertEqual(digest, triple_manifest['fingerprint']['source_sha256'])
        self.assertEqual(digest, single_manifest['fingerprint']['source_sha256'])

    def test_apostrophe_repair_and_non_latin_text_keep_existing_policy(self):
        for raw in ('They don t know where he s gone. They don t know why.',
                    'Горячий пар поднимался над водой. Пар окутал парк и паровоз.'):
            with self.subTest(raw=raw):
                single, _, expected = self.run_cli(gs, raw)
                triple, _, _ = self.run_cli(tp, raw)
                self.assertEqual(expected, ' '.join(entry['text'] for entry in single))
                self.assertEqual(expected, ' '.join(entry['text'] for entry in triple))
                if raw.startswith('Горячий'):
                    self.assertEqual(raw, expected)

    def test_utf16_nul_input_is_refused_before_healing_or_provider_dispatch(self):
        for encoding in ('utf-16-le', 'utf-16-be', 'utf-16'):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as transcript:
                source = Path(tmp) / 'book.txt'; raw = 'BOOK Alice stood beside the door.'.encode(encoding); source.write_bytes(raw)
                output = Path(tmp) / 'out.json'
                with patch.object(tp.sys,'argv',['generator',str(source),'--output',str(output)]), \
                     patch.object(tp,'ensure_ideal_settings') as heal, patch.object(tp,'make_run_client') as client:
                    with self.assertRaises(SystemExit) as exited:
                        tp.main()
                    self.assertEqual(1, exited.exception.code)
                    heal.assert_not_called(); client.assert_not_called()
                self.assertIn('unsafe control', transcript.getvalue())
                self.assertEqual(raw, source.read_bytes())
                self.assertEqual([source], list(Path(tmp).iterdir()))
