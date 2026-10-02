"""Real route artifacts remain complete and preserve report history."""
import copy
from contextlib import ExitStack
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from routers import dataset_builder, script
import core


def prepare_report_task_ownership(stack, state, root):
    """Share the fixture state with the current reservation/worker owner."""
    for name, value in (("process_state", state), ("_task_claims", {}), ("_gpu_leases", {})):
        stack.enter_context(patch.object(core, name, value))
    stack.enter_context(patch.object(core, "DATA_DIR", str(root)))
    stack.enter_context(patch.object(core, "acquire_gpu_lock", return_value=None))
    stack.enter_context(patch.object(core, "llm_is_on_this_gpu", return_value=True))



class SamplePublicationTests(unittest.TestCase):
    def test_single_and_background_batch_publish_complete_audio_and_preserve_old_on_failure(self):
        real_copy = dataset_builder.shutil.copy2
        real_replace = dataset_builder.os.replace
        real_thread = threading.Thread
        for mode in ('single', 'batch'):
            for failure in (None, 'copy', 'replace'):
                with self.subTest(mode=mode, failure=failure), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                    root = Path(tmp)
                    work = root / 'voice'
                    work.mkdir()
                    source = root / 'generated.wav'
                    target = work / 'sample_000.wav'
                    sf.write(source, np.full(800, .25), 16000, subtype='PCM_16')
                    sf.write(target, np.full(600, -.25), 16000, subtype='PCM_16')
                    before, after = target.read_bytes(), source.read_bytes()
                    sentinel = work / '.sample_000.wav.unrelated'
                    sentinel.write_bytes(b'other invocation')
                    original = {'text':'Original', 'emotion':'calm', 'seed':0, 'status':'done',
                                'audio_url':'/old.wav', 'custom':'preserved'}
                    state = {'dataset_builder':{'running':False, 'cancel':False}}
                    prepare_report_task_ownership(stack,state,root)
                    claims, threads, result, errors = [], [], [], []
                    entered, resume = threading.Event(), threading.Event()
                    def claim(name):
                        claims.append(name)
                        state[name]['running'] = True
                        state[name]['cancel'] = False
                    def start_thread(*args, **kwargs):
                        thread = real_thread(*args, **kwargs)
                        threads.append(thread)
                        return thread
                    def interrupted_copy(src, dest, *args, **kwargs):
                        Path(dest).write_bytes(b'partial temporary bytes')
                        entered.set()
                        if not resume.wait(5):
                            raise RuntimeError('fixture reader did not release copy')
                        if failure == 'copy':
                            raise OSError('fixture copy failed')
                        return real_copy(src, dest, *args, **kwargs)
                    def replace(src, dest):
                        if Path(dest) == target and failure == 'replace':
                            raise OSError('fixture replace failed')
                        return real_replace(src, dest)
                    engine = SimpleNamespace(generate_voice_design=lambda **kwargs:(str(source), None))
                    for obj, attr, value in (
                        (dataset_builder,'DATASET_BUILDER_DIR',str(root)),
                        (dataset_builder,'process_state',state),
                        (dataset_builder,'project_manager',SimpleNamespace(get_engine=lambda:engine)),
                        (dataset_builder,'claim_gpu_task',claim),
                        (dataset_builder,'threading',SimpleNamespace(Thread=start_thread, Lock=threading.Lock)),
                        (core,'threading',SimpleNamespace(Thread=start_thread)),
                        (dataset_builder.shutil,'copy2',interrupted_copy),
                        (dataset_builder.os,'replace',replace)):
                        stack.enter_context(patch.object(obj, attr, value))
                    check = stack.enter_context(patch.object(dataset_builder,'check_global_gpu_lock'))
                    stack.enter_context(patch.object(dataset_builder.logger,'exception'))
                    stack.enter_context(patch.object(dataset_builder.logger,'error'))
                    dataset_builder._save_builder_state('voice',{'description':'warm', 'samples':[original]})
                    app = FastAPI()
                    app.include_router(dataset_builder.router)
                    app.mount('/dataset_builder',StaticFiles(directory=root))
                    client = stack.enter_context(TestClient(app))
                    static_app = FastAPI()
                    static_app.mount('/dataset_builder',StaticFiles(directory=root))
                    reader = stack.enter_context(TestClient(static_app))
                    def request_single():
                        try:
                            result.append(client.post('/api/dataset_builder/generate_sample',json={
                                'dataset_name':'voice','sample_index':0,'description':'warm',
                                'text':'New sample','seed':0}))
                        except Exception as error:
                            errors.append(error)
                    if mode == 'single':
                        worker = real_thread(target=request_single)
                        worker.start()
                        threads.append(worker)
                    else:
                        response = client.post('/api/dataset_builder/generate_batch',json={
                            'name':'voice','description':'warm','global_seed':0,
                            'samples':[{'text':'New sample','emotion':'calm'}]})
                        self.assertEqual(200,response.status_code,response.text)
                        self.assertEqual('started',response.json()['status'])
                    try:
                        self.assertTrue(entered.wait(5),'route never reached copy')
                        self.assertTrue(state['dataset_builder']['running'])
                        # This real static reader and decoder must see the complete old PCM.
                        visible = reader.get('/dataset_builder/voice/sample_000.wav')
                        self.assertEqual(200,visible.status_code)
                        self.assertEqual(before,visible.content)
                        pcm, rate = sf.read(io.BytesIO(visible.content))
                        self.assertEqual((600,16000),(len(pcm),rate))
                        np.testing.assert_allclose(pcm,-.25)
                    finally:
                        resume.set()
                        for thread in threads:
                            thread.join(5)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                    self.assertEqual([],errors)
                    self.assertEqual(['dataset_builder'] if mode == 'single' else [],claims)
                    self.assertEqual({},core._task_claims)
                    check.assert_called_once_with('dataset_builder')
                    self.assertFalse(state['dataset_builder']['running'])
                    self.assertEqual(after,source.read_bytes())
                    self.assertEqual(b'other invocation',sentinel.read_bytes())
                    self.assertEqual({'state.json','sample_000.wav',sentinel.name}, {p.name for p in work.iterdir()})
                    row = dataset_builder._load_builder_state('voice')['samples'][0]
                    self.assertEqual('preserved',row['custom'])
                    self.assertEqual('calm',row['emotion'])
                    visible = reader.get('/dataset_builder/voice/sample_000.wav')
                    if failure:
                        self.assertEqual(before,visible.content)
                        self.assertEqual('error',row['status'])
                        self.assertIn('fixture ' + failure + ' failed',row['error'])
                        self.assertEqual('/old.wav',row['audio_url'])
                        if mode == 'single': self.assertEqual(500,result[0].status_code)
                        else: self.assertIn('[DONE] Generated 0/1 samples',state['dataset_builder']['logs'])
                    else:
                        self.assertEqual(after,visible.content)
                        pcm, rate = sf.read(io.BytesIO(visible.content))
                        self.assertEqual((800,16000),(len(pcm),rate))
                        np.testing.assert_allclose(pcm,.25)
                        self.assertEqual('done',row['status'])
                        self.assertIn('/dataset_builder/voice/sample_000.wav?t=',row['audio_url'])
                        if mode == 'single': self.assertEqual(200,result[0].status_code)
                        else: self.assertIn('[DONE] Generated 1/1 samples',state['dataset_builder']['logs'])


