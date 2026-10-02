"""Real PCM and chunk files: no foreign decode or silent vanished result."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import voice_drift
from project import ProjectManager

if os.environ.get('VOICE_DRIFT_BOUNDARY_BASELINE'):
    baseline = Path(os.environ['VOICE_DRIFT_BOUNDARY_BASELINE'])
    exec(compile(baseline.read_text(), str(baseline), 'exec'), voice_drift.__dict__)


class VoiceDriftBoundaryTests(unittest.TestCase):
    def write_wav(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), 'wb') as handle:
            handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            handle.writeframes(b'\x00\x10' * 4000)

    def test_configured_and_fallback_references_cannot_decode_outside_root(self):
        for kind in ('clone', 'lora', 'builtin_lora', 'fallback', 'clone_symlink', 'lora_ref_symlink'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                root = base / 'project'
                outside = base / 'foreign/ref_sample.wav'
                self.write_wav(outside)
                self.write_wav(root / 'target.wav')
                self.write_wav(root / 'good.wav')
                self.write_wav(root / 'reference.wav')
                chunks = [{'uid': 'target', 'speaker': 'BAD', 'status': 'done', 'audio_path': 'target.wav'},
                          {'uid': 'good', 'speaker': 'GOOD', 'status': 'done', 'audio_path': 'good.wav'}]
                config = {'GOOD': {'type': 'clone', 'ref_audio': 'reference.wav'}}
                if kind == 'fallback':
                    chunks.insert(0, {'uid': 'outside', 'speaker': 'BAD', 'status': 'done', 'audio_path': str(outside)})
                    config['BAD'] = {'type': 'custom'}
                elif kind == 'clone_symlink':
                    (root / 'linked.wav').symlink_to(outside)
                    config['BAD'] = {'type': 'clone', 'ref_audio': 'linked.wav'}
                elif kind == 'lora_ref_symlink':
                    adapter = root / 'adapter'
                    adapter.mkdir()
                    (adapter / 'ref_sample.wav').symlink_to(outside)
                    config['BAD'] = {'type': 'lora', 'adapter_path': 'adapter'}
                else:
                    config['BAD'] = {'type': kind, 'ref_audio': '../foreign/ref_sample.wav',
                                     'adapter_path': str(outside.parent)}
                original = copy.deepcopy((chunks, config))
                decoded = []
                real_decode = voice_drift._decode_to_wav
                def decode(src, folder, stem):
                    decoded.append(Path(src).resolve())
                    return real_decode(src, folder, stem)
                seen = []
                def score(pairs, python):
                    seen.extend(pairs)
                    return [0.2] * len(pairs), None
                with patch.object(voice_drift, '_decode_to_wav', side_effect=decode):
                    report = voice_drift.check_voice_drift(chunks, config, str(root), sys.executable, 0.45,
                        indices=[i for i, row in enumerate(chunks) if row['uid'] != 'outside'],
                        resolve_asset_path=lambda p: os.path.join(root, p), score_pairs=score)
                by_uid = {row['uid']: row for row in report['results']}
                self.assertIn('outside project', by_uid['target'].get('error', ''))
                self.assertIsNone(by_uid['target']['score'])
                self.assertFalse(by_uid['target']['flagged'])
                self.assertTrue(by_uid['good']['flagged'])
                self.assertEqual(1, len(seen))
                self.assertNotIn(outside.resolve(), decoded)
                self.assertEqual(original, (chunks, config))

    def prepare_manager(self, root):
        manager = ProjectManager(str(root))
        rows = [{'id': i, 'uid': f'u{i}', 'speaker': 'A', 'text': str(i), 'status': 'done'} for i in range(3)]
        Path(manager.chunks_path).write_text(json.dumps(rows))
        results = [{'index': i, 'uid': f'u{i}', 'score': 0.2, 'flagged': True, 'reference': 'clone:A'} for i in range(3)]
        return manager, rows, results

    def test_disappeared_uid_is_reported_and_remaining_results_are_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, rows, results = self.prepare_manager(Path(tmp))
            original = copy.deepcopy(results)
            Path(manager.chunks_path).write_text(json.dumps([rows[0], rows[2]]))
            with self.assertRaisesRegex(ValueError, 'u1.*persisted 2/3 results'):
                voice_drift.apply_drift_results(manager, results, 0.45)
            saved = manager.load_chunks()
            self.assertEqual(['u0', 'u2'], [row['uid'] for row in saved])
            self.assertTrue(all(row['drift']['flagged'] for row in saved))
            self.assertEqual(original, results)

    def test_endpoint_logs_incomplete_persistence_without_success_summary(self):
        from fastapi import BackgroundTasks
        from routers import editor
        import core
        with tempfile.TemporaryDirectory() as tmp:
            manager, rows, results = self.prepare_manager(Path(tmp))
            Path(manager.voice_config_path).write_text('{}')
            state = editor.process_state['drift_check']
            previous = copy.deepcopy(state)
            def score(*args, **kwargs):
                Path(manager.chunks_path).write_text(json.dumps([rows[0], rows[2]]))
                return {'results': results, 'error': None}
            async def invoke():
                background = BackgroundTasks()
                await editor.drift_check_endpoint(editor.DriftCheckRequest(), background)
                await background()
            try:
                state['running'] = False
                with patch.object(core, 'DATA_DIR', tmp), \
                     patch.object(editor, 'project_manager', manager), \
                     patch.object(editor, 'load_app_config', return_value={}), \
                     patch.object(editor, '_load_voicelab_config', return_value={}), \
                     patch.object(voice_drift, 'get_speaker_model_python', return_value=sys.executable), \
                     patch.object(voice_drift, 'check_voice_drift', side_effect=score):
                    asyncio.run(invoke())
                logs = '\n'.join(state['logs'])
                self.assertIn('u1', logs)
                self.assertIn('persisted 2/3', logs)
                self.assertNotIn('Checked 3 chunk', logs)
                self.assertFalse(state['running'])
            finally:
                state.clear()
                state.update(previous)
