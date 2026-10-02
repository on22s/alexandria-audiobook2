"""Native Git/Bash source selection; fixture uv stops before package/model work."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = Path(os.environ.get('INDEXTTS_CHAIN_SOURCE',
                             ROOT / 'run_chains/cloud_tts_indextts_compare_20260824.sh'))


class IndexTtsSourcePinTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()

    def prepare(self, root, cached=None):
        upstream = root / 'upstream'
        upstream.mkdir()
        self.git(upstream, 'init', '-q')
        self.git(upstream, 'config', 'user.name', 'Fixture')
        self.git(upstream, 'config', 'user.email', 'fixture@example.com')
        self.git(upstream, 'config', 'core.hooksPath', str(root / 'no-hooks'))
        (upstream / 'source.py').write_text('pinned source\n')
        self.git(upstream, 'add', 'source.py')
        self.git(upstream, 'commit', '-qm', 'pinned')
        pinned = self.git(upstream, 'rev-parse', 'HEAD')
        (upstream / 'source.py').write_text('changed upstream default\n')
        self.git(upstream, 'commit', '-qam', 'changed default')
        base = root / 'comparison'
        source = base / 'src/index-tts'
        if cached:
            source.parent.mkdir(parents=True)
            subprocess.run(['git', 'clone', '-q', str(upstream), str(source)], check=True)
            if cached != 'wrong':
                self.git(source, 'checkout', '-q', '--detach', pinned)
            if cached == 'dirty':
                (source / 'source.py').write_text('changed tracked source\n')
            if cached == 'untracked':
                (source / 'untracked.py').write_text('extra executable source\n')
        scripts = root / 'run_chains'
        scripts.mkdir()
        shutil.copyfile(ROOT / 'run_chains/cloud_comparison_provenance.py',
                        scripts / 'cloud_comparison_provenance.py')
        text = SCRIPT.read_text().replace('ROOT=/home/ubuntu/alexandria-goals-be3e7ea',
                                         f'ROOT={root}').replace(
            'BASE=/home/ubuntu/tts_comparison_20260824', f'BASE={base}')
        text = re.sub(r'SOURCE_COMMIT=[0-9a-f]{40}', f'SOURCE_COMMIT={pinned}', text)
        script = root / 'chain.sh'
        script.write_text(text)
        bin_dir = root / 'bin'
        bin_dir.mkdir()
        real_git = shutil.which('git')
        git = bin_dir / 'git'
        git.write_text('#!' + sys.executable + '\nimport os,sys\n'
                       'args=sys.argv[1:]\n'
                       'args=[os.environ["FIXTURE_UPSTREAM"] if a=="https://github.com/index-tts/index-tts.git" else a for a in args]\n'
                       'os.execv(' + repr(real_git) + ',["git",*args])\n')
        git.chmod(0o755)
        uv = bin_dir / 'uv'
        uv.write_text('#!' + sys.executable + '\nimport json,os,sys\n'
                      'open(os.environ["UV_CALL"],"w").write(json.dumps(sys.argv[1:]))\n'
                      'sys.exit(91)\n')
        uv.chmod(0o755)
        env = {**os.environ, 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH'],
               'FIXTURE_UPSTREAM': str(upstream), 'UV_CALL': str(root / 'uv.json')}
        return script, source, pinned, env

    def test_fresh_and_matching_cached_sources_reach_uv_only_at_pinned_commit(self):
        for cached in (None, 'clean'):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script, source, pinned, env = self.prepare(root, cached)
                result = subprocess.run(['bash', str(script)], env=env,
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(91, result.returncode, result.stderr)
                self.assertEqual(pinned, self.git(source, 'rev-parse', 'HEAD'))
                self.assertEqual('pinned source\n', (source / 'source.py').read_text())
                self.assertEqual(['sync', '--locked', '--all-extras'],
                                 json.loads((root / 'uv.json').read_text()))
                self.assertEqual(pinned, (root / 'comparison/indextts2/source_commit.txt').read_text().strip())

    def test_wrong_dirty_and_untracked_cached_sources_fail_before_installation(self):
        for cached in ('wrong', 'dirty', 'untracked'):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script, source, pinned, env = self.prepare(root, cached)
                before = (source / 'source.py').read_bytes()
                receipt = root / 'comparison/indextts2/source_commit.txt'
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text('prior recorded source\n')
                result = subprocess.run(['bash', str(script)], env=env,
                                        capture_output=True, text=True, timeout=15)
                self.assertNotEqual(0, result.returncode)
                self.assertNotEqual(91, result.returncode)
                self.assertFalse((root / 'uv.json').exists())
                self.assertEqual(before, (source / 'source.py').read_bytes())
                self.assertEqual('prior recorded source\n', receipt.read_text())

    def test_missing_pinned_commit_fails_before_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script, source, pinned, env = self.prepare(root)
            script.write_text(script.read_text().replace(pinned, 'f' * 40))
            result = subprocess.run(['bash', str(script)], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / 'uv.json').exists())