class BoundedBatchHighlightsTests(unittest.TestCase):
    def test_actual_batch_route_keeps_bounded_stable_winners_and_complete_per_book_evidence(self):
        for bidirectional in (False, True):
            with self.subTest(bidirectional=bidirectional), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                books, reports = root/'scripts', root/'reports'
                books.mkdir()
                names = ['book_%02d'%i for i in range(14)]
                original = b'[{"speaker":"ALICE","text":"Keep exact original"}]'
                for name in names: (books/(name+'.json')).write_bytes(original)
                state = {'batch_review':{'running':False,'cancel':False}}
                history = {'text':[], 'speaker':[]}
                per_book, aliases, snapshots = {}, [], []
                def observe(current):
                    if not history['text']: return
                    pool = copy.deepcopy(current['diff_pool'])
                    snapshots.append(pool)
                    expected = {'text':sorted(history['text'],key=lambda h:h['magnitude'],reverse=True)[:5],
                                'speaker':history['speaker'][:5]}
                    self.assertEqual(expected,pool)
                    self.assertLessEqual(len(pool['text']),5)
                    self.assertLessEqual(len(pool['speaker']),5)
                def stream(command, _cwd, current, **kwargs):
                    observe(current)
                    name = names[current['current_task_idx']]
                    phase = current['current_pass']
                    token = name+'_'+phase
                    if any(str(arg).endswith('find_nicknames.py') for arg in command):
                        alias = {'variant':token,'canonical':'ALICE','evidence':'fixture evidence','book':name}
                        aliases.append(alias)
                        return 0,['Found 1 nickname/alias mapping(s):', "    '%s' -> 'ALICE' (fixture evidence)"%token]
                    n = len(per_book)
                    highlights = {'text_rewrites':[{'speaker':'ALICE','before':'old '+token+'_'+str(j),
                        'after':'new '+token+'_'+str(j),'magnitude':100 if n in (12,13,24,25) else 10} for j in range(6)],
                        'speaker_changes':[{'text':'speaker '+token+'_'+str(j),'before':'BOB','after':'ALICE','entry_number':j+1} for j in range(6)]}
                    per_book[(name,phase)] = copy.deepcopy(highlights)
                    history['text'].extend({**item,'book':name} for item in highlights['text_rewrites'])
                    history['speaker'].extend({**item,'book':name} for item in highlights['speaker_changes'])
                    return 0,['Review complete: 6 -> 6 entries','Total changes: 6',
                              'DIFF_PREVIEW_JSON: '+json.dumps(highlights)]
                for attr, value in (('SCRIPTS_DIR',str(books)),('REPORTS_DIR',str(reports)),
                    ('ROOT_DIR',str(root)),('process_state',state),('_stream_subprocess_to_logs',stream),
                    ('_run_claimed_background_task',lambda name,work:work()),
                    ('_init_task_log',lambda name:str(root/'task.log')),
                    ('_insert_llm_summary',lambda lines,*args,**kwargs:lines)):
                    stack.enter_context(patch.object(script,attr,value))
                prepare_report_task_ownership(stack,state,root)
                check=stack.enter_context(patch.object(script,'check_global_gpu_lock'))
                claim=stack.enter_context(patch.object(script,'claim_gpu_task'))
                app=FastAPI(); app.include_router(script.router)
                with TestClient(app) as client:
                    response=client.post('/api/review_script/batch/start',json={'script_names':names,
                        'find_nicknames':True,'dedupe_speakers':True,'bidirectional':bidirectional})
                self.assertEqual(200,response.status_code,response.text)
                current=state['batch_review']; observe(current)
                check.assert_called_once_with('batch_review'); claim.assert_not_called()
                self.assertEqual({},core._task_claims)
                self.assertFalse(current['running'])
                self.assertGreater(len(snapshots),14)
                report=Path(current['artifacts'][0]['artifact_path']).read_text()
                overall=report.split('## Highlights',1)[1].split('## New character names discovered',1)[0]
                expected={'text_rewrites':sorted(history['text'],key=lambda h:h['magnitude'],reverse=True)[:5],
                          'speaker_changes':history['speaker'][:5]}
                reference='\n'.join(script._markdown_diff_highlights_lines(expected,max_each=5))
                self.assertEqual(reference.strip(),overall.strip())
                self.assertEqual(14,current['totals_fwd']['books_done'])
                self.assertEqual(14 if bidirectional else 0,current['totals_bwd']['books_done'])
                self.assertEqual(aliases,current['aliases_fwd']+current['aliases_bwd'])
                for alias in aliases: self.assertIn('**'+alias['variant']+'**',report)
                for stored in current['tasks']:
                    self.assertNotIn('stats',stored)
                    self.assertNotIn('diffs',stored)
                    self.assertNotIn('failures',stored)
                    task=script.get_batch_review_task_snapshot(stored,bidirectional)
                    self.assertEqual('done',task['status'])
                    fwd=per_book[(task['name'],'fwd')]
                    self.assertEqual(fwd,task['diffs_fwd'])
                    combined=copy.deepcopy(fwd)
                    if bidirectional:
                        bwd=per_book[(task['name'],'bwd')]
                        self.assertEqual(bwd,task['diffs_bwd'])
                        for key in combined: combined[key]+=bwd[key]
                    self.assertEqual(combined,task['diffs'])
                    self.assertIn('### '+task['name'],report)
                    self.assertIn(fwd['text_rewrites'][0]['before'],report)
                    self.assertEqual(original,(books/(task['name']+'.json')).read_bytes())

    def test_selection_returns_new_lists_and_keeps_stable_ties(self):
        pool={'text':[{'magnitude':1,'id':i} for i in range(12)], 'speaker':[{'id':i} for i in range(12)]}
        before=copy.deepcopy(pool)
        selected=script.get_batch_review_highlights(pool)
        self.assertEqual({'text':pool['text'][:5],'speaker':pool['speaker'][:5]},selected)
        selected['text'].clear(); selected['speaker'].clear()
        self.assertEqual(before,pool)
