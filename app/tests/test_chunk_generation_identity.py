"""Real chunk JSON and PCM must keep request ownership across structural edits."""
import contextlib
import copy
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
from project import ProjectManager

class ChunkGenerationIdentityTests(unittest.TestCase):
    def get_manager(self,root):
        manager=ProjectManager(str(root));rows=[]
        for i,name in enumerate(('A','B','C')):
            rows.append({'uid':'uid-'+name,'id':i,'text':name+' words','speaker':name,'instruct':'calm',
                'status':'pending','audio_path':None,'pause_after':120,'custom':{'keep':name},'drift':{'old':True}})
        manager.save_chunks(rows);Path(manager.voice_config_path).write_text(json.dumps({name:{'type':'custom','voice':'Ryan'} for name in ('A','B','C')}))
        return manager

    def get_index(self,manager,uid):
        return next(i for i,row in enumerate(manager.load_chunks()) if row['uid']==uid)

    def write_audio(self,path,text):
        value={'A':0.1,'B':0.2,'C':0.3}.get(text[0],0.4)
        sf.write(path,np.full(4000,value,dtype='float32'),16000)

    def assert_audio(self,manager,uid,text):
        row=next(row for row in manager.load_chunks() if row['uid']==uid)
        self.assertEqual('done',row['status']);self.assertIn(uid,Path(row['audio_path']).name)
        samples,rate=sf.read(Path(manager.root_dir)/row['audio_path']);self.assertEqual((4000,16000),(len(samples),rate))
        self.assertAlmostEqual({'A':0.1,'B':0.2,'C':0.3}[text[0]],float(samples.mean()),places=3)
        self.assertEqual({'keep':text[0]},row['custom']);self.assertEqual(120,row['pause_after']);self.assertIsNone(row['drift'])

    def test_single_and_native_batch_preserve_identity_after_insert_delete_or_conversion_edit(self):
        for mode in ('single','native'):
            for phase in ('render','convert'):
                for edit in ('insert','delete_ahead'):
                    with self.subTest(mode=mode,phase=phase,edit=edit),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                        root=Path(tmp);manager=self.get_manager(root);before=copy.deepcopy(manager.load_chunks());changed=[]
                        def mutate():
                            if changed:return
                            changed.append(True)
                            if edit=='insert':
                                manager.insert_chunk(0);manager.update_chunk(1,{'text':'inserted safe'})
                            else:manager.delete_chunk(0)
                        def voice(text,instruct,speaker,config,path):
                            if phase=='render':mutate()
                            self.write_audio(path,text);return True
                        def batch(chunks,config,output,seed):
                            if phase=='render':mutate()
                            for chunk in chunks:self.write_audio(Path(output)/f"temp_batch_{chunk['index']}.wav",chunk['text'])
                            return {'completed':[chunk['index'] for chunk in chunks],'failed':[]}
                        manager.engine=SimpleNamespace(generate_voice=voice,generate_batch=batch)
                        original=manager._export_chunk_audio
                        def convert(*args):
                            result=original(*args)
                            if phase=='convert':mutate()
                            return result
                        with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')),patch.object(manager,'_export_chunk_audio',side_effect=convert):
                            result=manager.generate_chunk_audio(1) if mode=='single' else manager.generate_chunks_batch([1],batch_size=1)
                        self.assertTrue(result[0] if mode=='single' else result['completed']==[1]);self.assert_audio(manager,'uid-B','B words')
                        saved=manager.load_chunks();self.assertEqual(before[2],next(row for row in saved if row['uid']=='uid-C')|{'id':2})
                        if edit=='insert':self.assertEqual('inserted safe',saved[1]['text']);self.assertEqual('pending',saved[1]['status'])
                        self.assertEqual([],list(root.glob('chunk_*.wav')));self.assertEqual([],list((root/'voicelines').glob('.render-*')))

    def test_deleted_or_changed_target_rejects_old_status_audio_and_keeps_prior_file(self):
        for mode in ('single','native'):
            for phase in ('render','convert'):
                for field in ('delete','text','speaker','instruct','focus_speaker'):
                    with self.subTest(mode=mode,phase=phase,field=field),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                        root=Path(tmp);manager=self.get_manager(root);prior=root/'voicelines'/'voiceline_uid-B_B.wav';self.write_audio(prior,'C prior');prior_bytes=prior.read_bytes();saved_after=[]
                        def mutate():
                            if saved_after:return
                            if field=='delete':manager.delete_chunk(self.get_index(manager,'uid-B'))
                            elif field=='focus_speaker':manager._update_chunk_fields(self.get_index(manager,'uid-B'),focus_speaker='C',status='pending')
                            else:manager.update_chunk(self.get_index(manager,'uid-B'),{field:'C changed' if field!='speaker' else 'C'})
                            saved_after.extend(copy.deepcopy(manager.load_chunks()))
                        def voice(text,instruct,speaker,config,path):
                            if phase=='render':mutate()
                            self.write_audio(path,text);return True
                        def batch(chunks,config,output,seed):
                            if phase=='render':mutate()
                            for chunk in chunks:self.write_audio(Path(output)/f"temp_batch_{chunk['index']}.wav",chunk['text'])
                            return {'completed':[chunk['index'] for chunk in chunks],'failed':[]}
                        manager.engine=SimpleNamespace(generate_voice=voice,generate_batch=batch);original=manager._export_chunk_audio
                        def convert(*args):
                            result=original(*args)
                            if phase=='convert':mutate()
                            return result
                        with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')),patch.object(manager,'_export_chunk_audio',side_effect=convert):
                            result=manager.generate_chunk_audio(1) if mode=='single' else manager.generate_chunks_batch([1],batch_size=1)
                        success=result[0] if mode=='single' else bool(result['completed']);self.assertFalse(success)
                        message=result[1] if mode=='single' else result['failed'][0][1]
                        self.assertIn('generation inputs',message);self.assertEqual(saved_after,manager.load_chunks())
                        self.assertEqual(prior_bytes,prior.read_bytes());self.assertEqual([],list((root/'voicelines').glob('.render-*')))

    def test_parallel_queue_freezes_rows_before_an_earlier_render_changes_positions(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);manager=self.get_manager(root);entered=threading.Event();release=threading.Event();seen=[];result=[]
            def voice(text,instruct,speaker,config,path):
                seen.append(text)
                if text.startswith('A'):
                    entered.set();self.assertTrue(release.wait(5))
                self.write_audio(path,text);return True
            manager.engine=SimpleNamespace(generate_voice=voice)
            def run():result.append(manager.generate_chunks_parallel([0,1],max_workers=1))
            with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')):
                worker=threading.Thread(target=run);worker.start()
                try:
                    self.assertTrue(entered.wait(5));manager.insert_chunk(0);manager.update_chunk(1,{'text':'inserted safe'})
                finally:release.set();worker.join(10)
            self.assertFalse(worker.is_alive());self.assertEqual(['A words','B words'],seen)
            self.assertEqual({'completed':[0,1],'failed':[],'cancelled':0},result[0]);self.assert_audio(manager,'uid-A','A words');self.assert_audio(manager,'uid-B','B words')
            self.assertEqual('inserted safe',manager.load_chunks()[1]['text'])

    def test_parallel_and_native_oom_retries_keep_capture_and_reject_changed_inputs(self):
        for mode in ('parallel','native'):
            for edit in ('insert','change'):
                with self.subTest(mode=mode,edit=edit),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                    root=Path(tmp);manager=self.get_manager(root);calls=[]
                    def mutate():
                        if edit=='insert':manager.insert_chunk(0);manager.update_chunk(1,{'text':'inserted safe'})
                        else:manager.update_chunk(self.get_index(manager,'uid-B'),{'text':'C new text'})
                    def voice(text,instruct,speaker,config,path):
                        calls.append(text)
                        if len(calls)==1:mutate();raise RuntimeError('CUDA out of memory')
                        self.write_audio(path,text);return True
                    def batch(chunks,config,output,seed):
                        calls.extend(chunk['text'] for chunk in chunks)
                        if len(calls)==1:mutate();return {'completed':[],'failed':[(1,'CUDA out of memory')]}
                        for chunk in chunks:self.write_audio(Path(output)/f"temp_batch_{chunk['index']}.wav",chunk['text'])
                        return {'completed':[chunk['index'] for chunk in chunks],'failed':[]}
                    manager.engine=SimpleNamespace(generate_voice=voice,generate_batch=batch)
                    with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')):
                        result=manager.generate_chunks_parallel([1],max_workers=2) if mode=='parallel' else manager.generate_chunks_batch([1],batch_size=2)
                    if edit=='insert':
                        self.assertEqual(['B words','B words'],calls);self.assertEqual([1],result['completed']);self.assert_audio(manager,'uid-B','B words')
                        self.assertEqual('pending',manager.load_chunks()[1]['status'])
                    else:
                        self.assertEqual(['B words'],calls);self.assertEqual([],result['completed']);self.assertIn('generation inputs',result['failed'][0][1])
                        row=manager.load_chunks()[self.get_index(manager,'uid-B')];self.assertEqual(('C new text','pending',None),(row['text'],row['status'],row['audio_path']))

    def test_native_cancellation_and_exception_reset_only_original_uids(self):
        for outcome in ('cancel','exception'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                root=Path(tmp);manager=self.get_manager(root);cancelled=[];inserted=[]
                def batch(chunks,config,output,seed):
                    manager.insert_chunk(0);manager.update_chunk(1,{'text':'inserted safe'});manager._update_chunk_fields(1,status='editing',error='keep inserted');inserted.extend(copy.deepcopy(manager.load_chunks()[1:2]));cancelled.append(True)
                    if outcome=='exception':
                        manager.update_chunk(self.get_index(manager,'uid-B'),{'text':'C changed'});raise RuntimeError('fixture batch failure')
                    for chunk in chunks:self.write_audio(Path(output)/f"temp_batch_{chunk['index']}.wav",chunk['text'])
                    return {'completed':[chunk['index'] for chunk in chunks],'failed':[]}
                manager.engine=SimpleNamespace(generate_batch=batch)
                with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')):
                    if outcome=='exception':
                        with self.assertRaisesRegex(RuntimeError,'fixture batch failure'):manager.generate_chunks_batch([0,1],batch_size=1)
                    else:
                        result=manager.generate_chunks_batch([0,1],batch_size=1,cancel_check=lambda:bool(cancelled));self.assertEqual([0],result['completed']);self.assertEqual(1,result['cancelled']);self.assert_audio(manager,'uid-A','A words')
                self.assertEqual(inserted[0],manager.load_chunks()[1]);row=manager.load_chunks()[self.get_index(manager,'uid-B')]
                self.assertEqual('pending',row['status']);self.assertIsNone(row['audio_path'])
                if outcome=='exception':self.assertEqual('pending',manager.load_chunks()[0]['status']);self.assertEqual('C changed',row['text'])

    def test_native_hard_failure_after_input_edit_leaves_live_row_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);manager=self.get_manager(root);snapshot=[]
            def batch(chunks,config,output,seed):
                manager.update_chunk(1,{'text':'C changed'});snapshot.extend(copy.deepcopy(manager.load_chunks()));return {'completed':[],'failed':[(1,'provider failed')]}
            manager.engine=SimpleNamespace(generate_batch=batch);result=manager.generate_chunks_batch([1],batch_size=1)
            self.assertEqual([],result['completed']);self.assertIn('generation inputs',result['failed'][0][1]);self.assertEqual(snapshot,manager.load_chunks())

    def test_unrelated_edits_survive_final_publication(self):
        for mode in ('single','native'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                root=Path(tmp);manager=self.get_manager(root)
                def edit():
                    manager.update_chunk(1,{'pause_after':999});manager._update_chunk_fields(1,note='human annotation',custom={'keep':'updated human'})
                def voice(text,instruct,speaker,config,path):edit();self.write_audio(path,text);return True
                def batch(chunks,config,output,seed):
                    edit()
                    for chunk in chunks:self.write_audio(Path(output)/f"temp_batch_{chunk['index']}.wav",chunk['text'])
                    return {'completed':[chunk['index'] for chunk in chunks],'failed':[]}
                manager.engine=SimpleNamespace(generate_voice=voice,generate_batch=batch)
                with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')):
                    result=manager.generate_chunk_audio(1) if mode=='single' else manager.generate_chunks_batch([1],batch_size=1)
                self.assertTrue(result[0] if mode=='single' else result['completed']==[1]);row=manager.load_chunks()[1]
                self.assertEqual(('done',999,'human annotation',{'keep':'updated human'}),(row['status'],row['pause_after'],row['note'],row['custom']))
                samples,rate=sf.read(root/row['audio_path']);self.assertEqual((4000,16000),(len(samples),rate));self.assertAlmostEqual(0.2,float(samples.mean()),places=3)

    def test_parallel_cancellation_after_structural_edit_preserves_new_row_and_reports_deleted_uid(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);manager=self.get_manager(root);release=threading.Event();cancelled=[];inserted=[]
            def voice(text,instruct,speaker,config,path):
                if text.startswith('B'):self.assertTrue(release.wait(5))
                self.write_audio(path,text);return True
            def cancel():
                if not cancelled:
                    manager.insert_chunk(0);manager.update_chunk(1,{'text':'inserted safe'});manager._update_chunk_fields(1,status='editing',error='preserve')
                    inserted.extend(copy.deepcopy(manager.load_chunks()[1:2]));manager.delete_chunk(self.get_index(manager,'uid-C'));cancelled.append(True);release.set()
                return True
            manager.engine=SimpleNamespace(generate_voice=voice)
            with patch('project._export_audio_segment',side_effect=RuntimeError('force actual WAV fallback')):
                result=manager.generate_chunks_parallel([0,1,2],max_workers=1,cancel_check=cancel)
            self.assertIn(0,result['completed']);self.assertIn(2,[idx for idx,_ in result['failed']]);self.assertTrue(any(idx==2 and 'generation inputs' in error for idx,error in result['failed']))
            self.assertEqual(inserted[0],manager.load_chunks()[1]);self.assert_audio(manager,'uid-A','A words')
            if 1 in result['completed']:self.assert_audio(manager,'uid-B','B words')

    def test_single_and_parallel_failure_do_not_reset_changed_target(self):
        for mode in ('false','exception','parallel'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                root=Path(tmp);manager=self.get_manager(root);snapshot=[]
                def voice(text,instruct,speaker,config,path):
                    manager.update_chunk(1,{'speaker':'C','instruct':'new human'});snapshot.extend(copy.deepcopy(manager.load_chunks()))
                    if mode!='false':raise RuntimeError('provider failure')
                    return False
                manager.engine=SimpleNamespace(generate_voice=voice)
                result=manager.generate_chunks_parallel([1],max_workers=1) if mode=='parallel' else manager.generate_chunk_audio(1)
                self.assertFalse(result['completed'] if mode=='parallel' else result[0]);error=result['failed'][0][1] if mode=='parallel' else result[1]
                self.assertIn('generation inputs',error);self.assertEqual(snapshot,manager.load_chunks());self.assertEqual([],list(root.glob('chunk_*.wav')))
