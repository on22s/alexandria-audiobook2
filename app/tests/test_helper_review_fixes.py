import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core
import generate_script
import lmstudio_settings as lms
from project import ProjectManager
import review_script
from routers import system
import utils


class HelperReviewTests(unittest.TestCase):
    def test_object_parser_rejects_other_json_shapes(self):
        for text in ('[]', '[1]', '42', 'null', '"text"'):
            self.assertIsNone(utils.extract_json_object(text))
        self.assertEqual(utils.extract_json_object('prose {"a": 1}'), {"a": 1})

    def test_long_filename_collision(self):
        self.assertNotEqual(utils.secure_filename('a' * 150 + '!'),
                            utils.secure_filename('a' * 150 + '?'))
        self.assertEqual(utils.secure_filename('short!'), 'short_')

    def test_auth_rejects_garbage_and_supports_unicode(self):
        self.assertFalse(utils.check_basic_auth('Basic dTpw!!!', 'u', 'p'))
        self.assertTrue(utils.check_basic_auth('Basic dTpw', 'u', 'p'))
        encoded = base64.b64encode('名前:秘密'.encode()).decode()
        self.assertTrue(utils.check_basic_auth('Basic ' + encoded, '名前', '秘密'))

    def test_writes_reject_invalid_parameters_without_changing_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'value.json')
            utils.atomic_json_write({'old': 1}, path)
            for retries in (0, -1):
                with self.assertRaises(ValueError):
                    utils.atomic_json_write({'new': 2}, path, retries)
            with self.assertRaises(ValueError):
                utils.atomic_json_write_pair({}, path, {}, path)
            self.assertEqual(utils.safe_load_json(path), {'old': 1})

    def test_write_syncs_content_before_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'value.json')
            events = []
            replace = os.replace
            def replace_and_check(source, target):
                self.assertIn('sync', events)
                self.assertEqual(utils.safe_load_json(source), {'new': 2})
                replace(source, target)
            with patch.object(utils.os, 'fsync', side_effect=lambda fd: events.append('sync')), \
                    patch.object(utils.os, 'replace', side_effect=replace_and_check):
                utils.atomic_json_write({'new': 2}, path)
            self.assertEqual(utils.safe_load_json(path), {'new': 2})

    def test_disk_probe_failure_blocks(self):
        with patch.object(core.shutil, 'disk_usage', side_effect=OSError('offline')):
            self.assertEqual(core.check_disk_space('/missing', 2), (False, 0.0))

    def test_lms_skips_malformed_entries_and_preserves_good_model(self):
        raw = json.dumps([None, 42, 'bad', {'identifier': 'model', 'contextLength': 1, 'parallel': 1}])
        status = lms._parse_lms_ps_output(raw, 'model', {'context_length': 1, 'parallel': 1})
        self.assertTrue(status['loaded'])
        self.assertTrue(status['optimized'])

    def test_failed_lms_commands_do_not_claim_available(self):
        failed = SimpleNamespace(returncode=1, stdout='[]', stderr='failed')
        with patch.object(lms, 'find_lms_binary', return_value='lms'), \
                patch.object(lms.subprocess, 'run', return_value=failed):
            self.assertFalse(lms.get_lmstudio_status('model')['available'])
        with patch.object(lms, '_ssh_run', return_value=failed):
            self.assertFalse(lms.get_remote_lmstudio_status('host', 'model')['available'])

    def test_eta_surfaces_every_registered_task(self):
        for task in core.process_state:
            with patch.object(system, 'process_state', {task: {'running': True}}):
                status = asyncio.run(system.get_eta_status())
                self.assertTrue(status['running'], task)
                self.assertEqual(status['task'], task)

    def test_zero_chunk_size_is_rejected_and_valid_size_preserves_text(self):
        for size in (0, -1):
            with self.assertRaises(ValueError):
                generate_script.split_into_chunk_records('abcdef', size)
        self.assertEqual(''.join(generate_script.split_into_chunks('abcdef', 3)), 'abcdef')

    def test_persisted_shapes_and_line_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'value.json')
            for value in ({'bad': 1}, None, 42):
                utils.atomic_json_write(value, path)
                self.assertEqual(core._load_manifest(path), [])
            utils.atomic_json_write([None, {'id': 'good'}], path)
            self.assertEqual(core._load_manifest(path), [{'id': 'good'}])
            utils.atomic_json_write([None, 42, {'speaker': 'A', 'text': 'hello'}], path)
            self.assertEqual(core._script_line_counts(path), {'A': 1})
            utils.atomic_json_write({'shared': [1], 'casts': 'bad', 'favorites': 42}, path)
            with patch.object(core, 'VOICE_LIBRARY_PATH', path):
                self.assertEqual(core._load_voice_library(), {'shared': {}, 'casts': {}, 'favorites': []})
        lib = {'shared': {}, 'casts': {'cast': {'members': {
            'a': {'config': {'adapter_id': 'voice'}, 'assignments': {
                'one': {'line_count': 'bad'}, 'two': {'line_count': '5'}}}}}}}
        self.assertEqual(core.get_cast_adapter_usage(lib, 'cast')['voice']['total_lines'], 5)

    def test_bad_review_checkpoint_returns_no_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'book.json')
            for value in (None, [], 42):
                utils.atomic_json_write(value, path + '.review_checkpoint.json')
                self.assertIsNone(review_script.load_checkpoint(path, 1, 10, 2))

    def test_bad_chunks_are_backed_up_and_regenerated(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ProjectManager(directory)
            utils.atomic_json_write([{'speaker': 'A', 'text': 'hello'}], manager.script_path)
            for value in ({'bad': 1}, [None], 42):
                utils.atomic_json_write(value, manager.chunks_path)
                chunks = manager.load_chunks()
                self.assertEqual(chunks[0]['text'], 'hello')
                self.assertEqual(utils.safe_load_json(manager.chunks_path + '.corrupt'), value)
                self.assertEqual(utils.safe_load_json(manager.chunks_path), chunks)

    def test_audio_paths_reject_escape_in_loading_and_fingerprinting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'project'
            manager = ProjectManager(str(root))
            outside = Path(directory) / 'outside.wav'
            outside.touch()
            link = root / 'link.wav'
            link.symlink_to(outside)
            for path in ('../outside.wav', str(outside), 'link.wav'):
                chunk = {'audio_path': path}
                with self.assertRaises(ValueError):
                    manager._load_chunks_with_audio(chunks=[chunk])
                with self.assertRaises(ValueError):
                    manager._chapter_fingerprint([chunk])
            self.assertEqual(manager.get_chunk_audio_path('inside.wav'), str(root / 'inside.wav'))
