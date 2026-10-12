import copy,unittest
from literal_policy import bind_literal_policy,get_literal_policy,ensure_literal_chunk,ensure_literal_narration,CHUNK_POLICY_KEY
from project import group_into_chunks,get_chunk_text_key
from delivery_review import get_delivery_retry_chunks
from speech_text import get_speech_preparation
class IntegrationChecks(unittest.TestCase):
    def script(self):
        return bind_literal_policy('Wait, she said. Now.',[
          dict(speaker='MARA',text='Wait,',instruct='Urgent.',instruct_unchecked=True),
          dict(speaker='NARRATOR',text=', she said.',instruct='Neutral.'),
          dict(speaker='MARA',text='Now.',instruct='Urgent.',instruct_unchecked=True)])
    def saved(self,before):
        return [dict(c,id=i,uid=f'u{i}',status='done',audio_path=f'a{i}.wav') for i,c in enumerate(group_into_chunks(before))]
    def test_normalize_before_binding_without_changing_source(self):
        s=self.script();original=copy.deepcopy(s);chunks=group_into_chunks(s,include_source_indices=True)
        self.assertEqual('she said.',chunks[1]['text']);self.assertEqual([1],chunks[1]['source_entry_indices'])
        self.assertEqual(original,s);ensure_literal_narration(s)
        for c in chunks:ensure_literal_chunk(c,get_literal_policy(s))
    def test_speech_expansion_remains_allowed(self):
        s=bind_literal_policy('Tea & cake costs $5.',[dict(speaker='NARRATOR',text='Tea & cake costs $5.',instruct='Neutral.')])
        c=group_into_chunks(s)[0];ensure_literal_chunk(c,get_literal_policy(s))
        prepared=get_speech_preparation(c['text'],c['instruct']);self.assertIn('and',prepared['text']);self.assertNotEqual(s[0]['text'],prepared['text'])
    def test_retry_accepts_old_punctuation_and_preserves_unaffected_audio(self):
        s=self.script();chunks=self.saved(s);chunks[1]['text']=', she said.'
        before=copy.deepcopy(chunks);after=[dict(e,instruct='Calm.') if e['speaker']=='MARA' else copy.deepcopy(e) for e in s]
        out=get_delivery_retry_chunks(s,after,chunks)
        self.assertEqual(chunks,before);self.assertEqual(('u1','a1.wav'),(out[1]['uid'],out[1]['audio_path']))
        self.assertEqual([None,None],[out[0]['audio_path'],out[2]['audio_path']])
        for c in out:ensure_literal_chunk(c,get_literal_policy(s))
    def test_regrouped_protected_rows_receive_valid_markers(self):
        s=bind_literal_policy('She left. She returned.',[dict(speaker='NARRATOR',text='She left.',instruct='One.',instruct_unchecked=True),dict(speaker='NARRATOR',text='She returned.',instruct='Two.',instruct_unchecked=True)])
        after=[dict(e,instruct='Neutral.') for e in s];out=get_delivery_retry_chunks(s,after,self.saved(s))
        self.assertEqual(1,len(out));self.assertIsNone(out[0]['audio_path']);ensure_literal_chunk(out[0],get_literal_policy(s))
    def test_missing_corrupt_and_foreign_provenance_are_refused(self):
        s=self.script();after=[dict(e,instruct='Calm.') if e['speaker']=='MARA' else copy.deepcopy(e) for e in s]
        for kind in ['missing','corrupt','foreign']:
            chunks=self.saved(s)
            if kind=='missing':chunks[1].pop(CHUNK_POLICY_KEY)
            elif kind=='corrupt':chunks[1][CHUNK_POLICY_KEY]['narration_words']['sha256']='0'*64
            else:chunks[1][CHUNK_POLICY_KEY]['source_policy_sha256']='0'*64
            with self.subTest(kind=kind),self.assertRaises(ValueError):get_delivery_retry_chunks(s,after,chunks)
    def test_changed_words_remain_refused(self):
        s=self.script();chunks=self.saved(s);chunks[1]['text']='she whispered.'
        with self.assertRaises(ValueError):get_delivery_retry_chunks(s,s,chunks)
        s[1]['text']='she whispered.'
        with self.assertRaises(ValueError):group_into_chunks(s)
    def test_real_voice_overrides_are_still_refused(self):
        s=self.script();chunks=self.saved(s);chunks[1]['character_style']='Warm'
        with self.assertRaisesRegex(ValueError,'voice/style'):get_delivery_retry_chunks(s,s,chunks)
