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

    def test_continuation_after_bare_tag_catches_wrong_speaker(self):
        source = '“Go,” Nita said. “Now.” Kit looked at Nita. Kit left with Nita.'
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Now.'},
        ]
        names = {'NITA', 'KIT'}
        first_wrong = validate_attribution(
            frozen,
            [{'n': 0, 'speaker': 'KIT'}, {'n': 1, 'speaker': 'NARRATOR'}, {'n': 2, 'speaker': 'NITA'}],
            source_text=source, known_names=names)
        self.assertFalse(first_wrong['passed'])
        self.assertEqual('speaker_contradicts_source_tag', first_wrong['findings'][0]['code'])
        self.assertEqual(1, first_wrong['findings'][0]['entry_number'])
        self.assertEqual('NITA', first_wrong['findings'][0]['expected'])

        second_wrong = validate_attribution(
            frozen,
            [{'n': 0, 'speaker': 'NITA'}, {'n': 1, 'speaker': 'NARRATOR'}, {'n': 2, 'speaker': 'KIT'}],
            source_text=source, known_names=names)
        self.assertFalse(second_wrong['passed'])
        self.assertEqual('speaker_contradicts_source_tag', second_wrong['findings'][0]['code'])
        self.assertEqual(3, second_wrong['findings'][0]['entry_number'])
        self.assertEqual('NITA', second_wrong['findings'][0]['expected'])

        both_right = validate_attribution(
            frozen,
            [{'n': 0, 'speaker': 'NITA'}, {'n': 1, 'speaker': 'NARRATOR'}, {'n': 2, 'speaker': 'NITA'}],
            source_text=source, known_names=names)
        self.assertTrue(both_right['passed'])
        self.assertEqual({0: 'NITA', 2: 'NITA'},
                         get_attribution_tag_evidence(frozen, source, names))

        longer_source = '“Go,” Anna said. “Now we leave.”'
        longer_frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Anna said.'},
            {'type': 'SPOKEN', 'text': 'Now we leave.'},
        ]
        self.assertEqual({0: 'ANNA', 2: 'ANNA'},
                         get_attribution_tag_evidence(longer_frozen, longer_source, {'ANNA'}))
        self.assertEqual({0: 'ANNA'},
                         get_attribution_tag_evidence([longer_frozen[2]], longer_source, {'ANNA'}))

        for tag_text, expected, tag_names in (
            ('said Nita,', 'NITA', {'NITA', 'KIT'}),
            ('Nita quietly said.', 'NITA', {'NITA', 'KIT'}),
            ('Nita said quietly.', 'NITA', {'NITA', 'KIT'}),
            ('said Nita quietly,', 'NITA', {'NITA', 'KIT'}),
            ('Anna Billy said.', 'ANNA BILLY', {'ANNA BILLY', 'ANNA SMITH', 'KIT'}),
            ('Son\u00adya said.', 'SONYA', {'SONYA', 'KIT'}),
        ):
            with self.subTest(tag_text=tag_text):
                cont_source = f'“Go,” {tag_text} “Now.”'
                self.assertEqual({0: expected}, get_attribution_tag_evidence(
                    [{'type': 'SPOKEN', 'text': 'Now.'}], cont_source, tag_names))

    def test_continuation_own_tag_verbs_and_tag_outside_the_closing_quote(self):
        names = {'NITA', 'KIT'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
        ]
        # L1: the own-tag guard must know more than the nine base verbs, and a
        # following clause belongs to the tag, not to the continuation speaker.
        for source in (
                '“Go,” Nita said. “No,” Kit muttered.',
                '“Go,” Nita said. “No,” Kit began.',
                '“Go,” Nita said. “No,” Kit added, without looking up.',
                '“Go,” Nita said. “No,” Kit said, shaking his head.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA', 2: 'KIT'},
                                 get_attribution_tag_evidence(frozen, source, names))
        # L2: the tag may sit outside the closing quote, introduced by a dash.
        uncommaed = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No'},
        ]
        for source in (
                '“Go,” Nita said. “No”—Kit said.',
                '“Go,” Nita said. “No” — Kit said.',
                '“Go,” Nita said. “No”-Kit said.',
                '“Go,” Nita said. “No”, Kit said.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA', 2: 'KIT'},
                                 get_attribution_tag_evidence(uncommaed, source, names))

    def test_continuation_abstains_on_action_other_name_paragraph_own_tag_and_uncertain_quotes(self):
        frozen_cont = [{'type': 'SPOKEN', 'text': 'Now.'}]
        names = {'NITA', 'KIT', 'MARA JONES', 'MARA SMITH'}
        abstaining_sources = (
            '“Go,” Kit said. Nita shook her head. “Now.”',
            '“Go,” Nita said. She paused. “Now.”',
            '“Go,” Nita said, looking away. “Now.”',
            '“Go,” Nita said to Kit. “Now.”',
            '“Go,” Nita said, facing Kit. “Now.”',
            '“Go,” Nita said. Kit waited. “Now.”',
            '“Go,” Nita said Billy. “Now.”',
            '“Go,” Nita said.\n\n“Now.”',
            '“Go,” Nita said.\n“Now.”',
            '“Go,” Nita quietly said softly. “Now.”',
            '“Go,” she said. “Now.”',
            '“Go,” Nita’s said. “Now.”',
            '“Go,” Lord said. “Now.”',
            '“Go,” Nita said. “Now.” “Wait,” Kit said. “Now.”',
            '“Go,” Nita said. “Now."',
            '“Go, Nita said. “Now.”',
            '“Go,” Nita said. “Kit recalled "Now."”',
            '“Kit recalled "Go," Nita said.” “Now.”',
        )
        for source in abstaining_sources:
            with self.subTest(source=source):
                self.assertEqual({}, get_attribution_tag_evidence(
                    frozen_cont, source, names | {'LORD HENRY'}))

        # Continuation with its own tag keeps its own (or abstains if its own tag is uncertain).
        own_tag_frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Now,'},
        ]
        self.assertEqual({0: 'NITA', 2: 'KIT'}, get_attribution_tag_evidence(
            own_tag_frozen, '“Go,” Nita said. “Now,” Kit replied.', names))
        for own_uncertain in (
            '“Go,” Nita said. “Now,” Mara said.',
            '“Go,” Nita said. “Now,” he said.',
            '“Go,” Nita said. “Now,” Kit replied quietly.',
        ):
            with self.subTest(own_uncertain=own_uncertain):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    own_tag_frozen, own_uncertain, names))

        # A third quotation in the same paragraph does not inherit A's tag.
        three_frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Now.'},
            {'type': 'SPOKEN', 'text': 'Later.'},
        ]
        self.assertEqual({0: 'NITA', 2: 'NITA'}, get_attribution_tag_evidence(
            three_frozen, '“Go,” Nita said. “Now.” “Later.”', names))
