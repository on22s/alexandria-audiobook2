"""Explicit active-script publication retires both generation-state files."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import generate_script as gs
import three_pass_generate as tp
from tests import test_generation_model_binding as fixtures
from tests.test_support import write_three_pass_completion_manifest

CONFIG = {'llm_mode': 'local', 'llm_local': {'model_name': 'fixture', 'base_url': 'http://fixture/v1'},
          'generation': {'max_tokens': 4096}}
TEXT = 'Alice waited beside the garden gate.'


class ActiveScriptChunkRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.txt'
        self.source.write_text(TEXT, encoding='utf-8')
        self.active = self.root / 'annotated_script.json'
        self.active.write_text('[{"text":"old"}]', encoding='utf-8')
        self.chunks = self.root / 'chunks.json'
        self.journal = self.root / 'chunks.json.status.jsonl'

    def seed_state(self):
        self.chunks.write_text('[{"text":"old chunk"}]', encoding='utf-8')
        self.journal.write_text('{"uid":"u1","status":"done"}\n', encoding='utf-8')

    def assert_state_present(self):
        self.assertEqual('[{"text":"old chunk"}]', self.chunks.read_text(encoding='utf-8'))
        self.assertEqual('{"uid":"u1","status":"done"}\n', self.journal.read_text(encoding='utf-8'))

    def assert_state_retired(self):
        self.assertFalse(self.chunks.exists())
        self.assertFalse(self.journal.exists())

    def run_single_pass(self, output, *, quality_ok=True):
        self.seed_state()
        (self.root / 'config.json').write_text(json.dumps(fixtures.config()), encoding='utf-8')
        rows = [{'speaker': 'NARRATOR', 'text': TEXT, 'instruct': 'Plain.'}]

        def work(client, model, chunk, number, total, params, **kwargs):
            return list(rows), False

        with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(self.root)}), \
             patch.object(sys, 'argv', ['generate_script', str(self.source), '--output', output]), \
             patch.object(gs, 'split_into_chunks', return_value=[TEXT]), \
             patch.object(gs, 'ensure_ideal_settings',
                          return_value=(False, {'context_length': 8192, 'parallel': 1}, 'fixture')), \
             patch('llm_provider.make_llm_client', return_value=SimpleNamespace()), \
             patch.object(gs, 'process_chunk_adaptively', side_effect=work), \
             patch.object(gs, 'passes_final_generation_gate', return_value=quality_ok), \
             contextlib.redirect_stdout(io.StringIO()):
            if quality_ok:
                gs.main()
            else:
                with self.assertRaises(SystemExit):
                    gs.main()

    def run_three_pass(self, output, *, publish=True):
        self.seed_state()

        def work(client, model, text, params, chunk_size, **kwargs):
            entries = [{'speaker': 'NARRATOR', 'text': text, 'instruct': 'Plain.'}]
            if publish:
                write_three_pass_completion_manifest(
                    tp.three_pass_manifest_path(kwargs['output_path']),
                    entries, run_id=kwargs['run_id'])
            return entries

        with patch.object(sys, 'argv', ['three_pass_generate', str(self.source), '--output', output]), \
             patch.object(tp, 'get_runtime_data_dir', return_value=str(self.root)), \
             patch.object(tp, 'load_app_config', return_value=CONFIG), \
             patch.object(tp, 'ensure_ideal_settings',
                          return_value=(None, {'context_length': 32768}, 'fixture')), \
             patch.object(tp, 'make_run_client', return_value=object()), \
             patch.object(tp, 'run_three_pass', side_effect=work), \
             contextlib.redirect_stdout(io.StringIO()):
            if publish:
                tp.main()
            else:
                with self.assertRaises(SystemExit):
                    tp.main()

    def test_single_pass_explicit_active_spellings_retire_both(self):
        for output in (str(self.active), os.path.relpath(self.active)):
            with self.subTest(output=output):
                self.run_single_pass(output)
                self.assertEqual(TEXT, json.loads(self.active.read_text(encoding='utf-8'))[0]['text'])
                self.assert_state_retired()

    def test_three_pass_explicit_active_spellings_retire_both(self):
        for output in (str(self.active), os.path.relpath(self.active)):
            with self.subTest(output=output):
                self.run_three_pass(output)
                self.assertEqual(TEXT, json.loads(self.active.read_text(encoding='utf-8'))[0]['text'])
                self.assert_state_retired()

    def test_parent_directory_alias_retires_active_chunks(self):
        alias = self.root / 'directory-alias'
        alias.symlink_to(self.root, target_is_directory=True)
        for generate in (self.run_single_pass, self.run_three_pass):
            with self.subTest(generator=generate.__name__):
                self.active.write_text('[{"text":"old"}]', encoding='utf-8')
                generate(str(alias / self.active.name))
                self.assertEqual(TEXT, json.loads(self.active.read_text(encoding='utf-8'))[0]['text'])
                self.assert_state_retired()

    def test_output_leaf_alias_is_replaced_without_retiring_active_chunks(self):
        other = self.root / 'other'
        other.mkdir()
        output = other / self.active.name
        original = self.active.read_bytes()
        for generate in (self.run_single_pass, self.run_three_pass):
            with self.subTest(generator=generate.__name__):
                output.symlink_to(self.active)
                generate(str(output))
                self.assertFalse(output.is_symlink())
                self.assertEqual(TEXT, json.loads(output.read_text(encoding='utf-8'))[0]['text'])
                self.assertEqual(original, self.active.read_bytes())
                self.assert_state_present()
                output.unlink()

    def test_distinct_outputs_preserve_both(self):
        output = str(self.root / 'other.json')
        self.run_single_pass(output)
        self.assert_state_present()
        self.run_three_pass(output)
        self.assert_state_present()

    def test_failed_publication_preserves_both(self):
        self.run_single_pass(str(self.active), quality_ok=False)
        self.assert_state_present()
        self.assertEqual('[{"text":"old"}]', self.active.read_text(encoding='utf-8'))
        self.run_three_pass(str(self.active), publish=False)
        self.assert_state_present()
        self.assertEqual('[{"text":"old"}]', self.active.read_text(encoding='utf-8'))

    def test_app_owned_three_pass_uses_app_publication_not_cli_retirement(self):
        from script_selection_binding import capture_generation_selection, BINDING_NAME
        self.seed_state()
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'a', 'book_generation': 'one', 'input_file_path': str(self.source),
            'script_selection_pending': True, 'script_generation_run_id': 'f' * 32}), encoding='utf-8')
        state = json.loads((self.root / 'state.json').read_text(encoding='utf-8'))
        state['script_generation_base_sha256'] = hashlib.sha256(
            self.active.read_bytes()).hexdigest()
        (self.root / 'state.json').write_text(json.dumps(state), encoding='utf-8')
        selection = capture_generation_selection(str(self.root), str(self.source))

        def work(client, model, text, params, chunk_size, **kwargs):
            self.assertTrue(kwargs['defer_final_publication'])
            entries = [{'speaker': 'NARRATOR', 'text': text, 'instruct': 'Plain.'}]
            write_three_pass_completion_manifest(
                tp.three_pass_manifest_path(kwargs['output_path']),
                entries, run_id='f' * 32)
            return entries

        argv = ['three_pass_generate', str(self.source), '--active-book-token', selection['book_token'],
                '--active-source-sha256', selection['source_sha256'], '--active-run-id', 'f' * 32]
        with patch.object(sys, 'argv', argv), \
             patch.object(tp, 'get_runtime_data_dir', return_value=str(self.root)), \
             patch.object(tp, 'load_app_config', return_value=CONFIG), \
             patch.object(tp, 'ensure_ideal_settings',
                          return_value=(None, {'context_length': 32768}, 'fixture')), \
             patch.object(tp, 'make_run_client', return_value=object()), \
             patch.object(tp, 'run_three_pass', side_effect=work), \
             patch.object(tp, 'remove_chunk_snapshot') as cli_retire, \
             contextlib.redirect_stdout(io.StringIO()):
            tp.main()
        self.assertTrue((self.root / BINDING_NAME).exists())
        cli_retire.assert_not_called()
        self.assert_state_retired()


if __name__ == '__main__':
    unittest.main()
