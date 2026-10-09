"""Saved synthetic references work through state suggestions, Apply and playback."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from routers import voices
from tests import test_voice_followups_733 as fixtures
from tests import test_state_voices_js as ui
from tts import voice_config_for_chunk, _resolve_asset_path


class DesignedStateVoiceTests(unittest.TestCase):
    def catalog(self, root):
        directory=root/'designed_voices';directory.mkdir()
        rows=[]
        for gender in ('male','female'):
            for age in ('young_child','child'):
                name=gender+'_'+age
                sf.write(directory/(name+'.wav'),np.sin(np.arange(2400)*.1)*.2,24000)
                rows.append({'id':name,'name':name,'filename':name+'.wav','gender':gender,
                             'age_group':age,'description':'Synthetic child voice','sample_text':'A blue stone.'})
        (directory/'manifest.json').write_text(json.dumps(rows))
        return directory,rows

    def test_playable_references_only_and_exact_child_bands_rank_first(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory,rows=self.catalog(root)
            (directory/'broken.wav').write_bytes(b'broken')
            rows += [dict(rows[0],id='missing',filename='missing.wav'),
                     dict(rows[0],id='broken',filename='broken.wav'),
                     dict(rows[0],id='escaped',filename='../outside.wav'),
                     dict(rows[0],id='empty-text',sample_text='')]
            sf.write(root/'outside.wav',np.ones(24)*.2,24000)
            sf.write(directory/'unlabelled.wav',np.ones(24)*.2,24000)
            rows.append(dict(rows[0],id='unlabelled',filename='unlabelled.wav',gender=None,age_group=None,
                             description='A feminine-sounding young man'))
            (directory/'manifest.json').write_text(json.dumps(rows))
            with patch.object(voices,'DESIGNED_VOICES_DIR',str(directory)),patch.object(voices,'_build_lora_candidates',return_value=[]):
                candidates=voices.get_state_voice_candidates()
                self.assertEqual(5,len(candidates))
                unlabelled=next(c for c in candidates if c['adapter_id']=='designed:unlabelled')
                self.assertEqual(('unknown','unknown'),(unlabelled['gender'],unlabelled['age_group']))
                for age in ('young_child','child'):
                    got=voices.get_state_voice_sources({'gender':'male','age_group':age},'A',{},candidates,{})
                    selected=got['library_unused'][0]
                    self.assertEqual('designed:male_'+age,selected['adapter_id'])
                    self.assertEqual('clone',selected['config']['type'])
                    self.assertNotIn('adapter_path',selected['config'])
                    self.assertEqual('A blue stone.',selected['config']['ref_text'])
                    self.assertTrue(all(row['gender']=='male' for row in got['library_unused']))
                for age in ('infant','adult'):
                    got=voices.get_state_voice_sources({'gender':'male','age_group':age},'A',{},candidates,{})
                    self.assertEqual([],got['library_unused'])
                    self.assertTrue(got['offer_generate'])
                    own={'versions':{'kid':{'type':'clone','age_group':'young_child','gender':'male'}}}
                    own_sources=voices.get_state_voice_sources({'gender':'male','age_group':age},'A',own,[],{})
                    self.assertEqual([],own_sources['versions'])

    def test_saved_designer_version_retains_gender_and_excludes_opposite_state(self):
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            helper=fixtures.VoiceTimelineAudioTests()
            root,manager,client,path,_,_=helper.fixture(temp,stack)
            directory,_=self.catalog(root)
            stack.enter_context(patch.object(voices,'DESIGNED_VOICES_DIR',str(directory)))
            stack.enter_context(patch.object(voices,'_build_lora_candidates',side_effect=lambda: []))
            candidates=voices.get_state_voice_candidates()
            for gender in ('male','female'):
                candidate=next(c for c in candidates if c['adapter_id']=='designed:'+gender+'_young_child')
                token=client.get('/api/voice_config/snapshot').json()['book_token']
                response=client.post('/api/voices/A/versions',json={'book_token':token,'version_id':gender,
                    'age_group':'young_child','config':voices.get_library_voice_config(candidate)})
                self.assertEqual(200,response.status_code,response.text)
                entry=json.loads(path.read_text())['A']
                self.assertEqual(gender,entry['versions'][gender]['gender'])
            for gender in ('male','female'):
                sources=voices.get_state_voice_sources({'gender':gender,'age_group':'young_child'},'A',entry,[],{})
                self.assertEqual([gender],[v['version_id'] for v in sources['versions']])

    def test_reuse_counts_main_versions_and_relative_or_absolute_references(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);directory,_=self.catalog(root)
            with patch.object(voices,'DESIGNED_VOICES_DIR',str(directory)),patch.object(voices,'_build_lora_candidates',return_value=[]):
                candidates=voices.get_state_voice_candidates()
                first=candidates[0]
                config={'B':{'type':'clone','ref_audio':str(directory/'male_young_child.wav')},
                        'C':{'versions':{'kid':{'type':'clone','ref_audio':r'.\designed_voices\male_child.wav'}}}}
                before=copy.deepcopy(config);users=voices.get_adapter_users(config,candidates)
                self.assertEqual(['B'],users[first['adapter_id']])
                self.assertEqual(['C'],users['designed:male_child'])
                got=voices.get_state_voice_sources({'gender':'male','age_group':'young_child'},'A',{},candidates,users)
                self.assertEqual([],got['library_unused'])
                self.assertEqual(['B'],got['library_used'][0]['used_by'])
                mine=voices.get_state_voice_sources({'gender':'male','age_group':'young_child'},'B',{},candidates,users)
                self.assertNotIn(first['adapter_id'],[c['adapter_id'] for c in mine['library_used']])
                self.assertEqual(before,config)

    def test_actual_state_route_apply_and_playback_resolve_the_saved_reference(self):
        with tempfile.TemporaryDirectory() as temp,ExitStack() as stack:
            helper=fixtures.VoiceTimelineAudioTests()
            root,manager,client,path,_,_=helper.fixture(temp,stack)
            directory,_=self.catalog(root)
            stack.enter_context(patch.object(voices,'DESIGNED_VOICES_DIR',str(directory)))
            stack.enter_context(patch.object(voices,'_build_lora_candidates',return_value=[]))
            script=[{'speaker':'A','text':f'Line {i}','speaker_gender':'male','speaker_age_group':('young_child' if i<12 else 'teen')} for i in range(24)]
            (root/'annotated_script.json').write_text(json.dumps(script))
            manager.save_chunks([{**entry,'id':i,'uid':f'line{i}','status':'pending'} for i,entry in enumerate(script)])
            response=client.get('/api/voices/A/state_timeline');self.assertEqual(200,response.status_code,response.text)
            source=response.json()['states'][0]['sources']['library_unused'][0]
            token=client.get('/api/voice_config/snapshot').json()['book_token']
            saved=client.post('/api/voices/A/versions',json={'book_token':token,'version_id':'kid','age_group':'young_child','config':source['config']})
            self.assertEqual(200,saved.status_code,saved.text)
            applied=client.post('/api/voices/A/version_timeline',json={'book_token':token,'points':[{'from_index':0,'version_id':'kid'}]})
            self.assertEqual(200,applied.status_code,applied.text)
            config=json.loads(path.read_text());effective=voice_config_for_chunk(config,'A',0)['A']
            self.assertEqual('clone',effective['type']);self.assertEqual('A blue stone.',effective['ref_text'])
            with patch.dict('os.environ',{'ALEXANDRIA_DATA_DIR':temp}):
                actual=Path(_resolve_asset_path(effective['ref_audio']))
            self.assertEqual(directory/'male_young_child.wav',actual)
            self.assertGreater(float(np.max(np.abs(sf.read(actual)[0]))),0)
            from fastapi import FastAPI
            from fastapi.staticfiles import StaticFiles
            from fastapi.testclient import TestClient
            media=FastAPI();media.mount('/designed_voices',StaticFiles(directory=directory))
            with TestClient(media) as playback:
                response=playback.get('/'+effective['ref_audio'])
                self.assertEqual(200,response.status_code)
                self.assertEqual(actual.read_bytes(),response.content)

    def test_native_apply_preserves_clone_reference_and_reference_text(self):
        data={'speaker':'A','states':[{'from_index':0,'age_group':'young_child','sources':{'library_unused':[
            {'adapter_id':'designed:child','name':'Synthetic boy','gender':'male','age_group':'young_child',
             'config':{'type':'clone','ref_audio':'designed_voices/child.wav','ref_text':'A blue stone.'}}]}}]}
        result=ui.VoiceStatesJsTests().run_js(r'''
context.window._voiceStateSuggestions={A:payload.suggestion};context.window._voicesByName={A:{config:{}}};
const row={dataset:{fromIndex:'0',age:'young_child'},querySelector:()=>({value:'library:designed:child'})};
const card={dataset:{voice:'A'},querySelectorAll:q=>q==='.voice-state-row'?[row]:[]};
await context.applyVoiceStates({closest:()=>card});console.log(JSON.stringify(calls));
''',{'suggestion':data})
        version=next(row[2] for row in result if row[0]=='POST' and row[1].endswith('/versions'))
        self.assertEqual('clone',version['config']['type'])
        self.assertEqual('designed_voices/child.wav',version['config']['ref_audio'])
        self.assertEqual('A blue stone.',version['config']['ref_text'])

    def test_redesign_clears_stale_listening_metadata_but_identical_save_preserves_it(self):
        from routers import voice_design
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import shutil
        for changed in (None,"audio","description","transcript","caption"):
            with self.subTest(changed=changed),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);directory,rows=self.catalog(root)
                row=rows[0];import hashlib
                row['synthetic_design']={'listening_verdict':'accepted','reference_sha256':hashlib.sha256((directory/row['filename']).read_bytes()).hexdigest()}
                manifest=directory/'manifest.json';manifest.write_text(json.dumps(rows))
                previews=directory/'previews';previews.mkdir()
                preview=previews/'next.wav';shutil.copy2(directory/row['filename'],preview)
                if changed=="audio":
                    sf.write(preview,np.ones(2400)*.3,24000)
                app=FastAPI();app.include_router(voice_design.router)
                with patch.object(voice_design,'DESIGNED_VOICES_DIR',str(directory)),patch.object(voice_design,'DESIGNED_VOICES_MANIFEST',str(manifest)),TestClient(app) as client:
                    response=client.post('/api/voice_design/save',json={'voice_id':row['id'],'name':'New caption' if changed=='caption' else row['name'],
                        'description':'An elderly woman' if changed=='description' else row['description'],
                        'sample_text':'Another transcript' if changed=='transcript' else row['sample_text'],'preview_file':'next.wav'})
                    self.assertEqual(200,response.status_code,response.text)
                    saved=next(item for item in json.loads(manifest.read_text()) if item['id']==row['id'])
                    for key in ('gender','age_group','synthetic_design'):
                        if changed in ("audio","description","transcript"):self.assertNotIn(key,saved)
                        else:self.assertEqual(row[key],saved[key])
                    self.assertEqual(preview.read_bytes(),(directory/row['filename']).read_bytes())
                    with patch.object(voices,'DESIGNED_VOICES_DIR',str(directory)),patch.object(voices,'_build_lora_candidates',return_value=[]):
                        candidate=next(c for c in voices.get_state_voice_candidates() if c['adapter_id']=='designed:'+row['id'])
                    if changed in ('audio','description','transcript'):
                        self.assertEqual(('unknown','unknown'),(candidate['gender'],candidate['age_group']))
                    else:self.assertEqual(('male','young_child'),(candidate['gender'],candidate['age_group']))
