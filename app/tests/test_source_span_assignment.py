"""Known rejected tag assignments and valid dialogue splits validate the scorer."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from source_span_coverage import get_source_spans, get_span_coverage_findings


class SpanAssignmentTests(unittest.TestCase):
    def check(self, source, entries):
        spans=get_source_spans(source);before=copy.deepcopy((spans,entries))
        result=get_span_coverage_findings(spans,entries)
        self.assertEqual(before,(spans,entries))
        return result

    def test_same_global_words_but_extra_or_swapped_ids_are_rejected(self):
        source='First sentence. Second sentence.'
        for ids in ((['S001','S002'],['S001']),(['S002'],['S001'])):
            with self.subTest(ids=ids):
                findings=self.check(source,[{'text':'First sentence.','source_span_ids':ids[0]},{'text':'Second sentence.','source_span_ids':ids[1]}])
                self.assertEqual(2,sum(f['code']=='misassigned_source_span_ids' for f in findings))
                self.assertFalse(any(f['code']=='uncovered_source_spans' for f in findings))
                with tempfile.TemporaryDirectory() as tmp:
                    path=Path(tmp)/'coverage.json';path.write_text(json.dumps(findings))
                    self.assertEqual(findings,json.loads(path.read_text()))

    def test_duplicate_id_within_one_entry_is_rejected(self):
        findings=self.check('First sentence.',[{'text':'First sentence.','source_span_ids':['S001','S001']}])
        self.assertIn('duplicate_source_span_ids',[f['code'] for f in findings])

    def test_valid_split_span_and_combined_spans_keep_the_existing_prompt_contract(self):
        source='“Stay,” she said. Then she left.'
        entries=[{'text':'Stay','source_span_ids':['S001']},{'text':'she said. Then she left.','source_span_ids':['S001','S002']}]
        self.assertEqual([],self.check(source,entries))
        self.assertEqual([],self.check(source,[{'text':source,'source_span_ids':['S001','S002']}]))

    def test_repeated_identical_sentences_use_positions_not_text_membership(self):
        source='Wait here. Wait here.'
        good=[{'text':'Wait here.','source_span_ids':['S001']},{'text':'Wait here.','source_span_ids':['S002']}]
        self.assertEqual([],self.check(source,good))
        good[1]['source_span_ids']=['S001']
        self.assertTrue(any(f['code']=='misassigned_source_span_ids' for f in self.check(source,good)))

    def test_duplicated_or_missing_text_cannot_hide_behind_complete_id_union(self):
        for text in ('First. First.','First.','Second. First.'):
            with self.subTest(text=text):
                findings=self.check('First. Second.',[{'text':text,'source_span_ids':['S001','S002']}])
                self.assertTrue(any(f['code']=='source_span_text_mismatch' for f in findings))

    def test_unicode_script_tokenization_matches_the_shared_quality_scorer(self):
        self.assertEqual([],self.check('你好。 世界。',[{'text':'你好。 世界。','source_span_ids':['S001']}]))
        findings=self.check('First.',[{'source_span_ids':['S001']}])
        self.assertTrue(any(f['code']=='invalid_source_span_text' for f in findings))
