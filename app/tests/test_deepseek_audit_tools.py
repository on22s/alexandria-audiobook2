"""Malformed audit inputs and partial source extraction preserve trustworthy artifacts."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.audit import collect_results as collect, audit_experiment_artifacts as structural, audit_legacy_attribution as legacy
from experiments import replay_artifact as replay
from tools.voice_lab.voice_profiler import get_dataset_identity, extract_epub_passage
from tests import test_voice_dedup_source_cache as dedup_tests

class AuditToolTests(unittest.TestCase):
    def collect(self, correct, finished=None):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);artifact=root/'fixture.json'
            artifact.write_text(json.dumps({'meta':{'lmstudio':{},'git':{},'finished':finished},'rows':[{'id':'book:1','arm':'a','correct':correct}]}))
            with patch.object(collect,'REPO',tmp),patch.object(collect,'E',tmp),patch.object(collect,'_load_audit',return_value={'artifacts':[]}),patch.object(structural,'indexable_artifacts',return_value=([str(artifact)],[])),patch.object(replay,'resolve_producer',None),patch.object(replay,'resolve_producers',None),contextlib.redirect_stdout(io.StringIO()):
                collect.main([])
            with (root/'results_index.csv').open() as stream: return list(csv.DictReader(stream))

    def test_correctness_requires_boolean_and_keeps_rejection_visible(self):
        for value in ('false','true',1,0,None,[],{}):
            row=self.collect(value)[0]
            self.assertIn('correct',row['note']);self.assertEqual('',row['accuracy_pct'])
        for value,expected in ((False,'0'),(True,'1')):
            row=self.collect(value)[0];self.assertEqual(expected,row['correct'])
            self.assertEqual('100.0' if value else '0.0',row['accuracy_pct'])

    def test_invalid_timestamp_does_not_abort_index_or_hide_problem(self):
        for value in ('2026-08-05T12:00:00',True,[],float('inf')):
            row=self.collect(True,value)[0]
            self.assertIn('finished',row['note']);self.assertEqual('1',row['correct'])
        self.assertEqual('01-02 00:00',self.collect(True,86400)[0]['finished'])

    def test_missing_arm_classified_and_other_legacy_artifact_still_inspected(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(legacy,'EXPERIMENTS',tmp),patch.object(legacy,'_commit_is_in_history',return_value=False):
            bad=Path(tmp,'bad.json');bad.write_text(json.dumps({'meta':{'git':{'commit':'abc'},'experiment':'batch_alignment'},'rows':[{'id':'1','correct':True}]}))
            result=legacy.inspect_artifact('bad.json')
            self.assertEqual('exploratory',result['classification']);self.assertTrue(any('arm' in problem for problem in result['problems']))
            valid=Path(tmp,'valid.json');valid.write_text(json.dumps({'meta':{'git':{'commit':'abc'},'experiment':'batch_alignment'},'rows':[{'id':'1','arm':'a','correct':True,'in_candidates':True}]}))
            control=legacy.inspect_artifact('valid.json');self.assertEqual(['a'],control['arms'])

    def fixture(self):
        fixture=dedup_tests.VoiceDedupSourceCacheTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.ns['tqdm'].write=lambda *args:None
        return fixture

    def test_empty_dedup_replaces_running_state_with_terminal_failure(self):
        fixture=self.fixture();fixture.zip.unlink();fixture.narrator.rmdir()
        state=fixture.output/'phase_state.json';state.write_text('{"status":"running","narrators":{"old":{}}}')
        with contextlib.redirect_stdout(io.StringIO()): fixture.ns['run_dedup'](fixture.ns['fixture_model'],'cpu',fixture.zips,fixture.output)
        result=json.loads(state.read_text());self.assertEqual('failed',result['status']);self.assertEqual({},result['narrators']);self.assertIn('narrator',result['error'])

    def test_uppercase_wav_members_keep_spelling_and_train_preference(self):
        fixture=self.fixture()
        with zipfile.ZipFile(fixture.zip,'w') as archive:
            for member in ('elsewhere/other.wav','train/a.WAV','train/b.WaV','train/readme.txt'): archive.writestr(member,b'fixture')
        self.assertEqual(['train/a.WAV','train/b.WaV'],fixture.ns['list_wavs_in_zip'](str(fixture.zip)))

    def test_partial_dedup_never_replaces_prior_good_output_then_clean_retry_publishes(self):
        fixture=self.fixture();folder=fixture.zips/'_deduped';folder.mkdir()
        target=folder/fixture.ns['get_deduped_zip_name']('narrator',fixture.zip.name);target.write_bytes(fixture.zip.read_bytes());before=target.read_bytes()
        stale=folder/'stale.zip';stale.write_bytes(before)
        with zipfile.ZipFile(fixture.zip,'a') as archive: archive.writestr('train/bad.wav',b'not a WAV')
        fixture.run_dedup();self.assertEqual(before,target.read_bytes());self.assertTrue(stale.exists())
        self.assertEqual('partial',json.loads((fixture.output/'phase_state.json').read_text())['status'])
        fixture.write_zip(-.25);fixture.run_dedup();self.assertEqual(fixture.zip.read_bytes(),target.read_bytes());self.assertNotEqual(before,target.read_bytes());self.assertFalse(stale.exists())
        self.assertEqual('complete',json.loads((fixture.output/'phase_state.json').read_text())['status'])

    def test_asin_case_and_numeric_identity_controls(self):
        for token in ('B0B6Q7V4ZP','b0b6q7v4zp','B0b6Q7v4Zp'):
            self.assertEqual(('John Doe','My Book','B0B6Q7V4ZP'),get_dataset_identity('narrator_John_Doe_My_Book_'+token))
        self.assertEqual('1234567890',get_dataset_identity('narrator_John_Doe_Book_1234567890')[2])
        self.assertIsNone(get_dataset_identity('narrator_John_Doe_My_Book_Title')[2])

    def test_single_spine_epub_produces_passage_empty_and_frontmatter_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            for count in (0,1,2,10):
                path=Path(tmp,str(count)+'.epub')
                opf='<package><manifest>'+''.join(f'<item id="c{i}" href="c{i}.xhtml"/>' for i in range(count))+'</manifest><spine>'+''.join(f'<itemref idref="c{i}"/>' for i in range(count))+'</spine></package>'
                with zipfile.ZipFile(path,'w') as archive:
                    archive.writestr('META-INF/container.xml','<container><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>');archive.writestr('OEBPS/content.opf',opf)
                    for i in range(count): archive.writestr(f'OEBPS/c{i}.xhtml','<html><body><p>'+('FIRST ' if i==0 and count>1 else 'STORY ')*120+'</p></body></html>')
                passage=extract_epub_passage(str(path))
                if count: self.assertIn('STORY',passage);self.assertNotIn('FIRST',passage)
                else: self.assertEqual('',passage)
