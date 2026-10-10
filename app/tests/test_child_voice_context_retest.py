"""Retest artifacts must verify references and record failed clone generation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from experiments import child_voice_context_retest as retest
from experiments.provenance import file_sha256
from experiments.generation import GenerationFailed


class ChildContextRetestTests(unittest.TestCase):
    def get_manifest(self,root):
        ref=root/'reference.wav';sf.write(ref,np.full(2400,.1),24000)
        path=root/'candidates.json'
        path.write_text(json.dumps({'reference_text':'Synthetic reference.','candidates':[
            {'id':'synthetic','age_target':5,'gender_target':'female','seed':733,
             'clips':[{'path':ref.name,'sha256':file_sha256(ref)}]}]}))
        return path

    def test_reference_validation_rejects_changed_hash_duplicate_ids_and_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.get_manifest(root);data=json.loads(path.read_text())
            self.assertEqual(1,len(retest.get_candidates(path)))
            for kind in ('hash','duplicate','escape'):
                changed=json.loads(json.dumps(data))
                if kind=='hash':changed['candidates'][0]['clips'][0]['sha256']='0'*64
                elif kind=='duplicate':changed['candidates']*=2
                else:changed['candidates'][0]['clips'][0]['path']='../reference.wav'
                path.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):retest.get_candidates(path)

    def test_actual_clone_dispatch_writes_verified_complete_artifact_and_page(self):
        class Engine:
            def generate_clone_voice(self,text,speaker,config,path):
                sf.write(path,np.full(2400,.1),24000);return True
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.get_manifest(root);out=root/'out';out.mkdir()
            rows=retest.run_retest(Engine(),retest.get_candidates(path),out,{'fixture':True})
            saved=json.loads((out/'retest.json').read_text())
            self.assertEqual(('complete',3),(saved['status'],saved['completed_clips']))
            self.assertTrue(saved['ratings_pending'])
            self.assertEqual(['reference','neutral','age_appropriate','long'],[c['condition'] for c in rows[0]['clips']])
            for clip in rows[0]['clips']:self.assertEqual(clip['sha256'],file_sha256(out/clip['path']))
            page=(out/'listen.html').read_text()
            self.assertIn('child_voice_context_ratings_private.json',page)
            self.assertIn('Complete all age and quality ratings first.',page)
            self.assertIn('data-field="age_neutral"',page)
            self.assertIn('data-field="age_age_appropriate"',page)
            self.assertIn('data-field="age_long"',page)

    def test_failed_generation_cannot_count_existing_wav_as_success(self):
        class Engine:
            def generate_clone_voice(self,*args):return False
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=self.get_manifest(root);out=root/'out';out.mkdir()
            sf.write(out/'candidate_00_neutral.wav',np.full(2400,.1),24000)
            with self.assertRaises(GenerationFailed):
                retest.run_retest(Engine(),retest.get_candidates(path),out,{'fixture':True})
            saved=json.loads((out/'retest.json').read_text())
            self.assertEqual('failed',saved['status'])
            self.assertEqual(1,len(saved['candidates'][0]['clips']))
            self.assertFalse((out/'candidate_00_neutral.wav').exists())
            self.assertFalse((out/'listen.html').exists())

    def test_original_age_ratings_never_used_to_relabel_reference(self):
        self.assertEqual(retest.get_passages(5)[0],retest.get_passages(11)[0])
        self.assertEqual(retest.get_passages(5)[2],retest.get_passages(11)[2])
        self.assertNotEqual(retest.get_passages(5)[1],retest.get_passages(11)[1])
