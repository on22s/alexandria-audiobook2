"""The real launcher Reset clears one book in both data-root layouts (#2184)."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from book_recovery_inventory import CHECKPOINTS, STATIC_ARTIFACTS
from export_file_io import (EXPORT_RETIREMENT_DIRECTORY, PUBLICATION_JOURNAL,
                            PUBLICATION_SET_JOURNAL)
from project_scope_exports import EXPORTS, PUBLICATIONS_FILE
from reset_artifact_roots import DIRECTORY_ARTIFACTS, OWNED_NAMES
from sound_design_registry import ACTIVE_CUES
from sound_design_renditions import ACTIVE_RENDITIONS

ROOT = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get('AA2_TEST_PYTHON', sys.executable)
OLD = b'OLD PREVIOUS BOOK BYTES'
HARNESS = r'''
const fs=require('node:fs'),path=require('node:path');
const root=process.argv[2],python=process.argv[3];
const steps=require(path.join(root,'reset.js')).run;
const Lifecycle=require(path.join(root,'launcher_lifecycle.js')),helper=new Lifecycle();
const req={parent:{path:path.join(root,'reset.js')}};
const events=[],kernel={which:()=>python,api:{stop:async r=>events.push('stop:'+path.basename(r.params.uri))}};
(async()=>{
 let receipt=null;
 for(const step of steps){
  if(step.uri!=='launcher_lifecycle.js'){throw new Error('unexpected Reset mutation')}
  const outcome=await helper[step.method](req,()=>{},kernel);events.push(step.method);
  if(step.method==='clear_active_book'){receipt=outcome}
 }
 console.log(JSON.stringify({events,receipt}));
})().catch(error=>{console.error(error);process.exitCode=1});
'''
IO_BLOCKER = '''import os, shutil
_real = shutil.rmtree
def _blocked(path, *args, **kwargs):
    if (os.environ.get('AA2_BLOCK_RMTREE') == os.path.basename(os.fspath(path))):
        raise PermissionError(13, 'Permission denied', os.fspath(path))
    return _real(path, *args, **kwargs)
shutil.rmtree = _blocked
'''
PRESERVED = {
    'scripts/saved.json': b'SAVED BOOK',
    'voice_library.json': b'REUSABLE CAST',
    'uploads/source.wav': b'UPLOAD',
    'builtin_lora/voice/adapter_model.safetensors': b'ADAPTER',
    'pronunciation.json': b'SHARED LEXICON',
    'take_history/manifest.json': b'SHARED TAKE HISTORY',
    '.sound_design_assets/manifest.json': b'SHARED ASSETS',
    '.unrelated.json': b'UNRELATED',
}


def make_install(install):
    install.mkdir(parents=True)
    (install / 'app').mkdir()
    for source in (ROOT / 'app').glob('*.py'):
        shutil.copyfile(source, install / 'app' / source.name)
    shutil.copyfile(ROOT / 'gpu_stats.py', install / 'gpu_stats.py')
    for name in ('reset.js', 'launcher_lifecycle.js'):
        shutil.copyfile(ROOT / name, install / name)
    (install / 'harness.js').write_text(HARNESS)


def write_artifact(root, name, payload=OLD):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if name in DIRECTORY_ARTIFACTS:
        path.mkdir(exist_ok=True)
        (path / 'fixture.bin').write_bytes(payload)
    else:
        path.write_bytes(b'{}' if name == PUBLICATIONS_FILE else payload)
    return path


def seeded_names():
    # The two in-flight export journals are validated by ensure_book_state
    # before Reset; a fabricated record would be refused as real damage.
    return sorted(OWNED_NAMES - {PUBLICATION_JOURNAL, PUBLICATION_SET_JOURNAL})


def seed(root, install):
    for name in seeded_names():
        write_artifact(root, name)
    config = install / 'app' / 'config.json' if root == install else root / 'config.json'
    write_artifact(config.parent, config.name)
    for name, payload in PRESERVED.items():
        write_artifact(root, name, payload)
    return config


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*')
            if p.is_file() and not p.name.endswith('.lock')
            and '__pycache__' not in p.parts}


def run_reset(install, python, configured_root=None, blocked=None):
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    env['PYTHONPATH'] = str(install / 'app') + os.pathsep + env.get('PYTHONPATH', '')
    if configured_root is not None:
        env['ALEXANDRIA_DATA_DIR'] = str(configured_root)
    if blocked is None:
        env.pop('AA2_BLOCK_RMTREE', None)
    else:
        env['AA2_BLOCK_RMTREE'] = blocked
    return subprocess.run(['node', str(install / 'harness.js'), str(install), python],
                          capture_output=True, text=True, timeout=90, env=env,
                          cwd=str(install))


class ResetBookArtifactTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.install = self.tmp / 'install'
        make_install(self.install)

    def test_canonical_set_covers_companions_and_all_exports_without_shared_data(self):
        from script_selection_binding import BINDING_NAME
        required = {name for name in STATIC_ARTIFACTS if not name.startswith('take_history/')}
        required |= set(CHECKPOINTS) | set(EXPORTS) | {
            ACTIVE_CUES, ACTIVE_RENDITIONS, BINDING_NAME, PUBLICATIONS_FILE,
            PUBLICATION_JOURNAL, PUBLICATION_SET_JOURNAL, EXPORT_RETIREMENT_DIRECTORY,
            'voicelines', 'chunks.json.status.jsonl',
        }
        self.assertFalse(required - OWNED_NAMES, sorted(required - OWNED_NAMES))
        for name in ('scripts', 'voice_library.json', 'uploads', 'builtin_lora',
                     'pronunciation.json', 'take_history/manifest.json',
                     '.sound_design_assets/manifest.json', 'voices.json'):
            self.assertNotIn(name, OWNED_NAMES)

    def _assert_cleared(self, root, config, result):
        self.assertEqual(0, result.returncode, result.stderr[:800])
        report = json.loads(result.stdout)
        self.assertEqual(['stop_writers', 'recover_active_book', 'clear_active_book'],
                         report['events'][-3:])
        removed = set(report['receipt']['removed'])
        self.assertIn('state.json', removed)
        self.assertIn('audiobook.m4b', removed)
        self.assertEqual([], report['receipt']['skipped'])
        self.assertEqual([], report['receipt']['failed'])
        for name in OWNED_NAMES:
            self.assertFalse((root / name).exists(), name)
        self.assertFalse(config.exists(), 'config.json')
        for name, payload in PRESERVED.items():
            self.assertEqual(payload, (root / name).read_bytes(), name)

    def test_default_root_real_reset_clears_book_and_keeps_shared_files(self):
        config = seed(self.install, self.install)
        before = sorted(name for name in seeded_names() if (self.install / name).exists())
        self.assertEqual(seeded_names(), before)
        self._assert_cleared(self.install, config, run_reset(self.install, PYTHON))

    def test_configured_root_real_reset_clears_book_and_its_own_config(self):
        root = self.tmp / 'data'
        root.mkdir()
        config = seed(root, self.install)
        self.assertFalse((self.install / 'config.json').exists())
        self.assertEqual(seeded_names(),
                         sorted(name for name in seeded_names() if (root / name).exists()))
        result = run_reset(self.install, PYTHON, configured_root=root)
        self.assertEqual(0, result.returncode, result.stderr[:800])
        report = json.loads(result.stdout)
        self.assertEqual(str(root), report['receipt']['root'])
        self._assert_cleared(root, config, result)
        self.assertFalse((root / 'config.json').exists())
        self.assertFalse((self.install / 'app' / 'config.json').exists())

    def test_invalid_recovery_evidence_refuses_before_any_removal(self):
        config = seed(self.install, self.install)
        (self.install / '.active_book_transaction.json').write_bytes(b'not valid JSON')
        before = snapshot(self.install)
        result = run_reset(self.install, PYTHON)
        self.assertNotEqual(0, result.returncode, result.stdout[:800])
        after = snapshot(self.install)
        self.assertEqual(before, after)
        self.assertTrue(config.exists())

    def test_held_book_lock_refuses_before_any_removal(self):
        config = seed(self.install, self.install)
        code = ('import sys,time\n'
                'from book_state_transaction import ensure_book_state\n'
                'print("held",flush=True)\n'
                'with ensure_book_state(sys.argv[1]):\n'
                ' time.sleep(30)\n')
        holder = subprocess.Popen(
            [PYTHON, '-c', code, str(self.install)], cwd=str(self.install / 'app'),
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual('held', holder.stdout.readline().strip())
        holder.stdout.close()
        try:
            before = snapshot(self.install)
            result = run_reset(self.install, PYTHON)
            self.assertNotEqual(0, result.returncode, result.stdout[:800])
            after = snapshot(self.install)
            self.assertEqual(before, after)
            self.assertTrue(config.exists())
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stderr.close()

    def _case(self, configured):
        tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        install = tmp / 'install'
        make_install(install)
        root = tmp / 'data' if configured else install
        if configured:
            root.mkdir()
        return install, root

    def test_malformed_checkpoint_shard_refuses_before_any_removal(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                install, root = self._case(configured)
                config = seed(root, install)
                shard = root / (CHECKPOINTS[0] + '.parts-' + 'a' * 32)
                shard.write_bytes(b'not a shard directory')
                before = snapshot(root)
                result = run_reset(install, PYTHON, root if configured else None)
                self.assertNotEqual(0, result.returncode, result.stdout[:800])
                self.assertIn('Invalid generation checkpoint shard ownership', result.stderr)
                self.assertEqual(before, snapshot(root))
                self.assertTrue(config.exists())

    def test_io_failure_receipt_retains_fence_and_retry_finishes(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                install, root = self._case(configured)
                config = seed(root, install)
                (install / 'app' / 'sitecustomize.py').write_text(IO_BLOCKER)
                first = run_reset(install, PYTHON, root if configured else None,
                                  blocked='chapter_exports')
                self.assertEqual(0, first.returncode, first.stderr[:800])
                receipt = json.loads(first.stdout)['receipt']
                self.assertIn('chapter_exports', receipt['failed'])
                self.assertIn('chapter_exports', receipt['retained_exports'])
                self.assertNotIn('chapter_exports', receipt['removed'])
                self.assertIn(PUBLICATIONS_FILE, receipt['skipped'])
                self.assertIn('state.json', receipt['skipped'])
                self.assertNotIn(PUBLICATIONS_FILE, receipt['removed'])
                self.assertTrue((root / 'chapter_exports').exists())
                self.assertTrue((root / PUBLICATIONS_FILE).exists())
                self.assertTrue((root / 'state.json').exists())
                self.assertFalse(config.exists())
                self.assertEqual(b'SHARED LEXICON', (root / 'pronunciation.json').read_bytes())
                second = run_reset(install, PYTHON, root if configured else None)
                self.assertEqual(0, second.returncode, second.stderr[:800])
                retry = json.loads(second.stdout)['receipt']
                self.assertEqual([], retry['failed'])
                self.assertEqual([], retry['retained_exports'])
                self.assertIn('chapter_exports', retry['removed'])
                self.assertIn(PUBLICATIONS_FILE, retry['removed'])
                self.assertIn('state.json', retry['removed'])
                self.assertFalse((root / 'chapter_exports').exists())
                self.assertFalse((root / PUBLICATIONS_FILE).exists())
                self.assertFalse((root / 'state.json').exists())

    def test_protected_aliases_are_skipped_without_touching_their_targets(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                install, root = self._case(configured)
                config = seed(root, install)
                outside = self.tmp / ('outside-' + str(configured))
                outside.mkdir()
                (outside / 'target.json').write_bytes(b'OUTSIDE FILE')
                (outside / 'targetdir').mkdir()
                (outside / 'targetdir' / 'x').write_bytes(b'OUTSIDE DIR FILE')
                (root / 'state.json').unlink()
                (root / 'state.json').symlink_to(outside / 'target.json')
                shutil.rmtree(root / 'chapter_exports')
                (root / 'chapter_exports').symlink_to(outside / 'targetdir',
                                                     target_is_directory=True)
                result = run_reset(install, PYTHON, root if configured else None)
                self.assertEqual(0, result.returncode, result.stderr[:800])
                receipt = json.loads(result.stdout)['receipt']
                self.assertIn('state.json', receipt['skipped'])
                self.assertIn('chapter_exports', receipt['skipped'])
                self.assertIn('annotated_script.json', receipt['skipped'])
                self.assertTrue((root / 'state.json').is_symlink())
                self.assertTrue((root / 'chapter_exports').is_symlink())
                self.assertEqual(b'OUTSIDE FILE', (outside / 'target.json').read_bytes())
                self.assertEqual(b'OUTSIDE DIR FILE',
                                 (outside / 'targetdir' / 'x').read_bytes())
                self.assertTrue((root / 'annotated_script.json').exists())
                self.assertFalse(config.exists())

    def test_fence_authority_survives_partial_cleanup_and_retry(self):
        import hashlib
        from audio_take_history import _version
        from book_state_transaction import get_book_operation_identity
        from editor_poll_snapshot import get_snapshot_digest
        import project_scope_exports as pse

        for configured in (False, True):
            with self.subTest(configured=configured):
                install, root = self._case(configured)
                seed(root, install)
                source = root / 'source.txt'
                source.write_text('Synthetic source line.')
                state = {'active_book_id': 'oldbook', 'book_generation': 'oldgen',
                         'input_file_path': str(source),
                         '_source_scope_exports': {name: {} for name in EXPORTS}}
                (root / 'state.json').write_text(json.dumps(state))
                chapter_dir = root / 'chapter_exports'
                old = chapter_dir / 'one.mp3'
                old.write_bytes(b'RETAINED CHAPTER BYTES')
                modern = chapter_dir / 'modern.mp3'
                modern.write_bytes(b'MODERN CURRENT BYTES')
                legacy = root / 'legacy.mp3'
                legacy.write_bytes(b'LEGACY NO FENCE')
                token = get_snapshot_digest(get_book_operation_identity(
                    str(root), state=pse.read_book_state_bounded(str(root))))
                receipt = {'book_token': token, 'scope_token': None,
                           'sha256': hashlib.sha256(modern.read_bytes()).hexdigest(),
                           'file_version': list(_version(modern.stat()))}
                (root / PUBLICATIONS_FILE).write_text(json.dumps(
                    {modern.relative_to(root).as_posix(): receipt}))
                identity = pse.get_export_reader_identity(str(root))
                self.assertTrue(pse.is_scope_export_historical(str(root), old))
                with self.assertRaises(Exception):
                    pse.require_scope_export_current(str(root), old)
                pse.require_scope_export_current(str(root), modern)
                state_before = (root / 'state.json').read_bytes()
                ledger_before = (root / PUBLICATIONS_FILE).read_bytes()

                (install / 'app' / 'sitecustomize.py').write_text(IO_BLOCKER)
                first = run_reset(install, PYTHON, root if configured else None,
                                  blocked='chapter_exports')
                self.assertEqual(0, first.returncode, first.stderr[:800])
                report = json.loads(first.stdout)['receipt']
                self.assertIn('chapter_exports', report['failed'])
                self.assertEqual(b'RETAINED CHAPTER BYTES', old.read_bytes())
                self.assertEqual(b'MODERN CURRENT BYTES', modern.read_bytes())
                self.assertEqual(b'LEGACY NO FENCE', legacy.read_bytes())
                self.assertEqual(state_before, (root / 'state.json').read_bytes())
                self.assertEqual(ledger_before, (root / PUBLICATIONS_FILE).read_bytes())
                self.assertEqual(identity, pse.get_export_reader_identity(str(root)))
                self.assertTrue(pse.is_scope_export_historical(str(root), old))
                with self.assertRaises(Exception):
                    pse.require_scope_export_current(str(root), old)
                with self.assertRaises(Exception):
                    with pse.open_current_export_reader(str(root), old, expected=identity) as stream:
                        stream.read()
                pse.require_scope_export_current(str(root), modern)
                with pse.open_current_export_reader(str(root), modern, expected=identity) as stream:
                    self.assertEqual(b'MODERN CURRENT BYTES', stream.read())
                with pse.open_current_export_reader(str(root), legacy, expected=identity) as stream:
                    self.assertEqual(b'LEGACY NO FENCE', stream.read())

                second = run_reset(install, PYTHON, root if configured else None)
                self.assertEqual(0, second.returncode, second.stderr[:800])
                retry = json.loads(second.stdout)['receipt']
                self.assertEqual([], retry['failed'])
                self.assertFalse(chapter_dir.exists())
                self.assertFalse((root / 'state.json').exists())
                self.assertFalse((root / PUBLICATIONS_FILE).exists())
                self.assertTrue(legacy.exists())

    def test_shard_failure_keeps_header_and_retry_cannot_false_complete(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                install, root = self._case(configured)
                seed(root, install)
                name = CHECKPOINTS[0]
                header = root / name
                shard = root / (name + '.parts-' + 'c' * 32)
                shard.mkdir()
                (shard / 'piece').write_bytes(b'SHARD')
                header.write_bytes(b'HEADER')
                (install / 'app' / 'sitecustomize.py').write_text(IO_BLOCKER)
                first = run_reset(install, PYTHON, root if configured else None,
                                  blocked=shard.name)
                self.assertEqual(0, first.returncode, first.stderr[:800])
                one = json.loads(first.stdout)['receipt']
                self.assertIn(name, one['failed'])
                self.assertTrue(header.exists(), 'checkpoint header must survive a shard failure')
                self.assertTrue(shard.exists())
                self.assertEqual(b'HEADER', header.read_bytes())
                second = run_reset(install, PYTHON, root if configured else None,
                                   blocked=shard.name)
                self.assertEqual(0, second.returncode, second.stderr[:800])
                two = json.loads(second.stdout)['receipt']
                self.assertIn(name, two['failed'])
                self.assertNotIn(name, two['removed'])
                self.assertTrue(shard.exists())
                third = run_reset(install, PYTHON, root if configured else None)
                self.assertEqual(0, third.returncode, third.stderr[:800])
                three = json.loads(third.stdout)['receipt']
                self.assertIn(name, three['removed'])
                self.assertEqual([], three['failed'])
                self.assertFalse(shard.exists())
                self.assertFalse(header.exists())


    def test_reset_serializes_checkpoint_removal_with_a_standalone_writer(self):
        import generation_checkpoint_shards as checkpoints
        import reset_artifact_roots as reset

        root = self.tmp / 'data'
        root.mkdir()
        header = root / CHECKPOINTS[0]
        checkpoints.GenerationCheckpointShards(header, {'run': 1}).save_chunks([{'text': 'first'}])
        write_new_epoch = """
