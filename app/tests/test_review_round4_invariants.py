"""Additional CLI controls with real receipt/checkpoint files and synthetic text."""
import errno
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from tests import test_review_round4_independent as h
import review_script as rev
from utils import file_lock

class Boundaries(unittest.TestCase):
    setUp=h.Independent.setUp
    invoke=h.Independent.invoke
    completed=h.Independent.completed
    seed=h.Independent.seed
    edit=h.Independent.edit

    def test_cross_boundary_resegmentation_unknown_lineage_no_write(self):
        source=h.rows(['A copper','bell rang.'])
        h.dump(self.src,source)
        answers=[[h.reply('A'),h.reply('copper bell rang.')]]
        first=self.completed(answers,'fresh',count=1)
        proof=Path(str(self.out)+'.review_completed.json')
        previous=proof.read_bytes()
        h.dump(self.src,h.reverse(source))
        before=self.src.read_bytes()
        self.assertIsNone(rev.get_review_source_order_templates(h.load(self.out),h.load(self.src)))
        self.assertEqual(first,self.completed([],'reuse',count=0))
        self.assertEqual(before,self.src.read_bytes())
        self.assertEqual(previous,proof.read_bytes())
        self.assertFalse(Path(str(self.out)+'.review_checkpoint.json').exists())

    def test_reuse_of_already_correct_output_must_preserve_later_order_save(self):
        self.seed()
        self.assertEqual(h.keys(h.load(self.src)),h.keys(h.load(self.out)))
        edited=h.reverse(h.load(self.out))
        calls,exc,success=self.invoke([],'reuse',on_match=lambda:self.edit(self.out,edited))
        self.assertEqual(0,calls)
        self.assertEqual((self.root/'editor.json').read_bytes(),self.out.read_bytes())

    def test_cancel_before_flat_migration_leaves_the_flat_receipt(self):
        self.seed()
        proof=Path(str(self.out)+'.review_completed.json')
        flat=h.load(proof)
        del flat['facts']
        h.dump(proof,flat)
        original=proof.read_bytes()
        old=self.out.read_bytes()
        signal=self.root/'cancel'
        # A cancellation arrives during the read-only output transaction and before the distinct receipt write.
        derive=rev.get_review_source_order_templates
        def schedule(*args):
            signal.touch()
            return derive(*args)
        with patch.dict(os.environ,{'ALEXANDRIA_REVIEW_CANCEL_FILE':str(signal)}),patch.object(rev,'get_review_source_order_templates',side_effect=schedule):
            calls,exc,success=self.invoke([],'reuse')
        self.assertTrue(signal.exists())
        self.assertEqual(0,calls)
        self.assertEqual(old,self.out.read_bytes())
        self.assertEqual(original,proof.read_bytes(),'started receipt migration after cancellation was pending')
        self.assertFalse(success)

    def test_legacy_checkpoint_cannot_rebind_retained_work_to_future_batches(self):
        self.src=self.out=self.root/'annotated_script.json'
        source=h.rows(['A copper','bell rang.','Fog','rose.','A','copper bell rang.','Fog rose.','Dawn.'])
        h.dump(self.src,source)
        replies=[[h.reply('A'),h.reply('copper bell rang.')],[h.reply('Fog rose.')],
                 [h.reply('A'),h.reply('copper bell rang.')],[h.reply('Fog rose.'),h.reply('Dawn.')]]
        fresh=self.completed(replies,'control',count=4)
        h.dump(self.src,source)
        class Interrupted(Exception): pass
        save=rev.save_checkpoint
        def interrupt(*args,**kwargs):
            result=save(*args,**kwargs)
            if args[1]==2:
                raise Interrupted()
            return result
        with patch.object(rev,'save_checkpoint',side_effect=interrupt):
            count,error,success=self.invoke(replies,'interrupted','--force-review')
        self.assertEqual(2,count)
        self.assertIsInstance(error,Interrupted)
        self.assertFalse(success)
        ck=Path(str(self.out)+'.review_checkpoint.json')
        proof=rev.load_generation_checkpoint_document(str(ck))
        self.assertEqual([2,4],proof['source_offsets'])
        self.assertEqual([2,1],proof['batch_lengths'])
        self.assertEqual(h.keys([source[i] for i in (0,1,3)]),h.keys(proof['all_corrected']))
        for key in ('batch_lengths','review_accounting','review_batch_contributions'):
            del proof[key]
        h.dump(ck,proof)
        h.dump(self.root/'legacy-checkpoint-seed.json',proof)
        actual=self.completed(replies[2:],'resumed',count=2)
        self.assertEqual(h.json.loads(fresh),h.json.loads(actual))
        self.assertEqual(fresh,actual,'a valid historical source offset limits the parent search to the first four source rows')

    def test_distinct_existing_paths_with_symlink_parent_component(self):
        # Both paths are ordinary scratch scripts. On POSIX a symlink is resolved before "..".
        nested=self.root/'vendor'/'edition'
        nested.mkdir(parents=True)
        (self.root/'edition').symlink_to(nested,target_is_directory=True)
        self.src=self.root/'edition'/'..'/'input.json'
        self.out=self.root/'input.json'
        source=h.rows(['A bell rang.'])
        h.dump(self.src,source)
        h.dump(self.out,h.rows(['Previous export.']))
        self.assertFalse(os.path.samefile(self.src,self.out))
        self.assertEqual(os.path.abspath(self.src),os.path.abspath(self.out))
        calls,exc,success=self.invoke([[h.reply(source[0]['text'])]],'separate')
        self.assertEqual((1,None,True),(calls,exc,success),'different physical input and output were treated as in-place')

if __name__=='__main__':
    unittest.main()
