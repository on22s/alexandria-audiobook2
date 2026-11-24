"""A second incomplete review compacts the on-disk retained prefix."""
from unittest.mock import patch
from pathlib import Path
import unittest
import review_script as rev
from tests import test_review_round4_independent as h

class Compact(unittest.TestCase):
    setUp=h.Independent.setUp
    invoke=h.Independent.invoke

    def test_failed_tail_compaction_retains_original_prefix(self):
        self.src=self.out=self.root/'annotated_script.json'
        source=h.rows(['A copper','bell rang.','A','copper bell rang.','Wind rose.','Fog fell.'])
        h.dump(self.src,source)
        before=self.src.read_bytes()
        group=[h.reply('A'),h.reply('copper bell rang.')]
        save=rev.save_checkpoint
        class Interrupted(Exception): pass
        def stop(*args,**kwargs):
            result=save(*args,**kwargs)
            if args[1]==1:
                raise Interrupted()
            return result
        with patch.object(rev,'save_checkpoint',side_effect=stop):
            count,e,success=self.invoke([group],'first')
        self.assertEqual(1,count)
        self.assertIsInstance(e,Interrupted)
        self.assertFalse(success)
        ck=Path(str(self.src)+'.review_checkpoint.json')
        first=rev.load_generation_checkpoint_document(str(ck))
        self.assertEqual(h.keys(source[:2]),h.keys(first['all_corrected']))
        bad=[h.reply('Other words.')]
        self.assertEqual((4,None,False),self.invoke([group,bad,bad,bad],'partial-again'))
        second=rev.load_generation_checkpoint_document(str(ck))
        h.dump(self.root/'first-rehydrated.json',first)
        h.dump(self.root/'second-rehydrated.json',second)
        self.assertEqual([3],second['failed_batches'])
        self.assertEqual([2,4,6],second['source_offsets'])
        self.assertEqual(before,self.src.read_bytes())
        self.assertFalse(Path(str(self.src)+'.review_completed.json').exists())
        self.assertEqual(first['all_corrected'],second['all_corrected'][:2])
        self.assertEqual(h.keys(first['all_corrected']),h.keys(second['all_corrected'][:2]),'compacted checkpoint overwrote the original layouts of the retained rows')

if __name__=='__main__':
    unittest.main()
