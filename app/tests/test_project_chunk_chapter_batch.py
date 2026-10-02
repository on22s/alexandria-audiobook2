"""Bounded source text and native WAV chapter-preview/export agreement."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
import numpy as np
import soundfile as sf
import project


class ProjectChunkChapterBatchTests(unittest.TestCase):
    def test_single_oversized_entries_preserve_complete_text_metadata_and_final_pause(self):
        for text,limit in (('A long line. '*190+'Done.',500),('漢字かな。'*220,137),('x'*2001,500),('Abc def ghi.',1)):
            with self.subTest(limit=limit):
                entries=[{'speaker':'ALICE','text':text,'instruct':'urgent','pause_after':1500}]
                before=copy.deepcopy(entries);chunks=project.group_into_chunks(entries,max_chars=limit)
                self.assertGreater(len(chunks),1);self.assertTrue(all(0<len(chunk['text'])<=limit for chunk in chunks))
                self.assertEqual(text,''.join(chunk['text'] for chunk in chunks))
                self.assertTrue(all(chunk['speaker']=='ALICE' and chunk['instruct']=='urgent' for chunk in chunks))
                self.assertTrue(all('pause_after' not in chunk for chunk in chunks[:-1]))
                self.assertEqual(1500,chunks[-1]['pause_after']);self.assertEqual(before,entries)

    def test_oversized_entry_after_other_speaker_is_bounded_and_scene_pause_stays_at_boundary(self):
        text='A very long paragraph. '*90+'End.'
        entries=[{'speaker':'BOB','text':'Before.'},{'speaker':'ALICE','text':text,'pause_after':1700},
                 {'speaker':'BOB','text':'After.'}]
        chunks=project.group_into_chunks(entries,max_chars=200)
        self.assertEqual('Before.',chunks[0]['text']);self.assertEqual('After.',chunks[-1]['text'])
        alice=[chunk for chunk in chunks if chunk['speaker']=='ALICE']
        self.assertEqual(text,''.join(chunk['text'] for chunk in alice))
        self.assertTrue(all(len(chunk['text'])<=200 for chunk in chunks))
        self.assertEqual([1700],[chunk['pause_after'] for chunk in chunks if 'pause_after' in chunk])
        for limit in (0,-1,True,2.5):
            with self.subTest(limit=limit),self.assertRaises(ValueError):project.group_into_chunks(entries,max_chars=limit)

    def test_filename_builder_rejects_unsafe_extensions_before_forming_path(self):
        for extension in ('mp3/../../outside','../wav','wav\\..\\outside','.wav','wav\0x','',None):
            with self.subTest(extension=extension),self.assertRaises(ValueError):
                project.build_chapter_filename('{chapter_name}',1,'Safe',extension)
        self.assertEqual('Safe.wav',project.build_chapter_filename('{chapter_name}',1,'Safe','wav'))

    def test_native_preview_and_export_admit_same_audio_files_and_keep_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp,tempfile.TemporaryDirectory() as outside:
            root=Path(tmp);manager=project.ProjectManager(tmp)
            (root/'bad.wav').write_bytes(b'not a WAV')
            sf.write(root/'good.wav',np.full(2400,.1),24000)
            sf.write(root/'second.wav',np.full(4800,.2),24000)
            foreign=Path(outside)/'foreign.wav';sf.write(foreign,np.full(2400,.3),24000)
            (root/'escape.wav').symlink_to(foreign)
            rows=[]
            for index,audio in enumerate(('missing.wav','bad.wav','escape.wav','good.wav','second.wav')):
                rows.append({'uid':f'chunk{index}','speaker':'NARRATOR','text':f'Chapter {index}','audio_path':audio})
            manager.save_chunks(rows)
            watched=[root/'chunks.json',root/'good.wav',root/'second.wav',root/'bad.wav',foreign]
            before={path:path.read_bytes() for path in watched}
            preview=manager.preview_chapter_filenames(fmt='wav',per_chunk_chapters=True)
            self.assertEqual([1,2],[row['number'] for row in preview])
            self.assertEqual(['[NARRATOR] Chapter 3','[NARRATOR] Chapter 4'],[row['title'] for row in preview])
            ok,message=manager.export_chapters(fmt='wav',per_chunk_chapters=True)
            self.assertTrue(ok,message);self.assertIn('3 chunk(s) skipped',message)
            manifest=json.loads((root/project.CHAPTER_EXPORT_DIR/'manifest.json').read_text())
            self.assertEqual([row['file'] for row in preview],[row['file'] for row in manifest['chapters']])
            for row in manifest['chapters']:self.assertGreater(sf.info(root/project.CHAPTER_EXPORT_DIR/row['file']).frames,0)
            self.assertEqual(before,{path:path.read_bytes() for path in watched})
