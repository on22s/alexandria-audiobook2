import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import alexandria_compare as compare


class CompareDecisionJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.jsonl=str(self.root/'book.jsonl')
        self.identity={'source':'edition-a','jsonl':'input-a','output':'output-a'}
        self.decision={'action':'edit','text':'Edited text.','cursor_after':3,'ratio':.8}

    def start(self, decisions=None, cursor=0):
        return compare.save_checkpoint(self.jsonl,decisions or {},cursor,self.identity)

    def append(self, generation, key='0', decision=None, cursor=3):
        compare.save_decision_checkpoint(self.jsonl,key,decision or self.decision,cursor,generation)

    def load(self):
        return compare.load_checkpoint(self.jsonl,self.identity)

    def test_decision_append_recovers_and_does_not_rewrite_snapshot_or_mutate_inputs(self):
        generation=self.start();snapshot=compare.checkpoint_path(self.jsonl).read_bytes()
        before=dict(self.decision);self.append(generation)
        self.assertEqual(snapshot,compare.checkpoint_path(self.jsonl).read_bytes())
        self.assertEqual(before,self.decision)
        self.assertEqual({'0':self.decision},self.load()['decisions'])
        self.assertEqual(3,self.load()['cursor'])
        saved=json.loads(snapshot);compare.load_decision_journal(self.jsonl,saved)
        self.assertEqual({},saved['decisions'])

    def test_torn_final_record_recovers_durable_prefix_but_complete_corruption_fails_loudly(self):
        generation=self.start();self.append(generation)
        journal=compare.checkpoint_journal_path(self.jsonl);prefix=journal.read_bytes()
        journal.write_bytes(prefix+b'{"generation":"partial')
        output=io.StringIO()
        with contextlib.redirect_stdout(output):saved=self.load()
        self.assertEqual({'0':self.decision},saved['decisions'])
        self.assertIn('incomplete trailing',output.getvalue())
        self.start(saved['decisions'],saved['cursor']);self.assertFalse(journal.exists())
        for line in (b'not-json\n',b'{"generation":null}\n'):
            journal.write_bytes(line);before=journal.read_bytes()
            with self.assertRaisesRegex(SystemExit,'Invalid decision journal'):self.load()
            self.assertEqual(before,journal.read_bytes())

    def test_failed_snapshot_replacement_retains_old_snapshot_and_all_journal_decisions(self):
        generation=self.start();self.append(generation)
        cp=compare.checkpoint_path(self.jsonl);old=cp.read_bytes();journal=compare.checkpoint_journal_path(self.jsonl);old_journal=journal.read_bytes()
        with patch.object(Path,'replace',side_effect=OSError('rename failed')):
            with self.assertRaisesRegex(OSError,'rename failed'):self.start({'1':dict(self.decision,text='New')},4)
        self.assertEqual(old,cp.read_bytes());self.assertEqual(old_journal,journal.read_bytes())
        self.assertEqual({'0':self.decision},self.load()['decisions'])
        self.assertFalse(list(self.root.glob('*.tmp')))

    def test_interrupted_journal_cleanup_cannot_resurrect_undone_decisions(self):
        generation=self.start({'0':self.decision},3);self.append(generation,'1',dict(self.decision,text='Undo me'),6)
        journal=compare.checkpoint_journal_path(self.jsonl);original_unlink=Path.unlink
        def unlink(path,*args,**kwargs):
            if path==journal:raise OSError('cleanup interrupted')
            return original_unlink(path,*args,**kwargs)
        with patch.object(Path,'unlink',unlink):
            with self.assertRaisesRegex(OSError,'cleanup interrupted'):self.start({'0':self.decision},3)
        recovered=self.load()
        self.assertEqual({'0':self.decision},recovered['decisions']);self.assertEqual(3,recovered['cursor'])
        self.assertNotEqual(generation,recovered['generation'])

    def test_identity_and_missing_snapshot_refuse_before_replaying_or_writing(self):
        generation=self.start();self.append(generation)
        journal=compare.checkpoint_journal_path(self.jsonl);before=journal.read_bytes()
        with self.assertRaisesRegex(SystemExit,'different or older inputs'):
            compare.load_checkpoint(self.jsonl,dict(self.identity,source='edition-b'))
        compare.checkpoint_path(self.jsonl).unlink()
        with self.assertRaisesRegex(SystemExit,'missing.*journal exists'):self.load()
        self.assertEqual(before,journal.read_bytes())
        compare.clear_checkpoint(self.jsonl);self.assertFalse(journal.exists())
        self.assertEqual({},self.load()['decisions'])

    def test_actual_manual_loop_pauses_resumes_skips_and_undo_without_losing_decisions(self):
        entries=[{'text':word} for word in ('one','two','three')]
        output=str(self.root/'out.jsonl');log=self.root/'log.jsonl'
        def run(decisions,cursor,choices):
            with patch.object(compare,'find_best_match',side_effect=lambda *a,**k:(a[2],a[2]+1,1)), \
                 patch('builtins.input',side_effect=choices),contextlib.redirect_stdout(io.StringIO()):
                compare.run(entries,['one','two','three'],['one','two','three'],decisions,cursor,.9,True,self.jsonl,output,log,self.identity)
        with self.assertRaises(SystemExit):run({},0,['k','s','q'])
        saved=self.load();self.assertEqual(['keep','skip'],[d['action'] for d in saved['decisions'].values()])
        self.assertFalse(compare.checkpoint_journal_path(self.jsonl).exists())
        # Re-decide the skipped entry, undo it at the third entry, then re-decide.
        run(saved['decisions'],saved['cursor'],['k','u','k','k'])
        self.assertEqual(entries,compare.load_jsonl(output))
        self.assertFalse(compare.checkpoint_path(self.jsonl).exists())
        self.assertFalse(compare.checkpoint_journal_path(self.jsonl).exists())
        rows=[json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual([0,1,2],[row['entry_idx'] for row in rows])

    def test_targeted_reset_replays_journal_and_rewinds_before_discarding_selected_decision(self):
        entries=[{'text':'one'},{'text':'two'}]
        Path(self.jsonl).write_text(''.join(json.dumps(e)+'\n' for e in entries))
        output=self.root/'out.jsonl';output.write_text('{"text":"one"}\n{"text":"edited"}\n')
        generation=self.start({'0':dict(self.decision,action='keep',text='one',cursor_after=1)},1)
        self.append(generation,'1',dict(self.decision,text='edited',cursor_after=2),2)
        with contextlib.redirect_stdout(io.StringIO()):
            compare.apply_targeted_reset(self.jsonl,str(output),self.root/'log',{1},False,self.identity)
        recovered=self.load();self.assertEqual({'0'},set(recovered['decisions']));self.assertEqual(1,recovered['cursor'])
        self.assertEqual(entries,compare.load_jsonl(str(output)))
        self.assertFalse(compare.checkpoint_journal_path(self.jsonl).exists())

    def test_native_killed_manual_session_recovers_last_decision_from_journal(self):
        source=self.root/'source.txt';source.write_text('one two three four five another five words follow here')
        Path(self.jsonl).write_text('{"text":"one two three four five"}\n{"text":"another five words follow here"}\n')
        output=self.root/'out.jsonl';ready=self.root/'ready'
        bootstrap = '''import builtins,pathlib,sys
import alexandria_compare as compare
original=builtins.input
ready_path=pathlib.Path(sys.argv[1])
count=0
def choose(prompt):
 global count
 count+=1
 if count==1:return 'k'
 ready_path.write_text('ready')
 return original(prompt)
builtins.input=choose
sys.argv=['compare','--jsonl',sys.argv[2],'--source',sys.argv[3],'--output',sys.argv[4],'--review-all','--no-auto-anchor']
compare.main()
'''
        transcript=(self.root/'child.log').open('w+')
        self.addCleanup(transcript.close)
        child=subprocess.Popen([sys.executable,'-c',bootstrap,str(ready),self.jsonl,str(source),str(output)],
            stdin=subprocess.PIPE,stdout=transcript,stderr=transcript,text=True,
            env=dict(os.environ, PYTHONPATH=os.pathsep.join((
                str(Path(__file__).resolve().parents[2]),
                str(Path(__file__).resolve().parent.parent),
                os.environ.get('PYTHONPATH', '')))))
        try:
            deadline=time.monotonic()+5
            while not ready.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(.01)
            transcript.flush();transcript.seek(0)
            self.assertTrue(ready.exists(),transcript.read())
            self.assertEqual({},json.loads(compare.checkpoint_path(self.jsonl).read_text())['decisions'])
            child.kill();child.communicate(timeout=3)
            identity=compare.get_checkpoint_identity(self.jsonl,str(source),str(output))
            saved=compare.load_checkpoint(self.jsonl,identity)
            self.assertEqual({'0'},set(saved['decisions']))
            self.assertEqual('keep',saved['decisions']['0']['action'])
            self.assertEqual(5,saved['cursor'])
        finally:
            if child.poll() is None:child.kill()
            child.communicate(timeout=3)

    def test_manual_loop_preserves_unsnapshotted_auto_prefix_on_interruption(self):
        entries=[{'text':'one'},{'text':'two'}]
        with patch.object(compare,'find_best_match',side_effect=lambda *a,**k:(a[2],a[2]+1,1 if a[2]==0 else .1)), \
             patch('builtins.input',side_effect=RuntimeError('unexpected interruption')),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'unexpected interruption'):
                compare.run(entries,['one','two'],['one','two'],{},0,.9,False,self.jsonl,
                            str(self.root/'out.jsonl'),self.root/'log',self.identity)
        saved=self.load();self.assertTrue(saved['decisions']['0']['auto']);self.assertEqual(1,saved['cursor'])

    def test_failed_journal_sync_raises_and_preserves_recoverable_written_record(self):
        generation=self.start()
        with patch.object(compare.os,'fsync',side_effect=OSError('sync failed')):
            with self.assertRaisesRegex(OSError,'sync failed'):
                self.append(generation)
        saved=self.load()
        self.assertEqual({'0':self.decision},saved['decisions'])
        self.assertEqual(3,saved['cursor'])

    def test_malformed_complete_decision_is_rejected_without_changing_files(self):
        for overrides in ({'key':'not-an-index'}, {'cursor':True}, {'cursor':-1},
                          {'decision':{'action':'invented'}}):
            generation=self.start()
            record=dict(generation=generation,key='0',decision=self.decision,cursor=3)
            record.update(overrides)
            journal=compare.checkpoint_journal_path(self.jsonl)
            payload=(json.dumps(record)+'\n').encode();journal.write_bytes(payload)
            with self.assertRaisesRegex(SystemExit,'Invalid decision journal'):
                self.load()
            self.assertEqual(payload,journal.read_bytes())

    def test_legacy_snapshot_migrates_without_losing_decisions(self):
        cp=compare.checkpoint_path(self.jsonl)
        cp.write_text(json.dumps({'decisions':{'0':self.decision},'cursor':3,'identity':self.identity}))
        saved=self.load();self.assertEqual({'0':self.decision},saved['decisions'])
        generation=self.start(saved['decisions'],saved['cursor']);self.append(generation,'1',cursor=6)
        self.assertEqual({'0','1'},set(self.load()['decisions']))


