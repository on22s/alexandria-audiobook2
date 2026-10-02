"""Override decisions must refer to distinct known identities."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from voice_clustering import cluster_voices, load_cluster_overrides


class DistinctOverrideTests(unittest.TestCase):
    def test_self_split_and_duplicate_merge_labels_fail_without_mutating_inputs(self):
        for overrides in ({'split':[['A','A']]}, {'merge':[['A','A']]}, {'merge':[['A','B','A']]}):
            before=copy.deepcopy(overrides)
            matrix=np.eye(3)
            with self.subTest(overrides=overrides),self.assertRaisesRegex(ValueError,'distinct'):
                cluster_voices(['A','B','C'],matrix,1,overrides)
            self.assertEqual(before,overrides)
            np.testing.assert_array_equal(np.eye(3),matrix)

    def test_valid_overlapping_merges_and_unrelated_split_keep_real_decisions(self):
        labels=['A','B','C','D'];matrix=np.eye(4)
        overrides={'merge':[['A','B'],['B','C']],'split':[['C','D']]}
        before=copy.deepcopy(overrides)
        clusters,decisions=cluster_voices(labels,matrix,1,overrides)
        self.assertEqual([['A','B','C'],['D']],sorted(sorted(labels[i] for i in row) for row in clusters))
        self.assertEqual([{'type':'manual_merge','labels':['A','B']},{'type':'manual_merge','labels':['A','B','C']}],decisions)
        self.assertEqual(before,overrides)

    def test_non_string_override_label_has_controlled_validation_error(self):
        for label in (None,3,[],{}):
            for kind in ('merge','split'):
                with self.subTest(label=label,kind=kind),self.assertRaisesRegex(ValueError,'strings'):
                    cluster_voices(['A','B'],np.eye(2),1,{kind:[['A',label]]})

    def test_saved_override_file_is_rejected_before_misleading_decision_can_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'overrides.json'
            path.write_text(json.dumps({'version':1,'narrators':{'NARRATOR':{'merge':[['A','A']]}}}))
            before=path.read_bytes()
            overrides=load_cluster_overrides(path,'NARRATOR')
            with self.assertRaisesRegex(ValueError,'distinct'):
                cluster_voices(['A','B'],np.eye(2),1,overrides)
            self.assertEqual(before,path.read_bytes())
