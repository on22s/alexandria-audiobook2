import contextlib
import copy
import io
import tempfile
import unittest
from unittest.mock import patch

from project import ProjectManager
from tts import TTSEngine


class ProjectVoiceGroupTests(unittest.TestCase):
    def fixture(self):
        speakers = ['Custom', 'Ensemble', 'Clone', 'Design', 'Ensemble',
                    'Lora', 'Builtin', 'Unknown', 'CloneAlias', 'EnsembleAlias']
        chunks = [{'index': i, 'speaker': speaker, 'text': f'Line {i}.'}
                  for i, speaker in enumerate(speakers)]
        config = {'Custom': {'type': 'custom'}, 'Ensemble': {'type': 'ensemble', 'members': []},
                  'Clone': {'type': 'clone'}, 'Design': {'type': 'design'},
                  'Lora': {'type': 'lora', 'adapter_id': 'same'},
                  'Builtin': {'type': 'builtin_lora', 'adapter_id': 'same'},
                  'Unknown': {'type': 'historical-unknown'},
                  'CloneAlias': {'alias_of': 'Clone'},
                  'EnsembleAlias': {'alias_of': 'Ensemble'}}
        return chunks, config

    def test_groups_follow_shared_categories_and_keep_alias_and_original_order(self):
        chunks, config = self.fixture()
        before = copy.deepcopy((chunks, config))
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            with contextlib.redirect_stdout(io.StringIO()) as logs:
                grouped = manager._group_indices_by_voice_type(list(range(10)), chunks, config)
        self.assertEqual([0, 7, 1, 4, 9, 2, 8, 3, 5, 6], grouped)
        self.assertIn("Voice group 'ensemble': 3 chunks", logs.getvalue())
        self.assertIn("Voice group 'custom': 2 chunks", logs.getvalue())
        self.assertIn("Voice group 'clone:Clone': 2 chunks", logs.getvalue())
        self.assertIn("Voice group 'lora:same': 2 chunks", logs.getvalue())
        self.assertEqual(before, (chunks, config))

    def test_grouped_requests_match_actual_engine_category_dispatch(self):
        chunks, config = self.fixture()
        before = copy.deepcopy((chunks, config))
        dispatch = {}

        def provider(category):
            def run(rows, *args):
                dispatch[category] = [row['index'] for row in rows]
                return {'completed': dispatch[category], 'failed': []}
            return run

        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            with contextlib.redirect_stdout(io.StringIO()):
                order = manager._group_indices_by_voice_type(list(range(10)), chunks, config)
            requests = [{**chunks[i], 'speaker': manager._resolve_alias(chunks[i]['speaker'], config)}
                        for i in order]
            engine = TTSEngine({'tts': {'mode': 'local'}})
            with patch.object(engine, '_clear_gpu_cache'), \
                 patch.object(engine, '_sequential_ensemble', side_effect=provider('ensemble')), \
                 patch.object(engine, '_local_batch_custom', side_effect=provider('custom')), \
                 patch.object(engine, '_local_batch_clone', side_effect=provider('clone')), \
                 patch.object(engine, '_local_batch_lora', side_effect=provider('lora')), \
                 patch.object(engine, 'generate_design_voice', return_value=True) as design:
                result = engine.generate_batch(requests, config, root)
            self.assertEqual({'ensemble': [1, 4, 9], 'custom': [0, 7],
                              'clone': [2, 8], 'lora': [5, 6]}, dispatch)
            self.assertEqual(1, design.call_count)
            self.assertEqual('Line 3.', design.call_args.kwargs['text'])
            self.assertEqual(list(range(10)), sorted(result['completed']))
            self.assertEqual([], result['failed'])
        self.assertEqual(before, (chunks, config))
