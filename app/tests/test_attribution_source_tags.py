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

    def test_abbreviated_and_full_titles_remain_the_same_identity(self):
        # #2388 regression guard: the continuation guard must NOT extend the
        # shared title vocabulary. If it does, a correct abbreviated answer to
        # a full-title tag is rejected as a different character.
        for answered, tag in (
                ('CAPT WENTWORTH', 'Captain Wentworth'),
                ('REV COLLINS', 'Reverend Collins'),
                ('ST JOHN', 'Saint John'),
                ('PROF HIGGINS', 'Professor Higgins')):
            with self.subTest(answered=answered):
                for narration in (f'said {tag}.', f'{tag} said.'):
                    source = (f'{answered.title()} came in. {answered.title()} sat. '
                              f'{tag} came in. {tag} sat down. Anne waved. '
                              f'Anne smiled. “Go,” {narration}')
                    frozen = [{'type': 'SPOKEN', 'text': 'Go,'},
                              {'type': 'NARRATOR', 'text': narration}]
                    with self.subTest(narration=narration):
                        report = validate_attribution(
                            frozen,
                            [{'n': 0, 'speaker': answered},
                             {'n': 1, 'speaker': 'NARRATOR'}],
                            source_text=source,
                            known_names={answered, 'ANNE'})
                        self.assertTrue(report['passed'], report)

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
        source = '“Go,” Nita said. “Now.” She looked at Nita. Kit left with Nita.'
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
        # Guillemet and low-high quote pairs inherit exactly like curly quotes
        # (the continuation pairing is _TAG_QUOTE_PAIRS, not a literal dict).
        guillemet_frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Now.'},
        ]
        for source in ('«Go,» Nita said. «Now.»',
                       '„Go,“ Nita said. „Now.“'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA', 2: 'NITA'},
                                 get_attribution_tag_evidence(
                                     guillemet_frozen, source, {'NITA', 'KIT'}))

    def test_continuation_own_tag_rule_and_broader_clause_verbs(self):
        names = {'NITA', 'KIT'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
        ]
        # A tag shape the direct check recognises is B's own tag: the speaker
        # comes from that tag (not from the continuation), so #2388's wider
        # verb list and its adverbs/objects apply here unchanged.
        for source in (
                '“Go,” Nita said. “No,” Kit muttered.',
                '“Go,” Nita said. “No,” Kit continued.',
                '“Go,” Nita said. “No,” Kit added, without looking up.',
                '“Go,” Nita said. “No,” Kit said, shaking his head.',
                '“Go,” Nita said. “No,” Kit replied quietly.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA', 2: 'KIT'},
                                 get_attribution_tag_evidence(frozen, source, names))
        # Clause-shaped own tags that the direct rule does not carry ("began",
        # "went on", "cut in") still mean B has its own tag, so B must not
        # inherit Nita - and the clause must not invent direct evidence.
        for source in (
                '“Go,” Nita said. “No,” Kit began.',
                '“Go,” Nita said. “No,” Kit began to speak.',
                '“Go,” Nita said. “No,” Kit Vance began.',
                '“Go,” Nita said. “No,” Kit went on.',
                '“Go,” Nita said. “No,” Kit cut in.',
                '“Go,” Nita said. “No,” he began.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    frozen, source, names | {'KIT VANCE'}))
        # An articled/possessive subject is a named subject too: "the man" and
        # "his mother" must be recognised exactly like "Kit", or a correct
        # answer inherits Nita's speaker.
        for source in (
                '“Go,” Nita said. “No,” his mother began.',
                '“Go,” Nita said. “No,” the man began.',
                '“Go,” Nita said. “No,” the old man went on.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    frozen, source, names))
        # Control: a lowercase non-determiner subject is not treated as a name,
        # so the guard is not simply "any subject" (the mutant that returns
        # True for every subject dies here).
        self.assertEqual({0: 'NITA', 2: 'NITA'}, get_attribution_tag_evidence(
            frozen, '“Go,” Nita said. “No,” somebody began.', names))
        # The continuation matcher itself reads the tag shape main recognises,
        # not only the nine old reporting verbs (#2388, Rule 15).
        now = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Now.'},
        ]
        for source in (
                '“Go,” Nita muttered. “Now.”',
                '“Go,” Nita continued. “Now.”',
                '“Go,” Nita added quietly. “Now.”'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA', 2: 'NITA'},
                                 get_attribution_tag_evidence(now, source, names))

    def test_continuation_own_tag_with_a_dotted_honorific(self):
        # Prose abbreviates titles with a period ("Mrs.", "Dr.", "St.",
        # "Col.", "M.", "Messrs."). _TAG_WORD cannot cross that period, so a
        # dotted own tag was invisible: B inherited A's speaker and a correct
        # answer was rejected (gate 1 BLOCKER). The guard flattens the period
        # as a shape, so a title outside #2388's vocabulary works too.
        names = {'NITA', 'KIT', 'MRS BENNET', 'MR BENNET', 'DR KIT'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
        ]
        in_vocabulary = {'Mr.', 'Mrs.', 'Ms.', 'Dr.'}
        for title, subject in (
                ('Mr.', 'Bennet'), ('Mrs.', 'Bennet'), ('Ms.', 'Bennet'),
                ('Dr.', 'Kit'), ('St.', 'John'), ('Prof.', 'Vale'),
                ('Rev.', 'Vale'), ('Capt.', 'Vale'), ('Col.', 'Brandon'),
                ('Gen.', 'Vale'), ('Lt.', 'Vale'), ('Sgt.', 'Vale'),
                ('M.', 'Vale'), ('Mlle.', 'Vale'), ('Mme.', 'Vale'),
                ('Fr.', 'Vale'), ('Hon.', 'Vale'), ('Sr.', 'Vale'),
                ('Messrs.', 'Vale')):
            # Subject-first: the dotted title is B's own tag, so B abstains no
            # matter whether the title is in #2388's vocabulary.
            source = f'“Go,” Nita said. “No,” {title} {subject} said.'
            with self.subTest(title=title, source=source):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    frozen, source, names))
            # Verb-first: a dotted title inside the vocabulary makes the direct
            # read abstain (the title alone is not a name); one outside it is
            # still read as a bare name, exactly as on main (must not change).
            source = f'“Go,” Nita said. “No,” said {title} {subject}.'
            expected = ({0: 'NITA'} if title in in_vocabulary else
                        {0: 'NITA', 2: title.rstrip('.').upper()})
            with self.subTest(title=title, source=source):
                self.assertEqual(expected, get_attribution_tag_evidence(
                    frozen, source, names))
        # Undotted spellings still resolve as B's own direct tag.
        for title, subject, expected in (
                ('Mrs', 'Bennet', 'MRS BENNET'), ('Dr', 'Kit', 'DR KIT')):
            with self.subTest(title=title):
                self.assertEqual({0: 'NITA', 2: expected},
                                 get_attribution_tag_evidence(
                                     frozen,
                                     f'“Go,” Nita said. “No,” {title} {subject} said.',
                                     names))
        # The own-tag clause also sees the dotted title.
        for source in ('“Go,” Nita said. “No,” Mrs. Bennet began.',
                       '“Go,” Nita said. “No,” Dr. Kit went on.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    frozen, source, names))
        # #2391 r6 BLOCKER 2: the coordinated form has a lowercase "and" after
        # the first dotted title. Stripping the period only before a capitalised
        # word was how r4 lost the gate-2 fix, so every title is pinned in both
        # orders here. Verb-first keeps main's direct read: a title outside
        # #2388's vocabulary is still read there as a bare name.
        # #2391 r7 LOW 2: "&" is the same coordinated form; the guard
        # normalises it to "and" before matching, so every title and both orders
        # must behave exactly as for "and".
        in_vocabulary = {'Mr.', 'Mrs.', 'Ms.', 'Dr.'}
        titles = [title for title, _subject in (
                ('Mr.', 'Bennet'), ('Mrs.', 'Bennet'), ('Ms.', 'Bennet'),
                ('Dr.', 'Kit'), ('St.', 'John'), ('Prof.', 'Vale'),
                ('Rev.', 'Vale'), ('Capt.', 'Vale'), ('Col.', 'Brandon'),
                ('Gen.', 'Vale'), ('Lt.', 'Vale'), ('Sgt.', 'Vale'),
                ('M.', 'Vale'), ('Mlle.', 'Vale'), ('Mme.', 'Vale'),
                ('Fr.', 'Vale'), ('Hon.', 'Vale'), ('Sr.', 'Vale'),
                ('Messrs.', 'Vale'))]
        for conjunction in ('and', '&'):
            for title in titles:
                source = (f'“Go,” Nita said. “No,” {title} {conjunction} '
                          'Mrs. Brandon said.')
                with self.subTest(title=title, conjunction=conjunction,
                                  source=source):
                    self.assertEqual({0: 'NITA'},
                                     get_attribution_tag_evidence(
                                         frozen, source, names))
                source = (f'“Go,” Nita said. “No,” said {title} '
                          f'{conjunction} Mrs. Brandon.')
                expected = ({0: 'NITA'} if title in in_vocabulary else
                            {0: 'NITA', 2: title.rstrip('.').upper()})
                with self.subTest(title=title, conjunction=conjunction,
                                  source=source):
                    self.assertEqual(expected, get_attribution_tag_evidence(
                        frozen, source, names))

    def test_continuation_abstains_on_an_action_beat_naming_another_character(self):
        # A clause after the quotation that names a different character is an
        # action beat, not evidence that the quotation continues the tag
        # (gate 1 LOW 1). "No." Kit crossed his arms. must not reject KIT.
        names = {'NITA', 'KIT', 'MRS BENNET'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No.'},
        ]
        for source in (
                '“Go,” Nita said. “No.” Kit crossed his arms.',
                '“Go,” Nita said. “No.” Kit shook his head.',
                '“Go,” Nita said. “No.” Kit, however, crossed his arms.',
                '“Go,” Nita said. “No.” Mrs. Bennet smiled.',
                # #2391 r7 LOW 2: the "&" coordinated beat is read like "and",
                # so the named character still abstains.
                '“Go,” Nita said. “No.” Mr. & Mrs. Bennet smiled.',
                '“Go,” Nita said. “No.” Bram crossed his arms.',
                # A sentence-opening conjunction/adverb before the subject does
                # not hide the beat (LOW 1).
                '“Go,” Nita said. “No.” Then Kit laughed.',
                '“Go,” Nita said. “No.” But Kit laughed.',
                '“Go,” Nita said. “No.” However Kit laughed.',
                # LOW 1: an -ly adverb with a comma, two openers, and a
                # repeat word after the opener all hide nothing.
                '“Go,” Nita said. “No.” Quietly, Kit laughed.',
                '“Go,” Nita said. “No.” Slowly, Kit shook his head.',
                '“Go,” Nita said. “No.” Then again Kit laughed.',
                '“Go,” Nita said. “No.” And then Kit laughed.',
                '“Go,” Nita said. “No.” But once Kit laughed.',
                # #2391 r6 LOW 2: an opener with a comma, and an opener followed
                # by one adverb, hide nothing either.
                '“Go,” Nita said. “No.” Then, Kit laughed.',
                '“Go,” Nita said. “No.” Still, Kit laughed.',
                '“Go,” Nita said. “No.” Then suddenly Kit laughed.',
                '“Go,” Nita said. “No.” Yet quietly, Kit laughed.',
                # #2391 r7 LOW 1: a coordinated adverb phrase before the
                # subject is the adverb slot, not the beat's subject.
                '“Go,” Nita said. “No.” Slowly and carefully, Kit shook his '
                'head.',
                '“Go,” Nita said. “No.” Quietly and slowly, Kit laughed.',
                '“Go,” Nita said. “No.” Slowly, carefully, Kit shook his head.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    frozen, source, names))
        # Controls that keep the inherited speaker: a pronoun subject after the
        # opener, A's own name (a beat about A adds nothing), and an opener
        # whose subject is a pronoun.
        for source in (
                '“Go,” Nita said. “No.” She shook her head.',
                '“Go,” Nita said. “No.” Nita smiled.',
                '“Go,” Nita said. “No.” Then she left.',
                '“Go,” Nita said. “No.” But she left.',
                '“Go,” Nita said. “No.” Then again she left.',
                '“Go,” Nita said. “No.” Quietly, she left.',
                '“Go,” Nita said. “No.” Slowly and carefully, she shook her '
                'head.',
                '“Go,” Nita said. “No.” Then, she left.',
                '“Go,” Nita said. “No.” Still, she left.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA', 2: 'NITA'},
                                 get_attribution_tag_evidence(
                                     frozen, source, names))
        # A dotted honorific beat names the same spelling the own-tag guard
        # reads; A's own titled name keeps the inherited speaker.
        import pass_quality as pq
        self.assertEqual('Mrs Bennet',
                         pq._get_continuation_action_beat_subject(
                             ' Mrs. Bennet smiled.'))
        self.assertIsNone(pq._get_continuation_action_beat_subject(
            ' She shook her head.'))
        self.assertEqual({0: 'MRS BENNET', 2: 'MRS BENNET'},
                         get_attribution_tag_evidence(
                             frozen, '“Go,” Bennet said. “No.” Mrs. Bennet smiled.',
                             {'MRS BENNET'}))

    def test_continuation_own_tag_inverted_phrasal_and_interjected(self):
        # Own-tag shapes outside the decided list (gate 1 LOW 2): verb-first
        # clauses, phrasal reporting verbs, a comma-delimited interjection
        # between subject and verb, and a capitalised subject with any verb.
        names = {'NITA', 'KIT'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
        ]
        for source in (
                '“Go,” Nita said. “No,” began Kit.',
                '“Go,” Nita said. “No,” went on Kit.',
                '“Go,” Nita said. “No,” put in Kit.',
                '“Go,” Nita said. “No,” cut in Kit.',
                '“Go,” Nita said. “No,” Kit put in.',
                '“Go,” Nita said. “No,” Kit, however, said.',
                '“Go,” Nita said. “No,” Kit, however, began.',
                # LOW 2: a lowercase modifier before a dotted title, a verb-first
                # tag with a verb outside both lists, a longer interjection, a
                # parenthetical, and a possessive reply noun.
                '“Go,” Nita said. “No,” old Mr. Bennet said.',
                '“Go,” Nita said. “No,” interposed Kit.',
                '“Go,” Nita said. “No,” Kit (smiling) said.',
                '“Go,” Nita said. “No,” Kit, who had been silent, said.',
                '“Go,” Nita said. “No,” came Kit’s reply.',
                '“Go,” Nita said. “No,” was Kit’s answer.',
                # NIT 1: a subject longer than the shared four-word cap is
                # still B's own tag, so B abstains rather than inherits.
                '“Go,” Nita said. “No,” the stranger in the cloak said.',
                '“Go,” Nita said. “No,” said the man in the cloak.',
                '“Go,” Nita said. “No,” the old man by the door said.',
                # #2391 r6 LOW 3: a dash-bracketed interruption, an interjection
                # longer than six words, and a subject longer than eight words
                # are all still B's own tag.
                '“Go,” Nita said. “No,” Kit — smiling — said.',
                '“Go,” Nita said. “No,” Kit – smiling – said.',
                '“Go,” Nita said. “No,” Kit - smiling - said.',
                '“Go,” Nita said. “No,” Kit, who had been silent for a long '
                'while now, said.',
                '“Go,” Nita said. “No,” said the tall man in the grey coat by '
                'the door.',
                # A pronoun or A's own name isolates the interjection rule from
                # the action-beat rule, which would abstain anyway for Kit.
                '“Go,” Nita said. “No,” Nita, however, said.',
                '“Go,” Nita said. “No,” he, however, said.',
                '“Go,” Nita said. “No,” the man, however, said.',
                '“Go,” Nita said. “No,” Kit laughed.',
                '“Go,” Nita said. “No,” Kit sighed.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    frozen, source, names))
        # Control: a lowercase non-determiner subject is still not a name, so
        # the guard is not simply "any subject".
        self.assertEqual({0: 'NITA', 2: 'NITA'}, get_attribution_tag_evidence(
            frozen, '“Go,” Nita said. “No,” somebody began.', names))
        # A pronoun plus a direct verb is not an own tag without the
        # interjection: "she said this after a pause" is narration, not a
        # speech tag (found on TheGambler when the interjection path was
        # shared with the plain clause).
        reported = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No?'},
        ]
        self.assertEqual({0: 'NITA', 2: 'NITA'}, get_attribution_tag_evidence(
            reported, '“Go,” Nita said. “No?” She said this after a pause.',
            names))
        # The clause list deliberately repeats added/adds/continued/continues:
        # the direct rule's adverb slot accepts only a lowercase -ly adverb, so
        # a non-ly adverb ("she added afterwards,") is an own tag only through
        # this list.
        self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
            frozen, '“Go,” Nita said. “No,” she added afterwards, “I suppose.”',
            names))

    def test_continuation_own_tag_period_is_required_only_by_the_own_tag_check(self):
        # "No," Mrs. Bennet said. names A's own speaker (MRS BENNET), so the
        # action-beat check alone would keep the inherited speaker. Only the
        # own-tag check's period-stripping makes the quotation abstain, so
        # removing that step fails here while the beat check stays intact.
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Bennet said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
        ]
        self.assertEqual({0: 'MRS BENNET'}, get_attribution_tag_evidence(
            frozen, '“Go,” Bennet said. “No,” Mrs. Bennet said.',
            {'MRS BENNET'}))
        import pass_quality as pq
        self.assertEqual('Mrs Bennet',
                         pq._get_continuation_action_beat_subject(
                             ' Mrs. Bennet said.'))

    def test_continuation_abstains_when_the_paragraph_has_two_tagged_speakers(self):
        # LOW 3: a third quotation in a paragraph that already carries two
        # different tagged speakers must not inherit the nearer one.
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
            {'type': 'NARRATOR', 'text': 'said Kit.'},
            {'type': 'SPOKEN', 'text': 'Yes.'},
        ]
        names = {'NITA', 'KIT', 'MARA'}
        self.assertEqual({0: 'NITA', 2: 'KIT'}, get_attribution_tag_evidence(
            frozen, '“Go,” Nita said. “No,” said Kit. “Yes.”', names))
        # One hard line break is only a space inside a paragraph, so a wrap
        # before the second tagged speaker, or inside its tag, still counts
        # both speakers and abstains here (#2391 r5). The first form keeps the
        # second speaker on the first speaker's line, so its third quotation
        # still sees two tagged speakers in one paragraph (#2391 r6).
        for wrapped, expected in (
                ('“Go,” Nita said.\n“No,” said Kit. “Yes.”',
                 {0: 'NITA', 2: 'KIT'}),
                # #2391 r7 BLOCKER 1: CRLF is one line break, so the backward
                # two-speaker gap is not cut at it; CRLF and lone-CR spellings
                # behave exactly like LF.
                ('“Go,” Nita said.\r\n“No,” said Kit. “Yes.”',
                 {0: 'NITA', 2: 'KIT'}),
                ('“Go,” Nita said.\r“No,” said Kit. “Yes.”',
                 {0: 'NITA', 2: 'KIT'}),
                # A wrap inside B's own tag keeps the continuation guard's
                # abstention; the line-local direct read simply loses B's direct
                # evidence, which is still safer than inheriting Nita.
                ('“Go,” Nita said. “No,” said\nKit. “Yes.”', {0: 'NITA'}),
                ('“Go,” Nita said. “No,” said Kit.\n“Yes.”',
                 {0: 'NITA', 2: 'KIT'}),
                # The following-tag read folds its line wrap: without the fold
                # the first speaker's tag is unreadable and the third quotation
                # inherits the second speaker (NIT 2, #2391 r6).
                ('“Go,” Nita\nsaid. “No,” said Kit. “Yes.”', {2: 'KIT'}),
                ('“Go,” said\nNita. “No,” said Kit. “Yes.”', {2: 'KIT'})):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(expected,
                                 get_attribution_tag_evidence(
                                     frozen, wrapped, names))
                self.assertNotIn(4, get_attribution_tag_evidence(
                    frozen, wrapped, names))
        # One speaker throughout still continues: the third quotation inherits
        # Nita from the second tag.
        same_speaker = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Yes.'},
        ]
        self.assertEqual({0: 'NITA', 2: 'NITA', 4: 'NITA'},
                         get_attribution_tag_evidence(
                             same_speaker,
                             '“Go,” Nita said. “No,” Nita said. “Yes.”', names))
        # The third quotation starts its own line, so it is a new turn and
        # inherits nothing (#2391 r6).
        self.assertEqual({0: 'NITA', 2: 'NITA'},
                         get_attribution_tag_evidence(
                             same_speaker,
                             '“Go,” Nita said.\n“No,” Nita said.\n“Yes.”',
                             names))

    def test_continuation_abstains_when_a_tagged_speaker_cannot_be_read(self):
        # #2391 r6 LOW 1: the direct read cannot resolve a verb-first dotted
        # title or a long subject, but such a tag still names a speaker. A
        # paragraph holding it and the nearer tag has two tagged speakers, so
        # the continuation abstains instead of inheriting the nearer one.
        names = {'NITA', 'KIT', 'MRS BENNET'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
            {'type': 'NARRATOR', 'text': 'said Kit.'},
            {'type': 'SPOKEN', 'text': 'Yes.'},
        ]
        for source in (
                '“Go,” said Mrs. Bennet. “No,” said Kit. “Yes.”',
                '“Go,” said the old man by the door. “No,” said Kit. “Yes.”'):
            with self.subTest(source=source):
                evidence = get_attribution_tag_evidence(
                    frozen, source, names)
                self.assertNotIn(4, evidence)
                accepted = validate_attribution(
                    frozen,
                    [{'n': 0, 'speaker': 'MRS BENNET'},
                     {'n': 1, 'speaker': 'NARRATOR'},
                     {'n': 2, 'speaker': 'KIT'},
                     {'n': 3, 'speaker': 'NARRATOR'},
                     {'n': 4, 'speaker': 'MRS BENNET'}],
                    source_text=source, known_names=names)
                self.assertTrue(accepted['passed'])
        # Control: the same paragraph with a readable first tag still abstains
        # through the two-speaker rule, not through this one.
        self.assertNotIn(4, get_attribution_tag_evidence(
            frozen, '“Go,” said Nita. “No,” said Kit. “Yes.”', names))

    def test_continuation_hard_wrap_matches_the_one_line_form(self):
        # Gutenberg prose is hard-wrapped. A single line break inside a
        # paragraph is only a space for every continuation guard (#2391 r5).
        names = {'NITA', 'KIT'}
        own_tag = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No,'},
        ]
        # A wrap inside B's own tag: the tag is still B's own tag, so B must
        # not inherit Nita (the reviewer's repro, straight and curly quotes).
        for one_line, one_line_expected, wrapped in (
                ('"Go," Nita said. "No," Kit said.',
                 {0: 'NITA', 2: 'KIT'},
                 '"Go," Nita said. "No," Kit\nsaid.'),
                ('“Go,” Nita said. “No,” Kit said.',
                 {0: 'NITA', 2: 'KIT'},
                 '“Go,” Nita said. “No,” Kit\nsaid.'),
                ('“Go,” Nita said. “No,” the doctor said.',
                 {0: 'NITA'},
                 '“Go,” Nita said. “No,” the doctor\nsaid.')):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(one_line_expected,
                                 get_attribution_tag_evidence(
                                     own_tag, one_line, names))
                # The line-local direct read loses B's own evidence when its
                # tag is wrapped; the continuation guard must still abstain,
                # never inherit Nita.
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    own_tag, wrapped, names))
        # Wrapping at every space is the strongest hard-wrap form. A wrap that
        # does not put the continuation's opening quote at the start of a line
        # is only a space, so the decision is identical to the one-line form
        # (#2391 r6). The opening quotes are kept on the preceding line for this
        # comparison.
        cont = [{'type': 'SPOKEN', 'text': 'No.'}]
        for source in (
                '“Go,” Nita said. “No.”',
                '“Go?” Nita said. “No.”',
                '“Go,” Nita asked. “No.”',
                '“Go,” Nita said. “No.” She shook her head.',
                '“Go,” Nita said. “No.” Then she left.',
                '“Go,” Nita said. “No.” Kit crossed his arms.',
                '“Go,” Nita said. “No.” Then Kit laughed.',
                '“Go,” Nita said. “No.” Quietly, Kit laughed.',
                '“Go,” Nita said. “No.” Then again Kit laughed.',
                '“Go,” Nita said. “No.” And then Kit laughed.',
                '“Go,” Nita said. “No,” Kit began.',
                '“Go,” Nita said. “No,” Mrs. Bennet said.',
                '“Go,” Nita said. “No,” the stranger in the cloak said.',
                '“Go,” Nita said. “No,” said the man in the cloak.'):
            with self.subTest(source=source):
                wrapped = source.replace(" ", "\n")
                for opener in ('“', '"'):
                    wrapped = wrapped.replace("\n" + opener, " " + opener)
                expected = get_attribution_tag_evidence(cont, wrapped, names)
                self.assertEqual(
                    get_attribution_tag_evidence(cont, source, names),
                    expected)
                # #2391 r7 BLOCKER 1: CRLF is ONE line break, so both the CRLF
                # and the lone-CR spelling of this wrap behave exactly like LF.
                for break_form in ('\r\n', '\r'):
                    with self.subTest(source=source, break_form=break_form):
                        self.assertEqual(
                            expected,
                            get_attribution_tag_evidence(
                                cont, wrapped.replace("\n", break_form), names))
        # A quotation that starts its own line is a new turn even without a
        # blank line, so the continuation inherits nothing (#2391 r6).
        started_own_line = '“Go,” Nita said. “No.”'.replace(" ", "\n")
        self.assertEqual(
            get_attribution_tag_evidence(cont, started_own_line, names), {})
        # A blank line still ends the paragraph: no inheritance across it. A
        # CRLF blank line is two CRLF breaks, not three (#2391 r7).
        self.assertEqual({}, get_attribution_tag_evidence(
            cont, '“Go,” Nita said.\n\n“No.”', names))
        self.assertEqual({}, get_attribution_tag_evidence(
            cont, '“Go,” Nita said.\r\n\r\n“No.”', names))
        # #2391 r7 BLOCKER 1: the reviewer's three CRLF repros. A bare CRLF is
        # one hard wrap, not a paragraph break, so each source is one paragraph
        # and the evidence is byte-identical to main (main has no continuation
        # path): the tag names the same speaker and the correct answer is not
        # flagged. The wrap sits inside B's own tag, inside a long subject, and
        # before an action beat.
        for source, no_text in (
                ('“Go,” Nita said. “No,” Kit\r\nsaid.', 'No,'),
                ('“Go,” Nita said. “No.” Kit\r\ncrossed his arms.', 'No.'),
                ('“Go,” Nita said. “No,” the stranger\r\nin the cloak said.',
                 'No,')):
            entries = [{'type': 'SPOKEN', 'text': 'Go,'},
                       {'type': 'NARRATOR', 'text': 'Nita said.'},
                       {'type': 'SPOKEN', 'text': no_text}]
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'},
                                 get_attribution_tag_evidence(
                                     entries, source,
                                     names | {'THE STRANGER'}))
        # The following-tag read folds its wrap too (NIT 2, #2391 r6): a wrap
        # inside the earlier bare tag still counts Nita as the paragraph's one
        # tagged speaker, so the continuation keeps her instead of abstaining.
        for break_form in ('\n', '\r\n', '\r'):
            with self.subTest(break_form=break_form):
                self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
                    cont, f'“Go,” Nita{break_form}said. “No.”', names))
        # The same shared line-break reading drives the following-quote guard:
        # a tag followed by another quotation on the next line takes no
        # evidence, and a lone CR is a line break exactly like LF and CRLF
        # (#2391 r7 NIT 2).
        for break_form in ('\n', '\r\n', '\r'):
            with self.subTest(break_form=break_form):
                self.assertEqual({}, get_attribution_tag_evidence(
                    [{'type': 'SPOKEN', 'text': 'No.'}],
                    f'“No.” Nita said,{break_form}“Yes.”', names))

    def test_continuation_abstains_before_a_quotation_that_starts_its_line(self):
        # #2391 r6 BLOCKER 1: the user's light novels put one paragraph per line
        # (the format repair_source_encoding.py is calibrated on). A quotation
        # that starts its own line is a new turn, so the tag before the line
        # break must not reach it. A line break with tag text after it is still
        # only a space inside the paragraph.
        names = {'NITA', 'KIT'}
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No.'},
        ]
        # The gate's canonical alternating reply: the correct KIT answer is
        # accepted exactly as on main (main has no continuation evidence, so it
        # abstains too).
        accepted = validate_attribution(
            frozen,
            [{'n': 0, 'speaker': 'NITA'}, {'n': 1, 'speaker': 'NARRATOR'},
             {'n': 2, 'speaker': 'KIT'}],
            source_text='“Go,” Nita said.\n“No.”', known_names=names)
        self.assertTrue(accepted['passed'])
        cont = [{'type': 'SPOKEN', 'text': 'No.'}]
        # Every new-turn form in its LF spelling; the CRLF and lone-CR
        # spellings must behave exactly the same (#2391 r7 BLOCKER 1).
        for start_line in (
                '“You should go home,” Nita said.\n“No.”',
                '“Go,” Nita said.\n“No.” She sighed.',
                '“Go,” Nita said.\n“No.”\n“Why not?”',
                # One paragraph per line, the #628 chunk shape.
                '“Go,” Nita said.\n“No.”\n“Wait.”\n“Never.”',
                # Trailing blanks before the quote.
                '“Go,” Nita said.\n \t“No.”'):
            for break_form in ('\n', '\r\n', '\r'):
                source = start_line.replace('\n', break_form)
                with self.subTest(source=source):
                    self.assertEqual({}, get_attribution_tag_evidence(
                        cont, source, names))
        # Control: a wrap inside the bare tag does not start a quotation, so it
        # still folds to a space and the continuation inherits Nita.
        self.assertEqual({0: 'NITA'},
                         get_attribution_tag_evidence(
                             cont, '“Go,” Nita\nsaid. “No.”', names))
        self.assertEqual({0: 'NITA'},
                         get_attribution_tag_evidence(
                             cont, '“Go,” Nita said. “No.”', names))
        # A lone CR and a CRLF are line breaks too, so a tag wrapped on either
        # is still B's own tag and B abstains (#2391 r6 NIT 1, r7 BLOCKER 1)
        # instead of inheriting Nita.
        for break_form in ('\r', '\r\n'):
            with self.subTest(break_form=break_form):
                self.assertEqual({},
                                 get_attribution_tag_evidence(
                                     [{'type': 'SPOKEN', 'text': 'No,'}],
                                     '“Go,” Nita said. “No,” Kit'
                                     f'{break_form}said.', names))

    def test_continuation_abstains_after_a_question(self):
        names = {'NITA', 'KIT'}
        cont = [{'type': 'SPOKEN', 'text': 'No.'}]
        question_frozen = [
            {'type': 'SPOKEN', 'text': 'Go?'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No.'},
        ]
        # A bare reply after a question is very often the other speaker, so a
        # question quotation abstains even when its own tag is not a question
        # verb (the body asks about the question mark, not only the verb).
        for source in (
                '“Go?” Nita said. “No.”',
                '“Go?!” Nita said. “No.”',
                # Tag verb is a question verb while the quotation is not a
                # question: abstain too.
                '“Go,” Nita asked. “No.”',
                '“Go,” Nita inquired. “No.”',
                '“Go,” Nita enquired. “No.”',
                '“Go,” Nita demanded. “No.”'):
            with self.subTest(source=source):
                self.assertEqual({}, get_attribution_tag_evidence(
                    cont, source, names))
        # Control: a non-question continuation still inherits the tag speaker.
        self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
            cont, '“Go,” Nita said. “No.”', names))
        self.assertEqual({0: 'NITA'}, get_attribution_tag_evidence(
            question_frozen, '“Go?” Nita said. “No.”', names))

    def test_dash_attached_tag_is_own_tag_but_never_direct_evidence(self):
        names = {'NITA', 'KIT'}
        # #2388 deliberately leaves the em-dash gap in the direct tag rule; an
        # entry with only the dash-attached tag therefore has no evidence.
        self.assertEqual({}, get_attribution_tag_evidence(
            [{'type': 'SPOKEN', 'text': 'No'}],
            '“Go,” Nita said. “No”—Kit said.', names))
        # In the continuation path the same tag is still B's own tag, so "No"
        # does not inherit Nita (and still gets no direct evidence of its own).
        frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No'},
        ]
        for source in (
                '“Go,” Nita said. “No”—Kit said.',
                '“Go,” Nita said. “No” — Kit said.',
                '“Go,” Nita said. “No”-Kit said.'):
            with self.subTest(source=source):
                self.assertEqual({0: 'NITA'},
                                 get_attribution_tag_evidence(frozen, source, names))
        # Positive half: an untagged continuation in the same shape inherits
        # its tag speaker. origin/main has no continuation path, so the test as
        # a whole fails before the fix, not only kills a mutant.
        positive_frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'No.'},
        ]
        self.assertEqual({0: 'NITA', 2: 'NITA'}, get_attribution_tag_evidence(
            positive_frozen, '“Go,” Nita said. “No.”', names))

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

        # A quotation that starts its own line is a new turn, so it inherits
        # nothing even without a blank line (#2391 r6). A blank line still ends
        # the paragraph as before.
        self.assertEqual({}, get_attribution_tag_evidence(
            frozen_cont, '“Go,” Nita said.\n“Now.”', names))
        self.assertEqual({}, get_attribution_tag_evidence(
            frozen_cont, '“Go,” Nita said.\n\n“Now.”', names))

        # Continuation with its own tag keeps its own (or abstains if its own tag is uncertain).
        own_tag_frozen = [
            {'type': 'SPOKEN', 'text': 'Go,'},
            {'type': 'NARRATOR', 'text': 'Nita said.'},
            {'type': 'SPOKEN', 'text': 'Now,'},
        ]
        self.assertEqual({0: 'NITA', 2: 'KIT'}, get_attribution_tag_evidence(
            own_tag_frozen, '“Go,” Nita said. “Now,” Kit replied.', names))
        # #2388 resolves an adverb-modified tag, so it is direct evidence too.
        self.assertEqual({0: 'NITA', 2: 'KIT'}, get_attribution_tag_evidence(
            own_tag_frozen, '“Go,” Nita said. “Now,” Kit replied quietly.', names))
        for own_uncertain in (
            '“Go,” Nita said. “Now,” Mara said.',
            '“Go,” Nita said. “Now,” he said.',
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
            '“Let us go,” Anna quietly said quietly. Ben waited.',
            '“Let us go,” Anna said quietly softly. Ben waited.',
            '“Let us go,” Anna quietly told Ben quietly. Ben waited.',
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

    def test_object_pronoun_list_is_complete(self):
        # _TAG_OBJECT's pronoun branch is behaviourally redundant because
        # _TAG_NAME also matches every pronoun, so pin the list itself: a
        # shrink to him|her must fail (#2388 gate 2 nit).
        import pass_quality as pq
        expected = {'him', 'her', 'them', 'me', 'us', 'you',
                    'himself', 'herself'}
        self.assertLessEqual(expected, pq._TAG_OBJECT_PRONOUNS)
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        for pronoun in sorted(expected):
            with self.subTest(pronoun=pronoun):
                self.assertEqual({0: 'ANNA'}, get_attribution_tag_evidence(
                    entry, f'“Let us go,” Anna said to {pronoun}.',
                    {'ANNA', 'BEN'}))

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

    def test_next_quote_guard_pins_ambiguous_split_quotes_and_keeps_clear_ones(self):
        # Subject-first tag after a finished sentence introducing the NEXT quote abstains.
        # The ?/!/… cases are genuinely ambiguous: the tag can introduce the next
        # quotation or continue this one. The guard abstains instead of guessing,
        # and these subtests pin that ambiguity rather than discriminate it.
        for text, source in (
            ('Will you walk back?', '“Will you walk back?” Zella said, “I have the key.”'),
            ('Will you walk back?', '"Will you walk back?" Zella said, "I have the key."'),
            ('Do you hear that?', '“Do you hear that?” Anna said, “It came from the hall.”'),
            ('Mind the step.', '“Mind the step.” Zella said: “The lamp is lit.”'),
            ('Look out!', '“Look out!” Zella whispered, “The floor is wet.”'),
            ('Keep the gate shut.', '«Keep the gate shut.» Zella said, «The wind is rising.»'),
            ('Keep the gate shut.', '„Keep the gate shut.“ Zella said, „The wind is rising.“'),
            # Gutenberg texts are hard-wrapped, and `‘` opens the next
            # quotation in them without being a PAIRED convention (#2388 gate 2).
            ('Will you walk back?', '“Will you walk back?” Zella said,\n“I have the key.”'),
            # One blank line between the comma/colon and the next quotation is
            # still the same guard shape (#2388 gate 3).
            ('Will you walk back?', '“Will you walk back?” Zella said,\n\n“I have the key.”'),
            ('Mind the step.', '“Mind the step.” Zella said:\n\n“The lamp is lit.”'),
            ('Will you walk back?', '“Will you walk back?” Zella said, ‘I have the key.’'),
        ):
            with self.subTest(source=source):
                self.assertEqual({}, get_attribution_tag_evidence(
                    [{'type': 'SPOKEN', 'text': text}], source, {'ZELLA', 'BRAM'}))
        # `‘tis` is read as a `‘` opener, so the guard abstains and drops the
        # evidence; this errs safe and is documented in Limits (#2388 gate 5).
        self.assertEqual({}, get_attribution_tag_evidence(
            [{'type': 'SPOKEN', 'text': 'Are you in town?'}],
            '“Are you in town?” Anna said, ‘tis late.', {'ANNA', 'BEN'}))
        # A period-ending tag is the disclosed unguarded shape: a blank line
        # after it changes nothing (#2388 gate 3).
        self.assertEqual({0: 'ZELLA'}, get_attribution_tag_evidence(
            [{'type': 'SPOKEN', 'text': 'Will you walk back?'}],
            '“Will you walk back?” Zella said.\n\n“I have the key.”',
            {'ZELLA', 'BRAM'}))
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
        # 3. Deliberate duplicate-label join: the tag label SHERLOCK HOLMES is
        # established, but it also names the same character as the established
        # MR. SHERLOCK HOLMES. Unestablished titled/partial tags join by
        # surname as well; that is the trade-off disclosed in Limits.
        for speaker, tag, roster in (
            ('MARY VALE', 'said Miss Vale.', {'MARY VALE', 'HENRY VALE', 'BEN'}),
            ('JOHN MIDDLETON', 'cried Sir John.', {'JOHN MIDDLETON', 'BEN'}),
            ('MR. SHERLOCK HOLMES', 'said Sherlock Holmes.', {'MR. SHERLOCK HOLMES', 'SHERLOCK HOLMES', 'BEN'}),
            # The join also covers a parent/child pair and a title whose gender
            # conflicts with the predicted first name; both are disclosed in
            # Limits, and pinned here so a change to the join fails a test.
            ('TOM VALE', 'said Colonel Vale.', {'TOM VALE', 'BEN'}),
            ('LUCY VALE', 'said Mr Vale.', {'LUCY VALE', 'BEN'}),
        ):
            source = f'“Let us go,” {tag} Ben watched.'
            with self.subTest(speaker=speaker, tag=tag):
                report = validate_attribution(
                    [{'type': 'SPOKEN', 'text': 'Let us go,'}], [{'n': 0, 'speaker': speaker}],
                    source_text=source, known_names=roster)
                self.assertTrue(report['passed'], report)
        for speaker in ('LUCY VALE', 'ANNE VALE'):
            with self.subTest(tradeoff_speaker=speaker):
                self.assertTrue(validate_attribution(
                    [{'type': 'SPOKEN', 'text': 'Let us go,'}],
                    [{'n': 0, 'speaker': speaker}],
                    source_text='“Let us go,” said Miss Vale. Ben waited.',
                    known_names={'LUCY VALE', 'ANNE VALE', 'BEN'})['passed'])
        # 4. Real contradictions must STILL fail: distinct established roster entries,
        # conflicting titles, and different first names sharing a surname. When the
        # tag label itself is established, naming one sibling still contradicts the
        # other even though both share the surname.
        for speaker, tag, roster in (
            ('MARY VALE', 'continued Mrs Vale.', {'MARY VALE', 'MRS VALE', 'BEN'}),
            ('LUCY VALE', 'said Miss Vale.', {'LUCY VALE', 'ANNE VALE', 'MISS VALE', 'BEN'}),
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

    def test_married_woman_alias_does_not_claim_the_husbands_name(self):
        # "Mrs. John Vale" aliases to the wife, Fanny. The title-stripped
        # "John Vale" must not also become an identity key for her husband,
        # or the gate abstains exactly when the source names John.
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        roster = {'JOHN VALE', 'FANNY VALE', 'BEN'}
        aliases = {'MRS. JOHN VALE': 'FANNY VALE'}
        husband_source = '“Let us go,” said John Vale. Ben waited.'
        self.assertEqual({0: 'JOHN VALE'}, get_attribution_tag_evidence(
            entry, husband_source, roster, aliases))
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'BEN'}], source_text=husband_source,
            known_names=roster, speaker_aliases=aliases)
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
        self.assertEqual('JOHN VALE', report['findings'][0]['expected'])
        # The wife's own tag still resolves through the same alias.
        wife_source = '“Let us go,” said Mrs John Vale. Ben waited.'
        self.assertEqual({0: 'FANNY VALE'}, get_attribution_tag_evidence(
            entry, wife_source, roster, aliases))
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'FANNY VALE'}], source_text=wife_source,
            known_names=roster, speaker_aliases=aliases)['passed'])

    def test_married_woman_title_prevents_same_words_label_join(self):
        # Both labels are established and share the same personal words, but a
        # married-woman title makes one of them the wife: neither can be
        # predicted from the other's tag (#2388 gate 2). The duplicate-label
        # join for a non-married title (MR. SHERLOCK HOLMES) stays.
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        for roster, wife in (
            ({'JOHN VALE', 'MRS JOHN VALE', 'BEN'}, 'MRS JOHN VALE'),
            ({'JOHN VALE', 'MRS. JOHN VALE', 'BEN'}, 'MRS. JOHN VALE'),
        ):
            source = '“Let us go,” said John Vale. Ben waited.'
            with self.subTest(roster=roster, wife=wife):
                report = validate_attribution(
                    entry, [{'n': 0, 'speaker': wife}], source_text=source,
                    known_names=roster)
                self.assertFalse(report['passed'], report)
                self.assertEqual('speaker_contradicts_source_tag',
                                 report['findings'][0]['code'])
        # The reverse direction: the husband is not the wife named by the tag.
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'JOHN VALE'}],
            source_text='“Let us go,” said Mrs John Vale. Ben waited.',
            known_names={'JOHN VALE', 'MRS JOHN VALE', 'BEN'})
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag',
                         report['findings'][0]['code'])

    def test_same_words_distinct_labels_need_data_and_are_disclosed_joins(self):
        # Disclosed in Limits, no code change: these pairs have the same
        # personal words but may be different characters. Telling them apart
        # needs gender or relationship data the check does not have, so the
        # same-words join keeps them compatible. Each pair is pinned here in
        # both directions so a change to the rule fails a test (#2388 gate 2).
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        for roster, first_label, second_label, first_tag, second_tag in (
            ({'LADY BERTRAM', 'BERTRAM', 'BEN'},
             'LADY BERTRAM', 'BERTRAM', 'said Bertram.', 'said Lady Bertram.'),
            ({'DR JOHN VALE', 'JOHN VALE', 'BEN'},
             'DR JOHN VALE', 'JOHN VALE', 'said John Vale.', 'said Dr John Vale.'),
            ({'LADY MARY VALE', 'MARY VALE', 'BEN'},
             'LADY MARY VALE', 'MARY VALE', 'said Mary Vale.', 'said Lady Mary Vale.'),
            # LADY JOHN VALE here is the established spelling, not an alias.
            ({'LADY JOHN VALE', 'JOHN VALE', 'BEN'},
             'LADY JOHN VALE', 'JOHN VALE', 'said John Vale.', 'said Lady John Vale.'),
        ):
            for speaker, tag in ((first_label, first_tag), (second_label, second_tag)):
                source = f'“Let us go,” {tag} Ben waited.'
                with self.subTest(roster=roster, speaker=speaker, tag=tag):
                    self.assertTrue(validate_attribution(
                        entry, [{'n': 0, 'speaker': speaker}],
                        source_text=source, known_names=roster)['passed'])

    def test_married_woman_alias_stays_the_wife_without_the_husband_on_the_roster(self):
        # Decision 2's own rule (_MARRIED_WOMAN_TITLES) with no collision to
        # hide it: the husband is not on the roster, so only excluding the
        # title-stripped form keeps "said John Vale." from resolving to the
        # wife. Emptying _MARRIED_WOMAN_TITLES makes this case pass (#2388 gate 2).
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        roster = {'FANNY VALE', 'BEN'}
        aliases = {'MRS. JOHN VALE': 'FANNY VALE'}
        source = '“Let us go,” said John Vale. Ben waited.'
        self.assertEqual({0: 'JOHN VALE'}, get_attribution_tag_evidence(
            entry, source, roster, aliases))
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'FANNY VALE'}], source_text=source,
            known_names=roster, speaker_aliases=aliases)
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag',
                         report['findings'][0]['code'])
        self.assertEqual('JOHN VALE', report['findings'][0]['expected'])

    def test_lady_husband_alias_targets_the_wife_without_the_husband_on_the_roster(self):
        # Disclosed in Limits, no code change: "Lady" also precedes a woman's
        # own first name ("Lady Mary Vale"), so it is not a married-woman title.
        # With the husband absent from the roster, her alias still owns
        # "John Vale": the tag points at the wife and a wrong husband
        # prediction cannot be contradicted (#2388 gate 2).
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        roster = {'FANNY VALE', 'BEN'}
        aliases = {'LADY JOHN VALE': 'FANNY VALE'}
        source = '“Let us go,” said John Vale. Ben waited.'
        self.assertEqual({0: 'FANNY VALE'}, get_attribution_tag_evidence(
            entry, source, roster, aliases))
        self.assertFalse(validate_attribution(
            entry, [{'n': 0, 'speaker': 'BEN'}], source_text=source,
            known_names=roster, speaker_aliases=aliases)['passed'])
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'FANNY VALE'}], source_text=source,
            known_names=roster, speaker_aliases=aliases)['passed'])

    def test_tag_with_more_name_words_than_the_prediction_is_not_joined(self):
        # The prefix/suffix join is one-directional: an unestablished SHORTER
        # tag may join a prediction ("Miss Vale" -> LUCY VALE), but a tag that
        # carries more name words than the prediction is a different or more
        # specific label. Main contradicts every shape below; #2388 gate 3
        # found the head had started accepting them.
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        roster = {'JOHN VALE', 'BEN'}
        for tag in (
            'said Frau John Vale.',
            'said Madame John Vale.',
            'said Mme John Vale.',
            'said Señora John Vale.',
            'said John Vale Junior.',
            'said Young John Vale.',
        ):
            source = f'“Let us go,” {tag} Ben waited.'
            with self.subTest(tag=tag):
                report = validate_attribution(
                    entry, [{'n': 0, 'speaker': 'JOHN VALE'}],
                    source_text=source, known_names=roster)
                self.assertFalse(report['passed'], report)
                self.assertEqual('speaker_contradicts_source_tag',
                                 report['findings'][0]['code'])
        # A first name plus surname against a bare predicted first name.
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'JOHN'}],
            source_text='“Let us go,” said John Vale. Ben waited.',
            known_names={'JOHN', 'JOHN SMITH', 'BEN'})
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag',
                         report['findings'][0]['code'])
        # "Lady" is a title, so this unrostered tag joins the same personal
        # words. That is the disclosed Lady join (Limits), pinned here.
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'JOHN VALE'}],
            source_text='“Let us go,” said Lady John Vale. Ben waited.',
            known_names={'JOHN VALE', 'BEN'})['passed'])
        # The shorter-tag join is unchanged.
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'LUCY VALE'}],
            source_text='“Let us go,” said Miss Vale. Ben waited.',
            known_names={'LUCY VALE', 'BEN'})['passed'])

    def test_longer_prediction_with_a_different_person_word_is_not_joined(self):
        # FINDING 1 (#2388 gate 5). Mirror of the longer-tag guard: an
        # unestablished "said John Vale." tag used to join a LONGER established
        # prediction carrying a generational or foreign married-woman word.
        # Main contradicts every row below, and variant V6 keeps that verdict
        # by refusing the join when the prediction's extra words are those
        # markers, at no measured PDNC cost.
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        source = '“Let us go,” said John Vale. Ben waited.'
        for label in (
            'FRAU JOHN VALE',
            'MADAME JOHN VALE',
            'MME JOHN VALE',
            'SEÑORA JOHN VALE',
            'YOUNG JOHN VALE',
            'JOHN VALE JUNIOR',
            'JOHN VALE SENIOR',
            'OLD JOHN VALE',
            'LITTLE JOHN VALE',
        ):
            roster = {label, 'BEN'}
            with self.subTest(label=label):
                self.assertEqual({0: 'JOHN VALE'}, get_attribution_tag_evidence(
                    entry, source, roster))
                report = validate_attribution(
                    entry, [{'n': 0, 'speaker': label}],
                    source_text=source, known_names=roster)
                self.assertFalse(report['passed'], report)
                self.assertEqual('speaker_contradicts_source_tag',
                                 report['findings'][0]['code'])
        # Variant M (the English married-woman title) is unchanged by V6.
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'MRS JOHN VALE'}],
            source_text=source, known_names={'MRS JOHN VALE', 'BEN'})
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag',
                         report['findings'][0]['code'])
        # A qualifying title that names the SAME person still joins: the
        # measured "MARQIS DE GRIERS" / "De Griers" shape keeps its evidence.
        self.assertEqual({0: 'DE GRIERS'}, get_attribution_tag_evidence(
            entry, '“Let us go,” said De Griers. Ben waited.',
            {'MARQIS DE GRIERS', 'BEN'}))
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'MARQIS DE GRIERS'}],
            source_text='“Let us go,” said De Griers. Ben waited.',
            known_names={'MARQIS DE GRIERS', 'BEN'})['passed'])
        # The disclosed LADY row is unchanged: Lady is a title, so the
        # same-words join still applies (Limits). Explicit evidence pins the
        # comparison even though this head's title-stripped indexing already
        # resolves the tag to the full label.
        for evidence in ('JOHN VALE', 'LADY JOHN VALE'):
            with self.subTest(lady_evidence=evidence):
                self.assertTrue(validate_attribution(
                    entry, [{'n': 0, 'speaker': 'LADY JOHN VALE'}],
                    source_text=source, known_names={'LADY JOHN VALE', 'BEN'},
                    tag_evidence={0: evidence})['passed'])

    def test_non_manner_ly_word_fills_the_adverb_slot(self):
        # _TAG_ADVERB accepts any lowercase -ly word ("Anna family said.").
        # Kept as a disclosed limit: on PDNC it produced no false flag, and a
        # noun list would be a second guess (#2388 gate 3 nit). Pinned so a
        # narrower rule is a deliberate change.
        self.assertEqual({0: 'ANNA'}, get_attribution_tag_evidence(
            [{'type': 'SPOKEN', 'text': 'Let us go,'}],
            '“Let us go,” Anna family said. Ben waited.',
            {'ANNA', 'BEN'}))

    def test_lady_husband_name_alias_does_not_claim_the_husbands_name(self):
        # "Lady John Vale" is John's wife, the same convention decision 2 fixed
        # for "Mrs. John Vale". Her title-stripped JOHN VALE must not become an
        # identity key for her husband.
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        roster = {'JOHN VALE', 'FANNY VALE', 'BEN'}
        aliases = {'LADY JOHN VALE': 'FANNY VALE'}
        husband_source = '“Let us go,” said John Vale. Ben waited.'
        self.assertEqual({0: 'JOHN VALE'}, get_attribution_tag_evidence(
            entry, husband_source, roster, aliases))
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'BEN'}], source_text=husband_source,
            known_names=roster, speaker_aliases=aliases)
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
        self.assertEqual('JOHN VALE', report['findings'][0]['expected'])
        # A Lady bearing her own first name keeps the stripped form when no
        # other established character owns "Mary Vale".
        self.assertEqual({0: 'LADY MARY VALE'}, get_attribution_tag_evidence(
            entry, '“Let us go,” said Mary Vale. Ben waited.',
            {'LADY MARY VALE', 'BEN'}))
        # The wife's own tag still resolves through the alias.
        self.assertEqual({0: 'FANNY VALE'}, get_attribution_tag_evidence(
            entry, '“Let us go,” said Lady John Vale. Ben waited.',
            roster, aliases))

    def test_alias_chains_follow_to_the_final_label_and_cycles_terminate(self):
        import pass_quality as pq
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        source = '“Let us go,” said Miss Vale. Ben waited.'
        # A -> B -> C: the tag resolves to the final label, not the middle hop.
        self.assertEqual({0: 'MARY VALE'}, get_attribution_tag_evidence(
            entry, source, {'MARY VALE', 'BEN'},
            {'Miss Vale': 'Mistress Vale', 'Mistress Vale': 'MARY VALE'}))
        # A cycle stops instead of recursing forever.
        cycle = {'Miss Vale': 'Mary Vale', 'Mary Vale': 'Miss Vale'}
        self.assertIn(pq._resolve_alias_canonical('Miss Vale', cycle),
                      {'Miss Vale', 'Mary Vale'})

    def test_caller_supplied_tag_evidence_is_canonicalised(self):
        # A caller may supply the raw tag label instead of the proof's
        # canonical label; the alias must be resolved before comparing.
        aliases = {'AUNT VALE': 'MARY VALE'}
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'MARY VALE'}],
            source_text='“Let us go,” said Mary Vale. Ben waited.',
            known_names={'MARY VALE', 'BEN'},
            tag_evidence={0: 'AUNT VALE'},
            speaker_aliases=aliases)
        self.assertTrue(report['passed'], report)

    def test_unestablished_title_surname_abstains_against_a_first_name(self):
        # A predicted bare first name cannot be compared with a title+surname
        # that is not on the roster: they may or may not be the same person, so
        # no contradiction is emitted (the #2388 new-false-flag fix).
        source = '“It has been a very agreeable day,” said Miss Bennet to Elizabeth.'
        entry = [{'type': 'SPOKEN', 'text': 'It has been a very agreeable day,'}]
        self.assertEqual({0: 'MISS BENNET'}, get_attribution_tag_evidence(
            entry, source, {'JANE', 'ELIZABETH'}))
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'JANE'}], source_text=source,
            known_names={'JANE', 'ELIZABETH'})
        self.assertTrue(report['passed'], report)

    def test_unnamed_sibling_join_discloses_silenced_wrong_sibling(self):
        # FINDING 1 (#2388 rev1 blocker). The unnamed-sibling join is KEPT (it
        # removes 630 false flags on the measured recording) at the disclosed
        # cost of silencing real contradictions it cannot see. An UNESTABLISHED
        # title+surname tag ("said Miss Steele") cannot name which sibling
        # speaks, so a wrong-sibling prediction is NOT contradicted. On the
        # measured recording this silences 2 real catches (gold ANNE STEELE,
        # model named LUCY STEELE, tag "said Miss Steele"). The trade-off is
        # disclosed in the PR body and pinned here.
        source = '“Let us go,” said Miss Steele. Ben waited.'
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        siblings = {'ANNE STEELE', 'LUCY STEELE', 'BEN'}
        # The tag evidence is the unnamed sibling label itself...
        self.assertEqual({0: 'MISS STEELE'},
                         get_attribution_tag_evidence(entry, source, siblings))
        # ...and a model that named the wrong sibling is not contradicted.
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'LUCY STEELE'}], source_text=source,
            known_names=siblings)['passed'])
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'ANNE STEELE'}], source_text=source,
            known_names=siblings)['passed'])
        # But the trade-off is bounded: an ESTABLISHED sibling tag label still
        # contradicts the other sibling, and a spelled-out first name + surname
        # that differs is not silenced (see test_unnamed_sibling_negative...).
        established = {'ANNE STEELE', 'LUCY STEELE', 'MISS STEELE', 'BEN'}
        self.assertFalse(validate_attribution(
            entry, [{'n': 0, 'speaker': 'LUCY STEELE'}], source_text=source,
            known_names=established)['passed'])

    def test_unnamed_sibling_negative_case_contradicts(self):
        # FINDING 5 (#2388 rev1). Pin the sibling NEGATIVE case: an
        # implementation that joins EVERY unestablished surname match would
        # silence this. An unestablished tag that spells a different first name
        # + surname names a different sibling and still contradicts; only a
        # shorter title+surname / bare-surname tag joins (finding 1).
        entry = [{'type': 'SPOKEN', 'text': 'Let us go,'}]
        source = '“Let us go,” said Anne Vale. Ben waited.'
        # Sibling spelled out, NOT on the roster, model names the other sibling.
        roster = {'LUCY VALE', 'BEN'}
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'LUCY VALE'}], source_text=source,
            known_names=roster)
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
        # And with the sibling also established.
        report = validate_attribution(
            entry, [{'n': 0, 'speaker': 'LUCY VALE'}], source_text=source,
            known_names={'LUCY VALE', 'ANNE VALE', 'BEN'})
        self.assertFalse(report['passed'], report)
        self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])
        # Same result for a bare-surname-only comparison is NOT this case: an
        # unnamed "said Miss Vale" sibling tag is the finding-1 trade-off, not
        # the sibling negative; that is pinned separately.
        self.assertTrue(validate_attribution(
            entry, [{'n': 0, 'speaker': 'LUCY VALE'}],
            source_text='“Let us go,” said Miss Vale. Ben waited.',
            known_names={'LUCY VALE', 'BEN'})['passed'])
