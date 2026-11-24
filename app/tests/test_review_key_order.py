"""Review keeps each script row's pre-review JSON key order (#2257).

Dict equality is order-blind, so these tests assert ``list(row)`` explicitly:
the reported defect is a pure key-order change that every value-level test
passes over in silence.
"""
import copy
import json
import unittest
from unittest.mock import patch

from generate_script import LLMGenParams
import review_script as review
from tests.test_pass_boundary_retry_regressions import client_for


# Synthetic wording with the reported field sequence; the exact reporter example
# stays in scratch so no excerpt from a book enters the repository (#27).
ISSUE_ROW = json.loads(
    '{"text": "Welcome, prospective readers.", "speaker": "ARDEN FOX", '
    '"speaker_gender": "male", "speaker_age_group": "adult", "speaker_ageless": false, '
    '"instruct": "Measured opening, with a steady and welcoming tone.", '
    '"spoken": true, "source_span": [120, 149]}')
ISSUE_ORDER = list(ISSUE_ROW)


class ReviewKeyOrderTests(unittest.TestCase):
    def bind(self, source, reply, **kwargs):
        return review.get_review_source_binding(copy.deepcopy(source),
                                                copy.deepcopy(reply), **kwargs)

    def test_reported_reorder_is_fixed_and_no_value_changes(self):
        source = [copy.deepcopy(ISSUE_ROW)]
        bound, quality = self.bind(source, [{key: copy.deepcopy(value)
                                             for key, value in ISSUE_ROW.items()}])
        self.assertTrue(quality['passed'], quality)
        self.assertEqual(ISSUE_ORDER, list(bound[0]))
        self.assertEqual(ISSUE_ROW, bound[0])

    def test_modified_surviving_fields_keep_their_original_positions(self):
        source = [copy.deepcopy(ISSUE_ROW)]
        reply = [{**ISSUE_ROW, 'instruct': 'A new instruction.',
                  'speaker_age_group': 'young_adult'}]
        bound, quality = self.bind(source, reply)
        self.assertTrue(quality['passed'], quality)
        self.assertEqual(ISSUE_ORDER, list(bound[0]))
        self.assertEqual('A new instruction.', bound[0]['instruct'])
        self.assertEqual('young_adult', bound[0]['speaker_age_group'])

    def test_supported_keys_added_to_a_legacy_row_are_appended_in_schema_order(self):
        source = [{'text': 'A legacy line.', 'speaker': 'ALICE', 'instruct': 'Soft.'}]
        reply = [{**source[0], 'type': 'SPOKEN', 'spoken': True,
                  'speaker_gender': 'female', 'speaker_age_group': 'adult',
                  'speaker_ageless': False}]
        bound, quality = self.bind(source, reply)
        self.assertTrue(quality['passed'], quality)
        self.assertEqual(['text', 'speaker', 'instruct', 'type', 'spoken',
                          'speaker_gender', 'speaker_age_group', 'speaker_ageless'],
                         list(bound[0]))

    def test_explicitly_cleared_key_is_omitted_and_survivors_keep_order(self):
        source = [copy.deepcopy(ISSUE_ROW)]
        bound, quality = self.bind(source, [{**ISSUE_ROW, 'speaker_gender': None}])
        self.assertTrue(quality['passed'], quality)
        self.assertNotIn('speaker_gender', bound[0])
        self.assertEqual([key for key in ISSUE_ORDER if key != 'speaker_gender'],
                         list(bound[0]))

    def test_nested_metadata_key_order_is_not_rewritten(self):
        source = [{'text': 'A line.', 'speaker': 'ALICE', 'instruct': '',
                   'provenance': {'zeta': 1, 'alpha': 2}, 'source_span': [0, 7]}]
        bound, quality = self.bind(source, [{**copy.deepcopy(source[0]),
                                             'instruct': 'Soft.'}])
        self.assertTrue(quality['passed'], quality)
        self.assertEqual(['text', 'speaker', 'instruct', 'provenance', 'source_span'],
                         list(bound[0]))
        self.assertEqual(['zeta', 'alpha'], list(bound[0]['provenance']))

    def test_labels_only_corrected_speaker_keeps_source_key_order(self):
        source = [copy.deepcopy(ISSUE_ROW)]
        labels = [{'n': 0, 'speaker': 'A NEW SPEAKER', 'instruct': 'Changed.',
                   'speaker_gender': 'female', 'spoken': True}]
        ordered, quality = review.get_review_label_binding(source, labels)
        self.assertTrue(quality['passed'], quality)
        bound, warnings = review.get_review_labelled_entries(source, ordered)
        self.assertEqual(['text', 'speaker', 'speaker_gender', 'instruct',
                          'spoken', 'source_span'], list(bound[0]))

    def test_partial_label_retention_keeps_source_key_order(self):
        source = [copy.deepcopy(ISSUE_ROW)]
        bound, status = review.get_review_partial_label_retention(
            source, [{**ISSUE_ROW, 'instruct': 'New.'}], False)
        self.assertIsNotNone(bound, status)
        self.assertEqual(ISSUE_ORDER, list(bound[0]))

    def test_review_batch_unchanged_control(self):
        source = [copy.deepcopy(ISSUE_ROW)]
        client, _ = client_for([([copy.deepcopy(ISSUE_ROW)], 'stop')])
        with patch('generate_script.time.sleep'):
            result = review.review_batch(client, 'fixture', source, 1, 1,
                                         LLMGenParams(), max_retries=0)
        self.assertIsNotNone(result)
        self.assertEqual(ISSUE_ORDER, list(result[0]))


if __name__ == '__main__':
    unittest.main()
