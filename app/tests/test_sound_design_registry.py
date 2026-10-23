"""Strict cue history and real saved-book roundtrips with synthetic metadata."""
import copy
from contextlib import contextmanager, ExitStack
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import book_state_transaction as books
from book_state_transaction import apply_book_input_selection
from sound_design_cues import get_sound_design_cue_version
from sound_design_registry import (ACTIVE_CUES, MAX_REGISTRY_BYTES, get_saved_sound_design_path,
    get_sound_design_registry, get_sound_design_registry_bytes)
from tests.test_sound_design_cues import recipe
from tests import test_saved_book_publication as saved_publication
from routers import scripts_library, script


def registry(book_id='new-book', gain=-12):
    value = recipe(); value['book']['book_id'] = book_id; value['mix']['gain_db'] = gain
    version = get_sound_design_cue_version('cue-a', value)
    return dict(schema=1, book=copy.deepcopy(value['book']), versions=[version],
                selected={'cue-a': version['version']})


class SoundDesignRegistryTests(unittest.TestCase):
    def test_immutable_history_and_exact_selected_version_survive_serialization(self):
        value = registry(); first = value['versions'][0]
        updated = copy.deepcopy(first['recipe']); updated['mix']['gain_db'] = -20
        second = get_sound_design_cue_version('cue-a', updated, first['version'])
        value['versions'].append(second); value['selected']['cue-a'] = second['version']
        raw = get_sound_design_registry_bytes(value)
        result = get_sound_design_registry(raw)
        self.assertEqual(value, result)
        result['versions'][0]['recipe']['mix']['gain_db'] = -4
        self.assertEqual(-12, value['versions'][0]['recipe']['mix']['gain_db'])

    def test_unknown_parent_duplicate_origin_wrong_book_and_selection_refuse(self):
        for kind in ('parent', 'origin', 'book', 'selection'):
            value = registry()
            if kind == 'parent':
                value['versions'] = [get_sound_design_cue_version('cue-a', value['versions'][0]['recipe'], 'b' * 64)]
            elif kind == 'origin':
                r = copy.deepcopy(value['versions'][0]['recipe']); r['mix']['gain_db'] = -30
                value['versions'].append(get_sound_design_cue_version('cue-a', r))
            elif kind == 'book':
                value['book']['book_id'] = 'other'
            else:
                value['selected']['cue-a'] = 'b' * 64
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                get_sound_design_registry_bytes(value)

    def test_duplicate_nonfinite_invalid_unicode_deep_and_oversized_json_refuse(self):
        good = get_sound_design_registry_bytes(registry())
        cases = [good.replace(b'"schema":1', b'"schema":1,"schema":1', 1),
                 good.replace(b'"gain_db":-12', b'"gain_db":NaN', 1),
                 b'\xff', b'[' * 1100 + b']' * 1100, b' ' * (MAX_REGISTRY_BYTES + 1)]
        for raw in cases:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                get_sound_design_registry(raw)

    def test_joint_bytes_are_checked_before_version_hashing_or_copying(self):
        value = registry()
        with patch('sound_design_registry.MAX_REGISTRY_BYTES', 32), \
                patch('sound_design_cues.validate_sound_design_cue_version', side_effect=AssertionError('hashed too soon')):
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                get_sound_design_registry_bytes(value)


class SoundDesignSavedBookTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with saved_publication.SavedBookPublicationTests().fixture() as (root, api), ExitStack() as stack:
            for name, filename in (('AUDIOBOOK_PATH', 'cloned_audiobook.mp3'),
                                   ('M4B_PATH', 'audiobook.m4b'), ('CHUNKS_PATH', 'chunks.json')):
                stack.enter_context(patch.object(scripts_library, name, str(root / filename)))
            yield root, api

    def write_active(self, root, gain=-12):
        raw = b'\n' + get_sound_design_registry_bytes(registry(gain=gain)) + b'\n'
        (root / ACTIVE_CUES).write_bytes(raw)
        return raw

    def test_two_named_snapshots_restore_their_exact_own_cue_bytes(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            first = self.write_active(root)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'one'}).status_code)
            second = self.write_active(root, -24)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'two'}).status_code)
            for name, expected in (('one', first), ('two', second), ('one', first)):
                response = client.post('/api/scripts/load', json={'name': name})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(expected, (root / ACTIVE_CUES).read_bytes())
                self.assertEqual(expected, Path(get_saved_sound_design_path(root / 'scripts' / (name + '.json'))).read_bytes())

    def test_admitted_assets_and_historical_references_survive_book_save_reset_load(self):
        import numpy as np
        import soundfile as sf
        from sound_design_assets import SoundDesignAssetStore
        with self.fixture() as (root, api), TestClient(api) as client:
            store = SoundDesignAssetStore(root, source_limit=65536, pcm_limit=65536,
                                           total_limit=256000)
            descriptors = []
            for index, level in enumerate((.125, -.25)):
                name = f'effect-{index}.wav'
                sf.write(root / name, np.full(3000, level), 24000, subtype='PCM_16')
                descriptors.append(store.import_asset(name))
            value = recipe()
            value['book']['book_id'] = 'new-book'
            versions = []
            for descriptor in descriptors:
                value['asset'] = descriptor
                versions.append(get_sound_design_cue_version('cue-a', value,
                    versions[-1]['version'] if versions else None))
            document = dict(schema=1, book=value['book'], versions=versions,
                            selected={'cue-a': versions[-1]['version']})
            raw = get_sound_design_registry_bytes(document)
            (root / ACTIVE_CUES).write_bytes(raw)
            response = client.post('/api/scripts/save', json={'name': 'effects'})
            self.assertEqual(200, response.status_code, response.text)
            preserved = {path.name: path.read_bytes() for path in store.directory.iterdir()
                         if not path.name.endswith('.lock')}
            apply_book_input_selection(root, 'new.txt', 'new')
            self.assertFalse((root / ACTIVE_CUES).exists())
            response = client.post('/api/scripts/load', json={'name': 'effects'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(raw, (root / ACTIVE_CUES).read_bytes())
            self.assertEqual(preserved, {path.name: path.read_bytes() for path in store.directory.iterdir()
                                        if not path.name.endswith('.lock')})
            restored = get_sound_design_registry((root / ACTIVE_CUES).read_bytes())
            for version, descriptor in zip(restored['versions'], descriptors):
                with SoundDesignAssetStore(root).open_canonical(descriptor['asset_id']) as (stream, actual):
                    self.assertEqual(version['recipe']['asset'], actual)
                    self.assertEqual(12000, len(stream.read()))

    def test_checkpoint_snapshot_keeps_exact_cue_history_on_reload(self):
        with self.fixture() as (root, api):
            api.include_router(script.router)
            raw = self.write_active(root)
            entries = [{'speaker': 'Alice', 'text': 'Completed prefix.', 'instruct': 'Read'}]
            checkpoint = dict(stage='instruct', chunks_done=1, segmented=entries,
                              named=entries, annotated=entries)
            Path(script.three_pass_checkpoint_path(str(root / 'annotated_script.json'))).write_text(json.dumps(checkpoint))
            with patch.object(script, 'DATA_DIR', str(root)), \
                    patch.object(script, 'SCRIPTS_DIR', str(root / 'scripts')), \
                    patch.object(script, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                    patch.object(script, 'VOICE_CONFIG_PATH', str(root / 'voice_config.json')), \
                    patch.object(script, 'process_state', {'script': {'running': True}}), TestClient(api) as client:
                response = client.post('/api/generate_script/snapshot', json={'name': 'prefix'})
                self.assertEqual(200, response.status_code, response.text)
                companion = Path(get_saved_sound_design_path(root / 'scripts/prefix.json'))
                self.assertEqual(raw, companion.read_bytes())
                self.assertEqual(entries, json.loads((root / 'scripts/prefix.json').read_bytes()))
                self.write_active(root, -24)
                response = client.post('/api/scripts/load', json={'name': 'prefix'})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual(raw, (root / ACTIVE_CUES).read_bytes())
                self.assertEqual(raw, companion.read_bytes())
                # Invalid metadata must refuse a new snapshot before any family is published.
                (root / ACTIVE_CUES).write_bytes(b'{invalid')
                before = saved_publication.SavedBookPublicationTests().artifacts(root)
                response = client.post('/api/generate_script/snapshot', json={'name': 'invalid'})
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, saved_publication.SavedBookPublicationTests().artifacts(root))

    def test_load_cue_install_failure_restores_exact_active_book(self):
        with self.fixture() as (root, api), TestClient(api, raise_server_exceptions=False) as client:
            self.write_active(root)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'one'}).status_code)
            self.write_active(root, -24)
            before_cues = (root / ACTIVE_CUES).read_bytes()
            before_state = (root / 'state.json').read_bytes()
            move = books._move; failed = False
            def fail_after_install(source, destination):
                nonlocal failed
                move(source, destination)
                if Path(destination) == root / ACTIVE_CUES and not failed:
                    failed = True
                    raise OSError('fixture after cue install')
            with patch.object(books, '_move', side_effect=fail_after_install):
                response = client.post('/api/scripts/load', json={'name': 'one'})
            self.assertTrue(failed)
            self.assertEqual(500, response.status_code)
            self.assertEqual(before_cues, (root / ACTIVE_CUES).read_bytes())
            self.assertEqual(before_state, (root / 'state.json').read_bytes())
            self.assertFalse((root / books.JOURNAL).exists())

    def test_source_reset_process_death_rolls_back_or_finishes_committed_clear(self):
        worker = r"""
import os, sys
from pathlib import Path
import book_state_transaction as books
root = Path(sys.argv[1]); point = int(sys.argv[2]); count = 0
if point:
    move = books._move
    def stop_after_move(source, destination):
        global count
        move(source, destination); count += 1
        if count == point: os._exit(77)
    books._move = stop_after_move
else:
    save = books._save_journal
    def stop_after_commit(root, journal):
        save(root, journal)
        if journal['phase'] == 'committed': os._exit(78)
    books._save_journal = stop_after_commit
books.apply_book_input_selection(root, 'new.txt', 'new')
"""
        for point in (1, 2, 3, 0):
            with self.subTest(point=point), self.fixture() as (root, _api):
                before_cues = self.write_active(root)
                before_state = (root / 'state.json').read_bytes()
                process = subprocess.run([sys.executable, '-c', worker, str(root), str(point)],
                                         capture_output=True, text=True, timeout=20)
                self.assertEqual(77 if point else 78, process.returncode, process.stderr)
                self.assertTrue((root / books.JOURNAL).exists())
                with self.assertRaises(ValueError), books.ensure_book_state(root, recover=False):
                    pass
                with books.ensure_book_state(root):
                    pass
                self.assertFalse((root / books.JOURNAL).exists())
                if point:
                    self.assertEqual(before_cues, (root / ACTIVE_CUES).read_bytes())
                    self.assertEqual(before_state, (root / 'state.json').read_bytes())
                else:
                    self.assertFalse((root / ACTIVE_CUES).exists())
                    self.assertEqual('new.txt', json.loads((root / 'state.json').read_bytes())['input_file_path'])

    def test_private_companion_never_hides_preexisting_valid_book_names(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            self.write_active(root)
            for name in ('foo', 'foo.sound_design', 'book.sound_design'):
                response = client.post('/api/scripts/save', json={'name': name})
                self.assertEqual(200, response.status_code, response.text)
            names = {row['name'] for row in client.get('/api/scripts').json()}
            self.assertTrue({'foo', 'foo.sound_design', 'book.sound_design'} <= names)
            self.assertFalse(any(name.startswith('.') for name in names))

    def test_legacy_missing_companion_clears_active_cues_in_book_transaction(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'legacy'}).status_code)
            self.write_active(root)
            response = client.post('/api/scripts/load', json={'name': 'legacy'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse((root / ACTIVE_CUES).exists())

    def test_invalid_active_save_refuses_without_overwriting_saved_family(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            (root / ACTIVE_CUES).write_bytes(b'{invalid')
            before = saved_publication.SavedBookPublicationTests().artifacts(root)
            response = client.post('/api/scripts/save', json={'name': 'book'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, saved_publication.SavedBookPublicationTests().artifacts(root))
            self.assertEqual(b'{invalid', (root / ACTIVE_CUES).read_bytes())

    def test_corrupt_or_foreign_saved_companion_does_not_switch_active_book(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            self.write_active(root)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'one'}).status_code)
            state = (root / 'state.json').read_bytes(); cues = (root / ACTIVE_CUES).read_bytes()
            target = Path(get_saved_sound_design_path(root / 'scripts/one.json'))
            for raw in (b'{invalid', get_sound_design_registry_bytes(registry('foreign'))):
                target.write_bytes(raw)
                response = client.post('/api/scripts/load', json={'name': 'one'})
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(state, (root / 'state.json').read_bytes())
                self.assertEqual(cues, (root / ACTIVE_CUES).read_bytes())

    def test_source_selection_clears_valid_cues_and_preserves_invalid_metadata(self):
        with self.fixture() as (root, _api):
            self.write_active(root)
            apply_book_input_selection(root, 'new.txt', 'new')
            self.assertFalse((root / ACTIVE_CUES).exists())
            state = (root / 'state.json').read_bytes()
            (root / ACTIVE_CUES).write_bytes(b'{invalid')
            with self.assertRaises(ValueError):
                apply_book_input_selection(root, 'other.txt', 'other')
            self.assertEqual(state, (root / 'state.json').read_bytes())
            self.assertEqual(b'{invalid', (root / ACTIVE_CUES).read_bytes())

    def test_active_alias_hardlink_fifo_and_oversize_refuse_before_saved_family_changes(self):
        for kind in ('symlink', 'hardlink', 'fifo', 'oversize'):
            with self.subTest(kind=kind), self.fixture() as (root, api), TestClient(api) as client:
                active = root / ACTIVE_CUES
                target = root / 'protected.json'; raw = get_sound_design_registry_bytes(registry())
                target.write_bytes(raw)
                if kind == 'symlink':
                    active.symlink_to(target)
                elif kind == 'hardlink':
                    os.link(target, active)
                elif kind == 'fifo':
                    if not hasattr(os, 'mkfifo'):
                        continue
                    os.mkfifo(active)
                else:
                    active.write_bytes(b' ' * (MAX_REGISTRY_BYTES + 1))
                before = saved_publication.SavedBookPublicationTests().artifacts(root)
                response = client.post('/api/scripts/save', json={'name': 'book'})
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, saved_publication.SavedBookPublicationTests().artifacts(root))
                self.assertEqual(raw, target.read_bytes())

    def test_destination_alias_refuses_without_overwriting_either_named_book(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            self.write_active(root)
            target = root / 'protected.json'; raw = get_sound_design_registry_bytes(registry())
            target.write_bytes(raw)
            companion = Path(get_saved_sound_design_path(root / 'scripts/book.json'))
            companion.unlink(); companion.symlink_to(target)
            script = (root / 'scripts/book.json').read_bytes()
            response = client.post('/api/scripts/save', json={'name': 'book'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(script, (root / 'scripts/book.json').read_bytes())
            self.assertEqual(raw, target.read_bytes()); self.assertTrue(companion.is_symlink())

    def test_snapshot_overwrite_without_cues_removes_only_its_own_companion(self):
        with self.fixture() as (root, api), TestClient(api) as client:
            self.write_active(root)
            for name in ('one', 'two'):
                self.assertEqual(200, client.post('/api/scripts/save', json={'name': name}).status_code)
            second = Path(get_saved_sound_design_path(root / 'scripts/two.json')).read_bytes()
            (root / ACTIVE_CUES).unlink()
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'one'}).status_code)
            self.assertFalse(Path(get_saved_sound_design_path(root / 'scripts/one.json')).exists())
            self.assertEqual(second, Path(get_saved_sound_design_path(root / 'scripts/two.json')).read_bytes())


if __name__ == '__main__':
    unittest.main()
