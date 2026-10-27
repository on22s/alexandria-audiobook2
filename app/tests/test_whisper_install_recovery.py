"""Whisper installer status must reflect the current build, not a stale file."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
spec = importlib.util.spec_from_file_location('build_whisper_under_test', ROOT / 'tools/build_whisper.py')
build_whisper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_whisper)


class BuildWhisperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / '.marker'
        self.bin_dir = self.root / 'venv' / 'bin'
        self.bin_dir.mkdir(parents=True)
        (self.bin_dir / ('cmake.exe' if os.name == 'nt' else 'cmake')).write_text('placeholder')
        self.exe = self.bin_dir / 'python'
        self.verify = patch.object(build_whisper, 'verify_whisper_assets').start()
        self.addCleanup(patch.stopall)
        patch.object(build_whisper.importlib.metadata, 'version', return_value=build_whisper.CMAKE_VERSION).start()
        patch.object(build_whisper.sys, 'executable', str(self.exe)).start()

    def test_success_needs_verified_assets_configure_build_and_executable(self):
        binary = self.root / ('whisper.cpp/build/bin/Release/whisper-cli.exe' if os.name == 'nt' else 'whisper.cpp/build/bin/whisper-cli')
        binary.parent.mkdir(parents=True)
        binary.write_text('stub')
        binary.chmod(0o700)
        with patch.object(build_whisper, 'run_build_command') as run, patch.object(build_whisper.subprocess, 'run') as probe:
            build_whisper.build('metal', self.root, self.marker)
        self.verify.assert_called_once_with(self.root)
        self.assertEqual(2, run.call_count)
        self.assertIn('-DGGML_METAL=ON', run.call_args_list[0].args[0])
        self.assertIn('-DGGML_CUDA=OFF', run.call_args_list[0].args[0])
        self.assertIn('-DGGML_HIP=OFF', run.call_args_list[0].args[0])
        self.assertEqual([str(binary), '--help'], probe.call_args.args[0])
        self.assertTrue(self.marker.exists())

    @unittest.skipIf(os.name == 'nt', 'uses a POSIX executable fixture')
    def test_corrupt_executable_does_not_clear_pending_state(self):
        binary = self.root / 'whisper.cpp/build/bin/whisper-cli'
        binary.parent.mkdir(parents=True)
        binary.write_text('not an executable')
        binary.chmod(0o700)
        pending = self.marker.parent / '.whisper-install-pending'
        pending.write_text('pending')
        with patch.object(build_whisper, 'run_build_command'):
            with self.assertRaises(OSError):
                build_whisper.build('cpu', self.root, self.marker)
        self.assertTrue(pending.exists())
        self.assertFalse(self.marker.exists())

    def test_help_timeout_preserves_pending_and_rejects_old_success(self):
        binary = self.root / ('whisper.cpp/build/bin/Release/whisper-cli.exe' if os.name == 'nt' else 'whisper.cpp/build/bin/whisper-cli')
        binary.parent.mkdir(parents=True)
        binary.write_text('fixture')
        binary.chmod(0o700)
        pending = self.marker.parent / '.whisper-install-pending'
        pending.write_text('pending')
        self.marker.write_text('stale success')
        timeout = subprocess.TimeoutExpired([str(binary), '--help'], 15)
        with patch.object(build_whisper, 'run_build_command'), patch.object(build_whisper.subprocess, 'run', side_effect=timeout):
            with self.assertRaises(subprocess.TimeoutExpired):
                build_whisper.build('cpu', self.root, self.marker)
        self.assertTrue(pending.exists())
        self.assertFalse(self.marker.exists())

    def test_failure_clears_stale_marker_and_does_not_claim_success(self):
        for where in ('assets', 'configure', 'build', 'missing_binary'):
            with self.subTest(where=where):
                self.marker.write_text('old success')
                self.verify.reset_mock(side_effect=True)
                self.verify.side_effect = ValueError('bad assets') if where == 'assets' else None
                fail_at = 0 if where == 'configure' else 1 if where == 'build' else None
                def run(*args, **kwargs):
                    run.calls += 1
                    if run.calls - 1 == fail_at:
                        raise subprocess.CalledProcessError(1, args[0])
                run.calls = 0
                with patch.object(build_whisper, 'run_build_command', side_effect=run):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError, RuntimeError)):
                        build_whisper.build('cpu', self.root, self.marker)
                self.assertFalse(self.marker.exists())

    def test_missing_cmake_package_and_unknown_backend_do_not_claim_success(self):
        for version, backend in [('wrong', 'cpu'), (build_whisper.CMAKE_VERSION, 'invalid')]:
            self.marker.write_text('old')
            with patch.object(build_whisper.importlib.metadata, 'version', return_value=version):
                with self.assertRaises((RuntimeError, ValueError)):
                    build_whisper.build(backend, self.root, self.marker)
            self.assertFalse(self.marker.exists())


class LauncherRepairMenuTests(unittest.TestCase):
    def menu(self, paths, non_executable=None, empty=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copy2(ROOT / 'pinokio.js', root / 'pinokio.js')
            for name in paths:
                target = root / name
                if name == 'app/env':
                    target.mkdir(parents=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text('' if name == empty else 'fixture')
                    target.chmod(0o600 if name == non_executable else 0o700)
            script = """const fs=require('fs'),path=require('path'),root=process.argv[1],d=require(path.join(root,'pinokio.js'));let info={exists:x=>fs.existsSync(path.join(root,x)),running:()=>false,local:()=>null};d.menu({},info).then(v=>console.log(JSON.stringify(v)))"""
            return json.loads(subprocess.check_output(['node', '-e', script, str(root)], text=True))

    def test_partial_install_offers_repair_and_retains_start(self):
        menu = self.menu(['app/env'])
        self.assertEqual('install.js', menu[0]['href'])
        self.assertFalse(any(item.get('default') for item in menu))
        self.assertTrue(any(item['href'] == 'start.js' for item in menu))

    def test_existing_complete_install_keeps_start_default(self):
        for binary in ('whisper.cpp/build/bin/whisper-cli', 'whisper.cpp/build/bin/Release/whisper-cli.exe', 'whisper.cpp/build/Release/whisper-cli.exe'):
            menu = self.menu(['app/env', 'models/whisper.cpp/ggml-small.en.bin', binary])
            self.assertEqual('start.js', next(item['href'] for item in menu if item.get('default')))

    def test_failed_retry_offers_repair_even_when_old_files_exist(self):
        menu = self.menu(['app/env', 'app/.whisper-install-pending', 'app/.whisper-install-ok',
                          'models/whisper.cpp/ggml-small.en.bin', 'whisper.cpp/build/bin/whisper-cli'])
        self.assertEqual('install.js', menu[0]['href'])
        self.assertFalse(any(item.get('default') for item in menu))

    @unittest.skipIf(os.name == 'nt', 'POSIX executable permissions')
    def test_non_executable_legacy_binary_offers_repair(self):
        binary = 'whisper.cpp/build/bin/whisper-cli'
        menu = self.menu(['app/env', 'models/whisper.cpp/ggml-small.en.bin', binary], non_executable=binary)
        self.assertEqual('install.js', menu[0]['href'])
        self.assertFalse(any(item.get('default') for item in menu))

    def test_empty_legacy_files_offer_repair(self):
        binary = 'whisper.cpp/build/bin/whisper-cli'
        model = 'models/whisper.cpp/ggml-small.en.bin'
        for empty in (binary, model):
            with self.subTest(empty=empty):
                menu = self.menu(['app/env', model, binary], empty=empty)
                self.assertEqual('install.js', menu[0]['href'])
                self.assertFalse(any(item.get('default') for item in menu))

    def test_no_environment_offers_install(self):
        menu = self.menu([])
        self.assertEqual('install.js', next(item['href'] for item in menu if item.get('default')))

    def test_one_backend_is_selected_on_supported_platforms(self):
        script = """const d=require(process.argv[1]),platform=process.argv[2],gpu=process.argv[3];console.log(JSON.stringify(d.run.filter(s=>s.params && String(s.params.message||'').includes('tools/build_whisper.py') && eval(s.when.slice(2,-2))).map(s=>s.params.message)))"""
        for platform, gpu, backend in [('darwin', 'nvidia', 'metal'), ('darwin', 'apple', 'metal'),
                                       ('linux', 'amd', 'hip'), ('linux', 'nvidia', 'cuda'),
                                       ('linux', 'cpu', 'cpu'), ('win32', 'nvidia', 'cuda'), ('win32', 'amd', 'cpu')]:
            with self.subTest(platform=platform, gpu=gpu):
                selected = json.loads(subprocess.check_output(['node', '-e', script, str(ROOT/'install.js'), platform, gpu], text=True))
                self.assertEqual([f'python tools/build_whisper.py {backend}'], selected)


if __name__ == '__main__':
    unittest.main()
