"""Native CPU replay CLI boundaries and concurrent private Bash list files."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(os.environ.get('REPLAY_HELPER_SOURCE', ROOT / 'app/experiments/replay_artifact.py'))
CHAIN = Path(os.environ.get('REPLAY_CHAIN_SOURCE', ROOT / 'run_chains/replay_dirty_evidence_20260817.sh'))


class ReplayArgumentSafetyTests(unittest.TestCase):
    def prepare(self, root):
        experiments = root / 'app/experiments'
        experiments.mkdir(parents=True)
        helper = experiments / 'replay_artifact.py'
        shutil.copyfile(HELPER, helper)
        data = root / 'ab_test_runtime/experiments'
        data.mkdir(parents=True)
        return helper, experiments, data

    def test_native_argv_preserves_shell_syntax_newlines_and_unicode_as_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, experiments, data = self.prepare(root)
            marker = root / 'shell-executed'
            value = f'日本語\n; touch {marker}; $(touch {marker}) `touch {marker}`'
            (experiments / 'worker.py').write_text(
                'import json,sys\nfrom pathlib import Path\n'
                'Path(sys.argv[sys.argv.index("--out")+1]).write_text(json.dumps(sys.argv[1:]))\n')
            out = root / 'result.json'
            artifact = data / 'input.json'
            artifact.write_text(json.dumps({'provenance': {'script': 'worker.py',
                'args': {'value': value, 'out': str(out)}}}))
            result = subprocess.run([sys.executable, str(helper), '--python', sys.executable,
                                     '--argv0', str(artifact)], capture_output=True, timeout=5)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(result.stdout.endswith(b'\0'))
            argv = [os.fsdecode(part) for part in result.stdout[:-1].split(b'\0')]
            subprocess.run(argv, check=True, timeout=5)
            self.assertEqual(['--out', str(out), '--value', value], json.loads(out.read_text()))
            self.assertFalse(marker.exists())

    def test_outside_producers_and_symlinks_are_not_replayable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, experiments, data = self.prepare(root)
            outside = root / 'outside.py'
            outside.write_text('raise RuntimeError("must never execute")\n')
            (experiments / 'escape.py').symlink_to(outside)
            for name in ('../../outside.py', str(outside), 'escape.py'):
                with self.subTest(name=name):
                    artifact = data / 'input.json'
                    artifact.write_text(json.dumps({'provenance': {'script': name, 'args': {}}}))
                    result = subprocess.run([sys.executable, str(helper), str(artifact)],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(1, result.returncode, result.stderr)
                    self.assertEqual('', result.stdout)
                    self.assertIn('SKIP', result.stderr)

    def test_nul_argument_is_rejected_without_partial_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, experiments, data = self.prepare(root)
            (experiments / 'worker.py').write_text('pass\n')
            artifact = data / 'input.json'
            artifact.write_text(json.dumps({'provenance': {'script': 'worker.py',
                                              'args': {'value': 'bad\0argument'}}}))
            result = subprocess.run([sys.executable, str(helper), '--argv0', str(artifact)],
                                    capture_output=True, timeout=5)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b'', result.stdout)

    def test_concurrent_native_chains_preserve_legacy_symlink_and_clean_unique_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            helper, experiments, data = self.prepare(root)
            (root / 'app/env/bin').mkdir(parents=True)
            (root / 'app/env/bin/python').symlink_to(sys.executable)
            (experiments / '__init__.py').touch()
            audit = root / 'ab_test_runtime/audit/artifact_structural_audit.json'
            audit.parent.mkdir(parents=True)
            audit.write_text('{"artifacts": []}')
            lib = root / 'run_chains/lib/stage.sh'
            lib.parent.mkdir(parents=True)
            lib.write_text('stage_summary() { return 0; }\n')
            victim = root / 'victim'
            victim.write_bytes(b'preserve unrelated file')
            legacy = root / 'replay_rest.txt'
            legacy.symlink_to(victim)
            temporary = root / 'private'
            temporary.mkdir()
            bins = root / 'bin'
            bins.mkdir()
            wrapper = bins / 'mktemp'
            wrapper.write_text('#!' + sys.executable + '\nimport os,subprocess,sys\n'
                'p=subprocess.check_output([' + repr(shutil.which('mktemp')) + ',*sys.argv[1:]],text=True).strip()\n'
                'with open(os.environ["TEMP_RECORD"],"a") as f:f.write(p+"\\n")\nprint(p)\n')
            wrapper.chmod(0o755)
            source = CHAIN.read_text().replace(
                'REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                'REPO=' + shlex.quote(str(root))).replace('/tmp/replay_rest.txt', str(legacy))
            chain = root / 'chain.sh'
            chain.write_text(source)
            env = {**os.environ, 'TMPDIR': str(temporary), 'ALEXANDRIA_GPU_LOCK_HELD': '0',
                   'PATH': str(bins) + os.pathsep + os.environ['PATH'],
                   'TEMP_RECORD': str(root / 'temp-record')}
            processes = [subprocess.Popen(['bash', str(chain)], cwd=root, env=env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
            try:
                for process in processes:
                    stdout, stderr = process.communicate(timeout=10)
                    self.assertEqual(0, process.returncode, stdout + stderr)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=3)
            self.assertEqual(b'preserve unrelated file', victim.read_bytes())
            paths = (root / 'temp-record').read_text().splitlines()
            self.assertEqual(2, len(set(paths)))
            self.assertTrue(all(Path(p).parent == temporary and not Path(p).exists() for p in paths))
