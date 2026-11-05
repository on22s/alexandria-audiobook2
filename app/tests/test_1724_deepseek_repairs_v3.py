"""#1724 DeepSeek v3 repairs: backend regressions (real FastAPI routes, temp files).

Covers corrupt-listing refusal, nested-versions refusal, malformed-alias
eligibility, omitted-pin path-spelling preservation, and OSError admission
mapping. Synthetic fixtures only; no inference, no model loads.
"""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import voices
from voice_config_store import get_voice_config_revision
from book_state_transaction import ensure_book_state, get_book_snapshot, get_book_snapshot_token

PIN = 'a' * 64

# A script with persona-state targets (gender/age markers) so get_voice_rows
# exercises the persona_states branch. Borrow the canonical fixture shape.
from tests.test_state_personas import state_script  # noqa: E402


def _guards(root):
    with ensure_book_state(str(root)):
        snapshot = get_book_snapshot(str(root), allow_missing_script=True)
        return {'revision': get_voice_config_revision(snapshot['voices']),
                'book_token': get_book_snapshot_token(snapshot)}


class DeepSeekRepairsV3BackendTests(unittest.TestCase):
    def fixture(self, root, stack, script_entries, voice_config):
        script = root / 'annotated_script.json'
        script.write_text(json.dumps(script_entries), encoding='utf-8')
        path = root / 'voice_config.json'
        path.write_text(json.dumps(voice_config), encoding='utf-8')
        (root / 'state.json').write_text(
            '{"active_book_id":"book","book_generation":"one"}', encoding='utf-8')
        stack.enter_context(patch.object(voices, 'VOICE_CONFIG_PATH', str(path)))
        stack.enter_context(patch.object(voices, 'SCRIPT_PATH', str(script)))
        app = FastAPI()
        app.include_router(voices.router)
        client = stack.enter_context(TestClient(app, raise_server_exceptions=False))
        return client, path, script

    # --- corrupt listing (DS3005) -------------------------------------
    def test_corrupt_listing_refuses_both_files_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'custom', 'voice': 'Serena'}})
            for label, target in (('voice_config', path), ('script', script)):
                with self.subTest(corrupt=label):
                    original = target.read_bytes()
                    target.write_bytes(b'{not json')
                    before = {p: p.read_bytes() for p in (path, script)}
                    listing = client.get('/api/voices')
                    snapshot = client.get('/api/voice_config/snapshot')
                    self.assertEqual(400, listing.status_code, listing.text)
                    self.assertEqual(400, snapshot.status_code, snapshot.text)
                    self.assertEqual(before, {p: p.read_bytes() for p in (path, script)},
                                     'refusal must not rewrite either file')
                    target.write_bytes(original)

    def test_absent_and_inactive_malformed_legacy_stay_ordinary(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ZED_LEGACY': 'not-a-dict'})
            listing = client.get('/api/voices')
            self.assertEqual(200, listing.status_code, listing.text)
            rows = listing.json()
            self.assertEqual('ALICE', rows[0]['name'])
            self.assertEqual({}, rows[0]['config'], 'absent speaker stays unconfigured')
            snapshot = client.get('/api/voice_config/snapshot')
            self.assertEqual(200, snapshot.status_code, snapshot.text)
            self.assertEqual('not-a-dict', json.loads(path.read_text())['ZED_LEGACY'],
                             'inactive malformed legacy entry is preserved untouched')

    # --- nested versions (DS3015) --------------------------------------
    def test_nested_versions_null_means_absent_but_list_refuses(self):
        script_entries = state_script()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack, script_entries,
                {'ARTHUR': {'type': 'custom', 'voice': 'Ryan', 'versions': None}})
            listing = client.get('/api/voices')
            self.assertEqual(200, listing.status_code, listing.text)
            arthur = next(r for r in listing.json() if r['name'] == 'ARTHUR')
            self.assertIn('persona_states', arthur, 'null versions reads as absent')
            # Truly malformed active nested structure refuses clearly.
            config = json.loads(path.read_text())
            config['ARTHUR']['versions'] = []
            path.write_text(json.dumps(config))
            before = path.read_bytes()
            for method, url in (('GET', '/api/voices'),
                                ('GET', '/api/voice_config/snapshot')):
                with self.subTest(route=url):
                    resp = client.request(method, url)
                    self.assertEqual(400, resp.status_code, resp.text)
            self.assertEqual(before, path.read_bytes())

    def test_nested_versions_string_refuses_on_preview_too(self):
        script_entries = state_script()
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack, script_entries,
                {'ARTHUR': {'type': 'custom', 'voice': 'Ryan', 'versions': 'bad'}})
            listing = client.get('/api/voices')
            self.assertEqual(400, listing.status_code, listing.text)
            guards = _guards(root)
            resp = client.post('/api/voice_config/single-voice-preview', json={
                'source_speaker': 'ARTHUR', **guards})
            self.assertEqual(400, resp.status_code, resp.text)

    # --- malformed alias eligibility (DS3008) --------------------------
    def test_whitespace_and_nonstring_alias_do_not_suppress_persona_states(self):
        script_entries = state_script()
        for alias_value in (' ', 123, True):
            with self.subTest(alias=repr(alias_value)), \
                    tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                client, path, script = self.fixture(
                    root, stack, script_entries,
                    {'ARTHUR': {'type': 'custom', 'voice': 'Ryan',
                                'alias': alias_value}})
                listing = client.get('/api/voices')
                self.assertEqual(200, listing.status_code, listing.text)
                arthur = next(r for r in listing.json() if r['name'] == 'ARTHUR')
                self.assertIn('persona_states', arthur,
                              'runtime-ignored alias must not suppress state rows')
                self.assertIn('persona_states_pending', arthur)
        # A valid non-blank alias still suppresses (runtime resolves it).
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack, script_entries,
                {'ARTHUR': {'type': 'custom', 'voice': 'Ryan', 'alias': 'NARRATOR'}})
            arthur = next(r for r in client.get('/api/voices').json()
                          if r['name'] == 'ARTHUR')
            self.assertNotIn('persona_states', arthur)

    # --- omitted-pin path spelling (DS3031) ----------------------------
    def test_omitted_pin_same_asset_path_spelling_preserves_p(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'lora', 'adapter_id': 'voice1',
                           'adapter_path': 'lora_models/voice1',
                           'adapter_generation_sha256': PIN}})
            guards = _guards(root)
            # Omitted pin, same adapter id, absolute spelling of the same path.
            abs_path = str(root / 'lora_models' / 'voice1')
            resp = client.post('/api/voice_config/save', json={
                **guards, 'voices': {'ALICE': {'type': 'lora', 'adapter_id': 'voice1',
                                               'adapter_path': abs_path}}})
            self.assertEqual(200, resp.status_code, resp.text)
            saved = json.loads(path.read_text())
            self.assertEqual(PIN, saved['ALICE']['adapter_generation_sha256'],
                             'same-asset spelling move must preserve P')
            self.assertEqual(abs_path, saved['ALICE']['adapter_path'])

    def test_omitted_pin_genuine_replacement_clears_p(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'lora', 'adapter_id': 'voice1',
                           'adapter_path': 'lora_models/voice1',
                           'adapter_generation_sha256': PIN}})
            guards = _guards(root)
            resp = client.post('/api/voice_config/save', json={
                **guards, 'voices': {'ALICE': {'type': 'lora', 'adapter_id': 'voice2',
                                               'adapter_path': 'lora_models/voice2'}}})
            self.assertEqual(200, resp.status_code, resp.text)
            saved = json.loads(path.read_text())
            self.assertIsNone(saved['ALICE']['adapter_generation_sha256'],
                              'genuine asset replacement must clear the old pin')

    def test_explicit_pin_and_null_always_win(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'lora', 'adapter_id': 'voice1',
                           'adapter_path': 'lora_models/voice1',
                           'adapter_generation_sha256': PIN}})
            guards = _guards(root)
            resp = client.post('/api/voice_config/save', json={
                **guards, 'voices': {'ALICE': {'type': 'lora', 'adapter_id': 'voice2',
                                               'adapter_path': 'lora_models/voice2',
                                               'adapter_generation_sha256': 'b' * 64}}})
            self.assertEqual(200, resp.status_code, resp.text)
            self.assertEqual('b' * 64,
                             json.loads(path.read_text())['ALICE']['adapter_generation_sha256'])
            guards = _guards(root)
            resp = client.post('/api/voice_config/save', json={
                **guards, 'voices': {'ALICE': {'type': 'lora', 'adapter_id': 'voice2',
                                               'adapter_path': 'lora_models/voice2',
                                               'adapter_generation_sha256': None}}})
            self.assertEqual(200, resp.status_code, resp.text)
            self.assertIsNone(
                json.loads(path.read_text())['ALICE']['adapter_generation_sha256'])

    def test_whitespace_alias_same_voice_no_false_approval_reset(self):
        # DS3039: same direct voice + approved status + whitespace
        # alias (runtime-ignored) must not read as an audible change.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'},
                 {'speaker': 'BOB', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'custom', 'voice': 'Serena', 'alias': ' ',
                           'voice_status': 'approved'},
                 'BOB': {'type': 'custom', 'voice': 'Serena', 'alias': ' ',
                         'voice_status': 'approved'}})
            guards = _guards(root)
            resp = client.post('/api/voice_config/single-voice-preview', json={
                'source_speaker': 'ALICE', **guards})
            self.assertEqual(200, resp.status_code, resp.text)
            body = resp.json()
            for row in body['speakers']:
                self.assertNotIn('voice approval reset (needs new listening approval)',
                                 row['changes'])
                self.assertNotIn('voice changed', row['changes'])
            # Ready reset is unconditional (kept); applied patch keeps approval.
            save = client.post('/api/voice_config/save', json={
                'revision': body['revision'], 'book_token': body['book_token'],
                'voices': body['voices']})
            self.assertEqual(200, save.status_code, save.text)
            saved = json.loads(path.read_text())
            self.assertEqual('approved', saved['ALICE']['voice_status'])
            self.assertEqual('approved', saved['BOB']['voice_status'])
            self.assertNotIn('alias', saved['BOB'])
            self.assertFalse(saved['BOB'].get('alias_of'))

    def test_invalid_map_refuses_before_audio_invalidation(self):
        # DS3021 remainder: a valid voice change carrying nonfinite data
        # must refuse BEFORE any chunks/journal/audio invalidation.
        from tests.test_suggestion_audio_invalidation import SuggestionAudioInvalidationTests
        case = SuggestionAudioInvalidationTests()
        with case.fixture() as (root, client, manager):
            import copy as _copy
            before_chunks = _copy.deepcopy(manager.load_chunks())
            config = json.loads((root / 'voices.json').read_bytes())
            body = {'revision': get_voice_config_revision(config),
                    'book_token': get_book_snapshot_token(
                        get_book_snapshot(str(root), include_voices=False)),
                    'voices': {'Hero': {'type': 'custom', 'voice': 'Serena',
                                        'versions': {'unused': {'n': float('nan')}}}}}
            before = {p: p.read_bytes() for p in (root / 'voices.json', root / 'chunks.json')}
            reply = client.post('/api/voice_config/save', content=json.dumps(body),
                                headers={'content-type': 'application/json'})
            self.assertEqual(400, reply.status_code, reply.text)
            self.assertEqual(before, {p: p.read_bytes() for p in (root / 'voices.json', root / 'chunks.json')},
                             'invalid payload must leave config and chunks untouched')
            self.assertEqual(before_chunks, manager.load_chunks(),
                             'no audio row may be invalidated by a refused payload')
            # Finite valid control still persists and invalidates.
            body['voices'] = {'Hero': {'type': 'custom', 'voice': 'Serena'}}
            body['revision'] = get_voice_config_revision(config)
            reply = client.post('/api/voice_config/save', json=body)
            self.assertEqual(200, reply.status_code, reply.text)
            self.assertEqual('Serena', json.loads((root / 'voices.json').read_text())['Hero']['voice'])

    # --- DS3010 other-writer pin clearing -------------------------------
    def test_suggestions_writer_clears_unrelated_pin_only_on_replacement(self):
        import core
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'builtin_lora', 'adapter_id': 'a1',
                           'adapter_path': 'builtin_lora/a1',
                           'adapter_generation_sha256': PIN}})
            stack.enter_context(patch.object(core, 'DATA_DIR', str(root)))
            with patch.object(voices, '_script_line_counts', return_value={'ALICE': 1}), \
                    patch.object(voices, '_build_lora_candidates', return_value=[
                        {'adapter_id': 'a2', 'name': 'A2', 'type': 'builtin_lora'}]):
                resp = client.post('/api/suggest_voices/apply', json={'character': 'ALICE',
                    'suggestion': {'adapter_id': 'a2', 'book_id': 'book', 'reason': 'synthetic'}})
            self.assertEqual(200, resp.status_code, resp.text)
            saved = json.loads(path.read_text())
            self.assertEqual('a2', saved['ALICE']['adapter_id'])
            self.assertIsNone(saved['ALICE']['adapter_generation_sha256'],
                              'genuine replacement must clear unrelated pin')

    def test_version_select_writer_clears_unrelated_pin_only_on_replacement(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'lora', 'adapter_id': 'a1',
                           'adapter_path': 'lora_models/a1',
                           'adapter_generation_sha256': PIN,
                           'versions': {'v2': {'type': 'lora', 'adapter_id': 'a2',
                                               'adapter_path': 'lora_models/a2'}}}})
            guards = _guards(root)
            resp = client.post(f'/api/voices/ALICE/versions/v2/select',
                               json={'book_token': guards['book_token']})
            self.assertEqual(200, resp.status_code, resp.text)
            saved = json.loads(path.read_text())
            self.assertEqual('a2', saved['ALICE']['adapter_id'])
            self.assertIsNone(saved['ALICE']['adapter_generation_sha256'])

    # --- OSError admission mapping (DS3004) ----------------------------
    def test_filesystem_oserror_is_honest_refusal_not_500(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path, script = self.fixture(
                root, stack,
                [{'speaker': 'ALICE', 'text': 'Synthetic line.'}],
                {'ALICE': {'type': 'custom', 'voice': 'Serena'}})
            guards = _guards(root)

            def deny(*args, **kwargs):
                raise PermissionError('synthetic lock denial')

            with patch.object(voices, 'get_book_snapshot', side_effect=deny):
                snapshot = client.get('/api/voice_config/snapshot')
                self.assertIn(snapshot.status_code, (409, 503), snapshot.text)
                preview = client.post('/api/voice_config/single-voice-preview',
                                      json={'source_speaker': 'ALICE', **guards})
                self.assertIn(preview.status_code, (409, 503), preview.text)
                listing = client.get('/api/voices')
                self.assertIn(listing.status_code, (409, 503), listing.text)


class ReadAdmissionEntryTests(unittest.TestCase):
    """Entry-stage admission failures map honestly at the read facade."""

    def _client(self, root, stack):
        script = root / 'annotated_script.json'
        script.write_text('[{"speaker":"ALICE","text":"Synthetic line."}]')
        path = root / 'voice_config.json'
        path.write_text('{"ALICE":{"type":"custom","voice":"Serena"}}')
        (root / 'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
        stack.enter_context(patch.object(voices, 'VOICE_CONFIG_PATH', str(path)))
        stack.enter_context(patch.object(voices, 'SCRIPT_PATH', str(script)))
        app = FastAPI()
        app.include_router(voices.router)
        return stack.enter_context(TestClient(app, raise_server_exceptions=False)), path

    def test_ensure_book_state_entry_failures(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path = self._client(root, stack)
            guards = _guards(root)
            before = path.read_bytes()
            cases = [
                ('oserror', OSError('synthetic admission IO failure'), 409),
                ('valueerror-journal', ValueError('invalid journal'), 409),
                ('timeout', TimeoutError('synthetic busy'), 503),
            ]
            # Realistic entry failure: the context manager raises from
            # __enter__, exactly like a real admission/lock failure.
            import contextlib

            def entry_raiser(error):
                @contextlib.contextmanager
                def manager(*args, **kwargs):
                    raise error
                    yield
                return manager

            for label, error, expected in cases:
                with self.subTest(case=label), \
                        patch.object(voices, 'ensure_book_state', entry_raiser(error)):
                    listing = client.get('/api/voices')
                    self.assertEqual(expected, listing.status_code, listing.text)
                    snapshot = client.get('/api/voice_config/snapshot')
                    self.assertEqual(expected, snapshot.status_code, snapshot.text)
                    preview = client.post('/api/voice_config/single-voice-preview',
                                          json={'source_speaker': 'ALICE', **guards})
                    self.assertEqual(expected, preview.status_code, preview.text)
            self.assertEqual(before, path.read_bytes(), 'files unchanged on refusal')
            # Programmer errors stay visible (never masked as refusals).
            with patch.object(voices, 'ensure_book_state', entry_raiser(TypeError('bug'))):
                listing = client.get('/api/voices')
                self.assertEqual(500, listing.status_code)

    def test_lock_entry_timeout_is_busy(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, path = self._client(root, stack)
            with patch.object(voices, 'file_lock', side_effect=TimeoutError('busy')):
                snapshot = client.get('/api/voice_config/snapshot')
                self.assertEqual(503, snapshot.status_code, snapshot.text)


class SavedScriptLoadIdentityTests(unittest.TestCase):
    """DS3074: /api/scripts/load response identity belongs to loaded book."""

    def _setup(self, root, stack):
        import copy
        import core
        from routers import scripts_library as sl
        scripts = root / 'scripts'
        scripts.mkdir()
        (scripts / 'bookA.json').write_text(
            '[{"speaker":"ALICE","text":"A line."}]')
        (scripts / 'bookB.json').write_text(
            '[{"speaker":"BOB","text":"B line."}]')
        (root / 'state.json').write_text(
            '{"active_book_id":"bookA","book_generation":"genA"}')
        (root / 'voice_library.json').write_text('{}')
        state = copy.deepcopy(core.process_state)
        for value in state.values():
            value['running'] = False
        for owner, name, value in [
                (core, 'DATA_DIR', str(root)), (core, 'SCRIPTS_DIR', str(scripts)),
                (sl, 'DATA_DIR', str(root)), (sl, 'SCRIPTS_DIR', str(scripts)),
                (sl, 'SCRIPT_PATH', str(root / 'annotated_script.json')),
                (sl, 'VOICE_CONFIG_PATH', str(root / 'voice_config.json')),
                (sl, 'CHUNKS_PATH', str(root / 'chunks.json')),
                (sl, 'AUDIOBOOK_PATH', str(root / 'audiobook.wav')),
                (sl, 'M4B_PATH', str(root / 'audiobook.m4b')),
                (sl, 'process_state', state)]:
            stack.enter_context(patch.object(owner, name, value))
        return sl

    def test_returned_identity_belongs_to_loaded_book_not_later_book(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            from book_state_transaction import get_book_operation_token
            root = Path(tmp)
            lib = self._setup(root, stack)
            from routers.scripts_library import ScriptLoadRequest
            result_b = lib._load_script_sync(ScriptLoadRequest(name='bookB'))
            token_b = result_b['book_identity_token']
            self.assertRegex(token_b, r'^[0-9a-f]{64}$')
            self.assertEqual(token_b, get_book_operation_token(str(root)),
                             'returned identity must be the loaded book B token')
            # A second tab loads C-equivalent (bookA again): B's returned
            # identity is already captured and cannot become A's token.
            lib._load_script_sync(ScriptLoadRequest(name='bookA'))
            self.assertNotEqual(token_b, get_book_operation_token(str(root)))

    def test_token_captured_under_lease_survives_interleaving_book_switch(self):
        # Controlled interleaving at the load lease __exit__: after the
        # real lease exits, book C is committed to state.json. An
        # implementation reading the token unscoped after release would
        # return C's token for a load of B; the fixed implementation
        # derives B's token from the committed state record under lease.
        import contextlib
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            from book_state_transaction import get_book_operation_token
            root = Path(tmp)
            lib = self._setup(root, stack)
            from routers.scripts_library import ScriptLoadRequest
            real_ensure = lib.ensure_book_state

            @contextlib.contextmanager
            def hooked_ensure(*args, **kwargs):
                with real_ensure(*args, **kwargs):
                    yield
                # Lease released: another tab commits book C now.
                (root / 'state.json').write_text(json.dumps(
                    {'active_book_id': 'bookC', 'book_generation': 'genC',
                     'input_file_path': 'bookC.json'}))

            with patch.object(lib, 'ensure_book_state', hooked_ensure):
                result_b = lib._load_script_sync(ScriptLoadRequest(name='bookB'))
            current_c_token = get_book_operation_token(str(root))
            self.assertRegex(result_b['book_identity_token'], r'^[0-9a-f]{64}$')
            self.assertNotEqual(result_b['book_identity_token'], current_c_token,
                                'returned identity must belong to loaded B, not interleaved C')

    def test_unverifiable_identity_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            lib = self._setup(root, stack)
            from routers.scripts_library import ScriptLoadRequest
            with patch.object(lib, 'get_book_operation_token',
                              side_effect=ValueError('unverifiable')):
                result = lib._load_script_sync(ScriptLoadRequest(name='bookB'))
            self.assertIsNone(result['book_identity_token'],
                              'missing identity must fail closed, never adopt another book')


class SourceParentSubstitutionTests(unittest.TestCase):
    """DS3076: generation readers must refuse an aliased source parent."""

    def _swap_parent(self, data, outside):
        uploads = data / 'uploads'
        saved = data / 'uploads-original'
        uploads.mkdir(parents=True, exist_ok=True)
        source = uploads / 'source.txt'
        source.write_text('Inside synthetic source.', encoding='utf-8')
        outside.mkdir(parents=True, exist_ok=True)
        (outside / 'source.txt').write_text(
            'OUTSIDE SENTINEL. Must not be read.', encoding='utf-8')
        import os, subprocess
        os.rename(uploads, saved)
        if os.name == 'nt':
            subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                            str(uploads), str(outside)],
                           capture_output=True, check=True)
        else:
            os.symlink(outside, uploads, target_is_directory=True)
        return source

    def _restore_parent(self, data):
        import os
        uploads = data / 'uploads'
        saved = data / 'uploads-original'
        if saved.exists() and os.path.lexists(uploads):
            if os.path.islink(uploads):
                os.unlink(uploads)
            else:
                os.rmdir(uploads)
        if saved.exists():
            os.rename(saved, uploads)

    def test_both_generation_readers_refuse_swapped_parent(self):
        import os
        import subprocess
        from script_selection_binding import _read_source_snapshot, capture_generation_selection
        from generation_completion import get_generation_input
        with tempfile.TemporaryDirectory(prefix='aa2-1724-ds3076-') as name:
            owned = Path(name)
            data, outside = owned / 'data', owned / 'outside-fixture'
            data.mkdir()
            (data / 'state.json').write_text(json.dumps(
                {'active_book_id': 'book', 'book_generation': 'one'}))
            source = self._swap_parent(data, outside)
            try:
                # Valid control first would require pre-swap read; verify
                # the swap actually took effect by resolving the path.
                self.assertTrue(os.path.lexists(data / 'uploads'))
                with self.assertRaises(ValueError):
                    _read_source_snapshot(str(source))
                with self.assertRaises(ValueError):
                    get_generation_input(str(source), owning_root=str(data))
                # capture_generation_selection (HTTP generation seam) also
                # refuses through the same shared reader.
                (data / 'state.json').write_text(json.dumps(
                    {'active_book_id': 'book', 'book_generation': 'one',
                     'input_file_path': str(source)}))
                with self.assertRaises(ValueError):
                    capture_generation_selection(str(data), str(source))
            finally:
                self._restore_parent(data)
            self.assertEqual('Inside synthetic source.',
                             (data / 'uploads' / 'source.txt').read_text(encoding='utf-8'))

    def test_digest_only_read_requests_no_bytes(self):
        # DS3076 seam must stream digest-only reads without retaining
        # source bytes (no new 512 MiB allocation on admission checks).
        from script_selection_binding import _read_source_snapshot
        import project_scope_files
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            text = 'Digest only synthetic source.'
            source.write_bytes(text.encode('utf-8'))
            real_capture = project_scope_files.capture_scope_file
            seen = {}

            def spy(root_arg, relative, *, limit, include_bytes=False, cancel_check=None):
                seen['include_bytes'] = include_bytes
                return real_capture(root_arg, relative, limit=limit,
                                    include_bytes=include_bytes,
                                    cancel_check=cancel_check)

            with patch.object(project_scope_files, 'capture_scope_file', spy):
                digest, raw = _read_source_snapshot(str(source))
            self.assertIs(seen['include_bytes'], False,
                          'digest-only call must not request bytes')
            self.assertIsNone(raw)
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
            # Meaningful bytes control still returns the exact bytes.
            digest2, raw2 = _read_source_snapshot(str(source), include_bytes=True)
            self.assertEqual(text.encode(), raw2)
            self.assertEqual(digest, digest2)

    def test_plain_source_valid_controls(self):
        from script_selection_binding import _read_source_snapshot
        from generation_completion import get_generation_input
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            text = 'Plain synthetic source text.\nSecond line.'
            source.write_bytes(text.encode('utf-8'))
            digest, raw = _read_source_snapshot(str(source), include_bytes=True)
            self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), digest)
            self.assertEqual(text.encode(), raw)
            content, sha = get_generation_input(str(source), owning_root=str(root))
            self.assertEqual(digest, sha)
            self.assertIn('Plain synthetic source text.', content)


if __name__ == '__main__':
    unittest.main()