class CompareReviewLogPublicationTests(unittest.TestCase):
    def test_failed_rewrite_preserves_original_and_retry_removes_only_targets(self):
        real_open = open
        real_temporary = compare.tempfile.NamedTemporaryFile
        class FailingStream:
            def __init__(self, stream, failure):
                self.stream, self.failure = stream, failure
            def __enter__(self):
                self.stream.__enter__()
                return self
            def __exit__(self, *args):
                return self.stream.__exit__(*args)
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def write(self, text):
                if self.failure == 'write':
                    raise OSError('injected write failure')
                return self.stream.write(text)
            def flush(self):
                if self.failure == 'flush':
                    raise OSError('injected flush failure')
                return self.stream.flush()
        for failure in ('write', 'flush', 'sync', 'replace'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                log = Path(tmp) / 'review.jsonl'
                rows = ['{"entry_idx":0,"text":"élève"}', 'not valid JSON',
                        '{"entry_idx":1,"text":"日本語"}', '{"entry_idx":2}']
                before = ('\n'.join(rows) + '\n').encode('utf-8')
                log.write_bytes(before)
                def broken_open(path, mode='r', *args, **kwargs):
                    stream = real_open(path, mode, *args, **kwargs)
                    return FailingStream(stream, failure) if Path(path) == log and mode == 'w' else stream
                def broken_stage(*args, **kwargs):
                    return FailingStream(real_temporary(*args, **kwargs), failure)
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(compare, 'open', side_effect=broken_open, create=True))
                    stack.enter_context(patch.object(compare.tempfile, 'NamedTemporaryFile', side_effect=broken_stage))
                    if failure == 'sync':
                        stack.enter_context(patch.object(compare.os, 'fsync', side_effect=OSError('injected sync failure')))
                    if failure == 'replace':
                        stack.enter_context(patch.object(Path, 'replace', side_effect=OSError('injected replace failure')))
                    with self.assertRaisesRegex(OSError, 'injected'):
                        compare.remove_log_entries(log, {1})
                self.assertEqual(before, log.read_bytes())
                self.assertEqual([log], list(Path(tmp).iterdir()))
                self.assertEqual(1, compare.remove_log_entries(log, {1}))
                self.assertEqual('\n'.join([rows[0], rows[1], rows[3]]) + '\n', log.read_text(encoding='utf-8'))
                self.assertEqual([log], list(Path(tmp).iterdir()))

    def test_missing_log_is_not_created_and_empty_result_is_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / 'review.jsonl'
            self.assertEqual(0, compare.remove_log_entries(log, {0}))
            self.assertFalse(log.exists())
            log.write_text('{"entry_idx":0}\n', encoding='utf-8')
            self.assertEqual(1, compare.remove_log_entries(log, {0}))
            self.assertEqual(b'', log.read_bytes())
            self.assertEqual([log], list(Path(tmp).iterdir()))
