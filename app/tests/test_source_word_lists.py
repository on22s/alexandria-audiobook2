"""Source spelling and normalized word indices stay parallel through real alignment."""
import json
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import alexandria_alignment as alignment
import alexandria_compare as compare
from tests.test_preparer_run_state import preparer
from alexandria_run_manifest import get_run_identity


class SourceWordListTests(unittest.TestCase):
    def test_expanded_source_tokens_keep_individual_indices_and_original_spelling(self):
        cases = [('Carrigdon&Rudge, waited.', ['Carrigdon','&','Rudge,','waited.'],['carrigdon','and','rudge','waited']),
                 ('Mr.Smith', ['Mr.','Smith'],['mr','smith']),
                 ('FACTS...WERE...THEY', ['FACTS...','WERE...','THEY'],['facts','were','they']),
                 ('*word*and&word', ['*word*and','&','word'],['wordand','and','word']),
                 ('“I’m” twenty-minute Dr. & Bob', ['“I’m”','twenty','minute','Dr.','&','Bob'],["i'm",'twenty','minute','doctor','and','bob'])]
        for source, expected_display, expected_match in cases:
            with self.subTest(source=source):
                self.assertEqual((expected_display,expected_match),alignment.get_source_word_lists(source))
        self.assertEqual(([],[]),alignment.get_source_word_lists('... --- !'))

    def test_actual_compare_writes_the_complete_source_passage_on_accept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'book.txt';source.write_text('Carrigdon&Rudge, waited beside the river.')
            script=root/'book.jsonl';output=root/'corrected.jsonl'
            script.write_text(json.dumps({'text':'Carrigdon and Rudge waited beside the river',
                                          'audio_filepath':'sample.wav','start':0,'end':3.5})+'\n')
            with patch.object(sys,'argv',['compare','--jsonl',str(script),'--source',str(source),
                                         '--output',str(output),'--no-auto-anchor','--review-all']), \
                 patch('builtins.input',side_effect=['a','q']) as answer,redirect_stdout(io.StringIO()):
                compare.main()
            self.assertEqual(1,answer.call_count)
            self.assertEqual('Carrigdon & Rudge, waited beside the river.',json.loads(output.read_text())['text'])

    def test_source_state_matches_whole_normalized_passage_without_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'book.txt'
            source.write_text('Carrigdon&Rudge, waited beside the river.')
            state=preparer._build_source_state(str(source),no_auto_anchor=True)
            words=alignment.to_words('Carrigdon and Rudge waited beside the river')
            self.assertEqual(words,state['orig_match'])
            start,end,ratio=alignment.find_best_match(words,state['orig_match'],0)
            self.assertEqual((0,len(words),1.0),(start,end,ratio))
            self.assertEqual('Carrigdon & Rudge, waited beside the river.',' '.join(state['orig_display'][start:end]))

    def test_actual_chunker_keeps_the_aligned_audio_and_source_spelling_in_checkpoint(self):
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'book.txt';source.write_text('Carrigdon&Rudge, waited beside the river.')
            audio=root/'audio.wav';samples=np.sin(np.arange(96000)*.05).astype('float32')*.1
            sf.write(audio,samples,24000)
            identity=get_run_identity(SimpleNamespace(audio=str(audio),source=str(source)))
            words=[{'word':word,'start':i*.5,'end':(i+1)*.5,'confidence':1,'speaker':'ONLY'}
                   for i,word in enumerate(('Carrigdon','and','Rudge','waited','beside','the','river.'))]
            state=preparer._build_source_state(str(source),no_auto_anchor=True)
            llm=SimpleNamespace(create_chat_completion=Mock(side_effect=lambda **kw:{'choices':[{'message':{'content':kw['messages'][1]['content'].split('Annotate this segment:\n',1)[1]}}]}))
            previous=Path.cwd();os.chdir(root)
            try:
                with patch.object(preparer,'ensure_run_manifest'),patch.object(preparer,'_load_llm',return_value=llm),patch.object(preparer,'log_gpu_stats'),patch.object(preparer,'_calculate_chunk_snr',return_value=80):
                    rows=preparer.annotate_chunks(words,'CPU fixture.gguf',3,samples,source_state=state,
                        source_threshold=1,min_chunk_duration=1,run_identity=identity)
                self.assertEqual(1,len(rows))
                self.assertEqual('Carrigdon & Rudge, waited beside the river.',rows[0]['text'])
                self.assertEqual(7,state['cursor'])
                checkpoint=json.loads(Path('dataset_temp/metadata.jsonl').read_text())
                self.assertEqual(rows[0]['text'],checkpoint['text'])
                waveform,rate=sf.read(Path('dataset_temp')/checkpoint['audio_filepath'])
                self.assertEqual(24000,rate);self.assertAlmostEqual(3.5,len(waveform)/rate)
            finally:os.chdir(previous)
