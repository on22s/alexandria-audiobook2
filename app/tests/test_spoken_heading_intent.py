"""Synthetic source intent must survive chunking and chapter planning."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import soundfile as sf
from project import CHAPTER_EXPORT_DIR, ProjectManager, group_into_chunks


class SpokenHeadingIntentTests(unittest.TestCase):
    def test_spoken_chapter_names_remain_in_their_real_chapter(self):
        body = 'The rider crossed the empty valley and reached the distant village before sunset.'
        for text in ('Chapter 2', 'Prologue', 'Part IV'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                source = [{'speaker': 'NARRATOR', 'text': 'Chapter 1'},
                          {'speaker': 'NARRATOR', 'text': text, 'spoken': True},
                          {'speaker': 'NARRATOR', 'text': body}]
                original = copy.deepcopy(source)
                chunks = group_into_chunks(source, include_source_indices=True)
                self.assertEqual(original, source)
                spoken = next(row for row in chunks if row['text'] == text)
                self.assertIs(spoken.get('spoken'), True)
                self.assertEqual([1], spoken['source_entry_indices'])
                groups = ProjectManager(tmp)._chapter_groups(chunks, False)
                self.assertEqual([('Chapter 1', 0, len(chunks) - 1)], groups)

    def test_bounded_splits_keep_explicit_spoken_intent(self):
        source = [{'speaker': 'NARRATOR', 'text': 'Chapter 2 is the phrase the guide repeats to the listening audience.',
                   'spoken': True, 'focus_speaker': 'ALICE', 'pause_after': 740}]
        original = copy.deepcopy(source)
        chunks = group_into_chunks(source, max_chars=20, include_source_indices=True)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(row.get('spoken') is True for row in chunks))
        self.assertTrue(all(row['source_entry_indices'] == [0] for row in chunks))
        self.assertEqual(original, source)
        self.assertEqual(source[0]['text'], ''.join(row['text'] for row in chunks))
        self.assertTrue(all(row['focus_speaker'] == 'ALICE' for row in chunks))
        self.assertTrue(all('pause_after' not in row for row in chunks[:-1]))
        self.assertEqual(740, chunks[-1]['pause_after'])

    def test_merge_keeps_same_spoken_context_and_does_not_swallow_context_changes(self):
        line = 'The traveller walked through the valley and watched the distant lights come on at night.'
        source = [{'speaker': 'NARRATOR', 'text': line, 'spoken': True},
                  {'speaker': 'NARRATOR', 'text': line, 'spoken': True},
                  {'speaker': 'NARRATOR', 'text': line, 'spoken': False}]
        chunks = group_into_chunks(source, include_source_indices=True)
        self.assertEqual(2, len(chunks))
        self.assertIs(chunks[0].get('spoken'), True)
        self.assertEqual([0, 1], chunks[0]['source_entry_indices'])
        self.assertIs(chunks[1].get('spoken'), False)
        self.assertEqual([2], chunks[1]['source_entry_indices'])

    def test_legacy_and_explicit_structural_headings_keep_their_chapter_boundaries(self):
        for marker in ({}, {'spoken': False}):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                rows = [{'speaker': 'NARRATOR', 'text': 'Chapter 1', **marker},
                        {'speaker': 'NARRATOR', 'text': 'Chapter 2', **marker}]
                chunks = group_into_chunks(rows)
                self.assertEqual(['Chapter 1', 'Chapter 2'],
                                 [title for title, _, _ in ProjectManager(tmp)._chapter_groups(chunks, False)])


    def test_legacy_delivery_retry_does_not_treat_spoken_intent_as_a_voice_override(self):
        from delivery_review import get_delivery_retry_chunks
        for spoken in (True, False):
            with self.subTest(spoken=spoken):
                before = [dict(speaker='NARRATOR', text='A complete native narration sentence.',
                               instruct='neutral', instruct_unchecked=True,
                               spoken=spoken, focus_speaker='ALICE')]
                after = [dict(before[0], instruct='quiet')]
                after[0].pop('instruct_unchecked')
                legacy = group_into_chunks(before)
                legacy[0].pop('spoken', None)
                legacy[0].update(uid='prior', status='done', audio_path='prior.wav')
                original = copy.deepcopy(legacy)
                result = get_delivery_retry_chunks(before, after, legacy)
                self.assertEqual(original, legacy)
                self.assertEqual(1, len(result))
                self.assertIs(result[0].get('spoken'), spoken)
                self.assertEqual('ALICE', result[0]['focus_speaker'])
                self.assertEqual('pending', result[0]['status'])
                self.assertIsNone(result[0]['audio_path'])
                self.assertNotEqual('prior', result[0]['uid'])

    def test_embedded_and_quoted_spoken_labels_keep_source_text_context_and_pause(self):
        prose = 'The guide explains the names to the waiting listeners before the story continues.'
        for speaker, intent in (('NARRATOR', {'spoken': True}), ('ALICE', {})):
            for text in ('Chapter 2', 'Prologue', '“Chapter 2”', '\"Prologue\"',
                         'Chapter 2\n\n' + prose, 'Prologue\n\n' + prose):
                with self.subTest(speaker=speaker, text=text), tempfile.TemporaryDirectory() as tmp:
                    entry = dict(speaker=speaker, text=text, pause_after=740,
                                 focus_speaker='ALICE', instruct='Calm.', **intent)
                    source = [dict(speaker='NARRATOR', text='Chapter 1'), entry,
                              dict(speaker='NARRATOR', text='Chapter 3')]
                    original = copy.deepcopy(source)
                    chunks = group_into_chunks(source, max_chars=1000, include_source_indices=True)
                    self.assertEqual(original, source)
                    self.assertEqual(3, len(chunks))
                    self.assertEqual(' '.join(text.split()), ' '.join(chunks[1]['text'].split()))
                    self.assertEqual([1], chunks[1]['source_entry_indices'])
                    self.assertEqual('ALICE', chunks[1]['focus_speaker'])
                    self.assertEqual('Calm.', chunks[1]['instruct'])
                    self.assertEqual(740, chunks[1]['pause_after'])
                    self.assertEqual([('Chapter 1', 0, 1), ('Chapter 3', 2, 2)],
                                     ProjectManager(tmp)._chapter_groups(chunks, False))

    def test_preview_wav_export_and_m4b_metadata_share_spoken_chapter_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            source = [dict(speaker='NARRATOR', text='Chapter 1'),
                      dict(speaker='NARRATOR', text='Chapter 2', spoken=True),
                      dict(speaker='ALICE', text='Prologue', spoken=True),
                      dict(speaker='NARRATOR', text='Chapter 3')]
            chunks = group_into_chunks(source)
            for index, chunk in enumerate(chunks):
                relative = f'voicelines/{index}.wav'
                sf.write(Path(tmp) / relative, np.full(2400, .1 * (index + 1)), 24000)
                chunk.update(status='done', audio_path=relative)
            manager.save_chunks(chunks)
            self.assertEqual(['Chapter 1', 'Chapter 3'],
                             [row['title'] for row in manager.preview_chapter_filenames(fmt='wav')])
            timeline = [(chunk, bytes(1000), index * 1000) for index, chunk in enumerate(chunks)]
            self.assertEqual([('Chapter 1', 0, 3000), ('Chapter 3', 3000, 4000)],
                             manager._build_m4b_chapters(timeline, False))
            ok, message = manager.export_chapters(fmt='wav')
            self.assertTrue(ok, message)
            manifest = json.loads((Path(tmp) / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
            self.assertEqual(['Chapter 1', 'Chapter 3'], [row['title'] for row in manifest['chapters']])
            self.assertEqual([[0, 2], [3, 3]], [row['chunks'] for row in manifest['chapters']])
            self.assertEqual(4, len(manager._chapter_groups(chunks, True)))

if __name__ == '__main__':
    unittest.main()
