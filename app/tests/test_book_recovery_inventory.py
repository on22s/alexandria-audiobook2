"""Candidate inventory is observational and never authorizes recovery."""
import contextlib
import hashlib
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import core
import book_recovery_archive as archive
import book_recovery_inventory as inventory
from audio_take_history import AudioTakeHistory
from book_state_transaction import get_book_operation_token
from chunk_status_journal import ChunkStatusJournal
from generation_checkpoint_shards import GenerationCheckpointShards
from project_scope_admission import ensure_scope_admission


class BookRecoveryInventoryTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state = {'active_book_id': 'book', 'book_generation': 'generation',
                     'input_file_path': str(root / 'source.txt')}
            (root / 'state.json').write_text(json.dumps(state))
            (root / 'source.txt').write_text('A small source.')
            (root / 'annotated_script.json').write_text('[]')
            (root / 'chunks.json').write_text(json.dumps([{'uid': 'one', 'status': 'pending'}]))
            with patch.object(core, 'DATA_DIR', str(root)), \
                    patch.object(core, 'UPLOADS_DIR', str(root)), \
                    patch.object(core, 'SCRIPTS_DIR', str(root / 'scripts')), \
                    patch.object(core, 'CLONE_VOICES_DIR', str(root / 'voices')), \
                    patch.object(core, 'DESIGNED_VOICES_DIR', str(root / 'designed_voices')), \
                    patch.object(core, 'LORA_MODELS_DIR', str(root / 'voices')), \
                    patch.object(core, 'VOICELINES_DIR', str(root / 'voicelines')), \
                    patch.object(core, 'ensure_book_switch_allowed', return_value=contextlib.nullcontext()):
                with ensure_scope_admission(root):
                    yield root, state

    def test_inventory_refuses_when_admission_ends_during_scan(self):
        with self.fixture() as (root, _):
            with patch.object(inventory, 'is_scope_admitted', side_effect=[True, False]):
                with self.assertRaisesRegex(ValueError, 'admission ended during recovery inventory'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_invalid_book_identity_is_not_hashed_into_an_inventory_token(self):
        for key, value in (('active_book_id', None), ('active_book_id', ''),
                           ('book_generation', None), ('book_generation', ''),
                           ('book_generation', 'x' * 1025)):
            with self.subTest(key=key, value_type=type(value).__name__), \
                    self.fixture() as (root, state):
                state[key] = value
                (root / 'state.json').write_text(json.dumps(state))
                with self.assertRaisesRegex(ValueError, 'Invalid active book identity'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_partial_saved_sidecar_cannot_be_misclassified_as_a_book_source(self):
        with self.fixture() as (root, state):
            scripts = root / 'scripts'; scripts.mkdir()
            sidecar = scripts / 'book.json.partial.json'
            sidecar.write_text('[]')
            state['input_file_path'] = str(sidecar)
            (root / 'state.json').write_text(json.dumps(state))
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertIn('source_path_outside_selection_authority', result['unresolved'])
            self.assertIn({'kind':'selected_source', 'qualified':False}, result['external'])

    def test_replays_journal_and_inventories_referenced_audio_and_exports(self):
        with self.fixture() as (root, state):
            (root / 'voicelines').mkdir()
            (root / 'voicelines/one.wav').write_bytes(b'synthetic audio')
            (root / 'chapter_exports').mkdir()
            (root / 'chapter_exports/chapter.wav').write_bytes(b'export')
            (root / 'voice_config.json').write_text('{}')
            identity = {key: state[key] for key in ('active_book_id', 'book_generation')}
            ChunkStatusJournal(root / 'chunks.json', identity).apply_update(
                'one', {'status': 'done', 'audio_path': 'voicelines/one.wav'})
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            names = {item['path'] for item in result['files']}
            self.assertTrue({'state.json', 'source.txt', 'chunks.json.status.jsonl',
                             'voicelines/one.wav', 'chapter_exports/chapter.wav'} <= names)
            self.assertIn('voice_asset_resolution_and_shared_restore_not_qualified', result['unresolved'])
            self.assertIn('directory_restore_not_implemented', result['unresolved'])
            self.assertFalse(result['ready_for_capture'])
            self.assertEqual(result['revision'], inventory.inspect_active_book_recovery_candidates(
                root, byte_limit=1000000)['revision'])

    @unittest.skipIf(os.name == 'nt', 'Windows permissions do not use POSIX directory mode')
    def test_empty_directory_mode_is_observed_and_mode_changes_invalidate_revision(self):
        with self.fixture() as (root, _):
            directory = root / 'chapter_exports'
            directory.mkdir(mode=0o700)
            observed = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertEqual(2, observed['version'])
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode),
                             observed['directories']['chapter_exports']['mode'])
            directory.chmod(0o750)
            changed = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertNotEqual(observed['revision'], changed['revision'])
            self.assertEqual(0o750, changed['directories']['chapter_exports']['mode'])
            with self.assertRaisesRegex(ValueError, 'changed before capture'):
                inventory.capture_observed_project_candidates(root,
                    expected_revision=observed['revision'], byte_limit=1000000, quota_bytes=1000000)
            capture = inventory.capture_scope_file
            def change_during_scan(*args, **kwargs):
                if args[1] == 'chunks.json' and kwargs.get('include_bytes'):
                    directory.chmod(0o700)
                return capture(*args, **kwargs)
            with patch.object(inventory, 'capture_scope_file', side_effect=change_during_scan):
                with self.assertRaisesRegex(ValueError, 'directory changed during inventory'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_inventory_proofs_bind_a_synthetic_archive_without_authorizing_restore(self):
        try:
            archive._require_secure_staging()
        except ValueError as error:
            self.skipTest(str(error))
        with self.fixture() as (root, _):
            observation = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertFalse(observation['ready_for_capture'])
            proofs = observation['files']
            receipt = inventory.capture_observed_project_candidates(root,
                expected_revision=observation['revision'], byte_limit=1000000, quota_bytes=1000000)
            checked = archive.verify_recovery_archive(root, receipt['id'],
                expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=1000000,
                expected_observation_revision=observation['revision'])
            self.assertEqual(len(proofs), checked['files'])
            self.assertEqual(observation['revision'], checked['observation_revision'])
            self.assertIs(checked['observation_complete'], False)
            with self.assertRaisesRegex(ValueError, 'differs from the expected observation'):
                archive.verify_recovery_archive(root, receipt['id'],
                    expected_manifest_sha256=receipt['manifest_sha256'], quota_bytes=1000000,
                    expected_observation_revision='0' * 64)
            self.assertFalse(observation['ready_for_capture'])

    def test_observation_binding_refuses_stale_or_mid_capture_inventory(self):
        try:
            archive._require_secure_staging()
        except ValueError as error:
            self.skipTest(str(error))
        with self.fixture() as (root, _):
            observed = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            (root / 'voice_config.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'changed before capture'):
                inventory.capture_observed_project_candidates(root,
                    expected_revision=observed['revision'], byte_limit=1000000, quota_bytes=1000000)
            self.assertFalse((root / archive.ARCHIVE_DIR).exists())
        with self.fixture() as (root, _):
            observed = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            original = archive._copy_artifact
            def change_missing_candidate(*args, **kwargs):
                copied = original(*args, **kwargs)
                (root / 'voice_config.json').write_text('{}')
                return copied
            with patch.object(archive, '_copy_artifact', side_effect=change_missing_candidate):
                with self.assertRaisesRegex(ValueError, 'inventory changed before publication'):
                    inventory.capture_observed_project_candidates(root,
                        expected_revision=observed['revision'], byte_limit=1000000, quota_bytes=1000000)
            storage = root / archive.ARCHIVE_DIR
            self.assertFalse(any(archive._ID.fullmatch(path.name) for path in storage.iterdir()))
            self.assertEqual('{}', (root / 'voice_config.json').read_text())

    def test_observation_binding_rechecks_external_source_before_publication(self):
        try:
            archive._require_secure_staging()
        except ValueError as error:
            self.skipTest(str(error))
        with self.fixture() as (root, state), tempfile.TemporaryDirectory() as temp:
            uploads = Path(temp).resolve()
            source = uploads / 'book.txt'
            source.write_text('original source')
            state['input_file_path'] = str(source)
            (root / 'state.json').write_text(json.dumps(state))
            with patch.object(core, 'UPLOADS_DIR', str(uploads)):
                observed = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
                copy = archive._copy_artifact
                def mutate_external(*args, **kwargs):
                    result = copy(*args, **kwargs)
                    source.write_text('changed source')
                    return result
                with patch.object(archive, '_copy_artifact', side_effect=mutate_external):
                    with self.assertRaisesRegex(ValueError, 'inventory changed before publication'):
                        inventory.capture_observed_project_candidates(root,
                            expected_revision=observed['revision'], byte_limit=1000000, quota_bytes=1000000)
            storage = root / archive.ARCHIVE_DIR
            self.assertFalse(any(archive._ID.fullmatch(path.name) for path in storage.iterdir()))
            self.assertTrue(any(path.name.startswith('prepared-') for path in storage.iterdir()))
            self.assertEqual('changed source', source.read_text())

    def test_observation_binding_detects_missing_candidate_after_preparation(self):
        try:
            archive._require_secure_staging()
        except ValueError as error:
            self.skipTest(str(error))
        with self.fixture() as (root, _):
            observed = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            prepare = archive._write_prepared_receipt
            def change_after_prepare(*args, **kwargs):
                prepare(*args, **kwargs)
                (root / 'voice_config.json').write_text('{}')
            with patch.object(archive, '_write_prepared_receipt', side_effect=change_after_prepare):
                with self.assertRaisesRegex(ValueError, 'inventory changed before publication'):
                    inventory.capture_observed_project_candidates(root,
                        expected_revision=observed['revision'], byte_limit=1000000, quota_bytes=1000000)
            storage = root / archive.ARCHIVE_DIR
            self.assertFalse(any(archive._ID.fullmatch(path.name) for path in storage.iterdir()))
            self.assertTrue(any(path.name.startswith('prepared-') for path in storage.iterdir()))

    def test_checkpoint_shards_and_only_active_book_history_are_candidates(self):
        import numpy as np
        import soundfile as sf
        with self.fixture() as (root, _):
            source = root / 'current.wav'
            sf.write(source, np.zeros(2400, dtype='float32'), 24000)
            history = AudioTakeHistory(root)
            selected = history.capture(source, get_book_operation_token(root), 'one', {'take': 1})
            other = history.capture(source, 'a' * 64, 'other', {'take': 2})
            checkpoint = root / 'annotated_script.json.generation_checkpoint.json'
            GenerationCheckpointShards(checkpoint, {'source': 'fixture'}).save_chunks(
                [{'uid': 'shard', 'text': 'A test.'}])
            with patch.object(AudioTakeHistory, '_load', side_effect=AssertionError('must use captured bytes')):
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            names = {item['path'] for item in result['files']}
            self.assertIn('take_history/' + selected['version'] + '.wav', names)
            self.assertNotIn('take_history/' + other['version'] + '.wav', names)
            self.assertIn(checkpoint.name, names)
            self.assertTrue(any(name.startswith(checkpoint.name + '.parts-') and
                                name.endswith('/00000000.json') for name in names))
            self.assertIn('shared_take_history_restore_not_implemented', result['unresolved'])
            self.assertFalse(result['ready_for_capture'])

    def test_validated_cue_history_finds_sound_assets_and_detects_damage(self):
        import numpy as np
        import soundfile as sf
        from sound_design_assets import SoundDesignAssetStore
        from sound_design_cues import get_sound_design_cue_version
        from sound_design_registry import get_sound_design_registry_bytes
        from sound_design_renditions import get_sound_rendition_record, get_sound_rendition_ledger_bytes
        from tests.test_sound_design_cues import recipe
        with self.fixture() as (root, _):
            sf.write(root / 'sound.wav', np.zeros(2400, dtype='float32'), 24000)
            store = SoundDesignAssetStore(root, source_limit=65536, pcm_limit=65536, total_limit=256000)
            descriptor = store.import_asset('sound.wav')
            cue = recipe()
            cue['book']['book_id'] = 'book'
            cue['asset'] = {key: descriptor[key] for key in
                ('asset_id', 'sha256', 'frames', 'frame_rate', 'channels', 'sample_width')}
            version = get_sound_design_cue_version('cue-a', cue)
            registry = {'schema': 1, 'book': cue['book'], 'versions': [version],
                        'selected': {'cue-a': version['version']}}
            (root / '.sound_design_cues.json').write_bytes(get_sound_design_registry_bytes(registry))
            sf.write(root / 'alternate.wav', np.full(1200, 0.1, dtype='float32'), 24000)
            alternate = store.import_asset('alternate.wav')
            alternate_row = next(row for row in store._load()['assets']
                                 if row['asset_id'] == alternate['asset_id'])
            artifact = {'asset_id': alternate['asset_id'], 'source': alternate_row['source'],
                        'canonical': alternate_row['canonical'], 'offset_frames': 0}
            rendition = get_sound_rendition_record(
                {'cue_id': 'cue-a', 'version': version['version'], 'book': cue['book']},
                {'scaffold_sha256': None, 'arrangement_sha256': None, 'family_id': None},
                {'kind': 'imported_audio', 'renderer': None},
                {'kind': 'import', 'parent': None}, artifact, [])
            (root / '.sound_design_renditions.json').write_bytes(get_sound_rendition_ledger_bytes(
                {'schema': 1, 'book_id': 'book', 'records': [rendition], 'keep': []}))
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            names = {item['path'] for item in result['files']}
            base = '.sound_design_assets/' + descriptor['asset_id']
            self.assertTrue({'.sound_design_assets/manifest.json', base + '.source', base + '.pcm'} <= names)
            alternate_base = '.sound_design_assets/' + alternate['asset_id']
            self.assertTrue({alternate_base + '.source', alternate_base + '.pcm'} <= names)
            self.assertIn('sound_shared_asset_restore_not_implemented', result['unresolved'])
            self.assertNotIn('sound_asset_bytes_mismatch', result['unresolved'])
            pcm = root / (base + '.pcm')
            changed = bytearray(pcm.read_bytes()); changed[0] ^= 1; pcm.write_bytes(changed)
            damaged = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertIn('sound_asset_bytes_mismatch', damaged['unresolved'])
            self.assertEqual(bytes(changed), pcm.read_bytes())
            self.assertFalse(damaged['ready_for_capture'])

    def test_sound_companions_for_another_book_are_not_admitted(self):
        from sound_design_registry import get_sound_design_registry_bytes
        from tests.test_sound_design_registry import registry
        with self.fixture() as (root, _):
            raw = get_sound_design_registry_bytes(registry(book_id='other-book'))
            (root / '.sound_design_cues.json').write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'another book'):
                inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertEqual(raw, (root / '.sound_design_cues.json').read_bytes())

    def test_untrusted_external_source_is_not_opened(self):
        with self.fixture() as (root, state), tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'not-a-book.txt'
            secret.write_text('private')
            state['input_file_path'] = str(secret)
            (root / 'state.json').write_text(json.dumps(state))
            capture = inventory.capture_scope_file
            with patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertTrue(all(Path(call.args[0]) == root for call in opened.call_args_list))
            self.assertIn('source_path_outside_selection_authority', result['unresolved'])
            self.assertNotIn(str(secret), json.dumps(result))
            self.assertFalse(result['ready_for_capture'])

    def test_untrusted_voice_path_does_not_authorize_other_internal_files(self):
        with self.fixture() as (root, _):
            secret = root / 'private.json'
            secret.write_text('{"secret":"not a voice"}')
            (root / 'voice_config.json').write_text(json.dumps({'Alice': {'ref_audio': str(secret)}}))
            capture = inventory.capture_scope_file
            with patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertNotIn('private.json', [call.args[1] for call in opened.call_args_list])
            self.assertIn('external_or_invalid_voice_asset_requires_inspection', result['unresolved'])

    def test_configured_external_upload_and_companion_are_counted_and_fenced(self):
        with self.fixture() as (root, state), tempfile.TemporaryDirectory() as upload_tmp:
            uploads = Path(upload_tmp).resolve()
            source = uploads / 'original.txt'
            source.write_bytes(b'original source')
            companion = uploads / 'original.txt.publication.json'
            companion.write_bytes(b'{"still":"historical"}')
            state['input_file_path'] = str(source)
            (root / 'state.json').write_text(json.dumps(state))
            with patch.object(core, 'UPLOADS_DIR', str(uploads)):
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
                self.assertEqual('configured_upload', result['external'][0]['kind'])
                files = {row['path']: row for row in result['external'][0]['files']}
                self.assertTrue(files['original.txt']['exists'])
                self.assertTrue(files['original.txt.publication.json']['exists'])
                self.assertEqual('invalid', result['source_provenance'])
                self.assertIn('source_publication_provenance_invalid', result['unresolved'])
                self.assertIn('external_source_capture_and_restore_not_implemented', result['unresolved'])
                self.assertEqual(result['bytes'], sum(row['size'] for row in result['files']) +
                                 sum(row['size'] for row in files.values()))
                original_capture = inventory.capture_scope_file
                def mutate(directory, name, **kwargs):
                    if name == 'chunks.json' and kwargs.get('include_bytes'):
                        source.write_bytes(b'changed source')
                    return original_capture(directory, name, **kwargs)
                with patch.object(inventory, 'capture_scope_file', side_effect=mutate):
                    with self.assertRaisesRegex(ValueError, 'external candidate changed'):
                        inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
                self.assertEqual(b'changed source', source.read_bytes())
                self.assertFalse(result['ready_for_capture'])

    def test_external_upload_missing_companion_cannot_appear_mid_inventory(self):
        with self.fixture() as (root, state), tempfile.TemporaryDirectory() as upload_tmp:
            uploads = Path(upload_tmp).resolve()
            source = uploads / 'original.txt'
            source.write_bytes(b'original source')
            state['input_file_path'] = str(source)
            (root / 'state.json').write_text(json.dumps(state))
            capture = inventory.capture_scope_file
            def mutate(directory, name, **kwargs):
                if name == 'chunks.json' and kwargs.get('include_bytes'):
                    (uploads / 'original.txt.publication.json').write_bytes(b'new provenance')
                return capture(directory, name, **kwargs)
            with patch.object(core, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(inventory, 'capture_scope_file', side_effect=mutate):
                with self.assertRaisesRegex(ValueError, 'external candidate appeared'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertEqual(b'original source', source.read_bytes())

    def test_provenance_is_classified_without_rebinding_stale_metadata(self):
        with self.fixture() as (root, _):
            source = root / 'source.txt'
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            origin = hashlib.sha256(b'original epub').hexdigest()
            identity = hashlib.sha256(f'AA2_epub_publication_v1:{origin}:{digest}'.encode()).hexdigest()
            document = {'version': 1, 'binding': {'kind': 'text', 'sha256': digest},
                'candidates': [{'version': 1, 'id': identity,
                    'origin': {'kind': 'epub', 'filename': 'book.epub', 'sha256': origin,
                               'text_sha256': digest},
                    'fields': {'title': [], 'author': []}, 'other_creators': [],
                    'metadata_status': 'unavailable', 'cover': None,
                    'cover_status': 'not_declared', 'warnings': []}]}
            companion = root / 'source.txt.publication.json'
            original = json.dumps(document).encode()
            companion.write_bytes(original)
            current = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertEqual('current', current['source_provenance'])
            source.write_bytes(b'edited source')
            stale = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertEqual('stale', stale['source_provenance'])
            self.assertIn('source_publication_provenance_stale', stale['unresolved'])
            self.assertEqual(original, companion.read_bytes())

    def test_unselected_or_noncanonical_upload_names_never_grant_external_read(self):
        with self.fixture() as (root, state), tempfile.TemporaryDirectory() as upload_tmp:
            uploads = Path(upload_tmp).resolve()
            source = uploads / '.hidden.txt'
            source.write_bytes(b'outside source')
            state['input_file_path'] = str(source)
            (root / 'state.json').write_text(json.dumps(state))
            capture = inventory.capture_scope_file
            with patch.object(core, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertTrue(all(Path(call.args[0]) == root for call in opened.call_args_list))
            self.assertIn('source_path_outside_selection_authority', result['unresolved'])
            self.assertNotIn(str(source), json.dumps(result))

    def test_stored_source_path_cannot_authorize_arbitrary_internal_metadata(self):
        with self.fixture() as (root, state):
            secret = root / 'private' / 'config.json'
            secret.parent.mkdir()
            secret.write_text('{"private":"do not read"}')
            state['input_file_path'] = str(secret)
            (root / 'state.json').write_text(json.dumps(state))
            capture = inventory.capture_scope_file
            with patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertNotIn('private/config.json', [call.args[1] for call in opened.call_args_list])
            self.assertIn('source_path_outside_selection_authority', result['unresolved'])

    def test_selected_saved_script_is_inventoried_but_companions_are_not_sources(self):
        with self.fixture() as (root, state):
            scripts = root / 'scripts'
            scripts.mkdir()
            saved = scripts / 'story.json'
            saved.write_text('[{"text":"story"}]')
            state['input_file_path'] = str(saved)
            (root / 'state.json').write_text(json.dumps(state))
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertIn('scripts/story.json', {item['path'] for item in result['files']})
            companion = scripts / 'story.voice_config.json'
            companion.write_text('{"private":"voice configuration"}')
            state['input_file_path'] = str(companion)
            (root / 'state.json').write_text(json.dumps(state))
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertIn('source_path_outside_selection_authority', result['unresolved'])
            self.assertNotIn('scripts/story.voice_config.json', {item['path'] for item in result['files']})

    def test_valid_saved_book_suffixes_are_not_misclassified(self):
        from routers.scripts_library import _is_saved_book_filename
        with self.fixture() as (root, state):
            scripts = root / 'scripts'
            scripts.mkdir()
            for filename in ('story.checkpoint.json', 'story.review_completed.json'):
                with self.subTest(filename=filename):
                    saved = scripts / filename
                    self.assertTrue(_is_saved_book_filename(saved.name))
                    saved.write_text('[{"text":"story"}]')
                    state['input_file_path'] = str(saved)
                    (root / 'state.json').write_text(json.dumps(state))
                    result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
                    self.assertIn('scripts/' + filename, {item['path'] for item in result['files']})

    def test_changed_book_identity_is_refused_during_inventory(self):
        with self.fixture() as (root, state):
            capture = inventory.capture_scope_file
            def mutate(directory, name, **kwargs):
                if name == 'chunks.json':
                    state['book_generation'] = 'another-generation'
                    (root / 'state.json').write_text(json.dumps(state))
                return capture(directory, name, **kwargs)
            with patch.object(inventory, 'capture_scope_file', side_effect=mutate):
                with self.assertRaisesRegex(ValueError, 'candidate changed'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_chunk_path_outside_audio_store_does_not_authorize_metadata_read(self):
        with self.fixture() as (root, _):
            secret = root / 'private.json'
            secret.write_text('{"secret":"not audio"}')
            (root / 'chunks.json').write_text(json.dumps([{'uid': 'one', 'status': 'done',
                                                         'audio_path': 'private.json'}]))
            capture = inventory.capture_scope_file
            with patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertNotIn('private.json', [call.args[1] for call in opened.call_args_list])
            self.assertIn('chunk_audio_outside_known_storage', result['unresolved'])

    def test_voice_versions_discover_confined_files_without_reading_external_paths(self):
        with self.fixture() as (root, _), tempfile.TemporaryDirectory() as outside:
            secret = Path(outside) / 'private.wav'
            secret.write_bytes(b'not authorized')
            (root / 'voices').mkdir()
            (root / 'voices/reference.wav').write_bytes(b'reference')
            (root / 'voices/older.wav').write_bytes(b'older')
            (root / 'voices/adapter').mkdir()
            (root / 'voices/adapter/weights.bin').write_bytes(b'weights')
            voices = {'Alice': {'ref_audio': str(root / 'voices/reference.wav'),
                'versions': {'prior': {'ref_audio': str(root / 'voices/older.wav'),
                                       'adapter_path': str(root / 'voices/adapter')}},
                'candidates': [{'ref_audio': str(secret)}]}}
            (root / 'voice_config.json').write_text(json.dumps(voices))
            capture = inventory.capture_scope_file
            with patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            names = {item['path'] for item in result['files']}
            self.assertTrue({'voices/reference.wav', 'voices/older.wav',
                             'voices/adapter/weights.bin'} <= names)
            self.assertTrue(all(Path(call.args[0]) == root for call in opened.call_args_list))
            self.assertIn('external_or_invalid_voice_asset_requires_inspection', result['unresolved'])
            self.assertIn('voice_asset_resolution_and_shared_restore_not_qualified', result['unresolved'])
            self.assertNotIn(str(secret), json.dumps(result))
            self.assertFalse(result['ready_for_capture'])

    def test_changes_and_new_optional_files_during_scan_are_rejected(self):
        for mutate in ('change', 'appear'):
            with self.subTest(mutate=mutate), self.fixture() as (root, _):
                capture = inventory.capture_scope_file
                def side_effect(directory, name, **kwargs):
                    if name == 'chunks.json':
                        if mutate == 'change':
                            (root / 'annotated_script.json').write_text('[{"changed":true}]')
                        else:
                            (root / 'voice_config.json').write_text('{}')
                    return capture(directory, name, **kwargs)
                with patch.object(inventory, 'capture_scope_file', side_effect=side_effect):
                    with self.assertRaisesRegex(ValueError, 'changed|appeared'):
                        inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_missing_audio_is_reported_and_escaping_paths_are_refused(self):
        with self.fixture() as (root, _):
            chunks = [{'uid': 'one', 'status': 'done', 'audio_path': 'voicelines/missing.wav'}]
            (root / 'chunks.json').write_text(json.dumps(chunks))
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertIn('required_artifact_missing', result['unresolved'])
            chunks[0]['audio_path'] = '../outside.wav'
            (root / 'chunks.json').write_text(json.dumps(chunks))
            with self.assertRaisesRegex(ValueError, 'escapes'):
                inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_invalid_state_and_byte_limits_are_rejected(self):
        with self.fixture() as (root, _):
            with self.assertRaises(ValueError):
                inventory.inspect_active_book_recovery_candidates(root, byte_limit=4)
            (root / 'state.json').write_text('{"active_book_id":"book","extra":NaN}')
            with self.assertRaises(ValueError):
                inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_new_file_reads_are_bounded_by_remaining_inventory_budget(self):
        with self.fixture() as (root, _):
            capture = inventory.capture_scope_file
            with patch.object(inventory, 'capture_scope_file', wraps=capture) as opened:
                result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            calls = [call for call in opened.call_args_list if call.args[1] == 'source.txt']
            self.assertEqual(1, len(calls))
            self.assertLess(calls[0].kwargs['limit'], 1000000)
            self.assertLessEqual(result['bytes'], 1000000)

    def test_history_manifest_limit_is_applied_before_full_read(self):
        with self.fixture() as (root, _):
            history = root / 'take_history'
            history.mkdir()
            (history / 'manifest.json').write_bytes(b' ' * 100)
            with patch.object(inventory, 'MAX_HISTORY_MANIFEST_BYTES', 20):
                with self.assertRaises(ValueError):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_checkpoint_discovery_is_bounded_and_preserves_unknown_epochs(self):
        with self.fixture() as (root, _):
            unknown = root / 'annotated_script.json.generation_checkpoint.json.parts-unknown'
            unknown.mkdir()
            (unknown / 'keep').write_text('do not delete')
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertIn('unrecognized_checkpoint_artifacts_require_inspection', result['unresolved'])
            self.assertTrue((unknown / 'keep').exists())
            with patch.object(inventory, 'MAX_ROOT_ENTRIES', 1):
                with self.assertRaisesRegex(ValueError, 'root entry count'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_new_root_entry_after_checkpoint_discovery_is_rejected(self):
        with self.fixture() as (root, _):
            capture = inventory.capture_scope_file
            def side_effect(directory, name, **kwargs):
                if name == 'source.txt':
                    (root / 'new-root-file').write_text('concurrent update')
                return capture(directory, name, **kwargs)
            with patch.object(inventory, 'capture_scope_file', side_effect=side_effect):
                with self.assertRaisesRegex(ValueError, 'root changed'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_export_tree_discovery_is_bounded_before_materializing_entries(self):
        with self.fixture() as (root, _):
            directory = root / 'chapter_exports'
            directory.mkdir()
            for i in range(50):
                (directory / f'{i}.wav').write_bytes(b'x')
            with patch.object(inventory, 'MAX_FILES', 40):
                with self.assertRaisesRegex(ValueError, 'candidate count'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    @unittest.skipIf(os.name == 'nt', 'Case-conflicting directories require a case-sensitive filesystem')
    def test_inventory_refuses_conflicting_directory_prefixes(self):
        with self.fixture() as (root, _):
            exports = root / 'chapter_exports'
            exports.mkdir()
            for name, filename in (('Foo', 'first.wav'), ('foo', 'second.wav')):
                directory = exports / name
                try:
                    directory.mkdir()
                except FileExistsError:
                    self.skipTest('Filesystem is case-insensitive')
                (directory / filename).write_bytes(b'audio')
            with self.assertRaisesRegex(ValueError, 'conflicting portable paths'):
                inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            self.assertTrue((exports / 'Foo/first.wav').exists())
            self.assertTrue((exports / 'foo/second.wav').exists())

    def test_global_candidate_limit_counts_directories_and_later_files_together(self):
        with self.fixture() as (root, _):
            exports = root / 'chapter_exports'
            exports.mkdir()
            for index in range(4):
                (exports / str(index)).mkdir()
            result = inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)
            total = len(result['files']) + len(result['missing']) + len(result['directories'])
            with patch.object(inventory, 'MAX_FILES', total - 1):
                with self.assertRaisesRegex(ValueError, 'candidate count'):
                    inventory.inspect_active_book_recovery_candidates(root, byte_limit=1000000)

    def test_sound_companion_read_honors_cancellation(self):
        import project_scope_files
        from audio_generation_owner import AudioGenerationCancelled
        from sound_design_renditions import capture_sound_design_companions
        from sound_design_registry import get_sound_design_registry_bytes
        from tests.test_sound_design_registry import registry
        with self.fixture() as (root, _):
            path = root / '.sound_design_cues.json'
            raw = get_sound_design_registry_bytes(registry(book_id='book'))
            path.write_bytes(raw)
            capture = project_scope_files.capture_scope_file
            cancelled = [False]
            def read(directory, name, **kwargs):
                if name == '.sound_design_cues.json':
                    cancelled[0] = True
                return capture(directory, name, **kwargs)
            with patch.object(project_scope_files, 'capture_scope_file', side_effect=read):
                with self.assertRaises(AudioGenerationCancelled):
                    capture_sound_design_companions(root, '.sound_design_cues.json',
                        '.sound_design_renditions.json', expected_book_id='book',
                        cancel_check=lambda: cancelled[0])
            self.assertEqual(raw, path.read_bytes())
