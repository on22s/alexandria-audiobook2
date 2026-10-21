"""Ordered transcript evidence retains source, ambiguity and authored repeats."""
import unittest
from speech_verification_compare import (get_speech_comparison, get_material_regions,
    get_corroborated_regions, SpeechComparisonUnavailable)


class SpeechVerificationComparisonTests(unittest.TestCase):
    def test_exact_comparison_normalizes_only_comparison_typography(self):
        source="Jose\u0301 said, ‘I can’t leave.’"
        result=get_speech_comparison(source,"JOSÉ said I can't leave")
        self.assertEqual('no_transcript_difference',result['status'])
        self.assertEqual([],result['differences'])
        self.assertEqual("Jose\u0301 said, ‘I can’t leave.’",source)
        self.assertEqual(result['expected_words'],result['matched_words'])

    def test_equal_counts_do_not_hide_missing_and_extra_words(self):
        result=get_speech_comparison('alpha beta gamma delta epsilon zeta',
                                     'alpha beta beta gamma delta zeta')
        self.assertEqual(result['expected_words'],result['observed_words'])
        self.assertEqual('review',result['status'])
        self.assertGreater(result['unmatched_expected'],0)
        self.assertGreater(result['unmatched_observed'],0)
        self.assertTrue(any(block['kind']=='insert' for block in result['differences']))
        self.assertTrue(any(block['kind']=='delete' for block in result['differences']))

    def test_authored_repetition_permits_only_its_actual_multiplicity(self):
        source='Please go now. Please go now.'
        exact=get_speech_comparison(source,source)
        extra=get_speech_comparison(source,source+' Please go now.')
        self.assertEqual([],exact['differences'])
        self.assertEqual(3,extra['unmatched_observed'])
        self.assertTrue(any(block['transcript_repetition'] for block in extra['differences']))

    def test_spelling_similarity_does_not_erase_negation_or_names(self):
        for source,heard in [('The door is locked.','The door is unlocked.'),
                             ('Louis greeted Louis.','Louie greeted Louie.')]:
            with self.subTest(source=source):
                result=get_speech_comparison(source,heard)
                self.assertEqual('review',result['status'])
                self.assertGreater(result['unmatched_expected'],0)
                self.assertEqual([],get_material_regions(result))

    def test_numeric_regions_remain_ambiguous_and_changed_values_are_not_forgiven(self):
        cases=[('25','twenty five'),('2024','twenty twenty four'),('007','seven'),
               ('1.05','one point five'),('21st','twenty first'),('25','26')]
        for source,heard in cases:
            with self.subTest(source=source):
                result=get_speech_comparison('We carefully read the value '+source,
                                            'We carefully read the value '+heard)
                self.assertEqual('review',result['status'])
                self.assertTrue(any('numeric_normalization' in b['ambiguity'] for b in result['differences']))
                self.assertEqual([],get_material_regions(result))

    def test_pronunciation_ambiguity_does_not_hide_an_unrelated_omission(self):
        source='LOO ee greeted the quiet guide beside the garden gate.'
        result=get_speech_comparison(source,'Louis greeted the quiet guide.',
                                    ambiguous_expected_tokens=['loo','ee'])
        self.assertTrue(any('pronunciation_projection' in b['ambiguity'] for b in result['differences']))
        material=get_material_regions(result)
        self.assertEqual(1,len(material))
        self.assertEqual(['beside','the','garden','gate'],material[0]['expected_tokens'])

    def test_empty_transcript_never_becomes_confirmed_lexical_loss(self):
        result=get_speech_comparison('The calm guide quietly closed the garden gate.','')
        self.assertTrue(result['empty_transcript'])
        self.assertEqual('review',result['status'])
        self.assertEqual([],get_material_regions(result))

    def test_adjacent_pronunciation_and_numeric_uncertainty_preserves_missing_tail(self):
        for protected,regions in [('LOO ee',[(3,5)]),('twenty five',[])]:
            with self.subTest(protected=protected):
                source='The guide thanked '+protected+' beneath the quiet garden gate today.'
                result=get_speech_comparison(source,'The guide thanked.',ambiguous_expected_regions=regions)
                self.assertEqual(1,len(result['differences']))
                self.assertEqual(3,result['differences'][0]['expected_from'])
                material=get_material_regions(result)
                self.assertEqual(1,len(material))
                self.assertEqual(['beneath','the','quiet','garden','gate','today'],material[0]['expected_tokens'])
                self.assertEqual(5,material[0]['expected_from'])
                self.assertEqual(material,get_corroborated_regions(result,result))

    def test_concordance_requires_same_expected_projection_and_region(self):
        source='The calm guide quietly closed the garden gate before leaving.'
        primary=get_speech_comparison(source,'The calm guide quietly closed the garden gate.')
        self.assertEqual([],get_material_regions(primary))
        primary=get_speech_comparison(source,'The calm guide quietly closed.')
        confirmation=get_speech_comparison(source,'The calm guide quietly closed.')
        other=get_speech_comparison(source,'quietly closed the garden gate before leaving.')
        self.assertTrue(get_corroborated_regions(primary,confirmation))
        self.assertEqual([],get_corroborated_regions(primary,other))
        with self.assertRaisesRegex(ValueError,'changed'):
            get_corroborated_regions(primary,get_speech_comparison('Different actual speech.','Different actual speech.'))

    def test_short_lines_have_no_ratio_material_verdict(self):
        result=get_speech_comparison('Go now.','Go now. Go now. Go now.')
        self.assertEqual('review',result['status'])
        self.assertTrue(result['differences'][0]['transcript_repetition'])
        self.assertEqual([],get_material_regions(result))

    def test_shared_respelling_replacements_remain_review_evidence(self):
        source='Louis Louis Louis Louis Louis Louis.'
        comparison=get_speech_comparison(source,'Louie Louie Louie Louie Louie Louie.')
        self.assertEqual('review',comparison['status'])
        self.assertEqual([],get_corroborated_regions(comparison,comparison))
        ordinal=get_speech_comparison('We heard fifteenth fifteenth fifteenth on the recording.',
                                     'We heard next next next on the recording.')
        self.assertIn('numeric_normalization',ordinal['differences'][0]['ambiguity'])

    def test_extra_passage_corroborates_only_exact_shared_ordered_subregions(self):
        source='Louis greeted Louis beside the quiet garden gate.'
        primary=get_speech_comparison(source,source+' '+source)
        confirmation=get_speech_comparison(source,(source+' '+source).replace('Louis','Louie'))
        regions=get_corroborated_regions(primary,confirmation)
        self.assertEqual(1,len(regions))
        self.assertEqual([['beside','the','quiet','garden','gate']],regions[0]['exact_shared_extra_regions'])
        unrelated=get_speech_comparison(source,source+' Louie found Louie under a distant bridge.')
        self.assertEqual([],get_corroborated_regions(primary,unrelated))
        authored=get_speech_comparison(source+' '+source,(source+' '+source).replace('Louis','Louie'))
        self.assertEqual([],get_material_regions(authored))

    def test_resource_caps_refuse_whole_comparison_without_prefix_pass(self):
        cases=[('x'*20001,'x'),('a '*2049,'a'),('a '*1024,'a '*1024),('', 'heard'),
               ('bad\ud800','bad'),('\u0301text','text')]
        for source,heard in cases:
            with self.subTest(length=len(source)),self.assertRaises(SpeechComparisonUnavailable):
                get_speech_comparison(source,heard)
        result=get_speech_comparison('a '*1023,'a '*1023)
        self.assertEqual('no_transcript_difference',result['status'])

    def test_too_many_difference_blocks_is_unavailable_not_partial_evidence(self):
        source=' '.join('anchor'+str(i)+' apple' for i in range(40))
        heard=' '.join('anchor'+str(i)+' pear' for i in range(40))
        with self.assertRaisesRegex(SpeechComparisonUnavailable,'block_limit'):
            get_speech_comparison(source,heard)

    def test_evidence_snippets_are_bounded_but_token_regions_are_complete(self):
        source=' '.join('word'+str(i) for i in range(100))
        result=get_speech_comparison(source,'')
        block=result['differences'][0]
        self.assertLessEqual(len(block['expected']),160)
        self.assertEqual(100,len(block['expected_tokens']))
        self.assertEqual(100,block['expected_to'])
