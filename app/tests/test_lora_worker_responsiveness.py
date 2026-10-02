import asyncio
import builtins
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from fastapi import HTTPException
import core
from routers import lora


class LoraWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_dataset_and_model_scans_run_outside_event_loop_and_keep_results(self):
        main_thread=threading.get_ident()
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);dataset=root/'datasets/book';dataset.mkdir(parents=True)
            metadata=dataset/'metadata.jsonl';metadata.write_text('{}\n\n{}\n')
            models=root/'models';models.mkdir();(models/'voice').mkdir()
            manifest=models/'manifest.json';manifest.write_text('[{"id":"voice"}]')
            observed=[];real_open=builtins.open
            def open_file(path,*args,**kwargs):
                if Path(path)==metadata:
                    observed.append(threading.get_ident());time.sleep(.05)
                return real_open(path,*args,**kwargs)
            def builtin():observed.append(threading.get_ident());time.sleep(.05);return []
            for name,value in (('LORA_DATASETS_DIR',str(root/'datasets')),('LORA_MODELS_DIR',str(models)),
                               ('LORA_MODELS_MANIFEST',str(manifest)),('EVALUATION_REVIEWS_DIR',str(root/'reviews'))):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',side_effect=builtin))
            stack.enter_context(patch.object(lora,'_load_voice_library',return_value={'favorites':['voice']}))
            stack.enter_context(patch('builtins.open',side_effect=open_file))
            tick=asyncio.Event()
            async def heartbeat():await asyncio.sleep(.01);tick.set()
            for route in (lora.lora_list_datasets,lora.lora_list_models):
                tick.clear();pulse=asyncio.create_task(heartbeat());result=await route()
                self.assertTrue(tick.is_set(),'list scan starved heartbeat')
                await pulse
                self.assertTrue(result)
                if route==lora.lora_list_datasets:self.assertEqual([{'dataset_id':'book','sample_count':2}],result)
                else:self.assertEqual(('voice',True),(result[0]['id'],result[0]['favorite']))
            self.assertTrue(observed);self.assertNotIn(main_thread,observed)

    async def test_cancelled_render_keeps_task_claim_until_publication_and_cleanup(self):
        for preview in (False,True):
            with self.subTest(preview=preview),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);models=root/'models';adapter=models/'voice';adapter.mkdir(parents=True)
                manifest=models/'manifest.json';manifest.write_text('[{"id":"voice"}]')
                state={'lora_test':{'running':False,'cancel':False},'audio':{'running':False}}
                started=threading.Event();release=threading.Event();finished=threading.Event();worker_threads=[]
                def generate(output_path,**kwargs):
                    worker_threads.append(threading.get_ident());started.set()
                    if not release.wait(1):raise RuntimeError('fixture worker release timeout')
                    sf.write(output_path,np.full(2400,.2),24000)
                    return True
                def publish(*args):
                    try:return original_publish(*args)
                    finally:finished.set()
                original_publish=lora.apply_lora_audio_publication
                for target,name,value in ((core,'DATA_DIR',tmp),(core,'process_state',state),(lora,'process_state',state),
                    (lora,'LORA_MODELS_DIR',str(models)),(lora,'LORA_MODELS_MANIFEST',str(manifest)),
                    (lora,'project_manager',SimpleNamespace(get_engine=lambda:SimpleNamespace(generate_voice=generate)))):
                    stack.enter_context(patch.object(target,name,value))
                # Real per-task kernel lease; physical GPU acquisition is excluded from this CPU fixture.
                stack.enter_context(patch.object(core,'acquire_gpu_lock',return_value=None))
                stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
                stack.enter_context(patch.object(lora,'apply_lora_audio_publication',side_effect=publish))
                request=asyncio.create_task(lora.lora_preview('voice') if preview else
                        lora.lora_test_model(lora.LoraTestRequest(adapter_id='voice',text='Hello there.')))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait,.5))
                    self.assertFalse(request.done(),'render blocked event loop until termination')
                    request.cancel()
                    with self.assertRaises(asyncio.CancelledError):await request
                    self.assertTrue(state['lora_test']['running'])
                    with self.assertRaises(HTTPException):core.claim_gpu_task('audio')
                    self.assertNotIn(threading.get_ident(),worker_threads)
                finally:
                    release.set()
                    if not request.done():
                        try:await request
                        except (asyncio.CancelledError,HTTPException):pass
                    deadline=time.monotonic()+2
                    while state['lora_test']['running'] and time.monotonic()<deadline:await asyncio.sleep(.01)
                self.assertFalse(state['lora_test']['running'])
                self.assertTrue(finished.is_set())
                outputs=list(adapter.glob('*.wav'));self.assertEqual(1,len(outputs))
                samples,rate=sf.read(outputs[0]);self.assertEqual((2400,24000),(len(samples),rate))
                np.testing.assert_allclose(samples,.2,atol=4e-5)
                self.assertEqual([],list(models.glob('.lora-test-*'))+list(models.glob('.preview_*')))
