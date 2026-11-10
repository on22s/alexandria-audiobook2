"""Real durable scope rollback and abrupt process death over audio/history/journal."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave
import book_state_transaction as transaction
from audio_take_history import AudioTakeHistory
from book_state_transaction import ensure_book_state, get_book_operation_token
from chunk_status_journal import ChunkStatusJournal
from project import group_into_chunks
from project_scope_admission import ensure_scope_admission
from project_scope_apply import (apply_source_scope, apply_source_scope_undo,
                                 get_source_scope_undo_preview, FILES, UNDO)
from project_scope_plan import get_scope_preview
from take_retention import get_captured_take
from tests.test_support import book_publication_mutations


class InjectedScopeFault(OSError):
    pass


def get_fixture(root):
    script = [{'speaker':name, 'text':text, 'instruct':''} for name,text in
              [('ALICE','Removed synthetic line.'), ('BOB','Retained synthetic line.')]]
    source = root/'source.txt'; source.write_text('\n'.join(row['text'] for row in script))
    state = {'active_book_id':'fixture','book_generation':'one','input_file_path':str(source)}
    rows = [dict(row,id=i,uid=f'uid-{i}',status='pending',audio_path=None,take=0)
            for i,row in enumerate(group_into_chunks(script))]
    for name,value in [('state.json',state),('annotated_script.json',script),
                       ('voice_config.json',{}),('chunks.json',rows)]:
        (root/name).write_text(json.dumps(value,indent=3))
    journal = ChunkStatusJournal(root/'chunks.json', {'active_book_id':'fixture','book_generation':'one'})
    for i in range(2):
        path = root/f'{i}.wav'
        with wave.open(str(path),'wb') as out:
            out.setnchannels(1);out.setsampwidth(2);out.setframerate(8000);out.writeframes(b'\x01\x00'*800)
        journal.apply_update(f'uid-{i}', {'status':'done','audio_path':f'{i}.wav','audio_revision':str(i)*32})
    history = AudioTakeHistory(root)
    for row in journal.get_rows():
        history.capture(root/row['audio_path'],get_book_operation_token(root),row['uid'],get_captured_take(row))


@contextmanager
def admission(root):
    with patch('core.DATA_DIR',str(root)),patch('core.process_state',{}),ensure_scope_admission(root):
        yield


def get_bytes(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not p.name.endswith('.lock') and '.task_ownership' not in p.parts
            and p.name != transaction.JOURNAL and not any(part.startswith('.book-switch-') for part in p.parts)}


def apply_operation(root, mode):
    if mode == 'apply':
        apply_source_scope(root,1,get_scope_preview(root,1)['revision'])
    else:
        apply_source_scope_undo(root,get_source_scope_undo_preview(root)['revision'])


def run_crash_child(root, mode, boundary):
    with admission(root):
        if boundary == 'committed':
            transaction._finish_transaction = lambda *args,**kwargs:os._exit(73)
            apply_operation(root, mode)
        else:
            point = int(boundary)
            def crash_after(ordinal, name, *_args):
                if ordinal == point:
                    os._exit(73)
            with book_publication_mutations(transaction, after=crash_after):
                apply_operation(root, mode)
    raise AssertionError('Requested crash boundary was not reached')


class ScopeFailureTests(unittest.TestCase):
    def mutation_points(self, mode):
        """How many post-journal mutations this protocol's publication makes."""
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();get_fixture(root)
            with admission(root):
                if mode=='undo':apply_operation(root,'apply')
                with book_publication_mutations(transaction) as state:
                    apply_operation(root,mode)
            return state['count']

    def test_staged_write_and_partial_replace_failures_restore_exact_originals(self):
        for mode in ('apply','undo'):
            points=tuple(range(1,self.mutation_points(mode)+1))
            for failure in ('write',)+points:
                with self.subTest(mode=mode,failure=failure),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp).resolve();get_fixture(root)
                    with admission(root):
                        if mode=='undo':apply_operation(root,'apply')
                        before=get_bytes(root)
                        reached=[]
                        with ExitStack() as faults:
                            if failure=='write':
                                def staging_fault(name):
                                    original=getattr(transaction,name);calls=[]
                                    def fail_write(*args,**kwargs):
                                        calls.append(name)
                                        if len(calls)==1:
                                            reached.append(name)
                                            raise InjectedScopeFault('injected scope fault')
                                        return original(*args,**kwargs)
                                    return fail_write
                                for name in ('_stage_book_stream','save_adapter_publication_bytes'):
                                    faults.enter_context(patch.object(transaction,name,staging_fault(name)))
                                expected=InjectedScopeFault
                            else:
                                def fail_at(ordinal,name,_args,failure=failure):
                                    if ordinal==failure:
                                        raise OSError('injected scope fault')
                                # Fail every post-journal mutation in turn: a v3
                                # move or a v4 exchange/placeholder create or
                                # delete (#1660), not one protocol's ordinals.
                                faults.enter_context(book_publication_mutations(
                                    transaction,before=fail_at))
                                expected=OSError
                            with self.assertRaisesRegex(expected,'injected scope fault'):
                                apply_operation(root,mode)
                        if failure=='write':self.assertEqual(1,len(reached))
                        self.assertEqual(before,get_bytes(root))
                        self.assertEqual([],list(root.glob('.book-switch-*')))

    def test_real_process_death_recovers_precommit_and_preserves_committed_apply_and_undo(self):
        for mode in ('apply','undo'):
            points=self.mutation_points(mode)
            chosen=sorted({1,max(1,points//2),points})
            for boundary in tuple(str(n) for n in chosen)+('committed',):
                with self.subTest(mode=mode,boundary=boundary),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp).resolve();get_fixture(root)
                    with admission(root):
                        if mode=='undo':apply_operation(root,'apply')
                        before=get_bytes(root)
                    command=[sys.executable,'-c',
                        'from pathlib import Path; import sys; from tests.test_project_scope_failures import run_crash_child; '
                        'run_crash_child(Path(sys.argv[1]),sys.argv[2],sys.argv[3])',str(root),mode,boundary]
                    result=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=15)
                    self.assertEqual(73,result.returncode,result.stdout+result.stderr)
                    self.assertTrue((root/transaction.JOURNAL).exists())
                    published=get_bytes(root)
                    if boundary=='committed':
                        self.assertEqual('committed',json.loads((root/transaction.JOURNAL).read_bytes())['phase'])
                        self.assertEqual(1 if mode=='apply' else 2,len(json.loads((root/'chunks.json').read_bytes())))
                        self.assertEqual(mode=='apply',(root/'.project_scope_undo.json').exists())
                        self.assertEqual(mode=='undo',(root/'chunks.json.status.jsonl').exists())
                        for name,raw in before.items():
                            if name not in FILES and name != UNDO:
                                self.assertEqual(raw,published[name])
                    with ensure_book_state(root):pass
                    self.assertEqual(published if boundary=='committed' else before,get_bytes(root))
                    self.assertFalse((root/transaction.JOURNAL).exists())
                    self.assertEqual([],list(root.glob('.book-switch-*')))
                    rows=ChunkStatusJournal(root/'chunks.json',{'active_book_id':'fixture','book_generation':'one'}).get_rows()
                    self.assertTrue(all(row['status']=='done' for row in rows))
