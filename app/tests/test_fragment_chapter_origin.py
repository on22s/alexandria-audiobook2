"""Source role survives performance fragments without changing authored speech."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import unittest

import numpy as np
import soundfile as sf
from project import CHAPTER_EXPORT_DIR, ProjectManager, group_into_chunks


class FragmentChapterOriginTests(unittest.TestCase):
    @staticmethod
    def groups(rows, per_chunk=False):
        return ProjectManager.__new__(ProjectManager)._chapter_groups(rows, per_chunk)

    def test_aside_words_hints_source_and_pause_keep_one_real_export_chapter(self):
        from verbalization import SET_APART_HINT
        source = [dict(speaker='NARRATOR', text='Chapter 1'),
                  dict(speaker='NARRATOR', text='The <quiet guide> returned to the waiting listeners.',
                       instruct='Calm.', focus_speaker='ALICE', narrator_version='second', pause_after=740)]
        before = copy.deepcopy(source)
        rows = group_into_chunks(source, include_source_indices=True)
        self.assertEqual(before, source)
        self.assertEqual(['Chapter 1', 'The', 'quiet guide', 'returned to the waiting listeners.'],
                         [row['text'] for row in rows])
        self.assertIn(SET_APART_HINT, rows[2]['instruct'])
        for row in rows[1:]:
            self.assertEqual([1], row['source_entry_indices'])
            self.assertEqual('ALICE', row['focus_speaker'])
            self.assertEqual('second', row['narrator_version'])
            self.assertIn('Calm.', row['instruct'])
        self.assertEqual([740], [row['pause_after'] for row in rows if 'pause_after' in row])
        self.assertEqual([('Chapter 1', 0, 3)], self.groups(rows))
        self.assertTrue(all('_chapter_start' not in row for row in rows))
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            for i, row in enumerate(rows):
                relative = f'voicelines/{i}.wav'
                sf.write(Path(tmp)/relative, np.full(2400, .2, dtype='float32'), 24000)
                row.update(status='done', audio_path=relative)
            manager.save_chunks(rows)
            self.assertEqual(['Chapter 1'], [r['title'] for r in manager.preview_chapter_filenames(fmt='wav')])
            self.assertTrue(manager.export_chapters(fmt='wav')[0])
            manifest = json.loads((Path(tmp)/CHAPTER_EXPORT_DIR/'manifest.json').read_text())
            self.assertEqual(['Chapter 1'], [r['title'] for r in manifest['chapters']])
            timeline = [(r, bytes(1000), i*1000) for i, r in enumerate(rows)]
            self.assertEqual([('Chapter 1', 0, 4000)], manager._build_m4b_chapters(timeline, False))
            ok,message=manager.merge_m4b(metadata={'title':'Source role fixture'})
            self.assertTrue(ok,message)
            tags=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_chapters',
                '-of','json',str(Path(tmp)/'audiobook.m4b')],text=True))
            self.assertEqual(['Chapter 1'],[r['tags']['title'] for r in tags['chapters']])


    def test_scene_cue_aside_and_bounded_prefixes_cannot_invent_titles(self):
        for body in ('The <Chapter 2> returned to the waiting listeners.',
                     'The ■guide returned to the waiting listeners.',
                     'The <aside>\nIntroduction\n\nto the story was written by her.',
                     '♪Prologue\n\nof the story was sung.♪',
                     'The quiet guide described Chapter 2 to the waiting listeners.'):
            with self.subTest(body=body):
                rows = group_into_chunks([dict(speaker='NARRATOR', text='Chapter 1'),
                                          dict(speaker='NARRATOR', text=body)], max_chars=25)
                self.assertEqual([('Chapter 1', 0, len(rows)-1)], self.groups(rows))

    def test_genuine_numbered_unnumbered_and_long_keyword_titles_still_start_chapters(self):
        long_title = 'Chapter 2 ' + 'The distant traveller ' * 6
        for heading in ('Prologue', 'CHAPTER II.', long_title):
            with self.subTest(heading=heading):
                source = [dict(speaker='NARRATOR', text='Chapter 1'),
                          dict(speaker='NARRATOR', text=heading),
                          dict(speaker='NARRATOR',text='The guide returned to the listeners.')]
                rows = group_into_chunks(source, max_chars=500)
                self.assertEqual(2, len(self.groups(rows)))
                self.assertEqual(heading.strip()[:117]+'...' if len(heading.strip())>120 else heading.strip(),
                                 self.groups(rows)[1][0])

    def test_bounded_heading_keeps_only_first_boundary_and_tail_pause(self):
        text = 'Chapter 2 ' + 'The distant traveller ' * 10
        rows = group_into_chunks([dict(speaker='NARRATOR', text=text, pause_after=740)], max_chars=35)
        self.assertGreater(len(rows), 2)
        self.assertEqual(1, len(self.groups(rows)))
        self.assertTrue(all(len(row['text'])<=35 for row in rows))
        self.assertEqual([740], [row['pause_after'] for row in rows if 'pause_after' in row])
        self.assertEqual(text.strip(), ''.join(row['text'] for row in rows))

    def test_ordinary_same_context_prose_still_merges_before_evidence_is_finalized(self):
        source = [dict(speaker='NARRATOR', text='The guide returned to the waiting listeners.'),
                  dict(speaker='NARRATOR', text='The story continued across the quiet valley.')]
        rows = group_into_chunks(source, include_source_indices=True)
        self.assertEqual(1,len(rows))
        self.assertEqual(' '.join(row['text'] for row in source), rows[0]['text'])
        self.assertEqual([0,1], rows[0]['source_entry_indices'])

    def test_actual_text_edit_invalidates_negative_evidence_without_deleting_old_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            rows = group_into_chunks([dict(speaker='NARRATOR', text='Chapter 1'),
                                      dict(speaker='NARRATOR', text='A complete narrative sentence.')])
            audio=Path(tmp)/'prior.wav'
            sf.write(audio,np.full(2400,.2,dtype='float32'),24000)
            rows[1].update(id=1,uid='saved',status='done',audio_path='prior.wav',audio_revision='old')
            rows[0].update(id=0,uid='head')
            manager.save_chunks(rows)
            before_audio=audio.read_bytes()
            manager.update_chunk(1,dict(text='Chapter 2'))
            current=manager.load_chunks()
            self.assertEqual(['Chapter 1','Chapter 2'],[title for title,_,_ in self.groups(current)])
            self.assertIsNone(current[1].get('audio_path'))
            self.assertEqual(before_audio,audio.read_bytes())

    def test_same_text_edits_delete_restore_keep_bound_negative_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager=ProjectManager(tmp)
            rows=group_into_chunks([dict(speaker='NARRATOR',text='Chapter 1'),
                                    dict(speaker='NARRATOR',text='The <quiet guide> returned.')])
            for i,row in enumerate(rows): row.update(id=i,uid=f'row-{i}')
            manager.save_chunks(rows)
            manager.update_chunk(1,dict(instruct='Quiet.',pause_after=650))
            current=manager.load_chunks()
            self.assertEqual(1,len(self.groups(current)))
            removed=copy.deepcopy(current[1])
            manager.delete_chunk(1)
            manager.restore_chunk(1,removed)
            self.assertEqual(1,len(self.groups(manager.load_chunks())))

    def test_legacy_malformed_and_stale_markers_keep_legacy_fallback_and_explicit_mode(self):
        for evidence in (None, {}, 'bad', '0'*64):
            with self.subTest(evidence=evidence):
                rows=[dict(speaker='NARRATOR',text='Chapter 1'),
                      dict(speaker='NARRATOR',text='Chapter 2',chapter_origin=evidence)]
                self.assertEqual(2,len(self.groups(rows)))
                self.assertEqual(2,len(self.groups(rows,True)))
        rows=group_into_chunks([dict(speaker='NARRATOR',text='The <quiet guide> returned.')])
        self.assertEqual(len(rows),len(self.groups(rows)))  # Established no-heading fallback.
        self.assertEqual(len(rows),len(self.groups(rows,True)))

    def test_quoted_labels_between_asides_cannot_bypass_original_context_admission(self):
        for left,right in (('"','"'),('“','”')):
            with self.subTest(quote=left):
                text='The guide quoted '+left+'<a quiet aside>\n\nPrologue\n\n<a second aside>'+right+' and continued.'
                rows=group_into_chunks([dict(speaker='NARRATOR',text='Chapter 1'),
                                        dict(speaker='NARRATOR',text=text)])
                self.assertIn('Prologue',[row['text'] for row in rows])
                self.assertEqual([('Chapter 1',0,len(rows)-1)],self.groups(rows))

    def test_first_bounded_heading_with_terminal_period_keeps_source_start(self):
        title='Chapter A. A long keyword title continuing across many bounded pieces'
        rows=group_into_chunks([dict(speaker='NARRATOR',text='Chapter 1'),
                                dict(speaker='NARRATOR',text=title)],max_chars=12)
        self.assertEqual('Chapter A. ',rows[1]['text'])
        self.assertEqual(2,len(self.groups(rows)))
        self.assertEqual(('Chapter A.',1,len(rows)-1),self.groups(rows)[1])
        edited=copy.deepcopy(rows)
        edited[1]['text']='A complete sentence.'
        self.assertEqual(1,len(self.groups(edited)))

    def test_authored_heading_above_aside_keeps_boundary_ambiguous_inline_title_defers(self):
        for text,expected in (('Chapter II\n\n<subtitle>',2),('Chapter II <subtitle>',1),
                              ('A short <formatted> title',1)):
            with self.subTest(text=text):
                rows=group_into_chunks([dict(speaker='NARRATOR',text='Chapter 1'),
                                        dict(speaker='NARRATOR',text=text)])
                self.assertEqual(expected,len(self.groups(rows)))

    def test_literal_word_binding_and_forced_body_splits_preserve_source_role(self):
        from literal_policy import bind_literal_policy,get_literal_policy,ensure_literal_chunk,get_word_identity
        text='Chapter 1\nThe <quiet guide> returned to the waiting listeners. '
        source=bind_literal_policy(text,[dict(speaker='NARRATOR',text=text,pause_after=740)])
        rows=group_into_chunks(source,max_chars=16,include_source_indices=True)
        self.assertGreater(len(rows),5)
        self.assertEqual(get_word_identity([text]),get_word_identity(row['text'] for row in rows))
        for row in rows:
            ensure_literal_chunk(row,get_literal_policy(source))
            self.assertEqual([0],row['source_entry_indices'])
        self.assertEqual([740],[row['pause_after'] for row in rows if 'pause_after' in row])
        self.assertEqual(1,len(self.groups(rows)))

    def test_role_validation_is_strict_text_bound_and_cannot_override_spoken_gate(self):
        from project import get_chapter_text_digest
        digest=get_chapter_text_digest('Chapter 2')
        for role in (dict(is_heading=0,text_sha256=digest),dict(is_heading=1,text_sha256=digest),
                     dict(is_heading=False,text_sha256='0'*64),dict(is_heading=False),
                     dict(is_heading=True,text_sha256=None)):
            with self.subTest(role=role):
                rows=[dict(speaker='NARRATOR',text='Chapter 1'),
                      dict(speaker='NARRATOR',text='Chapter 2',chapter_origin=role)]
                self.assertEqual(2,len(self.groups(rows)))
        rows=[dict(speaker='NARRATOR',text='Chapter 1'),
              dict(speaker='NARRATOR',text='Chapter 2',spoken=True,
                   chapter_origin=dict(is_heading=True,text_sha256=digest))]
        self.assertEqual(1,len(self.groups(rows)))
        self.assertNotEqual(get_chapter_text_digest('quiet guide'),get_chapter_text_digest('quiet guide.'))

    def test_unknown_glyph_and_unpaired_surrogate_are_retained_without_hash_failure(self):
        text='The guide carried \ud800 and an unknown \u0378 sign.'
        source=[dict(speaker='NARRATOR',text='Chapter 1'),dict(speaker='NARRATOR',text=text)]
        review=[]
        rows=group_into_chunks(source,max_chars=25,review_sink=review)
        self.assertEqual(' '.join(' '.join(r['text'] for r in source).split()),
                         ' '.join(' '.join(r['text'] for r in rows).split()))
        self.assertEqual(1,len(self.groups(rows)))
        self.assertIn('\ud800',[r['character'] for r in review])

    def test_legacy_delivery_retry_remains_voice_only_and_saved_audio_is_not_migrated(self):
        from delivery_review import get_delivery_retry_chunks
        before=[dict(speaker='NARRATOR',text='A complete native narration sentence.',
                     instruct='neutral',instruct_unchecked=True,focus_speaker='ALICE')]
        after=[dict(before[0],instruct='quiet')]
        after[0].pop('instruct_unchecked')
        legacy=group_into_chunks(before)
        legacy[0].pop('chapter_origin',None)
        legacy[0].update(uid='old',status='done',audio_path='prior.wav')
        original=copy.deepcopy(legacy)
        result=get_delivery_retry_chunks(before,after,legacy)
        self.assertEqual(original,legacy)
        self.assertEqual(1,len(result))
        self.assertEqual('pending',result[0]['status'])
        with tempfile.TemporaryDirectory() as tmp:
            manager=ProjectManager(tmp)
            audio=Path(tmp)/'prior.wav'
            sf.write(audio,np.full(2400,.2,dtype='float32'),24000)
            manager.save_chunks(legacy)
            paths=[Path(manager.chunks_path),audio]
            saved={p:p.read_bytes() for p in paths}
            self.assertEqual(legacy,manager.load_chunks())
            self.assertEqual(saved,{p:p.read_bytes() for p in paths})

if __name__=='__main__': unittest.main()
