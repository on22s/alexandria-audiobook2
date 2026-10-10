"""A real name can still contradict the speaker named by a source tag."""
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams
from pass_quality import get_attribution_tag_evidence, validate_attribution
from tests.test_three_pass_generate import _client_returning


class SourceTagTests(unittest.TestCase):
    TEXT = 'Bring the silver lantern.'
    FROZEN = [{'type': 'SPOKEN', 'text': TEXT}]
    NAMES = {'MARA', 'ELENA'}

    def report(self, source, speaker='ELENA', names=None, aliases=None):
        return validate_attribution(self.FROZEN, [{'n': 0, 'speaker': speaker}],
                                    source_text=source, known_names=names or self.NAMES,
                                    speaker_aliases=aliases)

    def test_both_tag_orders_reject_another_real_character(self):
        for tag in ('Mara said.', 'said Mara.', 'Mara whispered, looking away.'):
            source = f'“{self.TEXT}” {tag} Elena watched Mara.'
            with self.subTest(tag=tag):
                report = self.report(source)
                self.assertFalse(report['passed'])
                self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
                self.assertEqual('MARA', report['findings'][0]['expected'])
                self.assertTrue(self.report(source, 'MARA')['passed'])

    def test_uncertain_sources_abstain(self):
        sources = (
            f'“{self.TEXT}” Mara said. “{self.TEXT}” Elena said.',
            f'“First, {self.TEXT}” Mara said.',
            f'“{self.TEXT} Then we leave.” Mara said.',
            f'“{self.TEXT}”\n\nMara said.',
            f'“{self.TEXT}” she said.',
            f'“{self.TEXT}” He said.',
            f'“{self.TEXT}” Mara’s said.',
            f'“{self.TEXT}” Mara’s voice rang out.',
            f'“{self.TEXT}” Mara said the final words of the spell.',
            f'“{self.TEXT}” said Mara the enchantment.',
            f'“{self.TEXT}” Mara dijo.',
            f'“{self.TEXT}” UnknownPerson said.',
            f'{self.TEXT} Mara said.',
            f'“Elena recalled \"{self.TEXT}\" Mara said.”',
            f'\"Elena recalled “{self.TEXT}” Mara said.\"',
            f'“{self.TEXT}\" Mara said.',
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(self.report(source, names=self.NAMES | {'HE', 'MARAS'})['passed'])

    def test_aliases_and_first_name_ambiguity(self):
        source = f'“{self.TEXT}” Mara said.'
        self.assertTrue(self.report(source, 'MARA JONES',
            {'MARA JONES', 'MARA SMITH', 'ELENA'})['passed'])
        self.assertTrue(self.report(source, 'ELENA',
            {'MARA JONES', 'MARA SMITH', 'ELENA'})['passed'])
        self.assertTrue(self.report(source, 'MARY',
            {'MARA', 'MARY', 'ELENA'}, {'MARY': 'MARA'})['passed'])
        report = self.report(f'“{self.TEXT}” Mary said.', 'ELENA',
            {'MARA', 'MARY', 'ELENA'}, {'MARY': 'MARA'})
        self.assertEqual('MARA', report['findings'][0]['expected'])
        self.assertTrue(self.report(source, 'MARA JONES',
            {'MARA JONES', 'ELENA'})['passed'])

    def test_unresolved_proposed_name_and_unknown_abstain(self):
        source = f'“{self.TEXT}” Mara said. Louise arrived.'
        self.assertTrue(self.report(source, 'LOUISE')['passed'])
        self.assertTrue(self.report(source, 'UNKNOWN')['passed'])

    def test_matching_is_shared_across_the_batch(self):
        with patch('dialogue_spans.get_source_match_positions', wraps=__import__('dialogue_spans').get_source_match_positions) as locate:
            evidence = get_attribution_tag_evidence(self.FROZEN * 2,
                f'“{self.TEXT}” Mara said.', self.NAMES)
        self.assertEqual({0: 'MARA', 1: 'MARA'}, evidence)
        self.assertEqual(1, locate.call_count)

    def run_batch(self, answers, mode='fail', retries=2, requests=None):
        params = LLMGenParams(system_prompt='s', user_prompt_template='{roster}{batch}',
                              max_tokens=500, temperature=0.0)
        client = _client_returning(answers)
        create = client.chat.completions.create
        def record(**kwargs):
            if requests is not None:
                requests.append(kwargs)
            return create(**kwargs)
        client.chat.completions.create = record
        return tp.attribute_batch(client, 'fixture', self.FROZEN,
            params, roster=list(self.NAMES), source_text=f'“{self.TEXT}” Mara said.',
            max_retries=retries, on_exhaustion=mode,
            cast={'known_names': self.NAMES})

    def test_real_retry_corrects_the_label_without_changing_text(self):
        wrong, right = [{'n': 0, 'speaker': 'ELENA'}], [{'n': 0, 'speaker': 'MARA'}]
        with patch('generate_script.time.sleep'), patch('three_pass_generate.get_attribution_tag_evidence', wraps=get_attribution_tag_evidence) as evidence:
            requests = []
            result = self.run_batch([wrong, right], requests=requests)
        self.assertEqual([{'text': self.TEXT, 'speaker': 'MARA'}], result)
        self.assertEqual(1, evidence.call_count)
        self.assertEqual(2, len(requests))
        self.assertIn('MARA', str(requests[1]['messages']))
        self.assertIn('source speech tag', str(requests[1]['messages']))

    def test_keep_exhaustion_is_flagged_and_cannot_seed_roster(self):
        wrong = [{'n': 0, 'speaker': 'ELENA'}]
        result = self.run_batch([wrong] * 10, 'keep', 1)
        self.assertEqual(self.TEXT, result[0]['text'])
        self.assertEqual('ELENA', result[0]['speaker'])
        self.assertEqual(['speaker_contradicts_source_tag'], result[0]['attribution_unchecked'])
        self.assertEqual([], tp.build_roster(result, 'Elena spoke. ' * 400))

    def test_fail_and_fallback_keep_existing_policies(self):
        wrong = [{'n': 0, 'speaker': 'ELENA'}]
        with self.assertRaises(tp.PassExhausted):
            self.run_batch([wrong] * 10, 'fail', 1)
        result = self.run_batch([wrong] * 10, 'fallback', 1)
        self.assertEqual(self.TEXT, result[0]['text'])
        self.assertNotEqual('ELENA', result[0]['speaker'])
