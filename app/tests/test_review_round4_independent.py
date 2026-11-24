"""Round-4 synthetic review-entrypoint checks. Run with ./run-python independent "$PWD/independent.py" -v."""
import contextlib
import hashlib
import itertools
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_script
import review_script as rev
from completed_review_receipt import get_review_progress_paths, get_review_entries_fingerprint
from tests.test_pass_boundary_retry_regressions import client_for
from tests import test_review_source_spans as spans
from utils import atomic_json_write, file_lock

HERE = Path(__file__).resolve().parent
ARTIFACTS = Path(tempfile.mkdtemp(prefix='aa2-round4-'))
print(f'Artifacts {ARTIFACTS}', flush=True)
LAYOUTS = list(itertools.permutations(['text','speaker','instruct','custom']))
def rows(texts, speaker='MIRA'):
    result = []
    for i, text in enumerate(texts):
        values = {'speaker': speaker, 'text': text, 'instruct': 'Old.', 'custom': {'z': 2, 'a': 1}}
        result.append({key: values[key] for key in LAYOUTS[i*5 % 24]})
    return result

def reply(text, speaker='MIRA'):
    return {'text': text, 'speaker': speaker, 'instruct': 'Reviewed.'}

def dump(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')

def load(p):
    return json.loads(p.read_bytes())

def keys(rows):
    return [list(r) for r in rows]

def reverse(rows):
    return [dict(reversed(list(r.items()))) for r in rows]

def digest(data):
    return hashlib.sha256(data).hexdigest()

class Independent(unittest.TestCase):
    def setUp(self):
        self.root = ARTIFACTS / self._testMethodName
        self.root.mkdir()
        self.steps = []
        self.src = self.root/'input.json'
        self.out = self.root/'output.json'

    def invoke(self, replies, name, *options, on_match=None):
        client, calls = client_for([(batch, 'stop') for batch in replies])
        matcher = rev.get_reusable_review_completion
        def matched(*args, **kwargs):
            answer = matcher(*args, **kwargs)
            self.assertTrue(answer[0], answer)
            on_match()
            return answer
        with spans.ReviewSourceSpanTests().cli(str(self.root), client) as log, contextlib.ExitStack() as hooks:
            hooks.enter_context(patch.object(rev.sys, 'argv', ['review', '--input', str(self.src), '--output', str(self.out), *options]))
            hooks.enter_context(patch.object(generate_script, 'get_response_log_path', return_value=str(self.root/'responses.log')))
            if on_match:
                hooks.enter_context(patch.object(rev,'get_reusable_review_completion',side_effect=matched))
            try:
                rev.main()
                exc = None
            except (Exception, SystemExit) as error:
                exc = error
        (self.root / (name+'.log')).write_text(log.getvalue())
        checkpoint, receipt = map(Path, get_review_progress_paths(str(self.out)))
        step = dict(name=name, calls=len(calls), exception=repr(exc), claimed='Task review completed successfully.' in log.getvalue())
        for label, path in [('input',self.src),('output',self.out),('receipt',receipt),('checkpoint',checkpoint)]:
            if path.is_file():
                data=path.read_bytes()
                (self.root / (name+'-'+label+'.json')).write_bytes(data)
                step[label] = digest(data)
        if self.out.exists():
            step['layouts'] = keys(load(self.out))
        self.steps.append(step)
        dump(self.root/'steps.json', self.steps)
        return len(calls), exc, step['claimed']

    def completed(self, replies, name, *options, count):
        self.assertEqual((count,None,True), self.invoke(replies,name,*options))
        return self.out.read_bytes()

    def test_chain_and_rotated_source(self):
        source = rows(['Ash.','Bell.','bell!','Cedar. Dawn.','Edge.','Frost.','frost!','Gale. Hush.'])
        dump(self.src,source)
        replies = [[reply('Ash. Bell.')], [reply('bell!'),reply('Cedar.'),reply('Dawn.')],
                   [reply('Edge. Frost.')],[reply('frost!'),reply('Gale.'),reply('Hush.')]]
        parents = [1,2,3,3,5,6,7,7]
        first = self.completed(replies,'fresh',count=4)
        self.assertEqual(keys([source[i] for i in parents]), keys(json.loads(first)))
        self.assertEqual(first, self.completed([], 'reuse',count=0))
        self.assertEqual(self.steps[0]['receipt'],self.steps[1]['receipt'])
        self.assertEqual(first, self.completed(replies, 'force','--force-review',count=4))
        source = reverse(source)
        dump(self.src,source)
        rotated = self.completed([], 'rotated',count=0)
        self.assertEqual(keys([source[i] for i in parents]),keys(json.loads(rotated)))
        self.assertEqual(json.loads(first),json.loads(rotated))
        self.assertEqual(rotated,self.completed([], 'rotated-again',count=0))

    def test_post_narrator_merge_idempotence(self):
        source = rows(['Ash fell.','Echo faded.','echo faded!'],'NARRATOR')
        dump(self.src,source)
        answers=[[reply(source[0]['text'],'NARRATOR'),reply(source[1]['text'],'NARRATOR')],[reply(source[2]['text'])]]
        first=self.completed(answers,'fresh',count=2)
        # A NARRATOR run legitimately keeps its first row's layout; reuse,
        # fresh and --force-review all agree on it (#2257 round 5).
        self.assertEqual(keys([source[0],source[2]]),keys(json.loads(first)))
        again=self.completed([],'reuse',count=0)
        forced=self.completed(answers,'force','--force-review',count=2)
        self.assertEqual(first,forced)
        self.assertEqual(json.loads(first),json.loads(again))
        self.assertEqual(first,again,'zero-call reuse changed a post-processed narrator merge')

    def test_punctuation_only_row_before_spoken_line(self):
        source=rows(['…','Come closer.'])
        dump(self.src,source)
        answers=[[reply(r['text']) for r in source]]
        first=self.completed(answers,'fresh',count=1)
        self.assertEqual(keys(source),keys(json.loads(first)))
        again=self.completed([],'reuse',count=0)
        self.assertEqual(first,self.completed(answers,'force','--force-review',count=1))
        self.assertEqual(json.loads(first),json.loads(again))
        self.assertEqual(first,again,'punctuation acquired the next row layout')

    def test_native_checkpoint_ignores_unprocessed_suffix(self):
        source=rows(['A copper','bell rang.','A','copper bell rang.'])
        self.src=self.out=self.root/'annotated_script.json'
        dump(self.src,source)
        opens=[[reply('A'),reply('copper bell rang.')]]
        tail=[[reply(r['text']) for r in source[2:]]]
        expected=keys(source)
        fresh=self.completed(opens+tail,'control',count=2)
        self.assertEqual(expected,keys(json.loads(fresh)))
        dump(self.src,source)
        bad=[reply('Unrelated.')]
        self.assertEqual((4,None,False),self.invoke(opens+[bad,bad,bad],'partial','--force-review'))
        proof=rev.load_generation_checkpoint_document(str(self.out)+'.review_checkpoint.json')
        self.assertEqual([2,4],proof['source_offsets'])
        self.assertEqual([2],proof['failed_batches'])
        self.assertEqual(expected[:2],keys(proof['all_corrected'][:2]))
        final=self.completed(tail,'resumed',count=1)
        self.assertEqual(json.loads(fresh),json.loads(final))
        self.assertEqual(fresh,final,'retained batch acquired key orders from the unprocessed batch')

    def test_duplicate_in_next_batch_does_not_defeat_checkpoint_rebind(self):
        self.src=self.out=self.root/'annotated_script.json'
        source=rows(['A bell rang.','She stopped.','A bell rang.','Then silence.'])
        dump(self.src,source)
        bad=[reply('Unrelated.')]
        answers=[[reply(r['text']) for r in source[:2]]]
        self.assertEqual((4,None,False),self.invoke(answers+[bad,bad,bad],'partial'))
        rotated=reverse(source)
        dump(self.src,rotated)
        self.completed([[reply(r['text']) for r in rotated[2:]]],'resumed',count=1)
        self.assertEqual(keys(rotated),keys(load(self.src)), 'proven retained prefix was left in the obsolete layout')

    def seed(self):
        source=rows(['A bell rang.'])
        dump(self.src,source)
        self.completed([[reply(source[0]['text'])]],'fresh',count=1)
        return source
    def edit(self, path, doc):
        with file_lock(str(path)):
            atomic_json_write(doc,str(path),allow_nonatomic_fallback=False)
        (self.root/'editor.json').write_bytes(path.read_bytes())

    def test_same_file_completion_after_editor_save(self):
        directory = self.root/'standalone'
        directory.mkdir()
        link = self.root/'linked'
        link.symlink_to(directory, target_is_directory=True)
        self.src,self.out = directory/'book.json',link/'book.json'
        self.seed()
        doc=load(self.src)
        doc[0]['text']='The editor added a new sentence.'
        count,error,success=self.invoke([],'reuse',on_match=lambda:self.edit(self.src,doc))
        self.assertEqual(0,count)
        self.assertEqual((self.root/'editor.json').read_bytes(),self.src.read_bytes())
        fact=load(Path(str(self.out)+'.review_completed.json'))['facts']['forward']
        self.assertNotEqual(fact['output_sha256'],get_review_entries_fingerprint(load(self.out)))
        self.assertFalse(success,'reported a completed review against a different script')

    def test_key_order_only_editor_save_after_receipt_match(self):
        source=self.seed()
        dump(self.src,reverse(source))
        doc=load(self.out)
        old=doc[0]
        rotated_keys=list(old)[1:]+list(old)[:1]
        editor=[{k:old[k] for k in rotated_keys}]
        count,error,success=self.invoke([], 'reuse',on_match=lambda:self.edit(self.out,editor))
        self.assertEqual(0,count)
        self.assertEqual((self.root/'editor.json').read_bytes(),self.out.read_bytes(),'overwrote an edit between receipt match and output lock')

    def test_editor_changes_separate_input_while_output_locked(self):
        source=self.seed()
        dump(self.src,reverse(source))
        before=self.out.read_bytes()
        matcher=rev.get_review_source_order_templates
        editor=reverse(source)
        editor[0]['text']='New words saved by the editor.'
        fired=[]
        def edit_after_source_read(*args):
            # The writer uses the application's input lock. Reuse already owns its output lock here.
            self.edit(self.src,editor)
            fired.append(True)
            return matcher(*args)
        with patch.object(rev,'get_review_source_order_templates',side_effect=edit_after_source_read):
            count,error,success=self.invoke([],'reuse')
        self.assertEqual([True],fired)
        self.assertEqual(0,count)
        self.assertEqual((self.root/'editor.json').read_bytes(),self.src.read_bytes())
        self.assertFalse(success and self.out.read_bytes()!=before,'rewrote the publication after its source proof became stale')

    def test_cancel_between_transient_rename_attempts(self):
        source=self.seed()
        dump(self.src,reverse(source))
        old=self.out.read_bytes()
        cancel=self.root/'cancel.signal'
        rename=os.replace
        attempts=[]
        def schedule(src,dst,*args,**kwargs):
            if Path(dst)==self.out:
                attempts.append(True)
                if len(attempts)==1:
                    cancel.touch()
                    raise PermissionError('The process cannot access the file')
            return rename(src,dst,*args,**kwargs)
        with patch.dict(os.environ,{'ALEXANDRIA_REVIEW_CANCEL_FILE':str(cancel)}),patch.object(os,'replace',side_effect=schedule):
            count,error,success=self.invoke([],'reuse')
        self.assertTrue(attempts)
        self.assertEqual(0,count)
        self.assertEqual(old,self.out.read_bytes(),'published a layout after cancellation during rename retry')
        self.assertFalse(success)
        self.assertEqual(self.steps[0]['receipt'], self.steps[-1]['receipt'])

if __name__=='__main__':
    unittest.main()
