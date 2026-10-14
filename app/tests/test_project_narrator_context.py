"""Explicit narrator context survives rebuilding and actual render dispatch."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf
import project
from tests.test_batch_style_timeline import get_engine, get_torch


CASES = (
    ('focus', {'focus_speaker': 'ALICE'}, 'Vivian'),
    ('focus', {'character_focus': 'BOB'}, 'Eric'),
    ('chapter', {'narrator_version': 'first'}, 'Vivian'),
    ('chapter', {'chapter_narrator_version': 'second'}, 'Eric'),
    ('gender', {'narrator_gender': 'female'}, 'Vivian'),
    ('gender', {'focus_gender': 'male'}, 'Eric'),
    ('age', {'narrator_age_group': 'adult'}, 'Vivian'),
    ('age', {'focus_age_group': 'teen'}, 'Eric'),
)
LEVELS = {'Ryan': .1, 'Vivian': .5, 'Eric': .7, 'Aiden': .3}


def get_voices(strategy, speaker='NARRATOR'):
    first = {'type': 'custom', 'voice': 'Vivian', 'gender': 'female', 'age_group': 'adult'}
    second = {'type': 'custom', 'voice': 'Eric', 'gender': 'male', 'age_group': 'teen'}
    narrator = 'Narrator' if speaker == 'Narrator' else 'NARRATOR'
    voices = {narrator: {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': strategy,
                        'versions': {'first': first, 'second': second}},
              'ALICE': copy.deepcopy(first), 'BOB': copy.deepcopy(second)}
    if speaker == 'STORY_VOICE':
        voices[speaker] = {'alias_of': narrator}
    return voices


class ProjectNarratorContextTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self, entries, voices, *, rebuild=False):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager = project.ProjectManager(tmp)
            Path(manager.script_path).write_text(json.dumps(entries), encoding='utf-8')
            Path(manager.voice_config_path).write_text(json.dumps(voices), encoding='utf-8')
            if not rebuild:
                manager.save_chunks([dict(entry, status='pending', audio_path=None)
                                     for entry in entries])
            manager.load_chunks()
            yield root, manager

    def render(self, manager, mode):
        calls = []
        def synthesize(**kwargs):
            speakers = kwargs['speaker']
            speakers = [speakers] if isinstance(speakers, str) else speakers
            texts = kwargs['text']
            texts = [texts] if isinstance(texts, str) else texts
            instructions = kwargs['instruct']
            instructions = [instructions] if isinstance(instructions, str) else instructions
            audio = []
            for text, voice, instruct in zip(texts, speakers, instructions):
                calls.append({'text': text, 'voice': voice, 'instruct': instruct})
                samples = np.sin(np.arange(4000) * (2 * np.pi * 440 / 16000))
                audio.append((samples * LEVELS[voice]).astype('float32'))
            return audio, 16000
        engine = get_engine()
        engine._init_local_custom = Mock(return_value=SimpleNamespace(generate_custom_voice=synthesize))
        manager.engine = engine
        before = Path(manager.voice_config_path).read_bytes()
        indices = list(range(len(manager.load_chunks())))
        with patch.dict(sys.modules, {'torch': get_torch()}):
            if mode == 'single':
                for index in indices:
                    success, result = manager.generate_chunk_audio(index)
                    self.assertTrue(success, result)
            elif mode == 'parallel':
                result = manager.generate_chunks_parallel(indices, max_workers=2)
                self.assertEqual([], result['failed'])
                self.assertEqual(set(indices), set(result['completed']))
            else:
                result = manager.generate_chunks_batch(indices, batch_size=2)
                self.assertEqual([], result['failed'])
                self.assertEqual(set(indices), set(result['completed']))
        rows = manager.load_chunks()
        artifacts = []
        for row in rows:
            self.assertEqual('done', row['status'])
            path = Path(manager.root_dir) / row['audio_path']
            samples, rate = sf.read(path)
            self.assertEqual(16000, rate)
            self.assertGreater(len(samples), 0)
            self.assertGreater(float(np.sqrt(np.mean(samples ** 2))), .04)
            artifacts.append({'audio_path': row['audio_path'], 'frames': len(samples),
                              'rms': float(np.sqrt(np.mean(samples ** 2)))})
        self.assertEqual(before, Path(manager.voice_config_path).read_bytes())
        return calls, artifacts

    def test_rebuild_retains_all_supported_fields_and_separates_different_selections(self):
        for strategy, first, expected in CASES:
            field = next(iter(first))
            second = {field: {'ALICE': 'BOB', 'BOB': 'ALICE', 'first': 'second',
                              'second': 'first', 'female': 'male', 'male': 'female',
                              'adult': 'teen', 'teen': 'adult'}[first[field]]}
            entries = [dict(speaker='NARRATOR', text='First native sentence.', instruct='neutral', **first),
                       dict(speaker='NARRATOR', text='Second native sentence.', instruct='neutral', **second)]
            original = copy.deepcopy(entries)
            with self.subTest(field=field), self.fixture(entries, get_voices(strategy), rebuild=True) as (root, manager):
                rows = manager.load_chunks()
                self.assertEqual(2, len(rows))
                self.assertEqual([first[field], second[field]], [row[field] for row in rows])
                voices = json.loads(Path(manager.voice_config_path).read_text())
                selected = [manager.get_chunk_voice_inputs(voices, row, index)['NARRATOR']['voice']
                            for index, row in enumerate(rows)]
                self.assertEqual([expected, 'Eric' if expected == 'Vivian' else 'Vivian'], selected)
                self.assertEqual(entries, json.loads(Path(manager.script_path).read_text()))
                self.assertEqual(original, entries)

    def test_identical_context_merges_and_length_splits_keep_metadata_and_final_pause(self):
        context = {field: value for _, entry, _ in CASES for field, value in entry.items()}
        entries = [dict(speaker='NARRATOR', text='First sentence.', instruct='neutral', **context),
                   dict(speaker='NARRATOR', text='Second sentence.', instruct='neutral', pause_after=1200, **context)]
        original = copy.deepcopy(entries)
        chunks = project.group_into_chunks(entries, include_source_indices=True)
        self.assertEqual(1, len(chunks))
        self.assertEqual('First sentence. Second sentence.', chunks[0]['text'])
        self.assertEqual([0, 1], chunks[0]['source_entry_indices'])
        self.assertEqual(context, {field: chunks[0].get(field) for field in context})
        self.assertEqual(original, entries)
        text = 'Native complete words. ' * 70 + 'Final.'
        long_entries = [dict(entries[0], text=text, pause_after=1700)]
        original = copy.deepcopy(long_entries)
        split = project.group_into_chunks(long_entries, max_chars=37, include_source_indices=True)
        self.assertGreater(len(split), 1)
        self.assertEqual(text, ''.join(row['text'] for row in split))
        self.assertTrue(all(len(row['text']) <= 37 for row in split))
        for row in split:
            self.assertEqual(context, {field: row.get(field) for field in context})
            self.assertEqual([0], row['source_entry_indices'])
        self.assertTrue(all('pause_after' not in row for row in split[:-1]))
        self.assertEqual(1700, split[-1]['pause_after'])
        self.assertEqual(original, long_entries)

    def test_scene_and_delivery_parts_keep_their_source_context(self):
        context = {'focus_speaker': 'ALICE', 'narrator_gender': 'female', 'narrator_age_group': 'adult'}
        entries = [dict(speaker='NARRATOR', text='First sentence. <Second sentence.> ■ Last sentence.', **context)]
        original = copy.deepcopy(entries)
        rows = project.group_into_chunks(entries, include_source_indices=True)
        self.assertGreaterEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(context, {field: row.get(field) for field in context})
            self.assertEqual([0], row['source_entry_indices'])
        self.assertEqual(project.EXPLICIT_SILENCE_MS, rows[-2]['pause_after'])
        self.assertNotIn('pause_after', rows[-1])
        self.assertEqual(original, entries)

    def test_single_parallel_and_fast_batch_use_every_supported_selector_and_narrator_alias(self):
        for strategy, context, expected in CASES:
            for speaker in ('NARRATOR', 'Narrator', 'STORY_VOICE'):
                for mode in ('single', 'parallel', 'batch'):
                    with self.subTest(strategy=strategy, context=context, speaker=speaker, mode=mode):
                        entries = [dict(speaker=speaker, text='Native synthetic narration.', instruct='neutral', **context)]
                        with self.fixture(entries, get_voices(strategy, speaker)) as (_, manager):
                            calls, artifacts = self.render(manager, mode)
                            self.assertEqual([expected], [call['voice'] for call in calls])
                            self.assertEqual(1, len(artifacts))

    def test_selected_timelines_and_fallbacks_match_invalidation_and_all_render_modes(self):
        cases = [('focus', {'focus_speaker': 'ALICE'}, 'Aiden'),
                 ('focus', {'focus_speaker': 'UNKNOWN'}, 'Ryan'),
                 ('chapter', {'narrator_version': 'missing'}, 'Ryan'),
                 ('global', {'focus_speaker': 'ALICE'}, 'Ryan'),
                 ('character_gender_age', {'focus_speaker': 'ALICE', 'narrator_gender': 'male',
                                           'narrator_age_group': 'teen'}, 'Ryan')]
        for strategy, context, expected in cases:
            for mode in ('single', 'parallel', 'batch'):
                with self.subTest(strategy=strategy, mode=mode):
                    voices = get_voices(strategy, 'STORY_VOICE')
                    if expected == 'Aiden':
                        voices['ALICE'].update(seed=0, version_timeline=[{'from_index': 0, 'version_id': 'chosen'}],
                                               versions={'chosen': {'voice': 'Aiden', 'character_style': 'aged'}})
                    entries = [dict(speaker='STORY_VOICE', text='Native timeline narration.', instruct='neutral', **context)]
                    with self.fixture(entries, voices) as (_, manager):
                        inputs = manager.get_chunk_voice_inputs(voices, manager.load_chunks()[0], 0)
                        self.assertEqual(expected, inputs['NARRATOR']['voice'])
                        calls, _ = self.render(manager, mode)
                        self.assertEqual([expected], [call['voice'] for call in calls])
                        if expected == 'Aiden':
                            self.assertIn('aged', calls[0]['instruct'])

    def test_fresh_rebuild_focus_rows_render_their_own_voices_in_all_modes(self):
        entries = [dict(speaker='NARRATOR', text='First synthetic narration.', focus_speaker='ALICE'),
                   dict(speaker='NARRATOR', text='Second synthetic narration.', focus_speaker='BOB')]
        for mode in ('single', 'parallel', 'batch'):
            with self.subTest(mode=mode), self.fixture(entries, get_voices('focus'), rebuild=True) as (_, manager):
                calls, artifacts = self.render(manager, mode)
                self.assertEqual(['Eric', 'Vivian'], sorted(call['voice'] for call in calls))
                self.assertEqual(2, len(artifacts))
                self.assertLess(artifacts[0]['rms'], artifacts[1]['rms'])

    def test_native_delivery_retry_keeps_source_context_and_refuses_editor_overrides(self):
        from delivery_review import (apply_delivery_retry, ensure_delivery_review_snapshot,
                                     get_delivery_retry_chunks)
        for _, context, _ in CASES:
            before = [dict(speaker='NARRATOR', text='Native source narration.',
                           instruct='neutral', instruct_unchecked=True, **context)]
            after = [dict(before[0], instruct='quiet')]
            after[0].pop('instruct_unchecked')
            chunks = [dict(row, uid='source-owned', status='done', audio_path='prior.wav')
                      for row in project.group_into_chunks(before)]
            with self.subTest(context=context):
                result = get_delivery_retry_chunks(before, after, chunks)
                self.assertEqual(context, {field: result[0].get(field) for field in context})
                self.assertEqual('pending', result[0]['status'])
                self.assertIsNone(result[0]['audio_path'])
                self.assertNotEqual('source-owned', result[0]['uid'])
                for edit in ('change', 'missing', 'extra', 'style'):
                    edited = copy.deepcopy(chunks)
                    if edit == 'change':
                        edited[0][next(iter(context))] = 'manual override'
                    elif edit == 'missing':
                        edited[0].pop(next(iter(context)))
                    elif edit == 'extra':
                        extra = next(field for field in project.NARRATOR_GENERATION_FIELDS if field not in context)
                        edited[0][extra] = 'manual override'
                    else:
                        edited[0]['character_style'] = 'manual override'
                    with self.subTest(edit=edit), self.assertRaisesRegex(ValueError, 'voice/style'):
                        get_delivery_retry_chunks(before, after, edited)

        before = [dict(speaker='NARRATOR', text='First source narration.', instruct='neutral',
                       instruct_unchecked=True, focus_speaker='ALICE'),
                  dict(speaker='NARRATOR', text='Second source narration.', instruct='steady',
                       focus_speaker='BOB')]
        with self.fixture(before, get_voices('focus'), rebuild=True) as (root, manager):
            chunks = manager.load_chunks()
            for index, chunk in enumerate(chunks):
                chunk.update(status='done', audio_path=f'prior-{index}.wav')
                sf.write(root / chunk['audio_path'], np.full(4000, .1), 16000)
            manager.save_chunks(chunks)
            artifacts = {chunk['audio_path']: (root / chunk['audio_path']).read_bytes() for chunk in chunks}
            original = copy.deepcopy(chunks)
            snapshot = ensure_delivery_review_snapshot(str(root))
            after = [dict(before[0], instruct='quiet'), before[1]]
            after[0].pop('instruct_unchecked')
            self.assertEqual({'count': 0, 'entries': []}, apply_delivery_retry(str(root), snapshot, after))
            saved = manager.load_chunks()
            self.assertEqual(['ALICE', 'BOB'], [row.get('focus_speaker') for row in saved])
            self.assertEqual('pending', saved[0]['status'])
            self.assertIsNone(saved[0]['audio_path'])
            self.assertEqual(original[1], saved[1])
            self.assertEqual(after, json.loads(Path(manager.script_path).read_text()))
            self.assertEqual(artifacts, {name: (root / name).read_bytes() for name in artifacts})
