"""All native voice batches retain real PCM and partial-failure accounting."""
import contextlib
import copy
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

import numpy as np
import soundfile as sf
import tts
from tests.test_batch_style_timeline import get_engine,get_torch


class BatchWaveformPublicationTests(unittest.TestCase):
    def test_custom_clone_lora_preserve_indices_pcm_and_missing_or_malformed_outputs(self):
        for family in ('custom','clone','lora'):
            for missing in (False,True):
                with self.subTest(family=family,missing=missing),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);adapter=root/'adapter';adapter.mkdir()
                    chunks=[{'index':index,'speaker':'A','text':f'line {i}','instruct':''} for i,index in enumerate((7,2,9,10))]
                    config={'A':{'type':family,'voice':'Ryan','adapter_path':str(adapter)}};before=copy.deepcopy((chunks,config))
                    waves=None if missing else [np.full(240,.125),np.zeros((2,3,4)),[np.full(120,.25),np.full(120,.5)]]
                    model=SimpleNamespace(generate_custom_voice=Mock(return_value=(waves,24000)),generate_voice_clone=Mock(return_value=(waves,24000)))
                    prompt=[SimpleNamespace(ref_code=np.zeros(4),ref_text='reference')]
                    engine=get_engine();engine._build_sub_batches=lambda texts,max_items:[(0,len(texts))]
                    engine._init_local_custom=Mock(return_value=model);engine._init_local_clone=Mock(return_value=model)
                    engine._get_clone_prompt=Mock(return_value=prompt);engine._ensure_local_lora_generation=Mock(return_value=(model,prompt))
                    with patch.dict('sys.modules',{'torch':get_torch()}),contextlib.redirect_stdout(io.StringIO()):
                        result=getattr(engine,'_local_batch_'+family)(chunks,config,str(root),0)
                    self.assertEqual(before,(chunks,config))
                    if missing:
                        self.assertEqual([],result['completed']);self.assertEqual([7,2,9,10],[i for i,_ in result['failed']])
                        self.assertTrue(all(message=='Batch returned None' for _,message in result['failed']))
                    else:
                        self.assertEqual([7,9],result['completed']);self.assertEqual([10,2],[i for i,_ in result['failed']])
                        self.assertIn('no waveform',result['failed'][0][1]);self.assertIn('requires mono',result['failed'][1][1])
                        first,rate=sf.read(root/'temp_batch_7.wav');self.assertEqual(24000,rate);np.testing.assert_allclose(first,.125)
                        third,rate=sf.read(root/'temp_batch_9.wav');self.assertEqual(24000,rate)
                        np.testing.assert_allclose(third,np.r_[np.full(120,.25),np.full(120,.5)])
                    for index in (2,10):self.assertFalse((root/f'temp_batch_{index}.wav').exists())
