"""Native Bash selection admission and GPU/CPU dispatch order with CPU fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class SecondEnglishSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'repository with spaces'
        for directory in ('run_chains/lib', 'app/env/bin', 'bin', 'ab_test_runtime/experiments'):
            (self.root / directory).mkdir(parents=True)
        for relative in ('run_chains/second_english_eval_20260820.sh',
                         'run_chains/lib/stage.sh', 'run_chains/lib/server_cleanup.sh'):
            shutil.copyfile(REPO / relative, self.root / relative)
        self.write('bin/pgrep', '#!/bin/sh\nexit 1\n')
        self.write('gpu_job.sh', '#!/bin/bash\nshift\nexec "$@"\n')
        self.write('app/env/bin/python', '#!' + sys.executable + '\n' + r'''
import json,os,pathlib,sys
args=sys.argv[1:]
if args[0]=='-':
 sys.argv=args
 exec(compile(sys.stdin.read(),'<actual selection validator>','exec'))
 sys.exit(0)
if args[0]=='-u':args=args[1:]
name=pathlib.Path(args[0]).name
if name=='check_artifact_shrinkage.py':sys.exit(0)
with open(os.environ['FIXTURE_CALLS'],'a') as stream:stream.write(json.dumps(args)+'\n')
if name=='ljspeech_generate.py' and os.environ.get('FAIL_GENERATION')=='1':sys.exit(4)
if name=='library_eval_build.py' and os.environ.get('FAIL_BUILD')=='1':sys.exit(6)
if name=='ljspeech_generate.py' and os.environ.get('MUTATE_SELECTION')=='1':
 pathlib.Path('ab_test_runtime/second_english_voices.txt').write_text('changed|data/changed|nan\n')
if '--out' in args:
 output=pathlib.Path(args[args.index('--out')+1]);output.parent.mkdir(parents=True,exist_ok=True);output.write_text('{}')
''')
        self.env = dict(os.environ, PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'],
                        FIXTURE_CALLS=str(self.root / 'calls'))
        self.write('ab_test_runtime/second_english_voices.txt', 'alpha|data/alpha|0.555\nbeta|data/beta|0.781\n')
        for arguments in (('init', '-q', '-b', 'main'), ('config', 'user.name', 'Fixture'),
                          ('config', 'user.email', 'fixture@example.com'),
                          ('config', 'core.hooksPath', str(self.root / 'no-hooks')),
                          ('add', '.'), ('commit', '-qm', 'baseline')):
            subprocess.run(['git', '-C', str(self.root), *arguments], check=True, capture_output=True)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755)

    def run_chain(self, selection=None):
        if selection is not None:
            self.write('ab_test_runtime/second_english_voices.txt', selection)
            subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'selection'], capture_output=True)
        result = subprocess.run(['bash', str(self.root / 'run_chains/second_english_eval_20260820.sh')],
                                cwd=self.root, env=self.env, capture_output=True, text=True, timeout=15)
        calls = self.root / 'calls'
        return result, [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    def test_invalid_later_score_refuses_before_any_worker(self):
        for score in ('not-a-number', 'nan', 'inf', '1.01', '-1.01', ''):
            with self.subTest(score=score):
                calls_path = self.root / 'calls'
                calls_path.unlink(missing_ok=True)
                result, calls = self.run_chain('alpha|data/alpha|0.555\nbeta|data/beta|' + score + '\n')
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual([], calls, 'invalid selection consumed worker time')

    def test_duplicate_voice_or_adapter_refuses_before_workers(self):
        for selection in ('alpha|data/alpha|0.555\nalpha|data/beta|0.781\n',
                          'alpha|data/alpha|0.555\nbeta|data/alpha|0.781\n'):
            with self.subTest(selection=selection):
                (self.root / 'calls').unlink(missing_ok=True)
                result, calls = self.run_chain(selection)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual([], calls)

    def test_generation_batches_before_cpu_scoring(self):
        result, calls = self.run_chain()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        workers = [Path(args[0]).name for args in calls]
        generation = [index for index, name in enumerate(workers) if name == 'ljspeech_generate.py']
        scoring = [index for index, name in enumerate(workers) if name == 'prosody_fidelity.py']
        self.assertEqual(2, len(generation))
        self.assertEqual(2, len(scoring))
        self.assertLess(max(generation), min(scoring))
        self.assertIn('identity score 0.555', result.stdout)

    def test_equal_scores_for_distinct_voices_are_valid(self):
        result, calls = self.run_chain('alpha|data/alpha|0.555\nbeta|data/beta|0.555\n')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(2, sum(Path(args[0]).name == 'ljspeech_generate.py' for args in calls))

    def test_failed_generation_does_not_score_stale_output(self):
        self.env['FAIL_GENERATION'] = '1'
        self.write('ab_test_runtime/experiments/second_english__alpha_generate.json', '{"stale":true}')
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '-qm', 'old output'], check=True, capture_output=True)
        result, calls = self.run_chain()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(2, sum(Path(args[0]).name == 'ljspeech_generate.py' for args in calls))
        self.assertFalse(any(Path(args[0]).name == 'prosody_fidelity.py' for args in calls))

    def test_failed_build_is_in_failure_summary_and_blocks_dependent_work(self):
        self.env['FAIL_BUILD'] = '1'
        result, calls = self.run_chain()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(2, sum(Path(args[0]).name == 'library_eval_build.py' for args in calls))
        self.assertFalse(any(Path(args[0]).name in ('ljspeech_generate.py', 'prosody_fidelity.py')
                             for args in calls))
        self.assertIn('SUMMARY', result.stdout)

    def test_empty_malformed_or_missing_selection_refuses(self):
        for selection in ('', '\n', 'alpha|data/alpha|0.55|extra\n', '|data/alpha|0.55\n'):
            with self.subTest(selection=selection):
                (self.root / 'calls').unlink(missing_ok=True)
                result, calls = self.run_chain(selection)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual([], calls)
        (self.root / 'ab_test_runtime/second_english_voices.txt').unlink()
        result, calls = self.run_chain()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], calls)

    def test_original_eight_voice_selection_preserves_all_adapters(self):
        selection = (REPO / 'ab_test_runtime/second_english_voices.txt').read_text()
        result, calls = self.run_chain(selection.rstrip('\n'))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        expected = [line.split('|')[1] + '/adapter' for line in selection.splitlines() if line.strip()]
        generated = [args[args.index('--adapter') + 1] for args in calls
                     if Path(args[0]).name == 'ljspeech_generate.py']
        self.assertEqual([str(self.root / adapter) for adapter in expected], generated)
        self.assertEqual(8, len(generated))

    def test_validated_snapshot_survives_input_replacement_during_generation(self):
        self.env['MUTATE_SELECTION'] = '1'
        result, calls = self.run_chain()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        generated = [args for args in calls if Path(args[0]).name == 'ljspeech_generate.py']
        scored = [args for args in calls if Path(args[0]).name == 'prosody_fidelity.py']
        self.assertEqual(2, len(generated))
        self.assertEqual(2, len(scored))
        self.assertTrue(generated[1][generated[1].index('--adapter') + 1].endswith('data/beta/adapter'))
        self.assertEqual([], list((self.root / 'ab_test_runtime/second_english_eval').glob('.voice-selection.*')))
