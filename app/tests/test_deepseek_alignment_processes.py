import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import alexandria_alignment as alignment
import alexandria_compare as compare
from tests.test_restart_signal_tree import is_live
ROOT=Path(__file__).resolve().parents[2]

class AlignmentProcessTests(unittest.TestCase):
    def test_grouped_hundreds_preserve_both_alignment_boundaries(self):
        for phrase,number in (('one twenty three',123),('two thirteen',213),('one twenty-three',123),('one hundred twenty three',123),('twenty twenty-four',2024),('twenty five',25)):
            words=phrase.split();self.assertEqual(number,alignment._parse_number(words));self.assertTrue(alignment._num_eq(words,[str(number)]))
            self.assertEqual((0,2),alignment.trim_span_to_alignment(['in',*words],['in',str(number)],0,2))
            self.assertEqual((0,2),alignment.trim_span_to_alignment([*words,'began'],[str(number),'began'],0,2))
        for words,number in ((['one','two','three'],123),(['one','two'],102),(['one','unknown','three'],123)):
            self.assertFalse(alignment._num_eq(words,[str(number)]))

    def test_quit_preserves_pre_entry_cursor_with_and_without_accepted_prefix(self):
        for prefix in (False,True):
            with self.subTest(prefix=prefix),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                source=Path(tmp,'input.jsonl');output=Path(tmp,'out.jsonl');log=Path(tmp,'log.jsonl')
                entries=[{'text':text,'audio_filepath':'a.mp3','start':0,'end':1} for text in (['hello','world'] if prefix else ['hello'])]
                source.write_text(''.join(json.dumps(row)+'\n' for row in entries))
                answers=iter(['a','q'] if prefix else ['q']);matches=iter([(0,1,.8,False,False),(1,2,.8,False,False)] if prefix else [(0,1,.8,False,False)])
                with patch('builtins.input',side_effect=lambda *_:next(answers)),patch.object(compare,'get_alignment_match',side_effect=lambda *_a,**_k:next(matches)):
                    with self.assertRaises(SystemExit):compare.run(entries,['hello','world'] if prefix else ['hello'],['hello','world'] if prefix else ['hello'],{},0,1.,True,str(source),str(output),log,{})
                checkpoint=compare.load_checkpoint(str(source),{})
                self.assertEqual(1 if prefix else 0,checkpoint['cursor']);self.assertEqual(1 if prefix else 0,len(checkpoint['decisions']))

    def test_release_checks_all_cards_and_preserves_unknown_policy(self):
        for used,expected in (([1073741824,4294967296],1),([1073741824,1073741824],0),([1073741824,None],0),([],0)):
            with self.subTest(used=used),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);probe=root/'rocm-smi';probe.write_text('#!/bin/bash\n'+''.join(f'echo "GPU[{i}] : VRAM Total Used Memory (B): {value}"\n' for i,value in enumerate(used)));probe.chmod(0o755)
                command='source "$1"; sleep(){ :; }; STAGE_VRAM_WAIT=1; wait_for_server_vram_release'
                result=subprocess.run(['bash','-c',command,'probe',str(ROOT/'run_chains/lib/server_cleanup.sh')],env={**os.environ,'PATH':str(root)+os.pathsep+os.environ['PATH']},capture_output=True,text=True,timeout=5)
                self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
                if expected:self.assertNotIn('VRAM reclaimed',result.stdout)
                elif used and None not in used:self.assertIn('VRAM reclaimed',result.stdout)
                else:self.assertIn('unknown',result.stdout)

    def test_cleanup_refuses_the_wrapper_process_group(self):
        source = (ROOT / 'run_with_restart.sh').read_text()
        definition = re.search(r'^stop_preparer_group\(\) \{.*?^\}', source, re.M | re.S).group()
        command = definition + '\nCHILD_PGID=$(ps -o pgid= -p "$$"); CHILD_PGID=${CHILD_PGID//[[:space:]]/}; stop_preparer_group'
        result = subprocess.run(['bash', '-c', command], capture_output=True, text=True,
                                start_new_session=True, timeout=5)
        self.assertEqual(1, result.returncode)
        self.assertIn('REFUSING preparer cleanup', result.stderr)

    def test_parent_death_reaps_live_group_before_clearing_reference(self):
        source=(ROOT/'run_with_restart.sh').read_text()
        definitions='\n'.join(re.findall(r'^(?:stop_preparer_group|cleanup_preparer|run_preparer)\(\) \{.*?^\}',source,re.M|re.S))
        for crashes in (False,True):
            with self.subTest(crashes=crashes),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);pidfile=root/'child.pid';worker=root/'parent.py';shell=root/'probe.sh';unrelated=subprocess.Popen(['sleep','60'],start_new_session=True)
                worker.write_text('import os,subprocess,sys\n'+('p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nopen(sys.argv[1],"w").write(str(p.pid))\nos.kill(os.getpid(),9)\n' if crashes else 'raise SystemExit(0)\n'))
                shell.write_text(definitions+'\nPYTHON="$1";PREPARER="$2";CHILD_PGID=""\nrun_preparer "$3"\nrc=$?\nprintf "rc=%s pgid=%s\\n" "$rc" "$CHILD_PGID"\n')
                try:
                    result=subprocess.run(['bash',str(shell),sys.executable,str(worker),str(pidfile)],capture_output=True,text=True,timeout=30,start_new_session=True)
                    self.assertIn(f'rc={137 if crashes else 0} pgid=\n',result.stdout)
                    if crashes:self.assertFalse(is_live(int(pidfile.read_text())),'descendant survived retry cleanup')
                    self.assertIsNone(unrelated.poll())
                finally:
                    if pidfile.exists() and is_live(int(pidfile.read_text())):
                        os.kill(int(pidfile.read_text()),signal.SIGKILL)
                    unrelated.terminate();unrelated.wait(timeout=3)
