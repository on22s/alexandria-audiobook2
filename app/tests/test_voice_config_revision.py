from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices
from voice_config_store import get_voice_config_revision
from book_state_transaction import ensure_book_state, apply_book_state_locked, apply_book_input_selection


class VoiceConfigRevisionTests(unittest.TestCase):
    def fixture(self, root, stack):
        script=root/'annotated_script.json';script.write_text('[{"speaker":"ALICE","text":"Original source."}]')
        path=root/'voice_config.json';path.write_text('{"ALICE":{"description":"initial","persona_voice_audit":{"notes":"preserve"}}}')
        (root/'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
        stack.enter_context(patch.object(voices,'VOICE_CONFIG_PATH',str(path)))
        stack.enter_context(patch.object(voices,'SCRIPT_PATH',str(script)))
        app=FastAPI();app.include_router(voices.router)
        return stack.enter_context(TestClient(app)),path

    def payload(self, snapshot, description):
        return {'revision':snapshot['revision'],'book_token':snapshot['book_token'],'voices':{'ALICE':{'type':'design','description':description}}}

    def test_success_acknowledges_exact_revision_and_preserves_audit(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);client,path=self.fixture(root,stack)
            snapshot=client.get('/api/voice_config/snapshot').json();self.assertEqual('ALICE',snapshot['voices'][0]['name'])
            result=client.post('/api/voice_config/save',json=self.payload(snapshot,'new voice'));self.assertEqual(200,result.status_code,result.text)
            saved=json.loads(path.read_bytes());self.assertEqual('new voice',saved['ALICE']['description']);self.assertEqual({'notes':'preserve'},saved['ALICE']['persona_voice_audit'])
            self.assertEqual(get_voice_config_revision(saved),result.json()['revision']);self.assertEqual(snapshot['book_token'],result.json()['book_token'])

    def test_stale_revision_after_another_tab_or_audit_edit_cannot_overwrite_bytes(self):
        for mutation in ('tab','audit'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);client,path=self.fixture(root,stack);snapshot=client.get('/api/voice_config/snapshot').json()
                if mutation=='tab':
                    result=client.post('/api/voice_config/save',json=self.payload(snapshot,'newer tab'));self.assertEqual(200,result.status_code,result.text)
                else:
                    result=client.post('/api/voices/ALICE/persona-voice-audit',json={'suggestion_reason':'new human audit'});self.assertEqual(200,result.status_code,result.text)
                before=path.read_bytes();result=client.post('/api/voice_config/save',json=self.payload(snapshot,'stale edit'));self.assertEqual(409,result.status_code,result.text);self.assertEqual(before,path.read_bytes())

    def test_book_switch_reload_source_change_and_input_selection_reject_old_book(self):
        for mutation in ('book','reload','source','input'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);client,path=self.fixture(root,stack);snapshot=client.get('/api/voice_config/snapshot').json()
                if mutation=='input':apply_book_input_selection(tmp,'different.txt','different')
                else:
                    replacements={'state.json':b'{"active_book_id":"different","book_generation":"two"}'} if mutation=='book' else {'state.json':b'{"active_book_id":"book","book_generation":"two"}'} if mutation=='reload' else {'annotated_script.json':b'[{"speaker":"ALICE","text":"Changed source."}]'}
                    with ensure_book_state(tmp):apply_book_state_locked(tmp,replacements,[])
                before=path.read_bytes();result=client.post('/api/voice_config/save',json=self.payload(snapshot,'wrong-book edit'));self.assertEqual(409,result.status_code,result.text);self.assertIn('Active book changed',result.json()['detail']);self.assertEqual(before,path.read_bytes())

    def test_two_concurrent_clients_have_one_winner_for_the_same_revision(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);client,path=self.fixture(root,stack);snapshot=client.get('/api/voice_config/snapshot').json()
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(client.post,'/api/voice_config/save',json=self.payload(snapshot,value)) for value in ('first','second')]
                results=[future.result(timeout=15) for future in futures]
            self.assertEqual([200,409],sorted(result.status_code for result in results))
            winner=next(value for value,result in zip(('first','second'),results) if result.status_code==200)
            self.assertEqual(winner,json.loads(path.read_text())['ALICE']['description'])

    def test_initial_empty_book_and_bad_tokens_are_handled_without_false_admission(self):
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);client,path=self.fixture(root,stack);(root/'annotated_script.json').unlink();path.unlink()
            response=client.get('/api/voice_config/snapshot');self.assertEqual(200,response.status_code,response.text);snapshot=response.json();self.assertEqual([],snapshot['voices']);self.assertEqual({},snapshot['config'])
            payload=self.payload(snapshot,'bad');payload['revision']='invalid';result=client.post('/api/voice_config/save',json=payload);self.assertEqual(422,result.status_code);self.assertFalse(path.exists())
            payload=self.payload(snapshot,'bad');payload['book_token']='0'*64;result=client.post('/api/voice_config/save',json=payload);self.assertEqual(409,result.status_code);self.assertFalse(path.exists())

    def test_nonfinite_and_deep_nesting_refuse_before_any_write(self):
        # Raw NaN/Infinity must travel as request bytes (httpx json= would
        # refuse client-side); the server must validate the final proposed
        # map BEFORE committing, leaving every file byte-identical.
        # Parser limits vary by runtime (some parsers accept deep bodies
        # that the storage encoder cannot write), so the authoritative
        # refusal is the shared storable validator matching the stored
        # encoding, not the request parser.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); client, path = self.fixture(root, stack)
            (root / 'chunks.json').write_text('[{"id": 1, "speaker": "ALICE", "text": "Original source.", "status": "done", "audio_path": "take.wav"}]')
            snapshot = client.get('/api/voice_config/snapshot').json()
            before_files = {name: (root / name).read_bytes() for name in
                            ('voice_config.json', 'annotated_script.json', 'state.json', 'chunks.json')}
            def raw_payload(versions_value):
                payload = {'revision': snapshot['revision'], 'book_token': snapshot['book_token'],
                           'voices': {'ALICE': {'type': 'custom', 'voice': 'Serena',
                                                'versions': {'v1': versions_value}}}}
                return json.dumps(payload)  # allow_nan=True: emits NaN/Infinity tokens
            cases = {
                'nan': {'n': float('nan')},
                'infinity': {'n': float('inf')},
                'neg_infinity': {'n': float('-inf')},
            }
            for label, versions_value in cases.items():
                with self.subTest(case=label):
                    result = client.post('/api/voice_config/save', content=raw_payload(versions_value),
                                         headers={'Content-Type': 'application/json'})
                    self.assertIn(result.status_code, (400, 409, 422), result.text)
                    self.assertEqual(before_files, {name: (root / name).read_bytes() for name in before_files},
                                     'nonfinite payload must not be written')
            # Deeply nested: whether the request parser or the storable
            # validator refuses it, nothing is written and no exception
            # escapes as a 500.
            with self.subTest(case='deep_nesting'):
                deep_raw = '{"n":' * 3000 + '1' + '}' * 3000
                raw = ('{"revision":"' + snapshot['revision'] + '","book_token":"'
                       + snapshot['book_token'] + '","voices":{"ALICE":{"type":"custom",'
                       '"voice":"Serena","versions":{"v1":' + deep_raw + '}}}}')
                result = client.post('/api/voice_config/save', content=raw,
                                     headers={'Content-Type': 'application/json'})
                self.assertIn(result.status_code, (400, 409, 422), result.text)
                self.assertEqual(before_files, {name: (root / name).read_bytes() for name in before_files},
                                 'deep payload must not be written')
            # Valid finite control still persists with a matching revision.
            result = client.post('/api/voice_config/save',
                                 content=raw_payload({'n': 1.5}),
                                 headers={'Content-Type': 'application/json'})
            self.assertEqual(200, result.status_code, result.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual(1.5, saved['ALICE']['versions']['v1']['n'])
            self.assertEqual(get_voice_config_revision(saved), result.json()['revision'])

    def test_deep_programmatic_map_reaches_storable_validator_before_invalidation(self):
        # A programmatically built deep map bypasses request-parser limits
        # on every runtime and reaches the storage-encoding validator.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); client, path = self.fixture(root, stack)
            (root / 'chunks.json').write_text('[{"id": 1, "speaker": "ALICE", "text": "Original source.", "status": "done", "audio_path": "take.wav"}]')
            before_files = {name: (root / name).read_bytes() for name in
                            ('voice_config.json', 'annotated_script.json', 'state.json', 'chunks.json')}
            deep = 1
            for _ in range(3000):
                deep = {'n': deep}
            def deep_update(current):
                updated = dict(current)
                entry = dict(updated.get('ALICE') or {})
                entry['versions'] = {'v1': deep}
                updated['ALICE'] = entry
                return updated
            with ensure_book_state(str(root)):
                with self.assertRaisesRegex(ValueError, 'finite JSON values'):
                    voices._apply_book_voice_update(deep_update)
            self.assertEqual(before_files, {name: (root / name).read_bytes() for name in before_files},
                             'deep map must be refused before invalidation or any write')
            # Valid finite control through the same seam persists.
            def valid_update(current):
                updated = dict(current)
                entry = dict(updated.get('ALICE') or {})
                entry['description'] = 'controlled voice'
                updated['ALICE'] = entry
                return updated
            with ensure_book_state(str(root)):
                saved = voices._apply_book_voice_update(valid_update)
            self.assertEqual('controlled voice', json.loads(path.read_bytes())['ALICE']['description'])
            self.assertEqual(get_voice_config_revision(saved), get_voice_config_revision(json.loads(path.read_bytes())))

    def test_storable_validator_preserves_revision_and_bounds_nesting(self):
        from voice_config_store import ensure_voice_config_storable
        # Mixed-type dict keys refuse via sort_keys in the revision digest.
        with self.assertRaisesRegex(ValueError, 'finite JSON values'):
            ensure_voice_config_storable({1: 'a', 'b': 2})
        # Unpaired surrogates refuse via the UTF-8 encode, not just dumps str.
        with self.assertRaisesRegex(ValueError, 'finite JSON values'):
            ensure_voice_config_storable({'ALICE': {'description': '\ud800'}})
        # Nested arrays and dicts beyond the bound refuse iteratively,
        # before any serialization recursion.
        deep_list = 1
        for _ in range(150):
            deep_list = [deep_list]
        with self.assertRaisesRegex(ValueError, 'finite JSON values'):
            ensure_voice_config_storable({'ALICE': {'versions': {'v1': deep_list}}})
        deep_dict = 1
        for _ in range(150):
            deep_dict = {'n': deep_dict}
        with self.assertRaisesRegex(ValueError, 'finite JSON values'):
            ensure_voice_config_storable({'ALICE': {'versions': {'v1': deep_dict}}})
        # Valid control: moderate nesting, finite, sortable, UTF-8 clean.
        moderate = 1
        for _ in range(20):
            moderate = [{'n': moderate}]
        ensure_voice_config_storable({'ALICE': {'versions': {'v1': moderate}, 'seed': 0}})

    def test_revision_is_order_independent_and_includes_unknown_fields_and_zero(self):
        first={'ALICE':{'seed':0,'future':{'a':1,'b':2}}};second={'ALICE':{'future':{'b':2,'a':1},'seed':0}}
        self.assertEqual(get_voice_config_revision(first),get_voice_config_revision(second))
        second['ALICE']['future']['a']=3;self.assertNotEqual(get_voice_config_revision(first),get_voice_config_revision(second))
        second['ALICE']['future']['a']=1;second['ALICE']['seed']=1;self.assertNotEqual(get_voice_config_revision(first),get_voice_config_revision(second))