from functools import partial
import sys
import generation_checkpoint_shards as checkpoints
from utils import file_lock

checkpoints.file_lock = partial(file_lock, timeout=0)
try:
    checkpoints.GenerationCheckpointShards(sys.argv[1], {'run': 2}).save_chunks([{'text': 'second'}])
except TimeoutError:
    sys.exit(2)
"""
        enumerations = 0
        attempts = []

        def enumerate_removal(path):
            nonlocal enumerations
            artifacts = checkpoints.get_generation_checkpoint_artifacts(path)
            enumerations += 1
            if enumerations == 2:
                attempts.append(subprocess.run(
                    [PYTHON, '-c', write_new_epoch, str(header)], cwd=ROOT / 'app',
                    capture_output=True, text=True, timeout=20))
            return artifacts

        with patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(root)}), \
                patch.object(reset, 'get_generation_checkpoint_artifacts', enumerate_removal):
            receipt = reset.reset_active_book(self.install)

        self.assertIn(header.name, receipt['removed'])
        self.assertEqual([], receipt['failed'])
        self.assertFalse(header.exists())
        self.assertEqual([], list(root.glob(header.name + '.parts-*')))
        [attempt] = attempts
        self.assertEqual(2, attempt.returncode, attempt.stderr)


if __name__ == '__main__':
    unittest.main()
