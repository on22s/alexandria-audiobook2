"""HTTP alias corrections use the same bounded canonical graph policy."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
from routers import script
import speaker_identity


class ManualAliasValidationTests(unittest.TestCase):
    def test_invalid_graphs_and_oversized_registry_preserve_native_saved_bytes(self):
        app = FastAPI()
        app.include_router(script.router)
        name_limit = getattr(speaker_identity, 'MAX_ALIAS_NAME_LENGTH', 1024)
        count_limit = getattr(speaker_identity, 'MAX_ALIAS_COUNT', 10000)
        cases = [({'A': 'B', 'B': 'A'}, 'cycle'),
                 ({'A': ' b ', 'B': 'a'}, 'cycle'),
                 ({'A': 'ROOT', ' a ': 'OTHER'}, 'conflicting'),
                 ({'A': 'ROOT', ' A ': 'OTHER'}, 'conflicting'),
                 ({'a' * (name_limit + 1): 'ROOT'}, 'length'),
                 ({'A': 'r' * (name_limit + 1)}, 'length'),
                 ({f'ALIAS_{i}': 'ROOT' for i in range(count_limit + 1)}, 'count')]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.json'
            path.write_bytes(b'{"existing":"ROOT"}\n')
            before = path.read_bytes()
            with patch.object(script, 'CHARACTER_ALIASES_PATH', str(path)), \
                 patch.object(script, 'DATA_DIR', tmp), patch.object(core, 'process_state', {}), \
                 patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}), \
                 TestClient(app) as client:
                for payload, detail in cases:
                    with self.subTest(case=detail, size=len(payload)):
                        original = copy.deepcopy(payload)
                        response = client.post('/api/character_aliases', json=payload)
                        self.assertEqual(400, response.status_code, response.text[:200])
                        self.assertIn(detail, response.json()['detail'])
                        self.assertEqual(before, path.read_bytes())
                        self.assertEqual(original, payload)

    def test_identity_case_fixes_chains_and_blank_editor_rows_keep_existing_contract(self):
        app = FastAPI()
        app.include_router(script.router)
        payload = {' A ': ' B ', 'B': 'ROOT', 'bob': 'BOB', 'IDENTITY': 'IDENTITY', ' ': 'discard'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.json'
            with patch.object(script, 'CHARACTER_ALIASES_PATH', str(path)), \
                 patch.object(script, 'DATA_DIR', tmp), patch.object(core, 'process_state', {}), \
                 patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}), \
                 TestClient(app) as client:
                response = client.post('/api/character_aliases', json=payload)
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual({'status': 'saved', 'count': 4}, response.json())
                expected = {'A': 'B', 'B': 'ROOT', 'bob': 'BOB', 'IDENTITY': 'IDENTITY'}
                self.assertEqual(expected, json.loads(path.read_text()))
                self.assertEqual({'A': 'B', 'B': 'ROOT', 'bob': 'BOB'}, client.get('/api/character_aliases').json())
                self.assertEqual(expected, speaker_identity.get_validated_alias_graph(expected))

    def test_protected_merges_are_refused_but_legacy_edges_stay_readable(self):
        app = FastAPI()
        app.include_router(script.router)
        legacy = {'UNKNOWN': 'MARA'}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.json'
            path.write_text(json.dumps(legacy), encoding='utf-8')
            before = path.read_bytes()
            with patch.object(script, 'CHARACTER_ALIASES_PATH', str(path)), \
                 patch.object(script, 'DATA_DIR', tmp), patch.object(core, 'process_state', {}), \
                 patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}), \
                 TestClient(app) as client:
                for payload in ({'UNKNOWN': 'MARA'}, {'MARA': 'UNKNOWN'}, {'NARRATOR': 'MARA'}):
                    with self.subTest(payload=payload):
                        response = client.post('/api/character_aliases', json=payload)
                        self.assertEqual(400, response.status_code, response.text)
                        self.assertIn('protected', response.json()['detail'])
                        self.assertEqual(before, path.read_bytes())
                # A legacy registry holding UNKNOWN is still readable, and its
                # protected edge is never flattened into a spoken-label merge.
                self.assertEqual(legacy, speaker_identity.get_validated_alias_graph(legacy))
                self.assertEqual({}, speaker_identity.get_resolved_speaker_merge_map(legacy))
                response = client.post('/api/character_aliases', json={'BOB': 'ROBERT'})
                self.assertEqual(200, response.status_code, response.text)
                self.assertEqual({'BOB': 'ROBERT'}, json.loads(path.read_text()))

    def test_alias_save_refuses_while_a_model_task_runs(self):
        app = FastAPI()
        app.include_router(script.router)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'aliases.json'
            path.write_text('{"EXISTING": "ROOT"}', encoding='utf-8')
            before = path.read_bytes()
            with patch.object(script, 'CHARACTER_ALIASES_PATH', str(path)), \
                 patch.object(core, 'DATA_DIR', tmp), \
                 patch.object(core, 'process_state', {'script': {'running': True}}), \
                 patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}), \
                 TestClient(app) as client:
                response = client.post('/api/character_aliases', json={'BOB': 'ROBERT'})
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(before, path.read_bytes())

    def test_shared_reader_policy_has_same_limits_without_mutating_inputs(self):
        for payload in ({'A': 'x' * 1025}, {f'K{i}': 'ROOT' for i in range(10001)}):
            original = copy.deepcopy(payload)
            with self.assertRaises(ValueError):
                speaker_identity.get_validated_alias_map(payload)
            self.assertEqual(original, payload)

    def test_long_chain_reuses_validated_roots_without_quadratic_identity_checks(self):
        count = 1000
        mapping = {f'K{i}': f'K{i + 1}' for i in range(count)}
        identity_key = speaker_identity._identity_key
        with patch.object(speaker_identity, '_identity_key', wraps=identity_key) as normalize:
            roots, cyclic = speaker_identity._get_alias_roots(mapping)
        self.assertFalse(cyclic)
        self.assertEqual({key: f'K{count}' for key in mapping}, roots)
        self.assertLessEqual(normalize.call_count, 2 * count)
        self.assertEqual(list(mapping), list(roots))
