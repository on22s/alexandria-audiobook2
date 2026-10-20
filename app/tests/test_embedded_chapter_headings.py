"""Synthetic embedded heading structure, text fidelity and real-media exports."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import soundfile as sf
from project import CHAPTER_EXPORT_DIR, ProjectManager, group_into_chunks


def tone(seconds=1, rate=24000, hz=220):
    return (.2 * np.sin(2 * np.pi * hz * np.arange(round(seconds * rate)) / rate)).astype('float32')


class EmbeddedStructuralHeadingTests(unittest.TestCase):
    def test_structural_paragraphs_preserve_mapping_metadata_and_final_pause(self):
        prose = 'The sun rose over the distant mountains and the quiet village began to wake. ' * 2
        for heading in ('Prologue', 'Epilogue', 'Prelude', 'Introduction', 'Conclusion'):
            with self.subTest(heading=heading), tempfile.TemporaryDirectory() as tmp:
                script = [dict(speaker='NARRATOR', text=heading+'\r\n\r\n'+prose,
                               instruct='Calm.', pause_after=740, focus_speaker='Alice')]
                original = json.dumps(script)
                chunks = group_into_chunks(script, include_source_indices=True)
                self.assertEqual(original, json.dumps(script))
                self.assertEqual(heading, chunks[0]['text'])
                self.assertEqual([0], chunks[0]['source_entry_indices'])
                self.assertEqual('Alice', chunks[0]['focus_speaker'])
                self.assertNotIn('pause_after', chunks[0])
                self.assertEqual(740, chunks[-1]['pause_after'])
                self.assertEqual(' '.join(script[0]['text'].split()), ' '.join(c['text'] for c in chunks))
                manager = ProjectManager(tmp)
                self.assertEqual([(heading, 0, len(chunks)-1)], manager._chapter_groups(chunks, False))
                self.assertEqual(heading, manager._get_chapter_export_plans(chunks, {
                    'per_chunk_chapters': False, 'template': '{chapter_name}', 'format': 'wav',
                    'padding': 2, 'book_name': '', 'series_name': '', 'volume_number': ''}, (0, 0))[0]['title'])
                timeline = [(chunk, bytes(1000), index * 1000) for index, chunk in enumerate(chunks)]
                self.assertEqual([(heading, 0, len(chunks) * 1000)],
                                 manager._build_m4b_chapters(timeline, False))
                for index, chunk in enumerate(chunks):
                    relative = f'voicelines/{index}.wav'
                    sf.write(Path(tmp) / relative, tone(), 24000)
                    chunk.update(status='done', audio_path=relative)
                manager.save_chunks(chunks)
                self.assertEqual(heading, manager.preview_chapter_filenames(fmt='wav')[0]['title'])
                self.assertTrue(manager.export_chapters(fmt='wav')[0])
                manifest = json.loads((Path(tmp) / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
                self.assertEqual([heading], [row['title'] for row in manifest['chapters']])

    def test_quoted_parenthesized_spoken_and_nonparagraph_labels_are_not_split(self):
        for text in ('“A reader called it\n\nPrologue\n\nbut she disagreed.”',
                     '"A reader called it\n\nPrologue\n\nbut she disagreed."',
                     '(A reader called it\n\nEpilogue\n\nbut she disagreed.)',
                     'The\nIntroduction\nto the story was brief.',
                     'Prologue is a word in this ordinary sentence.'):
            with self.subTest(text=text):
                chunks = group_into_chunks([dict(speaker='NARRATOR', text=text, instruct='')])
                self.assertEqual(1, len(chunks))
        for extra in ({'speaker': 'Alice'}, {'speaker': 'NARRATOR', 'spoken': True}):
            chunks = group_into_chunks([dict(text='Prologue\n\nThe story begins.', instruct='', **extra)])
            self.assertEqual(1, len(chunks))

    def test_removed_aside_scene_and_cue_edges_do_not_invent_unnumbered_headings(self):
        cases = [('The <aside>\nIntroduction\n\nto the story was written by her.', 'Introduction'),
                 ('The *\nIntroduction\n\nto the story was written by her.', 'Introduction'),
                 ('♪Prologue\n\nof the story was sung.♪', 'Prologue')]
        for text, label in cases:
            with self.subTest(text=text):
                source = [dict(speaker='NARRATOR', text='Chapter 1'),
                          dict(speaker='NARRATOR', text=text)]
                original = copy.deepcopy(source)
                chunks = group_into_chunks(source)
                self.assertEqual(original, source)
                self.assertNotIn(label, [row['text'] for row in chunks])
                groups = ProjectManager.__new__(ProjectManager)._chapter_groups(chunks, False)
                self.assertNotIn(label, [title for title, _, _ in groups])

    def test_real_label_is_independent_of_an_unrelated_synthetic_label(self):
        text = 'Prologue\n\nThe <aside>\nIntroduction\n\nto the story was written by her.'
        chunks = group_into_chunks([dict(speaker='NARRATOR', text=text)])
        self.assertEqual('Prologue', chunks[0]['text'])
        self.assertNotIn('Introduction', [row['text'] for row in chunks])

    def test_same_label_ambiguity_defers_new_splits_without_disabling_numbered_heads(self):
        text = 'CHAPTER I.\nBefore.\n\nPrologue\n\nThe <aside>\nPrologue\n\nto the story was written by her.'
        chunks = group_into_chunks([dict(speaker='NARRATOR', text=text)])
        self.assertEqual('CHAPTER I.', chunks[0]['text'])
        self.assertNotIn('Prologue', [row['text'] for row in chunks])
        self.assertEqual(2, ' '.join(row['text'] for row in chunks).count('Prologue'))

    def test_repeated_genuine_labels_remain_separate_structural_boundaries(self):
        prose = 'The distant traveller returned to the waiting listeners.'
        text = 'Prologue\n\n' + prose + '\n\nPrologue\n\n' + prose
        chunks = group_into_chunks([dict(speaker='NARRATOR', text=text)])
        self.assertEqual(2, [row['text'] for row in chunks].count('Prologue'))
        self.assertEqual(['Prologue', 'Prologue'], [title for title, _, _ in
            ProjectManager.__new__(ProjectManager)._chapter_groups(chunks, False)])

    def test_sung_and_elongation_scopes_cover_every_original_fragment_child(self):
        from verbalization import SUNG_HINT, ELONGATION_HINT
        cases = [('♪Before.\nCHAPTER II.\nAfter.♪', 'CHAPTER II.', SUNG_HINT),
                 ('♪Before.\n\nPrologue\n\nAfter.♪', 'Prologue', SUNG_HINT),
                 ('Befo~re.\n\nPrologue\n\nAfter.', 'Prologue', ELONGATION_HINT)]
        for text, heading, hint in cases:
            with self.subTest(text=text):
                source = [dict(speaker='NARRATOR', text=text, instruct='Calm.', pause_after=1540)]
                original = copy.deepcopy(source)
                chunks = group_into_chunks(source, include_source_indices=True)
                self.assertEqual(['Before.', heading, 'After.'], [row['text'] for row in chunks])
                self.assertTrue(all(hint in row['instruct'] and 'Calm.' in row['instruct'] for row in chunks))
                self.assertEqual([1540], [row['pause_after'] for row in chunks if 'pause_after' in row])
                self.assertEqual(1540, chunks[-1]['pause_after'])
                self.assertTrue(all(row['source_entry_indices'] == [0] for row in chunks))
                self.assertEqual(original, source)

    def test_original_labels_keep_literal_words_context_limits_scene_and_final_pauses(self):
        from literal_policy import bind_literal_policy, ensure_literal_chunk, get_literal_policy, get_word_identity
        from project import EXPLICIT_SILENCE_MS
        for newline in ('\n', '\r\n', '\r'):
            with self.subTest(newline=repr(newline)):
                text = newline.join(['Prologue', '', 'Before. ■During.', '', 'Epilogue', '', 'After.'])
                source = bind_literal_policy(text, [dict(speaker='NARRATOR', text=text,
                    instruct='Calm.', focus_speaker='ALICE', narrator_version='second', pause_after=1700)])
                original = copy.deepcopy(source)
                chunks = group_into_chunks(source, max_chars=23, include_source_indices=True)
                self.assertEqual(original, source)
                self.assertEqual(get_word_identity([text]), get_word_identity(row['text'] for row in chunks))
                self.assertEqual([EXPLICIT_SILENCE_MS, 1700],
                                 [row['pause_after'] for row in chunks if 'pause_after' in row])
                self.assertEqual(1700, chunks[-1]['pause_after'])
                for row in chunks:
                    self.assertLessEqual(len(row['text']), 23)
                    self.assertEqual([0], row['source_entry_indices'])
                    self.assertEqual('ALICE', row['focus_speaker'])
                    self.assertEqual('second', row['narrator_version'])
                    self.assertEqual('Calm.', row['instruct'])
                    ensure_literal_chunk(row, get_literal_policy(source))
                self.assertEqual(['Prologue', 'Epilogue'], [title for title, _, _ in
                    ProjectManager.__new__(ProjectManager)._chapter_groups(chunks, False)])

    def test_quoted_new_label_does_not_disable_an_existing_numbered_boundary(self):
        text = 'CHAPTER I.\nBefore “a reader called it\n\nPrologue\n\nbut she disagreed.”'
        chunks = group_into_chunks([dict(speaker='NARRATOR', text=text)])
        self.assertEqual('CHAPTER I.', chunks[0]['text'])
        self.assertNotIn('Prologue', [row['text'] for row in chunks])

    def test_saved_legacy_chunks_and_audio_are_not_silently_rebuilt(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            source = [dict(speaker='NARRATOR', text='Prologue\n\nThe distant traveller returned.')]
            Path(manager.script_path).write_text(json.dumps(source))
            audio = Path(tmp) / 'legacy.wav'
            sf.write(audio, tone(), 24000)
            saved = [dict(id=0, uid='legacy', speaker='NARRATOR',
                text='Prologue The distant traveller returned.', instruct='Calm.',
                status='done', audio_path='legacy.wav')]
            manager.save_chunks(saved)
            before = {path: path.read_bytes() for path in (Path(manager.script_path), Path(manager.chunks_path), audio)}
            self.assertEqual(saved, manager.load_chunks())
            self.assertEqual(before, {path: path.read_bytes() for path in before})

if __name__ == '__main__':
    unittest.main()
