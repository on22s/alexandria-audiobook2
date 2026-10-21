"""Exact occurrence anchors, bounded resolution, and canonical speech preparation."""
import copy
import unittest
import pronunciation as p
from speech_text import get_prelexicon_speech_normalization


class OccurrenceResolverTests(unittest.TestCase):
    def rule(self, raw='Ann Ann', query='Ann', number=0, replacement='Ah-n', **extra):
        prepared=get_prelexicon_speech_normalization(raw)['text']
        anchor=p.get_occurrence_pronunciation_candidates(prepared,'uid-A',raw,query)['candidates'][number]
        anchor={key:value for key,value in anchor.items() if key not in ('before','after')}
        return {**anchor,'id':'rule-1','state':'approved','replacement':replacement,**extra}

    def resolve(self, raw, rules, shared=None, context=None):
        prepared=get_prelexicon_speech_normalization(raw)['text']
        return p.get_occurrence_pronunciation_resolution(prepared,shared or {},rules=rules,
            uid='uid-A',text_digest=p.get_pronunciation_text_digest(raw),context=context)

    def test_one_identical_occurrence_changes_and_replacements_never_rescan(self):
        raw='Ann Ann'
        r=self.rule(raw,replacement='Bee')
        result=self.resolve(raw,[r],{'Ann':'Global','Bee':'Oops'})
        self.assertEqual('Bee Global',result['text'])
        self.assertEqual(['occurrence',None],[row.get('scope') for row in result['substitutions']])
        self.assertTrue(result['decisions'][0]['applied'])
        self.assertEqual('Ann Ann',raw)

    def test_unicode_and_crlf_anchors_are_server_codepoints_in_exact_prelexicon(self):
        raw='𠀀𠀁𠀂𠀃 Ann Ann'
        prepared=get_prelexicon_speech_normalization(raw)['text']
        candidates=p.get_occurrence_pronunciation_candidates(prepared,'uid-A',raw,'Ann')['candidates']
        self.assertEqual([5,9],[r['start'] for r in candidates])
        self.assertEqual(9,len(prepared[:5].encode('utf-16-le'))//2)
        self.assertEqual('𠀀𠀁𠀂𠀃 Ah-n Ann',self.resolve(raw,[self.rule(raw)])['text'])
        self.assertEqual('𠀀𠀁𠀂𠀃 Ann Ah-n',self.resolve(raw,[self.rule(raw,number=1)])['text'])
        self.assertEqual('unicode_codepoint',candidates[0]['offset_unit'])
        raw='𠀀𠀁𠀂𠀃 Ann\r\nAnn'
        candidates=p.get_occurrence_pronunciation_candidates(raw,'uid-A',raw,'Ann')['candidates']
        self.assertEqual([5,10],[row['start'] for row in candidates])
        self.assertEqual('𠀀𠀁𠀂𠀃 Ann\r\nAh-n',self.resolve(raw,[self.rule(raw,number=1)])['text'])

    def test_cleanup_has_exact_stage_and_stale_raw_text_refuses_even_if_prepared_matches(self):
        raw='Ann ☃ Ann'
        r=self.rule(raw,number=1)
        prepared=get_prelexicon_speech_normalization(raw)['text']
        p.validate_occurrence_pronunciation_rules([r],'uid-A',raw,prepared)
        self.assertNotEqual(raw.index('Ann',4),r['start'])
        changed=raw.replace('☃','☂')
        self.assertEqual(prepared,get_prelexicon_speech_normalization(changed)['text'])
        result=self.resolve(changed,[r],{'Ann':'Global'})
        self.assertFalse(result['decisions'][0]['applied'])
        self.assertEqual('stale_chunk_text',result['decisions'][0]['reason'])
        with self.assertRaisesRegex(ValueError,'stale_chunk_text'):
            p.validate_occurrence_pronunciation_rules([r],'uid-A',changed,
                get_prelexicon_speech_normalization(changed)['text'])

    def test_overlap_is_refused_independent_of_restrictions_and_not_resolved_by_order(self):
        raw='Ann Marie'
        a=self.rule(raw,restrictions={'language':'English'})
        b=self.rule(raw,query='Ann Marie',id='rule-2',restrictions={'language':'Japanese'})
        with self.assertRaisesRegex(ValueError,'overlap'):
            p.validate_occurrence_pronunciation_rules([a,b],'uid-A',raw,raw)
        for rules in ([a,b],[b,a]):
            result=self.resolve(raw,rules,{'Ann Marie':'Global'},context={'language':'English'})
            self.assertEqual('Global',result['text'])
            self.assertTrue(all(row['reason']=='overlapping_approved_spans' for row in result['decisions']))

    def test_scoped_subspan_suppresses_entire_global_phrase_and_identity_is_explicit(self):
        raw='Ann Marie Ann'
        self.assertEqual('Ah-n Marie Shared',self.resolve(raw,[self.rule(raw)],
            {'Ann Marie':'Wrong phrase','Ann':'Shared'})['text'])
        self.assertEqual('Ann Shared',self.resolve('Ann Ann',[self.rule(replacement='Ann')],
            {'Ann':'Shared'})['text'])

    def test_all_restrictions_use_captured_context_and_bypass_leaves_shared_rules(self):
        context={'speaker':'ALICE','voice_sha256':'a'*64,'language':'English','engine_mode':'local'}
        r=self.rule(restrictions=context)
        self.assertEqual('Ah-n Global',self.resolve('Ann Ann',[r],{'Ann':'Global'},context)['text'])
        for key in context:
            changed={**context,key:'other'}
            result=self.resolve('Ann Ann',[r],{'Ann':'Global'},changed)
            self.assertEqual('Global Global',result['text'])
            self.assertEqual('scope_mismatch:'+key,result['decisions'][0]['reason'])

    def test_plaintext_phonetic_hints_have_explicit_fallback_or_unsupported_decision(self):
        r=self.rule(replacement=None,phonetic_hint='æn')
        result=self.resolve('Ann Ann',[r],{'Ann':'Shared'})
        self.assertEqual('Shared Shared',result['text'])
        self.assertEqual('unsupported_phonetic_hint',result['decisions'][0]['reason'])
        r['fallback']='Ah-n'
        result=self.resolve('Ann Ann',[r])
        self.assertEqual('Ah-n Ann',result['text'])
        self.assertEqual('applied_phonetic_fallback',result['decisions'][0]['reason'])

    def test_notes_ids_drafts_and_order_do_not_pollute_captured_render_inputs(self):
        a=self.rule(); b=self.rule(number=1,id='rule-2')
        before=copy.deepcopy([a,b])
        changed=copy.deepcopy([b,a]); changed[0].update(note='Reviewed again',id='different-id')
        changed.append(self.rule(id='draft',state='draft',replacement='Different'))
        self.assertEqual(p.get_occurrence_pronunciation_inputs(before),p.get_occurrence_pronunciation_inputs(changed))
        source_rule=self.rule(restrictions={'speaker':'A'})
        projection=p.get_occurrence_pronunciation_inputs([source_rule])
        projection[0]['restrictions']['speaker']='B'
        self.assertEqual(before,[a,b])
        self.assertEqual('A',source_rule['restrictions']['speaker'])

    def test_duplicate_ids_stale_hashes_boolean_offsets_wrong_uid_and_limits_refuse(self):
        raw='Ann Ann'; a=self.rule(); b=self.rule(number=1)
        with self.assertRaisesRegex(ValueError,'unique'):
            p.validate_occurrence_pronunciation_rules([a,b],'uid-A',raw,raw)
        result=self.resolve(raw,[a,b],{'Ann':'Shared'})
        self.assertEqual('Shared Shared',result['text'])
        self.assertTrue(all(row['reason']=='duplicate_rule_id' for row in result['decisions']))
        for update in ({'start':False},{'end':True},{'uid':'other'},
                       {'text_sha256':'0'*64},{'prepared_sha256':'0'*64},
                       {'start':4,'end':6},{'offset_unit':'utf16'}, {'restrictions':{'engine_mode':'magic'}}):
            with self.subTest(update=update),self.assertRaises(ValueError):
                p.validate_occurrence_pronunciation_rules([{**a,**update}],'uid-A',raw,raw)
        with self.assertRaises(ValueError):
            p.validate_occurrence_pronunciation_rules([a]*33,'uid-A',raw,raw)

    def test_candidates_bound_output_escape_query_and_preserve_combining_marks_and_surrogates(self):
        text='Ann '*70
        result=p.get_occurrence_pronunciation_candidates(text,'uid-A',text,'Ann')
        self.assertEqual(64,len(result['candidates'])); self.assertTrue(result['truncated'])
        self.assertEqual([],p.get_occurrence_pronunciation_candidates('Anne','uid-A','Anne','Ann')['candidates'])
        raw='A.* A.*'
        self.assertEqual(2,len(p.get_occurrence_pronunciation_candidates(raw,'uid-A',raw,'A.*')['candidates']))
        for raw,query in (('Cafe\u0301 Cafe\u0301','Cafe\u0301'),('\ud800 Ann Ann','Ann')):
            rule=self.rule(raw,query=query)
            self.assertEqual(1,self.resolve(raw,[rule])['text'].count('Ah-n'))


    def test_canonical_hook_applies_after_cleanup_and_keeps_insertions_literal(self):
        from speech_text import get_speech_normalization
        raw='Ann © Ann'
        rule=self.rule(raw,number=1,replacement='© Bee')
        row={'uid':'uid-A','text':raw,'pronunciation_rules':[rule]}
        with p.ensure_pronunciation_audition({'Ann':'Shared','Bee':'Oops'}), \
                p.ensure_occurrence_pronunciation_context(row):
            result=get_speech_normalization(raw)
        self.assertEqual('Shared copyright © Bee.',result['text'])
        self.assertEqual('applied_respelling',result['pronunciation_decisions'][0]['reason'])
        self.assertEqual(raw,row['text'])

    def test_context_is_captured_nested_empty_scope_clears_and_exception_resets(self):
        from speech_text import normalize_for_speech
        rule=self.rule(restrictions={'speaker':'ALICE'})
        row={'uid':'uid-A','text':'Ann Ann','pronunciation_rules':[rule]}
        context={'speaker':'ALICE'}
        self.assertIsNone(p._occurrence_context.get())
        with p.ensure_pronunciation_audition({'Ann':'Shared'}):
            with self.assertRaisesRegex(RuntimeError,'body failure'):
                with p.ensure_occurrence_pronunciation_context(row,context):
                    rule['replacement']='Wrong';context['speaker']='OTHER'
                    self.assertEqual('Ah-n Shared.',normalize_for_speech(row['text']))
                    with p.ensure_occurrence_pronunciation_context({'uid':'other','text':'Ann Ann'}):
                        self.assertEqual('Shared Shared.',normalize_for_speech('Ann Ann'))
                    self.assertEqual('Ah-n Shared.',normalize_for_speech('Ann Ann'))
                    raise RuntimeError('body failure')
            self.assertEqual('Shared Shared.',normalize_for_speech('Ann Ann'))
        self.assertIsNone(p._occurrence_context.get())

    def test_actual_public_engine_dispatch_uses_canonical_context_once(self):
        from unittest.mock import patch
        from tests.test_batch_style_timeline import get_engine
        engine=get_engine();seen=[]
        row={'uid':'uid-A','text':'Ann Ann','pronunciation_rules':[self.rule(replacement='Bee')]}
        with p.ensure_pronunciation_audition({'Ann':'Shared','Bee':'Oops'}), \
                p.ensure_occurrence_pronunciation_context(row), \
                patch.object(engine,'_generate_voice_prepared',side_effect=lambda *args: seen.append(args[0]) or True):
            self.assertTrue(engine.generate_voice('Ann Ann','','NARRATOR',{},'unused-private-output'))
        self.assertEqual(['Bee Shared.'],seen)



    def test_many_expanding_shared_matches_keep_one_scoped_identity_and_exact_order(self):
        raw='Ann '*4096
        rule=self.rule(raw,replacement='First')
        result=self.resolve(raw,[rule],{'Ann':'x'*256})
        self.assertEqual('First '+('x'*256+' ')*4095,result['text'])
        self.assertEqual(4096,len(result['substitutions']))
        draft={**rule,'state':'draft'}
        bypass=self.resolve(raw,[draft],{'Ann':'x'*256})
        self.assertEqual(('x'*256+' ')*4096,bypass['text'])
        self.assertEqual('draft',bypass['decisions'][0]['reason'])



    def test_unknown_rule_data_rejects_before_serializing_or_traversing_unbounded_values(self):
        from unittest.mock import patch
        nested=None
        for _ in range(2000): nested={'nested':nested}
        for extra in ({'unknown':nested},{'unknown':'x'*1048576},
                      {'restrictions':{str(i):'x' for i in range(10000)}}):
            with self.subTest(field=next(iter(extra))), \
                    patch.object(p.json,'dumps',side_effect=AssertionError('must reject before serialization')), \
                    self.assertRaisesRegex(ValueError,'invalid_'):
                p.validate_occurrence_pronunciation_rules([{**self.rule(),**extra}],
                                                        'uid-A','Ann Ann','Ann Ann')



if __name__=='__main__': unittest.main()
