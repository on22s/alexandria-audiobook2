"""Verify physical source spans, the next checkpoint write, and ambiguous on-disk binding."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_review_round4_independent as h
import review_script as rev
from tests import test_review_source_spans as spans

class Controls(unittest.TestCase):
    setUp=h.Independent.setUp
    invoke=h.Independent.invoke
    completed=h.Independent.completed

    def test_real_source_spans_around_repeated_text(self):
        texts=['Ash fell.','Echo faded.','echo faded!']
        raw='\n\n'.join(texts)
        source=h.rows(texts)
        offset=0
        for i,r in enumerate(source):
            pairs=list(r.items())
            pairs.insert(i,('source_span',[offset,offset+len(r['text'])]))
            source[i]=dict(pairs)
            offset+=len(r['text'])+2
        self.src,_,_=spans.create_book(str(self.root),raw=raw,source=source)
        proof=rev.get_review_source_snapshot(str(self.root),str(self.src),self.src.read_bytes(),source)
        self.assertIsNotNone(proof)
        self.assertEqual(raw,proof['text'])
        replies=[[h.reply('Ash fell. Echo faded.')],[h.reply('echo faded!')]]
        output=self.completed(replies,'fresh',count=2)
        published=json.loads(output)
        self.assertEqual(h.keys(source),h.keys(published))
        self.assertEqual(texts,[raw[slice(*r['source_span'])] for r in published])
        self.assertEqual(output,self.completed([],'reuse',count=0))
        self.assertEqual(output,self.completed(replies,'forced','--force-review',count=2))

    def test_second_interruption_preserves_checkpoint_prefix_layout(self):
        self.src=self.out=self.root/'annotated_script.json'
        source=h.rows(['A copper','bell rang.','A','copper bell rang.','Wind rose.','Fog fell.'])
        h.dump(self.src,source)
        src_bytes=self.src.read_bytes()
        replies=[[h.reply('A'),h.reply('copper bell rang.')]]*2+[[h.reply('Wind rose.'),h.reply('Fog fell.')]]
        save=rev.save_checkpoint
        ck=Path(str(self.src)+'.review_checkpoint.json')
        class Interrupted(Exception): pass
        def interrupted(replies,at,name):
            def stop(*args,**kwargs):
                result=save(*args,**kwargs)
                if args[1]==at:
                    raise Interrupted()
                return result
            with patch.object(rev,'save_checkpoint',side_effect=stop):
                n,e,success=self.invoke(replies,name)
            self.assertEqual(1,n)
            self.assertIsInstance(e,Interrupted)
            self.assertFalse(success)
            state=rev.load_generation_checkpoint_document(str(ck))
            h.dump(self.root/(name+'-rehydrated.json'),state)
            self.assertEqual(src_bytes,self.src.read_bytes())
            self.assertFalse(Path(str(self.src)+'.review_completed.json').exists())
            return state
        first=interrupted(replies,1,'first')
        self.assertEqual([2],first['source_offsets'])
        self.assertEqual(h.keys(source[:2]),h.keys(first['all_corrected']))
        second=interrupted(replies[1:],2,'second')
        self.assertEqual([2,4],second['source_offsets'])
        self.assertEqual(first['all_corrected'],second['all_corrected'][:2])
        self.assertEqual(h.keys(first['all_corrected']),h.keys(second['all_corrected'][:2]),'later checkpoint serialized the wrong retained parent orders')

    def test_ambiguous_receiver_does_not_change_disk(self):
        # Direct receiver contract with a retained subset. A synthetic compatible output is explicit here.
        source=h.rows(['One','Two.','one','two!'])
        h.dump(self.src,source)
        reply=h.reply('One Two.')
        values={**source[3],**reply}
        old=[{k:values[k] for k in source[3]}]
        h.dump(self.out,old)
        before=self.out.read_bytes()
        digest=h.get_review_entries_fingerprint(old)
        rev.ensure_reused_review_output_key_order(
            str(self.src),str(self.out),source,self.src.read_bytes(),digest,
            observed_output_bytes=before)
        (self.root/'before.json').write_bytes(before)
        self.assertEqual(before,self.out.read_bytes(),'two possible adjacent parent pairs were treated as a unique match')

if __name__=='__main__':
    unittest.main()
