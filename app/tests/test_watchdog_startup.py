"""Run the actual watchdog against controlled tmux session lifecycles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class WatchdogStartupTests(unittest.TestCase):
    def _run(self, states, expected_code):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'repository with spaces')
            root.mkdir()
            script = root / 'watch_subset.sh'
            script.write_bytes((ROOT / 'watch_subset.sh').read_bytes())
            helper = root / 'run_chains/lib/subset_outputs.sh'
            helper.parent.mkdir(parents=True)
            helper.write_bytes((ROOT / 'run_chains/lib/subset_outputs.sh').read_bytes())
            binary = root / 'bin'
            binary.mkdir()
            calls = root / 'calls.jsonl'
            state = root / 'states.json'
            state.write_text(json.dumps(states))
            tmux = binary / 'tmux'
            tmux.write_text('#!'+sys.executable+'\n'
                'import json,os,pathlib,sys\n'
                "p=pathlib.Path(os.environ['STATES'])\n"
                'states=json.loads(p.read_text())\n'
                'present=states.pop(0) if states else False\n'
                'p.write_text(json.dumps(states))\n'
                "with open(os.environ['CALLS'],'a') as f:f.write(json.dumps(['tmux',present]+sys.argv[1:])+'\\n')\n"
                'raise SystemExit(0 if present else 1)\n')
            tmux.chmod(0o755)
            for name in ('sleep','notify-send'):
                path = binary / name
                path.write_text('#!'+sys.executable+'\nimport json,os,sys\n'
                    "with open(os.environ['CALLS'],'a') as f:f.write(json.dumps([os.path.basename(sys.argv[0])]+sys.argv[1:])+'\\n')\n")
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(binary)+os.pathsep+os.environ['PATH'],
                       STATES=str(state), CALLS=str(calls))
            result = subprocess.run(['bash', str(script)], cwd=tmp, env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(expected_code, result.returncode, result.stdout+result.stderr)
            output = root / 'test_corpus_output'
            records = [json.loads(line) for line in calls.read_text().splitlines()]
            log = (output / 'watchdog.log').read_text()
            if expected_code:
                self.assertFalse((output / 'DONE.flag').exists())
                self.assertFalse(any(row[0]=='notify-send' for row in records))
                self.assertNotIn('detected completion', log)
                self.assertIn('never appeared', log)
                self.assertIn('never appeared', result.stderr)
                self.assertEqual(5, sum(row[0]=='tmux' for row in records))
                self.assertEqual(5, sum(row[:2]==['sleep','2'] for row in records))
            else:
                flag = (output / 'DONE.flag').read_text()
                self.assertIn('Watchdog detected completion:', flag)
                self.assertIn('Zip files in output:         0', flag)
                self.assertEqual(1, sum(row[0]=='notify-send' for row in records))
                self.assertIn('Watchdog exiting.', log)
                if len(states)>2:
                    self.assertTrue(any(row[:2]==['sleep','60'] for row in records))
            self.assertFalse(Path(tmp,'test_corpus_output').exists())

    def test_session_never_observed_fails_without_completion_artifact(self):
        self._run([False]*5, 1)

    def test_delayed_session_is_observed_polled_and_marked_complete(self):
        self._run([False,False,True,True,False], 0)

    def test_already_running_session_can_finish_after_initial_observation(self):
        self._run([True,False], 0)
