from tests.test_support import assert_file_lock_released
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import benchmark_environment


def checkout_output(revision, dirty=""):
    return (revision + "\n" + benchmark_environment.REMOTE_STATUS_BEGIN + "\n"
            + dirty + benchmark_environment.REMOTE_STATUS_END + "\n")


class BenchmarkEnvironmentTests(unittest.TestCase):
    def test_local_environment_combines_runtime_gpu_and_model_status(self):
        with patch.object(benchmark_environment, "get_runtime_info", return_value={
                "revision": "abc", "python": "3.10", "platform": {"system": "Linux"},
                "packages": {"torch": "2"}}), \
             patch.object(benchmark_environment, "get_gpu_name_and_backend",
                          return_value=("Local GPU", "rocm")), \
             patch.object(benchmark_environment, "get_lmstudio_status", return_value={
                 "available": True, "loaded": True, "context_length": 32768, "parallel": 1}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": True, "sha256": "tree-hash"}), \
             patch.object(benchmark_environment.platform, "node", return_value="local-host"):
            fingerprint = benchmark_environment.collect_local_environment("/repo", "model")
        self.assertEqual("local", fingerprint["target"])
        self.assertEqual("Local GPU", fingerprint["details"]["gpu_name"])
        self.assertEqual(32768, fingerprint["details"]["lmstudio"]["context_length"])
        self.assertEqual("tree-hash", fingerprint["details"]["worktree"]["sha256"])

    def test_local_worktree_identity_changes_with_untracked_source_content(self):
        def run(argv, **kwargs):
            outputs = {"status": "?? app/new.py\n", "diff": b"",
                       "ls-files": "app/new.py\n"}
            return type("Result", (), {"returncode": 0, "stdout": outputs[argv[3]]})()
        with patch.object(benchmark_environment.subprocess, "run", side_effect=run), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_bytes", return_value=b"first"):
            first = benchmark_environment._get_local_worktree_identity("/repo")
        with patch.object(benchmark_environment.subprocess, "run", side_effect=run), \
             patch.object(Path, "is_file", return_value=True), \
             patch.object(Path, "read_bytes", return_value=b"second"):
            second = benchmark_environment._get_local_worktree_identity("/repo")
        self.assertTrue(first["dirty"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_remote_runtime_uses_last_nonempty_line_after_banner(self):
        payload = {"hostname": "thunder", "python_version": "3.11",
                   "platform": {"system": "Linux"}}
        result = type("Result", (), {"returncode": 0,
                      "stdout": "decorative banner\n" + json.dumps(payload) + "\n",
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result) as run:
            observations = benchmark_environment._get_remote_runtime_observations("tnr-0")
        self.assertEqual(payload, observations)
        self.assertIn("python3 -c", run.call_args.args[1])

    def test_thunder_environment_fails_when_model_status_is_unavailable(self):
        with patch.object(benchmark_environment, "_get_remote_runtime_observations",
                          return_value={"hostname": "thunder", "python_version": "3.11",
                                        "platform": {}, "git_commit": "def", "packages": {"remote-package":"1"},
                                        "worktree": {"dirty":False,"sha256":"remote-tree"}}), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A6000", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info", return_value={
                 "revision": "abc", "python": "3.10", "platform": {}, "packages": {}}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": False, "sha256": "tree"}), \
             patch.object(benchmark_environment, "get_remote_lmstudio_status",
                          return_value={"available": False}):
            with self.assertRaisesRegex(ValueError, "LM Studio status is unavailable"):
                benchmark_environment.collect_thunder_environment(
                    "/repo", "tnr-0", "model")

    def test_lmstudio_observations_reject_an_unloaded_model(self):
        with self.assertRaisesRegex(ValueError, "model is not loaded"):
            benchmark_environment._get_lmstudio_observations(
                {"available": True, "loaded": False}, "model")

    def test_verify_comparable_environments_rejects_torch_minor_mismatch(self):
        local_env = {"details": {"packages": {"torch": "2.10.0+rocm7.0"}}}
        thunder_env = {"details": {"packages": {"torch": "2.7.0+cu126"}}}
        with self.assertRaisesRegex(ValueError, "different builds"):
            benchmark_environment.verify_comparable_environments(local_env, thunder_env)

    def test_verify_comparable_environments_allows_matching_torch_minor(self):
        local_env = {"details": {"packages": {"torch": "2.10.0+rocm7.0"}}}
        thunder_env = {"details": {"packages": {"torch": "2.10.1+cu126"}}}
        benchmark_environment.verify_comparable_environments(local_env, thunder_env)

    def test_unparseable_torch_versions_cannot_certify_comparability(self):
        for local, remote in (("nightly-a", "nightly-b"), ("unknown", "unknown"),
                              ("bad.version", "bad.version"), ("2.10.0", "unknown")):
            with self.subTest(local=local, remote=remote):
                one = {"details": {"packages": {"torch": local}}}
                two = {"details": {"packages": {"torch": remote}}}
                with self.assertRaisesRegex(ValueError, "unparseable Torch"):
                    benchmark_environment.verify_comparable_environments(one, two)
                self.assertEqual(local, one["details"]["packages"]["torch"])
                self.assertEqual(remote, two["details"]["packages"]["torch"])

    def test_verify_comparable_environments_skips_fingerprints_without_torch(self):
        local_env = {"details": {"packages": {}}}
        thunder_env = {"details": {}}
        benchmark_environment.verify_comparable_environments(local_env, thunder_env)

    def test_baseline_round_trips_through_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "environment_baselines.json"))
            environment = {"target": "local", "details": {"packages": {"torch": "2.10.0"}}}
            benchmark_environment.save_environment_baseline("local", environment, path)
            loaded = benchmark_environment.load_environment_baseline("local", path)
        self.assertEqual(environment, loaded["environment"])
        self.assertIn("collected_at", loaded)

    def test_baseline_load_returns_none_when_target_never_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "environment_baselines.json"))
            self.assertIsNone(benchmark_environment.load_environment_baseline("thunder", path))

    def test_baseline_save_preserves_other_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, "environment_baselines.json"))
            benchmark_environment.save_environment_baseline("local", {"a": 1}, path)
            benchmark_environment.save_environment_baseline("thunder", {"b": 2}, path)
            local_entry = benchmark_environment.load_environment_baseline("local", path)
        self.assertEqual({"a": 1}, local_entry["environment"])

    def test_baseline_staleness_uses_collected_at(self):
        fresh = {"collected_at": time.time()}
        stale = {"collected_at": time.time() - benchmark_environment.BASELINE_STALE_SECONDS - 1}
        self.assertFalse(benchmark_environment.is_baseline_stale(fresh))
        self.assertTrue(benchmark_environment.is_baseline_stale(stale))

    def test_verify_remote_checkout_accepts_clean_matching_tree(self):
        result = type("Result", (), {"returncode": 0, "stdout": checkout_output("a" * 40),
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "a" * 40}):
            commit = benchmark_environment._verify_remote_checkout(
                "/repo", "tnr-0", "/remote")
        self.assertEqual("a" * 40, commit)

    def test_verify_remote_checkout_rejects_revision_mismatch(self):
        result = type("Result", (), {"returncode": 0, "stdout": checkout_output("a" * 40),
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "b" * 40}):
            with self.assertRaisesRegex(ValueError, "match the local git revision"):
                benchmark_environment._verify_remote_checkout("/repo", "tnr-0", "/remote")

    def test_verify_remote_checkout_rejects_dirty_tree(self):
        result = type("Result", (), {"returncode": 0,
                      "stdout": checkout_output("a" * 40, " M app/foo.py\n"), "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "must be clean"):
                benchmark_environment._verify_remote_checkout("/repo", "tnr-0", "/remote")

    def test_thunder_environment_verifies_checkout_when_remote_root_given(self):
        with patch.object(benchmark_environment, "_get_remote_runtime_observations",
                          return_value={"hostname": "thunder", "python_version": "3.11",
                                        "platform": {}, "git_commit": "abc", "packages": {"remote-package":"1"},
                                        "worktree": {"dirty":False,"sha256":"remote-tree"}}), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A6000", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info", return_value={
                 "revision": "abc", "python": "3.10", "platform": {}, "packages": {}}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": False, "sha256": "tree"}), \
             patch.object(benchmark_environment, "get_remote_lmstudio_status",
                          return_value={"available": True, "loaded": True}), \
             patch.object(benchmark_environment, "_verify_remote_checkout",
                          return_value="abc") as verify:
            fingerprint = benchmark_environment.collect_thunder_environment(
                "/repo", "tnr-0", "model", remote_root="/remote")
        verify.assert_called_once_with("/repo", "tnr-0", "/remote")
        self.assertEqual("abc", fingerprint["details"]["remote_checkout_commit"])

    def test_thunder_environment_skips_checkout_check_without_remote_root(self):
        with patch.object(benchmark_environment, "_get_remote_runtime_observations",
                          return_value={"hostname": "thunder", "python_version": "3.11",
                                        "platform": {}, "git_commit": "def", "packages": {"remote-package":"1"},
                                        "worktree": {"dirty":False,"sha256":"remote-tree"}}), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A6000", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info", return_value={
                 "revision": "abc", "python": "3.10", "platform": {}, "packages": {}}), \
             patch.object(benchmark_environment, "_get_local_worktree_identity",
                          return_value={"dirty": False, "sha256": "tree"}), \
             patch.object(benchmark_environment, "get_remote_lmstudio_status",
                          return_value={"available": True, "loaded": True}), \
             patch.object(benchmark_environment, "_verify_remote_checkout") as verify:
            fingerprint = benchmark_environment.collect_thunder_environment(
                "/repo", "tnr-0", "model")
        verify.assert_not_called()
        self.assertNotIn("remote_checkout_commit", fingerprint["details"])

    def test_thunder_tts_rejects_checkout_that_does_not_match(self):
        payload = {"hostname": "thunder", "python_version": "3.11",
                   "torch": "2.7", "qwen_tts": "1"}
        result = type("Result", (), {"returncode": 0,
                      "stdout": "banner\n" + json.dumps(payload) + "\n" + checkout_output("d" * 40),
                      "stderr": ""})()
        with patch.object(benchmark_environment, "_ssh_run", return_value=result), \
             patch.object(benchmark_environment, "get_remote_gpu_name_and_backend",
                          return_value=("A100", "cuda")), \
             patch.object(benchmark_environment, "get_runtime_info",
                          return_value={"revision": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "match the local git revision"):
                benchmark_environment.collect_thunder_tts_environment(
                    "/repo", "tnr-0", "/remote", "/venv/python")


if __name__ == "__main__":
    unittest.main()


class BenchmarkArtifactBoundaryTests(unittest.TestCase):
    def test_bare_filename_baseline_roundtrips_and_preserves_other_target(self):
        import os
        before = os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                benchmark_environment.save_environment_baseline('local', {'hash': 'a'}, 'baseline.json')
                benchmark_environment.save_environment_baseline('thunder', {'hash': 'b'}, 'baseline.json')
                artifact = json.loads(Path('baseline.json').read_text())
                self.assertEqual({'hash': 'a'}, artifact['local']['environment'])
                self.assertEqual({'hash': 'b'}, artifact['thunder']['environment'])
                self.assertEqual(artifact['local'], benchmark_environment.load_environment_baseline('local', 'baseline.json'))
        finally:
            os.chdir(before)

    def test_untracked_content_boundary_collision_cannot_reuse_identity(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(['git', 'init', '-q', tmp], check=True, capture_output=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Initial'], check=True, capture_output=True)
            app = Path(tmp) / 'app'
            app.mkdir()
            first, second = app / 'a.py', app / 'b.py'
            marker = b'app/b.py'
            first.write_bytes(b'ab' + marker)
            second.write_bytes(b'c')
            old_preimage = b'app/a.py' + first.read_bytes() + marker + second.read_bytes()
            before = benchmark_environment._get_local_worktree_identity(tmp)
            first.write_bytes(b'ab')
            second.write_bytes(marker + b'c')
            self.assertEqual(old_preimage, b'app/a.py' + first.read_bytes() + marker + second.read_bytes())
            after = benchmark_environment._get_local_worktree_identity(tmp)
            self.assertNotEqual(before['sha256'], after['sha256'])
            self.assertTrue(before['dirty'])
            self.assertTrue(after['dirty'])
            self.assertEqual(after, benchmark_environment._get_local_worktree_identity(tmp))


class ConcurrentBaselineArtifactTests(unittest.TestCase):
    def test_overlapping_process_updates_preserve_both_real_baseline_records(self):
        import subprocess, sys
        worker = """
import sys,time
from pathlib import Path
import benchmark_environment as env
path,target,ready,loaded,release=sys.argv[1:]
real_load=env.safe_load_json
def load(*args,**kwargs):
    result=real_load(*args,**kwargs)
    Path(loaded).touch()
    if target=='local':
        deadline=time.monotonic()+5
        while not Path(release).exists():
            if time.monotonic()>deadline: raise RuntimeError('release timed out')
            time.sleep(.01)
    return result
env.safe_load_json=load
Path(ready).touch()
env.save_environment_baseline(target,{'runtime':target},path)
"""
        def wait_file(path):
            deadline = time.monotonic() + 5
            while not path.exists():
                if time.monotonic() > deadline:
                    self.fail('worker did not reach ' + str(path))
                time.sleep(.01)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'baseline.json'
            release = root / 'release'
            workers = []
            try:
                for target in ('local', 'thunder'):
                    ready, loaded = root / (target + '.ready'), root / (target + '.loaded')
                    proc = subprocess.Popen([sys.executable, '-c', worker, str(path), target,
                        str(ready), str(loaded), str(release)], stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)
                    workers.append(proc)
                    wait_file(ready)
                    if target == 'local':
                        wait_file(loaded)
                # Old code finishes the second write while the first reader
                # still holds its stale snapshot. A protected reader waits.
                try:
                    workers[1].wait(timeout=.5)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                release.touch()
                for proc in workers:
                    try:
                        stdout, stderr = proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        stdout, stderr = proc.communicate()
                    self.assertEqual(0, proc.returncode, stdout + stderr)
            artifact = json.loads(path.read_text())
            self.assertEqual({'local', 'thunder'}, set(artifact))
            for target in artifact:
                self.assertEqual({'runtime': target}, artifact[target]['environment'])
                self.assertGreater(artifact[target]['collected_at'], 0)
            assert_file_lock_released(str(path))


class RemoteCheckoutArtifactTests(unittest.TestCase):
    def test_both_remote_probes_accept_real_git_object_formats_and_reject_dirty_tools(self):
        import subprocess
        payload = {'hostname': 'fixture', 'python_version': '3.11', 'torch': '2.10', 'qwen_tts': 'fixture'}
        for object_format in ('sha1', 'sha256'):
            with self.subTest(object_format=object_format), tempfile.TemporaryDirectory() as tmp:
                subprocess.run(['git', 'init', '-q', '--object-format=' + object_format, tmp], check=True, capture_output=True)
                tools = Path(tmp) / 'tools'
                tools.mkdir()
                script = tools / 'worker.py'
                script.write_text('print("original")\n')
                subprocess.run(['git', '-C', tmp, 'add', 'tools'], check=True, capture_output=True)
                subprocess.run(['git', '-C', tmp, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '-qm', 'Fixture'], check=True, capture_output=True)
                revision = subprocess.check_output(['git', '-C', tmp, 'rev-parse', 'HEAD'], text=True).strip()
                self.assertEqual(40 if object_format == 'sha1' else 64, len(revision))
                def remote(_alias, command, **_kwargs):
                    prefix = ''
                    if not command.startswith('git '):
                        command = command.split(' && ', 1)[1]
                        prefix = json.dumps(payload) + '\n'
                    result = subprocess.run(['bash', '-c', command], capture_output=True, text=True)
                    result.stdout = 'login banner\n' + 'a' * 64 + '\n' + 'b' * 40 + '\n' + prefix + result.stdout
                    return result
                with patch.object(benchmark_environment, '_ssh_run', side_effect=remote), \
                     patch.object(benchmark_environment, 'get_runtime_info', return_value={'revision': revision}), \
                     patch.object(benchmark_environment, 'get_remote_gpu_name_and_backend', return_value=('fixture GPU', 'cuda')), \
                     patch.object(benchmark_environment, '_get_local_worktree_identity', return_value={'dirty': False, 'sha256': 'fixture'}):
                    self.assertEqual(revision, benchmark_environment._verify_remote_checkout(tmp, 'fixture', tmp))
                    fingerprint = benchmark_environment.collect_thunder_tts_environment(tmp, 'fixture', tmp, '/fixture/python')
                    self.assertEqual(revision, fingerprint['details']['git_commit'])
                    script.write_text('print("modified")\n')
                    with self.assertRaisesRegex(ValueError, 'must be clean'):
                        benchmark_environment._verify_remote_checkout(tmp, 'fixture', tmp)
                    with self.assertRaisesRegex(ValueError, 'must be clean'):
                        benchmark_environment.collect_thunder_tts_environment(tmp, 'fixture', tmp, '/fixture/python')


class LocalProbeOutputTests(unittest.TestCase):
    def test_real_selected_python_probe_ignores_import_and_exit_warnings(self):
        import os, sys
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'torch.py').write_text("import atexit\n__version__='2.10.0+fixture'\nprint('fixture import warning')\natexit.register(lambda: print('fixture exit warning'))\n")
            Path(tmp, 'qwen_tts.py').write_text("__version__='fixture-version'\n")
            path = tmp + os.pathsep + os.environ.get('PYTHONPATH', '')
            with patch.dict(os.environ, {'PYTHONPATH': path}), \
                 patch.object(benchmark_environment, 'get_runtime_info', return_value={'revision': 'fixture-revision'}), \
                 patch.object(benchmark_environment, 'get_gpu_name_and_backend', return_value=('fixture GPU', 'fixture backend')), \
                 patch.object(benchmark_environment, '_get_local_worktree_identity', return_value={'dirty': False, 'sha256': 'fixture'}):
                fingerprint = benchmark_environment.collect_local_tts_environment('/fixture', sys.executable)
            details = fingerprint['details']
            self.assertEqual({'torch': '2.10.0+fixture', 'qwen_tts': 'fixture-version'}, details['packages'])
            self.assertEqual(sys.executable, details['python_executable'])
            self.assertTrue(details['python_version'])

    def test_missing_or_invalid_probe_payload_fails_with_controlled_diagnostic(self):
        import subprocess
        for stdout in ('', '\n', 'only a warning\n', '[]\n', '{"unrelated": 1}\n',
                       '{"python": "3.11", "torch": "2.10"}\n'):
            with self.subTest(stdout=stdout), \
                 patch.object(benchmark_environment, 'get_runtime_info', return_value={'revision': 'fixture'}), \
                 patch.object(benchmark_environment, 'get_gpu_name_and_backend', return_value=('fixture', 'fixture')), \
                 patch.object(benchmark_environment.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout, '')):
                with self.assertRaisesRegex(ValueError, 'local TTS.*probe.*JSON'):
                    benchmark_environment.collect_local_tts_environment('/fixture', '/fixture/python')
