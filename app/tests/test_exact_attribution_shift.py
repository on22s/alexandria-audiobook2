"""Synthetic exact source shifts through actual request, provider and salvage gates."""
import contextlib
import copy
import datetime
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
import attribution_prompt_variants as apv
import generate_script as gs
import pass_quality as pq
import three_pass_generate as tp
from response_codecs import get_codec

SOURCE = '“Yes,” said Alice.'
FROZEN = [{'type': 'SPOKEN', 'text': 'Yes,', 'uid': 'quote'},
          {'type': 'NARRATOR', 'text': 'said Alice.', 'uid': 'tag'}]
SHIFT = [{'n': 0, 'speaker': 'NARRATOR'}, {'n': 1, 'speaker': 'ALICE'}]
GOOD = [{'n': 0, 'speaker': 'ALICE'}, {'n': 1, 'speaker': 'NARRATOR'}]

class Client:
    def __init__(self, replies, finish='stop'):
        self.replies, self.finish, self.calls = iter(replies), finish, []
        self.chat = NS(completions=NS(create=self.create))
    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return NS(choices=[NS(message=NS(content=json.dumps(reply)), finish_reason=self.finish)], usage=None)

class ExactShiftTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(patch.object(gs, 'get_response_log_path', side_effect=lambda n: str(self.path/n)))
        stack.enter_context(patch.object(gs.time, 'sleep'))
        stack.enter_context(patch.dict(os.environ, {'JUDGE_WHY_PATH': str(self.path/'why.jsonl')}))
    def batch(self, replies=None, **kw):
        client = kw.pop('client', None) or Client(replies or [SHIFT])
        attempts = kw.pop('attempts', [])
        result = tp.attribute_batch(client, 'synthetic', kw.pop('frozen', copy.deepcopy(FROZEN)),
            gs.LLMGenParams(structured_output='off', temperature=0.1), ['ALICE'],
            source_text=kw.pop('source', SOURCE), max_retries=kw.pop('max_retries', 0),
            repair_exact_shift=kw.pop('repair_exact_shift', True), attempt_observer=attempts.append, **kw)
        return result, attempts, client
    def repair(self, frozen=None, answer=None, source=SOURCE, names=('ALICE',), aliases=None):
        frozen = copy.deepcopy(FROZEN) if frozen is None else frozen
        answer = copy.deepcopy(SHIFT) if answer is None else answer
        aliases = aliases or {}
        proofs = pq.get_attribution_tag_proofs(frozen, source, names, aliases)
        evidence = {i: p['speaker'] for i,p in proofs.items()}
        snapshot = pq.get_attribution_snapshot_digest([frozen, sorted(names), [], aliases])
        return pq.get_exact_attribution_shift_repair(frozen, answer, source, proofs, evidence,
            snapshot, established_names=names, speaker_aliases=aliases)
    def test_raw_log_is_distinct_from_returned_accepted_copies(self):
        original = copy.deepcopy(SHIFT)
        result, attempts, _ = self.batch([original])
        self.assertEqual(['ALICE','NARRATOR'], [r['speaker'] for r in result])
        self.assertEqual(SHIFT, original)
        self.assertEqual(['quote','tag'], [r['uid'] for r in result])
        self.assertIn(json.dumps(SHIFT), (self.path/'llm_responses.log').read_text())
        self.assertEqual('accepted', attempts[-1]['outcome'])
        proof=attempts[-1]['accepted_entry_transform']['repairs'][0]
        self.assertEqual([0,1],proof['indexes'])
        self.assertEqual(' said Alice',SOURCE[slice(*proof['reporting_tag_bounds'])])
        self.assertEqual('”',SOURCE[proof['closing_offset']])
        self.assertEqual(64,len(proof['resolver_context_sha256']))
    def test_once_per_batch_producer_and_same_gate_projection_object(self):
        maps = []
        native = pq.validate_attribution
        def record(*args, **kwargs):
            maps.append(kwargs['tag_evidence'])
            return native(*args, **kwargs)
        with patch.object(pq,'get_attribution_tag_proofs',wraps=pq.get_attribution_tag_proofs) as proof, \
             patch.object(tp,'validate_attribution',side_effect=record), \
             patch.object(pq,'validate_attribution',side_effect=record):
            self.batch()
        self.assertEqual(1,proof.call_count)
        self.assertGreaterEqual(len(maps),3)
        self.assertTrue(all(m is maps[0] for m in maps))
    def test_legacy_opaque_metadata_evidence_and_repair_abstention(self):
        for value in (object(),datetime.datetime(2020,1,1)):
            frozen = [{**FROZEN[0],'uid':value,'custom':value},FROZEN[1]]
            self.assertEqual({0:'ALICE'},pq.get_attribution_tag_evidence(frozen,SOURCE,(n for n in ['ALICE'])))
            self.assertNotIn('accepted_metadata',self.repair(frozen=frozen))
    def test_native_delimiters_and_exact_narration(self):
        for delimiter in ',.!?:;':
            text = 'Alice said'+delimiter
            frozen = [FROZEN[0],{**FROZEN[1],'text':text}]
            source = '“Yes,” '+text
            result = self.repair(frozen=frozen,source=source)
            bounds = result['accepted_metadata']['repairs'][0]['narration_bounds']
            self.assertEqual(text,source[slice(*bounds)])
    def test_alias_and_ambiguous_identity(self):
        self.assertIn('accepted_metadata',self.repair(answer=[SHIFT[0],{**SHIFT[1],'speaker':'ALLY'}],aliases={'ALLY':'ALICE'}))
        self.assertNotIn('accepted_metadata',self.repair(names=('ALICE JONES','ALICE SMITH')))
    def test_repeated_quote_narration_and_wrong_or_partial_narration(self):
        for source,text in [(SOURCE+' “Yes,” said Bob.','said Alice.'),
                            (SOURCE+' Later said Alice.','said Alice.'),
                            (SOURCE,'Alice arrived.'),(SOURCE,'said Alice')]:
            self.assertNotIn('accepted_metadata',self.repair(frozen=[FROZEN[0],{**FROZEN[1],'text':text}],source=source))
    def test_traits_and_why_never_transfer(self):
        for field in pq._ATTRIBUTION_REPAIR_UNSAFE_FIELDS:
            for index in (0,1):
                answer = copy.deepcopy(SHIFT)
                answer[index][field]='synthetic'
                self.assertNotIn('accepted_metadata',self.repair(answer=answer))
        with self.assertRaises(tp.PassExhausted):
            self.batch(speaker_traits=True)
    def test_native_reorder_coercion_and_nested_metadata_copy(self):
        answer=[{**SHIFT[1],'n':1.0},{**SHIFT[0],'n':'00','custom':{'items':[1]}}]
        before=copy.deepcopy(answer)
        result=self.repair(answer=answer)
        self.assertEqual(['00',1.0],[r['n'] for r in result['entries']])
        result['entries'][0]['custom']['items'].append(2)
        self.assertEqual(before,answer)
        for n in (True,-1,'bad',0.5):
            self.assertNotIn('accepted_metadata',self.repair(answer=[{**SHIFT[0],'n':n},SHIFT[1]]))
    def test_two_pairs_or_whole_answer_rejection(self):
        source=SOURCE+' “No,” Bob said.'
        frozen=FROZEN+[{'type':'SPOKEN','text':'No,'},{'type':'NARRATOR','text':'Bob said.'}]
        answer=SHIFT+[{'n':2,'speaker':'NARRATOR'},{'n':3,'speaker':'BOB'}]
        self.assertEqual(2,len(self.repair(frozen,answer,source,('ALICE','BOB'))['accepted_metadata']['repairs']))
        frozen += [{'type':'SPOKEN','text':'Other.'}]
        answer += [{'n':4,'speaker':'NARRATOR'}]
        self.assertNotIn('accepted_metadata',self.repair(frozen,answer,source,('ALICE','BOB')))
    def test_changed_source_uid_or_alias_snapshot_refuses_stale_proof(self):
        proofs=pq.get_attribution_tag_proofs(FROZEN,SOURCE,['ALICE'])
        snapshot=pq.get_attribution_snapshot_digest([FROZEN,['ALICE'],[],{}])
        for frozen,source,aliases in [([{**FROZEN[0],'uid':'changed'},FROZEN[1]],SOURCE,{}),
                                     (FROZEN,'Earlier. '+SOURCE,{}),(FROZEN,SOURCE,{'ALLY':'ALICE'})]:
            result=pq.get_exact_attribution_shift_repair(frozen,SHIFT,source,proofs,{0:'ALICE'},snapshot,
                established_names=['ALICE'],speaker_aliases=aliases)
            self.assertNotIn('accepted_metadata',result)
    def test_native_valid_candidate_judge_failure_never_commits(self):
        provider=apv.make_provider('judge')
        before=provider.build_request(FROZEN,gs.LLMGenParams(),['ALICE'])
        attempts=[]
        with self.assertRaises(tp.PassExhausted) as failure:
            self.batch(entries_provider=provider,attempts=attempts)
        self.assertEqual(SHIFT,failure.exception.last_entries)
        self.assertEqual(['missing_judge_reason'],attempts[-1]['failure_codes'])
        self.assertIn('quality_findings',attempts[-1])
        self.assertEqual({'spoken_not_named','narrator_renamed'}, {f['code'] for f in attempts[-1]['raw_native_report']['findings']})
        self.assertEqual(['missing_judge_reason'], [f['code'] for f in attempts[-1]['full_provider_report']['findings']])
        self.assertNotIn('accepted_entry_transform',attempts[-1])
        self.assertFalse((self.path/'why.jsonl').exists())
        self.assertEqual(before,provider.build_request(FROZEN,gs.LLMGenParams(),['ALICE']))
    def test_judge_retry_accepts_only_later_raw_reason(self):
        good=[{**GOOD[0],'why':'Alice is named in the tag.'},GOOD[1]]
        result,attempts,_=self.batch([SHIFT,good],max_retries=1,entries_provider=apv.make_provider('judge'))
        self.assertEqual('ALICE',result[0]['speaker'])
        self.assertEqual(['quality_rejected','accepted'],[a['outcome'] for a in attempts])
        self.assertTrue(all('accepted_entry_transform' not in a for a in attempts))
        rows=[json.loads(line) for line in (self.path/'why.jsonl').read_text().splitlines()]
        self.assertEqual([good[0]['why']],[r['why'] for r in rows])
    def test_all_eleven_builtin_provider_paths(self):
        surround={'entries':[{**r,'n':i} for i,r in enumerate(FROZEN)],'before':'','after':''}
        for variant in apv.VARIANTS:
            with self.subTest(variant=variant),patch.object(apv,'rolling_summary',return_value='summary'):
                reply=([{**GOOD[0],'why':'Alice said it.'},GOOD[1]] if variant=='judge'
                       else [GOOD[0]] if variant=='michel2_spoken' else SHIFT)
                result,attempts,_=self.batch([reply],entries_provider=apv.make_provider(variant),surround=surround)
                self.assertEqual(['ALICE','NARRATOR'],[r['speaker'] for r in result])
                self.assertEqual(variant not in ('judge','michel2_spoken'),'accepted_entry_transform' in attempts[-1])
    def test_previous_and_tail_read_the_accepted_copied_labels(self):
        for variant in ('incremental','michel','continuity','michel2'):
            provider=apv.make_provider(variant)
            with patch.object(apv,'rolling_summary',return_value='summary'):
                self.batch(entries_provider=provider)
            _,user=provider.build_request(FROZEN,gs.LLMGenParams(),['ALICE'])
            self.assertNotIn('0: NARRATOR',user)
            self.assertIn('0: ALICE' if variant in ('incremental','michel') else 'ALICE: "Yes,"',user)
    def test_disabled_continuity_failure_summary_behavior_preserved(self):
        for enabled in (False,True):
            with patch.object(apv,'rolling_summary',return_value='source summary') as summary:
                with self.assertRaises(tp.PassExhausted):
                    self.batch(entries_provider=apv.make_provider('continuity'),repair_exact_shift=enabled,
                               source=SOURCE+' “Yes,” said Bob.')
                self.assertEqual(0 if enabled else 1,summary.call_count)
    def test_spoken_only_no_manufactured_shift_and_no_spoken_early_return(self):
        provider=apv.make_provider('michel2_spoken')
        surround={'entries':[{**r,'n':i} for i,r in enumerate(FROZEN)],'before':'','after':''}
        with self.assertRaises(tp.PassExhausted):
            self.batch(entries_provider=provider,surround=surround)
        surround['entries']=[{**FROZEN[1],'n':0}]
        result,attempts,client=self.batch(frozen=[FROZEN[1]],client=Client([]),entries_provider=provider,surround=surround)
        self.assertEqual('NARRATOR',result[0]['speaker'])
        self.assertEqual([],attempts)
        self.assertEqual([],client.calls)
    def test_provider_failure_keep_and_fallback_retain_raw_refusal(self):
        for mode in ('fail','keep','fallback'):
            if mode!='fallback':
                with self.assertRaises(tp.PassExhausted) as failure:
                    self.batch(entries_provider=apv.make_provider('judge'),on_exhaustion=mode,keep_scope='batch')
                self.assertEqual(SHIFT,failure.exception.last_entries)
            else:
                result,attempts,_=self.batch(entries_provider=apv.make_provider('judge'),on_exhaustion=mode)
                self.assertEqual('UNKNOWN',result[0]['speaker'])
                self.assertNotIn('accepted_entry_transform',attempts[-1])
    def test_votes_receive_only_accepted_copied_ballots(self):
        attempts=[]
        result,confidence=tp.attribute_batch_voted(Client([SHIFT]*3),'synthetic',FROZEN,
            gs.LLMGenParams(structured_output='off'),['ALICE'],votes=3,max_retries=0,
            source_text=SOURCE,repair_exact_shift=True,attempt_observer=attempts.append)
        self.assertEqual(['ALICE','NARRATOR'],[r['speaker'] for r in result])
        self.assertEqual([1.0,1.0],confidence)
        self.assertEqual(3,len(attempts))
        self.assertTrue(all(a['outcome']=='accepted' for a in attempts))
    def test_legacy_provider_no_extra_kwargs_until_explicit_opt_in(self):
        def provider(*args,**kwargs):
            self.assertNotIn('transform_entries',kwargs)
            self.assertTrue(kwargs['validate_entries'](GOOD)['passed'])
            return GOOD
        result,_,_=self.batch(entries_provider=provider,repair_exact_shift=False)
        self.assertEqual('ALICE',result[0]['speaker'])
        client=Client([])
        with self.assertRaisesRegex(ValueError,'does not support'):
            self.batch(client=client,entries_provider=provider)
        self.assertEqual([],client.calls)
    def test_length_refusal_does_not_publish_native_valid_proposal(self):
        attempts=[]
        with self.assertRaises(tp.PassExhausted):
            self.batch(client=Client([SHIFT],finish='length'),attempts=attempts)
        self.assertNotIn('accepted_entry_transform',attempts[-1])
        self.assertEqual('response_rejected',attempts[-1]['outcome'])
    def test_generic_salvage_accept_and_full_judge_rejection(self):
        codec=get_codec('json')
        salvage=NS(name=codec.name,extract=codec.extract,parse=lambda text:None,
                   salvage=codec.salvage,raw_fallback=codec.raw_fallback)
        original=gs.call_llm_for_entries
        def call(*args,**kw):
            return original(*args,**kw,codec=salvage)
        with patch.object(tp,'call_llm_for_entries',side_effect=call):
            result,attempts,_=self.batch()
        self.assertEqual('ALICE',result[0]['speaker'])
        self.assertIn('accepted_entry_transform',attempts[-1])
        self.assertFalse(attempts[-1]['raw_native_report']['passed'])
        self.assertTrue(attempts[-1]['full_provider_report']['passed'])
        with patch.object(apv,'call_llm_for_entries',side_effect=call):
            attempts=[]
            with self.assertRaises(tp.PassExhausted):
                self.batch(entries_provider=apv.make_provider('judge'),attempts=attempts)
        self.assertEqual('quality_rejected',attempts[-1]['outcome'])
        self.assertEqual(['missing_judge_reason'],attempts[-1]['failure_codes'])
        self.assertFalse(attempts[-1]['raw_native_report']['passed'])
        self.assertEqual(['missing_judge_reason'], [f['code'] for f in attempts[-1]['full_provider_report']['findings']])
        self.assertNotIn('accepted_entry_transform',attempts[-1])
    def test_bounded_plain_json_metadata_copies_only_at_commit(self):
        transformed={'accepted_metadata':{'repairs':[{'indexes':[0,1]}]}}
        metadata=gs.get_transform_metadata(transformed)
        transformed['accepted_metadata']['repairs'][0]['indexes'].append(2)
        self.assertEqual([0,1],metadata['repairs'][0]['indexes'])
        for bad in ({'bad':float('nan')},{'bad':object()},{'large':'x'*65537}):
            with self.assertRaises((ValueError,TypeError)):
                gs.get_transform_metadata({'accepted_metadata':bad})

    def test_api_retry_keeps_transform_and_only_accepts_final_attempt(self):
        import httpx
        from openai import APIConnectionError
        failure = APIConnectionError(request=httpx.Request('POST','http://127.0.0.1:1/v1/chat/completions'))
        result, attempts, client = self.batch([failure,SHIFT], max_retries=1)
        self.assertEqual('ALICE', result[0]['speaker'])
        self.assertEqual(2, len(client.calls))
        self.assertEqual('api_error', attempts[0]['outcome'])
        self.assertNotIn('accepted_entry_transform', attempts[0])
        self.assertNotIn('raw_native_report', attempts[0])
        self.assertNotIn('full_provider_report', attempts[0])
        self.assertEqual('accepted', attempts[1]['outcome'])
        self.assertIn('accepted_entry_transform', attempts[1])

    def test_runtime_split_forwards_opt_in_without_stale_parent_proposal(self):
        client = Client([[SHIFT[0]], [{'n':0,'speaker':'ALICE'}], [{'n':0,'speaker':'NARRATOR'}]])
        original = tp.attribute_batch
        with patch.object(tp,'get_exhausted_runtime_batch_ranges',return_value=[(0,1),(1,2)]), \
             patch.object(tp,'attribute_batch',wraps=original) as call:
            result, attempts, _ = self.batch(client=client)
        self.assertEqual(['ALICE','NARRATOR'], [r['speaker'] for r in result])
        self.assertEqual(3, call.call_count)
        self.assertTrue(all(c.kwargs['repair_exact_shift'] for c in call.call_args_list))
        self.assertTrue(all('accepted_entry_transform' not in a for a in attempts))

    def test_rejected_judge_ballot_cannot_become_vote_or_accepted_provenance(self):
        good=[{**GOOD[0],'why':'Alice is named.'},GOOD[1]]
        attempts=[]
        result,confidence=tp.attribute_batch_voted(Client([SHIFT,good,good]),'synthetic',FROZEN,
            gs.LLMGenParams(structured_output='off'),['ALICE'],votes=3,max_retries=0,
            source_text=SOURCE,repair_exact_shift=True,entries_provider=apv.make_provider('judge'),
            on_exhaustion='fallback',attempt_observer=attempts.append)
        self.assertEqual('ALICE',result[0]['speaker'])
        self.assertEqual([1.0,1.0],confidence)
        self.assertEqual('quality_rejected',attempts[0]['outcome'])
        self.assertTrue(all('accepted_entry_transform' not in a for a in attempts))

    def test_false_default_still_rejects_the_shift_without_new_metadata(self):
        attempts=[]
        with self.assertRaises(tp.PassExhausted) as failure:
            self.batch(repair_exact_shift=False,attempts=attempts)
        self.assertEqual(SHIFT,failure.exception.last_entries)
        self.assertNotIn('accepted_entry_transform',attempts[0])

    def test_unencodable_source_keeps_legacy_evidence_but_refuses_repair(self):
        source=SOURCE+' \ud800'
        self.assertEqual({0:'ALICE'},pq.get_attribution_tag_evidence(FROZEN,source,['ALICE']))
        self.assertNotIn('accepted_metadata',self.repair(source=source))

    def test_strict_snapshot_rejects_json_coercion_collisions_and_subclasses(self):
        class ListSubclass(list):
            pass
        class IntSubclass(int):
            pass
        class StringSubclass(str):
            pass
        for value in ({'uid':(1,2)}, {'uid':{1:'x'}}, ListSubclass([1]),
                      {'uid':IntSubclass(1)}, {StringSubclass('uid'):1},
                      {'uid':StringSubclass('x')}, {'uid':float('inf')}):
            self.assertIsNone(pq.get_attribution_snapshot_digest(value))
        self.assertIsNotNone(pq.get_attribution_snapshot_digest({'uid':[1,2]}))
        self.assertIsNotNone(pq.get_attribution_snapshot_digest({'uid':{'1':'x'}}))
        for uid in ((1,2),{1:'x'}):
            frozen=[{**FROZEN[0],'uid':uid},FROZEN[1]]
            self.assertEqual({0:'ALICE'},pq.get_attribution_tag_evidence(frozen,SOURCE,['ALICE']))
            self.assertNotIn('accepted_metadata',self.repair(frozen=frozen))

    def test_strict_snapshot_rejects_cycles_and_excessive_depth(self):
        cycle=[]
        cycle.append(cycle)
        self.assertIsNone(pq.get_attribution_snapshot_digest(cycle))
        deep=[]
        for _ in range(65):
            deep=[deep]
        self.assertIsNone(pq.get_attribution_snapshot_digest(deep))
        self.assertIsNotNone(pq.get_attribution_snapshot_digest([None,True,1,1.5,'x',{},[]]))

    def test_default_requests_match_opt_in_prompts_schema_and_budgets(self):
        ordinary=Client([GOOD])
        enabled=Client([SHIFT])
        self.batch(client=ordinary,repair_exact_shift=False)
        self.batch(client=enabled)
        self.assertEqual(ordinary.calls,enabled.calls)

    def test_generic_lad_tag_is_explicitly_outside_the_proper_name_mechanism(self):
        source='“Yes,” said the lad.'
        frozen=[FROZEN[0],{**FROZEN[1],'text':'said the lad.'}]
        self.assertEqual({},pq.get_attribution_tag_evidence(frozen,source,['THE LAD']))
        self.assertNotIn('accepted_metadata',self.repair(frozen=frozen,source=source,names=['THE LAD']))

    def test_observer_reports_are_detached_and_do_not_cross_mutate(self):
        result,attempts,_=self.batch()
        raw=copy.deepcopy(attempts[-1]['raw_native_report'])
        attempts[-1]['full_provider_report']['findings'].append({'code':'caller_change'})
        self.assertEqual(raw,attempts[-1]['raw_native_report'])
        self.assertFalse(raw['passed'])
        self.assertEqual('ALICE',result[0]['speaker'])

    def test_both_frozen_pair_rows_abstain_for_every_known_attribution_field(self):
        for field in pq._ATTRIBUTION_REPAIR_UNSAFE_FIELDS:
            for index in (0,1):
                with self.subTest(field=field,index=index):
                    frozen=copy.deepcopy(FROZEN)
                    frozen[index][field]='old attribution metadata'
                    self.assertEqual({0:'ALICE'},pq.get_attribution_tag_evidence(frozen,SOURCE,['ALICE']))
                    self.assertNotIn('accepted_metadata',self.repair(frozen=frozen))

    def test_frozen_explanations_and_traits_cannot_return_under_a_repaired_label(self):
        for field in ('why','gender','traits','reason'):
            for index in (0,1):
                frozen=copy.deepcopy(FROZEN)
                frozen[index][field]='old attribution metadata'
                attempts=[]
                with self.assertRaises(tp.PassExhausted) as failure:
                    self.batch(frozen=frozen,attempts=attempts)
                self.assertEqual(SHIFT,failure.exception.last_entries)
                self.assertNotIn('accepted_entry_transform',attempts[-1])
                result,_,_=self.batch([GOOD],frozen=frozen)
                self.assertEqual('old attribution metadata',result[index][field])
                self.assertEqual(['ALICE','NARRATOR'],[row['speaker'] for row in result])
