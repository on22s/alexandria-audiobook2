"""Literal suffix lineage preserves captured dependencies and refuses substitutions."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import literal_policy as literal
from literal_policy import (bind_literal_policy, get_literal_policy, ensure_literal_narration,
                            ensure_literal_chunk, CHUNK_POLICY_KEY, SCRIPT_POLICY_KEY)
from project import group_into_chunks, get_chunk_generation_inputs, ProjectManager
from project_scope_literal import get_literal_scope


class ScopeLiteralTests(unittest.TestCase):
    def fixture(self):
        source = 'First synthetic line.\nRetained exact words.'
        rows = [{'speaker':'ALICE', 'text':'First synthetic line.', 'instruct':''},
                {'speaker':'BOB', 'text':'Retained exact words.', 'instruct':''}]
        digest = hashlib.sha256(source.encode()).hexdigest()
        script = bind_literal_policy(source, rows, input_sha256=digest)
        chunks = [dict(row, id=i, uid=f'uid-{i}') for i,row in enumerate(group_into_chunks(script))]
        return source, digest, script, chunks

    def test_selected_policy_admits_unchanged_retained_marker_without_rewriting_generation_inputs(self):
        source, digest, script, chunks = self.fixture(); original = copy.deepcopy((script, chunks))
        inputs = get_chunk_generation_inputs(chunks[1])
        selected = get_literal_scope(source, script, chunks, 1, 1, input_sha256=digest)
        policy = ensure_literal_narration(selected)
        self.assertEqual('selected_source_words_v1', policy['boundary'])
        self.assertEqual('Retained exact words.', selected[0]['text'])
        self.assertEqual((3,6), (policy['scope_source']['start_word'], policy['scope_source']['end_word']))
        ensure_literal_chunk(chunks[1], policy)
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            Path(manager.script_path).write_text(json.dumps(selected))
            manager.ensure_literal_render_inputs(chunks[1:], [0])
        self.assertEqual(inputs, get_chunk_generation_inputs(chunks[1]))
        self.assertEqual(original, (script, chunks))

    def test_changed_uid_words_marker_and_source_cannot_inherit_retained_lineage(self):
        source, digest, script, chunks = self.fixture()
        policy = get_literal_policy(get_literal_scope(source, script, chunks, 1, 1, input_sha256=digest))
        for change in ({'uid':'removed'}, {'text':'Changed retained words.'},
                       {CHUNK_POLICY_KEY:{**chunks[1][CHUNK_POLICY_KEY], 'source_policy_sha256':'0'*64}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ensure_literal_chunk({**chunks[1], **change}, policy)
        with self.assertRaisesRegex(ValueError, 'origin policy'):
            get_literal_scope(source+' Extra.', script, chunks, 1, 1, input_sha256=digest)
        for key, value in [('start_word', True), ('end_word', 99), ('prepared_source_sha256','bad')]:
            changed = copy.deepcopy(policy); changed['scope_source'][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                get_literal_policy([{SCRIPT_POLICY_KEY:changed}])

    def test_batch_validation_hashes_lineage_once_and_rechecks_changed_policy(self):
        source, digest, script, chunks = self.fixture()
        policy = get_literal_policy(get_literal_scope(source, script, chunks, 1, 1, input_sha256=digest))
        rows = [dict(chunks[1], uid=f'new-{i}') for i in range(40)]
        real = literal.get_literal_policy_identity
        with patch.object(literal, 'get_literal_policy_identity', wraps=real) as identity:
            bound = literal.bind_literal_chunks(rows, policy)
            self.assertEqual(1, identity.call_count)
            identity.reset_mock()
            literal.ensure_literal_chunks(bound, policy)
            self.assertEqual(1, identity.call_count)
        changed = copy.deepcopy(policy)
        changed['speech_normalization'] = 'changed policy identity'
        with self.assertRaisesRegex(ValueError, 'different source policy'):
            literal.ensure_literal_chunks(bound, changed)
        with self.assertRaisesRegex(ValueError, 'changed or lacks provenance'):
            literal.ensure_literal_chunks([dict(bound[0], text='Substituted words.')], policy)
        literal.ensure_literal_chunks(chunks[1:], policy)
