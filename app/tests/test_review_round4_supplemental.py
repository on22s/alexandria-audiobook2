"""Receipt, alias, source-proof and ambiguity controls for the round-4 CLI."""
import contextlib
import errno
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests import test_review_round4_independent as h
import review_script as rev
from tests import test_review_save_as_source as save_as
from tests.test_review_checkpoint_disposition import run_review, SOURCE
from book_state_transaction import ensure_book_state
from script_selection_binding import verify_review_source_identity
from utils import file_lock

class Supplemental(unittest.TestCase):
    setUp=h.Independent.setUp
    invoke=h.Independent.invoke
    completed=h.Independent.completed
    seed=h.Independent.seed
    edit=h.Independent.edit

    def flat(self):
        path = Path(str(self.out)+'.review_completed.json')
        flat = h.load(path)
        del flat['facts']
        h.dump(path,flat)
        return path

    def test_alias_flat_receipt_reuses_reviewed_script(self):
        direct=self.root/'direct'
        alias=self.root/'alias'
        direct.mkdir()
        alias.symlink_to(direct,target_is_directory=True)
        for name, path in [('direct', direct), ('aliased', alias)]:
            with self.subTest(name=name):
                self.src=direct/(name+'.json')
                self.out=path/self.src.name
                source=self.seed()
                self.flat()
                reused=self.invoke([[h.reply(source[0]['text'])]],'upgrade-'+name)
                self.assertEqual((0,None,True),reused,'a valid flat same-file receipt must upgrade without reviewing again')

    def test_alias_disposition_uses_published_coordinates(self):
        for name in ['direct','alias']:
            d=self.root/name
            d.mkdir()
            direct=d/'direct'
            direct.mkdir()
            alias=d/'linked'
            alias.symlink_to(direct,target_is_directory=True)
            source=direct/'book.json'
            out=(direct if name=='direct' else alias)/source.name
            h.dump(source,SOURCE)
            first=run_review(d,source,[True,False],out=out,split_first=True)
            resumed=run_review(d,source,[True]*8,out=out)
            h.dump(d/'observations.json',dict(first=first,resumed=resumed))
            with self.subTest(name=name):
                self.assertTrue(first['disposition']['resumable_checkpoint'])
                self.assertEqual(2,first['disposition']['retry_offset'])
                self.assertEqual([[row['text']] for row in SOURCE[1:]],resumed['submitted'])

    def test_ambiguous_split_and_merge_groups(self):
        for shape,src_text,out_text,ambiguous_paths in [
                ('split',['One Two.','one two!'],['One','Two.'],[[0,0],[1,1]]),
                ('merge',['One','Two.','one','two!'],['One Two.'],[[1],[3]]),
                ('mixed',['One Two.','one','two!'],['One Two.'],[[0],[2]])]:
            with self.subTest(shape=shape):
                source=h.rows(src_text)
                published=h.reverse(h.rows(out_text))
                got=rev.get_review_source_order_templates(published,source)
                h.dump(self.root/(shape+'.json'),dict(source=source,output=published,
                       derivations=ambiguous_paths,templates=got))
                self.assertIsNone(got, 'more than one monotone token-preserving derivation has distinct parent layouts')

    def test_legacy_migration_failure_keeps_old_publication(self):
        source=self.seed()
        proof=self.flat()
        h.dump(self.src,h.reverse(source))
        old=self.out.read_bytes()
        flat=proof.read_bytes()
        rename=os.replace
        hits=[]
        def refuse(src,dst,*args,**kwargs):
            if Path(dst)==proof:
                hits.append(str(dst))
                raise OSError(errno.EXDEV,'synthetic receipt publication failure')
            return rename(src,dst,*args,**kwargs)
        with patch.object(os,'replace',side_effect=refuse):
            count,error,success=self.invoke([],'refused')
        changed=self.out.read_bytes()!=old
        self.assertEqual(0,count)
        self.assertFalse(success)
        self.assertEqual(errno.EXDEV,error.errno)
        self.assertTrue(hits)
        self.assertEqual(flat,proof.read_bytes())
        self.completed([],'retry',count=0)
        self.assertFalse(changed,'recovery left the output rewritten despite failure to publish its migrated receipt')

    def test_flat_match_uses_the_publication_it_actually_validated(self):
        source=self.seed()
        self.flat()
        v=self.out.read_bytes()
        outside=h.reverse(h.load(self.out))
        outside[0]['instruct']='Fresh editor instruction, never reviewed.'
        h.dump(self.out,outside)
        editor=self.out.read_bytes()
        (self.root/'editor.json').write_bytes(editor)
        real=rev.read_validated_completed_review
        def observe(*args,**kwargs):
            with file_lock(str(self.out)):
                self.out.write_bytes(v)
            answer=real(*args,**kwargs)
            self.assertTrue(answer[0])
            self.assertEqual(h.get_review_entries_fingerprint(json.loads(v)),answer[2]['output_sha256'])
            with file_lock(str(self.out)):
                self.out.write_bytes(editor)
            return answer
        with patch.object(rev,'read_validated_completed_review',side_effect=observe):
            count,error,success=self.invoke([],'reuse')
        self.assertEqual(0,count)
        self.assertEqual(editor,self.out.read_bytes(),'flat matcher authenticated a different output version than the one rewritten')

    def test_separate_reuse_checks_saved_ancestry_through_publication(self):
        fixture=save_as.ReviewSaveAsSourceTests()
        with contextlib.ExitStack() as stack:
            data,active,upload,state,api=fixture.workspace(self.root,stack,custom=True)
            self.root=data
            name=fixture.save_copy(api,2)
            fixture.library_request(api,'load',name)
            self.src=active
            self.out=data/'export.json'
            self.completed([[{'text':'Rain fell.','speaker':'NARRATOR','instruct':'Plain.','spoken':False},
                             {'text':'Open the gate!','speaker':'MIRA','instruct':'Plain.','spoken':True}]],'fresh',count=1)
            witness=rev.get_review_source_snapshot(str(data),str(active),active.read_bytes(),h.load(active))
            self.assertIsNotNone(witness)
            identity=witness['identity']
            self.assertIn('original.json',[Path(link['source_path']).name for link in identity['sources']])
            h.dump(self.out,h.reverse(h.load(self.out)))
            old=self.out.read_bytes()
            before=self.src.read_bytes()
            def publish_parent_editor_save():
                with ensure_book_state(str(data)),file_lock(str(upload)):
                    upload.write_bytes(b'Completely different source document.')
            count,error,success=self.invoke([],'reuse',on_match=publish_parent_editor_save)
            self.assertEqual(0,count)
            self.assertEqual(before,self.src.read_bytes())
            with ensure_book_state(str(data)),self.assertRaisesRegex(ValueError,'source identity changed'):
                verify_review_source_identity(str(data),identity)
            self.assertFalse(success and self.out.read_bytes()!=old,'rebound output after its saved source evidence ceased to validate')

    def test_legacy_receipt_is_not_restored_over_a_new_checkpoint(self):
        source=h.rows(['One fell.','Two fell.','Three fell.','Four fell.'])
        h.dump(self.src,source)
        answers=[[h.reply(r['text']) for r in source[i:i+2]] for i in [0,2]]
        self.completed(answers,'fresh',count=2)
        proof=self.flat()
        h.dump(self.src,h.reverse(source))
        old=self.out.read_bytes()
        real_match=rev.get_reusable_review_completion
        save=rev.save_checkpoint
        ck=Path(str(self.out)+'.review_checkpoint.json')
        def start_new_force_review():
            cancel=self.root/'cancel-forced-review'
            def cancel_after_durable_batch(*args,**kwargs):
                result=save(*args,**kwargs)
                cancel.touch()
                return result
            with patch.object(rev,'get_reusable_review_completion',real_match),patch.object(rev,'save_checkpoint',side_effect=cancel_after_durable_batch),patch.dict(os.environ,{'ALEXANDRIA_REVIEW_CANCEL_FILE':str(cancel)}):
                count,error,success=self.invoke(answers,'new-forced-review','--force-review')
            self.assertEqual(1,count)
            self.assertIsInstance(error,SystemExit)
            self.assertFalse(success)
            self.assertFalse(proof.exists())
            self.assertEqual([2],rev.load_generation_checkpoint_document(str(ck))['source_offsets'])
        count,error,success=self.invoke([],'old-reuse',on_match=start_new_force_review)
        self.assertEqual(0,count)
        self.assertTrue(ck.exists())
        self.assertFalse(proof.exists(),'recreated a superseded completion receipt beside a newer live checkpoint')
        self.assertEqual(old,self.out.read_bytes())
        self.assertFalse(success)

if __name__=='__main__':
    unittest.main()
