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

    def test_issue_2332_rows_and_w1_tag_forms(self):
        names = {'ANNA', 'BEN'}
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        for tag in (
            'Anna said.', 'Anna said, smiling.', 'Anna says.', 'says Anna.',
            'Anna called.', 'Anna muttered.', 'Anna added.', 'Anna continued.',
            'Anna told Ben.', 'Anna tells Ben.', 'Anna told him.',
            'Anna told Ben quietly.', 'Anna quietly told Ben.',
            'Anna said quietly.', 'said Anna quietly.', 'Anna quietly said.',
            'Anna said to Ben.', 'said Anna to him.', 'Anna said quietly to her.',
            'Anna whispers.', 'Anna replies.', 'Anna answers.', 'Anna asks.',
            'Anna cries.', 'Anna shouts.', 'Anna exclaims.',
            'Anna observed.', 'Anna remarked.', 'Anna rejoined.', 'Anna returned.',
            'Anna retorted.', 'Anna repeated.', 'Anna resumed.', 'Anna interrupted.',
            'Anna inquired.', 'Anna enquired.', 'Anna protested.', 'Anna declared.',
            'Anna insisted.', 'Anna demanded.', 'Anna explained.', 'Anna faltered.',
            'Anna stammered.', 'Anna responded.', 'Anna urged.', 'Anna pleaded.',
            'Anna begged.', 'Anna mutters.', 'Anna calls.', 'Anna adds.', 'Anna continues.',
        ):
            source = f'“Let us go,” {tag} Ben looked at Anna.'
            with self.subTest(tag=tag):
                self.assertEqual({0: 'ANNA'}, get_attribution_tag_evidence(entry, source, names))
        report = validate_attribution(
            [{'type': 'SPOKEN', 'text': 'Go,'}], [{'n': 0, 'speaker': 'KIT'}],
            source_text='“Go,” Nita says. Kit watched Nita.',
            known_names={'NITA', 'KIT'})
        self.assertFalse(report['passed'])
        self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
        self.assertEqual('NITA', report['findings'][0]['expected'])

    def test_w1_names_ending_in_ly_unicode_and_abstentions(self):
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        self.assertEqual({0: 'ANNA BILLY'}, get_attribution_tag_evidence(
            entry, '“Let us go,” Anna Billy said. Ben waited.', {'ANNA BILLY', 'BEN'}))
        self.assertEqual({0: 'ANNA BILLY'}, get_attribution_tag_evidence(
            entry, '“Let us go,” said Anna Billy. Ben waited.', {'ANNA BILLY', 'BEN'}))
        self.assertEqual({0: 'ÉLÉNA'}, get_attribution_tag_evidence(
            entry, '“Let us go,” Éléna quietly said. Ben waited.', {'ÉLÉNA', 'BEN'}))
        self.assertEqual({0: 'ANNA'}, get_attribution_tag_evidence(
            entry, '“Let us go,” Anna said to Éléna. Ben waited.', {'ANNA', 'ÉLÉNA'}))
        self.assertEqual({0: 'SONYA'}, get_attribution_tag_evidence(
            entry, '“Let us go,” Son\u00adya says. Ben waited.', {'SONYA', 'BEN'}))
        for source in (
            '“Let us go,” Mara called the dog.',
            '“Let us go,” Mara added sugar.',
            '“Let us go,” Mara continued walking.',
            '“Let us go,” Mara returned home.',
            '“Let us go,” Mara told the story.',
            '“Let us go,” Mara said to her mother.',
            '“Let us go,” Mara sighed.',
            '“Let us go,” Mara laughed.',
            '“Let us go,” Mara groaned.',
        ):
            with self.subTest(source=source):
                self.assertEqual({}, get_attribution_tag_evidence(
                    entry, source, {'MARA', 'ELENA'}))
        # _REPORTING_VERBS drives quote recovery, lexical quotes and name context
        # and must stay narrowed to the nine core past-tense verbs.
        import pass_quality as pq
        import re
        self.assertIsNone(re.fullmatch(pq._REPORTING_VERBS, 'says'))
        self.assertIsNone(re.fullmatch(pq._REPORTING_VERBS, 'muttered'))

    def test_guillemets_and_low_high_quote_pairs(self):
        entry = [{'type': 'SPOKEN', 'text': 'Let us go'}]
        for source in (
            '«Let us go», said Anna. Ben looked at Anna.',
            '»Let us go«, said Anna. Ben looked at Anna.',
            '„Let us go“, said Anna. Ben looked at Anna.',
            '„Let us go”, said Anna. Ben looked at Anna.',
        ):
            with self.subTest(source=source):
                self.assertEqual({0: 'ANNA'}, get_attribution_tag_evidence(
                    entry, source, {'ANNA', 'BEN'}))
        for bad in (
            '«Let us go”, said Anna. Ben looked at Anna.',
            '„Let us go», said Anna. Ben looked at Anna.',
        ):
            with self.subTest(bad=bad):
                self.assertEqual({}, get_attribution_tag_evidence(entry, bad, {'ANNA', 'BEN'}))

    def test_next_quote_introducer_abstains_while_split_quotes_fire(self):
        # Subject-first tag after a finished sentence introducing the NEXT quote abstains.
        for text, source in (
            ('Are you in town?', '“Are you in town?” Babs said, “Have you got a match?”'),
            ('Are you in town?', '"Are you in town?" Babs said, "Have you got a match?"'),
            ('Stay here.', '“Stay here.” Babs said: “I will return soon.”'),
            ('Watch out!', '“Watch out!” Babs whispered, “Someone is coming.”'),
            ('Stay here.', '«Stay here.» Babs said, «I will return soon.»'),
            ('Stay here.', '„Stay here.“ Babs said, „I will return soon.“'),
        ):
            with self.subTest(source=source):
                self.assertEqual({}, get_attribution_tag_evidence(
                    [{'type': 'SPOKEN', 'text': text}], source, {'MILLY', 'BABS'}))
        # Split quotations by the same speaker must still yield evidence.
        for text, source in (
            ('Let us go,', '“Let us go,” Anna said, “before dark.”'),
            ('Do you think so?', '“Do you think so?” said Anna: “I doubt it.”'),
            ('What happened?', '“What happened?” Anna exclaimed. “Everything!”'),
        ):
            with self.subTest(split=source):
                self.assertEqual({0: 'ANNA'}, get_attribution_tag_evidence(
                    [{'type': 'SPOKEN', 'text': text}], source, {'ANNA', 'BEN'}))

    def test_alias_and_identity_joining_prevents_same_character_false_flags(self):
        # 1. Title-stripped full name resolves to the titled roster entry.
        source_full = '“Let us go,” replied John Vale. Emma watched.'
        self.assertEqual({0: 'MR. JOHN VALE'}, get_attribution_tag_evidence(
            [{'type': 'SPOKEN', 'text': 'Let us go,'}], source_full, {'MR. JOHN VALE', 'EMMA'}))
        self.assertTrue(validate_attribution(
            [{'type': 'SPOKEN', 'text': 'Let us go,'}], [{'n': 0, 'speaker': 'MR. JOHN VALE'}],
            source_text=source_full, known_names={'MR. JOHN VALE', 'EMMA'})['passed'])
        # 2. Case/punctuation-normalized aliases join both predicted and tag speakers.
        self.assertTrue(validate_attribution(
            [{'type': 'SPOKEN', 'text': 'Let us go,'}], [{'n': 0, 'speaker': 'MARY VALE'}],
            source_text='“Let us go,” said Miss Vale. Ben waited.',
            known_names={'MARY VALE', 'BEN'},
            tag_evidence={0: 'MISS VALE'},
            speaker_aliases={'Miss Vale': 'Mary Vale'})['passed'])
        self.assertTrue(validate_attribution(
            [{'type': 'SPOKEN', 'text': 'Let us go,'}], [{'n': 0, 'speaker': 'MR HOLMES'}],
            source_text='“Let us go,” said Holmes. Watson waited.',
            known_names={'SHERLOCK HOLMES', 'MR HOLMES', 'WATSON'},
            tag_evidence={0: 'SHERLOCK HOLMES'},
            speaker_aliases={'Mr. Holmes': 'Sherlock Holmes'})['passed'])
        # 3. Unestablished titled/partial tag naming same character does not contradict.
        for speaker, tag, roster in (
            ('MARY VALE', 'said Miss Vale.', {'MARY VALE', 'HENRY VALE', 'BEN'}),
            ('JOHN MIDDLETON', 'cried Sir John.', {'JOHN MIDDLETON', 'BEN'}),
            ('MR. SHERLOCK HOLMES', 'said Sherlock Holmes.', {'MR. SHERLOCK HOLMES', 'SHERLOCK HOLMES', 'BEN'}),
        ):
            source = f'“Let us go,” {tag} Ben watched.'
            with self.subTest(speaker=speaker, tag=tag):
                report = validate_attribution(
                    [{'type': 'SPOKEN', 'text': 'Let us go,'}], [{'n': 0, 'speaker': speaker}],
                    source_text=source, known_names=roster)
                self.assertTrue(report['passed'], report)
        # 4. Real contradictions must STILL fail: distinct established roster entries,
        # conflicting titles, and different first names sharing a surname.
        for speaker, tag, roster in (
            ('MARY VALE', 'continued Mrs Vale.', {'MARY VALE', 'MRS VALE', 'BEN'}),
            ('FREDERICK VALE', 'said Henry Vale.', {'FREDERICK VALE', 'HENRY VALE', 'BEN'}),
            ('MR VALE', 'said Mrs Vale.', {'MR VALE', 'BEN'}),
            ('LORD VALE', 'said Lady Vale.', {'LORD VALE', 'BEN'}),
            ('JOHN VALE', 'said Alice Vale.', {'JOHN VALE', 'BEN'}),
        ):
            source = f'“Let us go,” {tag} Ben watched. Alice Vale waited. Mrs Vale waited. Lady Vale waited.'
            with self.subTest(contradict_speaker=speaker, tag=tag):
                report = validate_attribution(
                    [{'type': 'SPOKEN', 'text': 'Let us go,'}], [{'n': 0, 'speaker': speaker}],
                    source_text=source, known_names=roster)
                self.assertFalse(report['passed'], report)
                self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
