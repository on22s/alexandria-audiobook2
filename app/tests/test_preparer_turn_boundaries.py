"""Speaker turns split audio before quality, alignment and annotation stages."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_preparer_run_state import preparer
from alexandria_run_manifest import get_run_identity, ensure_run_manifest


class PreparerTurnBoundaryTests(unittest.TestCase):
    def test_lookahead_skips_invalid_rows_and_preserves_input(self):
        rows=[{'word':'old','start':0,'end':1,'speaker':'OLD'},
              {'word':'one','start':1,'end':2,'speaker':'A'},
              {'word':' ','start':2,'end':2,'speaker':'BAD'},
              {'word':'incomplete','speaker':'BAD'},
              {'word':'two','start':2,'end':3,'speaker':'B'},
              {'word':'trailing','start':3}]
        before=json.loads(json.dumps(rows))
        result=list(preparer.get_annotation_word_pairs(rows,1))
        self.assertEqual([(1,rows[1],rows[4]),(4,rows[4],None)],result)
        self.assertEqual(before,rows)

    def run_chunker(self, batch_size, texts, speakers, duration=.5, gaps=None, chunk_size=10, use_source=False, verify_resume=False):
        import numpy as np
        import soundfile as sf
        root=Path.cwd(); gaps=gaps or {}
        words=[];time=0
        for i,(text,speaker) in enumerate(zip(texts,speakers)):
            time+=gaps.get(i,0)
            words.append({'word':text,'start':time,'end':time+duration,
                          'confidence':1,'speaker':speaker})
            time+=duration
        samples=np.sin(np.arange(round((time+1)*24000))*.05).astype('float32')*.1
        sf.write(root/'audio.wav',samples,24000)
        source=None
        if use_source:
            source=root/'source.txt';source.write_text(' '.join(texts))
        identity=get_run_identity(SimpleNamespace(audio=str(root/'audio.wav'),source=str(source) if source else None))
        ensure_run_manifest(root/'dataset_temp',identity,fresh=True)
        prompts=[]
        def annotate(**kw):
            text=kw['messages'][1]['content'].split('Annotate this segment:\n',1)[1]
            prompts.append(text)
            return {'choices':[{'message':{'content':text}}]}
        llm=SimpleNamespace(create_chat_completion=Mock(side_effect=annotate))
        def batch(llm,items,*args):
            prompts.extend(item['text'] for item in items)
            return [(None,item['text']) for item in items]
        with patch.object(preparer,'_load_llm',return_value=llm),patch.object(preparer,'log_gpu_stats'),patch.object(preparer,'_calculate_chunk_snr',return_value=80),patch.object(preparer,'_annotate_batch',side_effect=batch):
            def run(resume=False):
                state=preparer._build_source_state(str(source),no_auto_anchor=True) if source else None
                return preparer.annotate_chunks(words,'CPU fixture.gguf',chunk_size,samples,
                    run_identity=identity,batch_size=batch_size,min_chunk_duration=1,
                    source_state=state,source_threshold=1,resume=resume)
            result=run()
            if verify_resume:
                baseline=(root/'dataset_temp/metadata.jsonl').read_bytes()
                audio_before={row['audio_filepath']:(root/'dataset_temp'/row['audio_filepath']).read_bytes() for row in result}
                save=preparer._save_chunk_metadata
                def interrupt(*args,**kwargs):
                    save(*args,**kwargs)
                    raise KeyboardInterrupt('interrupted after checkpoint fsync')
                with patch.object(preparer,'_save_chunk_metadata',side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt):run()
                result=run(resume=True)
                self.assertEqual(baseline,(root/'dataset_temp/metadata.jsonl').read_bytes())
                self.assertEqual(audio_before,{row['audio_filepath']:(root/'dataset_temp'/row['audio_filepath']).read_bytes() for row in result})
        saved=[json.loads(line) for line in (root/'dataset_temp/metadata.jsonl').read_text().splitlines()]
        for row in saved:
            waveform,rate=sf.read(root/'dataset_temp'/row['audio_filepath'])
            expected=samples[int(row['start']*24000):int(row['end']*24000)]
            self.assertEqual(24000,rate)
            np.testing.assert_allclose(expected,waveform,atol=1/32768)
            self.assertEqual([row['speaker']],row['speaker_labels'])
        return result,saved,prompts

    def test_three_turns_cut_below_target_in_perchunk_fullbatch_and_tail(self):
        for batch in (1,2):
            with self.subTest(batch=batch),tempfile.TemporaryDirectory() as tmp:
                old=Path.cwd();os.chdir(tmp)
                try:
                    rows,saved,prompts=self.run_chunker(batch,
                        'alpha bravo charlie delta echo foxtrot golf hotel india'.split(),
                        ['A']*3+['B']*3+['A']*3,gaps={3:.25,6:.25})
                    self.assertEqual(['A','B','A'],[row['speaker'] for row in rows])
                    self.assertEqual([(0,1.5),(1.75,3.25),(3.5,5)],[(row['start'],row['end']) for row in rows])
                    self.assertEqual(['alpha bravo charlie','delta echo foxtrot','golf hotel india'],prompts)
                finally:os.chdir(old)

    def test_same_phrase_from_different_speaker_is_not_a_retaken_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path.cwd();os.chdir(tmp)
            try:
                rows,_,_=self.run_chunker(1,['hello','there','friend']*2,['A']*3+['B']*3)
                self.assertEqual(['A','B'],[row['speaker'] for row in rows])
                self.assertEqual(['hello there friend']*2,[row['text'] for row in rows])
            finally:os.chdir(old)

    def test_short_turn_is_rejected_instead_of_joining_the_next_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path.cwd();os.chdir(tmp)
            try:
                rows,_,prompts=self.run_chunker(1,['hey','delta','echo','foxtrot'],['A','B','B','B'])
                self.assertEqual(['B'],[row['speaker'] for row in rows])
                self.assertEqual(.5,rows[0]['start'])
                self.assertEqual(['delta echo foxtrot'],prompts)
            finally:os.chdir(old)

    def test_source_guided_speaker_turns_and_durable_resume_keep_all_words(self):
        for batch in (1,2):
            with self.subTest(batch=batch),tempfile.TemporaryDirectory() as tmp:
                old=Path.cwd();os.chdir(tmp)
                try:
                    rows,saved,_=self.run_chunker(batch,
                        'alpha bravo charlie delta echo foxtrot golf hotel india'.split(),
                        ['A']*3+['B']*3+['A']*3,use_source=True,verify_resume=True)
                    self.assertEqual(['A','B','A'],[row['speaker'] for row in rows])
                    self.assertEqual([3,6,9],[row['source_position']['cursor'] for row in saved])
                    self.assertEqual('alpha bravo charlie delta echo foxtrot golf hotel india',' '.join(row['text'] for row in rows))
                finally:os.chdir(old)

    def test_same_speaker_retakes_still_get_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            old=Path.cwd();os.chdir(tmp)
            try:
                rows,_,_=self.run_chunker(1,['hello','there','friend.']*2,['A']*6,chunk_size=1.2)
                self.assertEqual(1,len(rows))
                self.assertEqual('hello there friend.',rows[0]['text'])
            finally:os.chdir(old)
