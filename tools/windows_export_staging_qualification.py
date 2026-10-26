#!/usr/bin/env python3
"""Standalone Windows staging/cleanup qualification harness.

Runs POSIX reference checks on Linux and current Windows staging checks on a
native Windows host. Private Windows retirement checks are reported as
unsupported until that protocol is implemented; unsupported checks are never
counted as passes.

Run from the repository root:
  python tools/windows_export_staging_qualification.py --json evidence.json
  python tools/windows_export_staging_qualification.py --require-windows
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform as platform_module
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

if not __debug__:
    raise SystemExit('Qualification harness requires assertions; do not run with -O')

REPO = Path(__file__).resolve().parents[1]
APP = REPO / 'app'
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import export_staging
from export_staging import create_export_staging, reclaim_export_staging, MARKER
from export_staging_directory import StageDirectoryChanged
from export_staging_retirement import DIRECTORY, PosixRetirement

IS_WINDOWS = os.name == 'nt'
CHECKS = []


class UnsupportedCheck(Exception):
    """A check cannot be exercised on this host/implementation."""


def record(name, status, detail, *, platforms, reason=None, required=False):
    CHECKS.append({'name': name, 'status': status, 'platforms': platforms,
                   'detail': detail, 'reason': reason, 'required': required})


def run_check(name, platforms, function, *, required=False):
    """platforms is 'posix', 'windows', or 'both'."""
    supported = ((platforms in ('posix', 'both') and not IS_WINDOWS)
                 or (platforms in ('windows', 'both') and IS_WINDOWS))
    if not supported:
        record(name, 'unsupported', None, platforms=platforms, required=required,
               reason=f'requires {platforms} host; current host is ' +
                      ('Windows' if IS_WINDOWS else 'POSIX'))
        return
    try:
        detail = function()
    except UnsupportedCheck as error:
        record(name, 'unsupported', None, platforms=platforms, reason=str(error),
               required=required)
    except Exception as error:
        record(name, 'failed', f'{type(error).__name__}: {error}', platforms=platforms,
               required=required)
    else:
        record(name, 'passed', detail, platforms=platforms, required=required)


def _child_env():
    env = dict(os.environ)
    env['PYTHONPATH'] = str(APP)
    env['PYTHONUTF8'] = '1'
    env['PYTHONIOENCODING'] = 'utf-8'
    return env


def child(command, *args, timeout=30):
    return subprocess.run([sys.executable, '-c', command, str(APP), *map(str, args)],
                          capture_output=True, encoding='utf-8', errors='replace',
                          timeout=timeout, env=_child_env())


def child_hold(command, *args, timeout=30):
    process = subprocess.Popen([sys.executable, '-c', command, str(APP), *map(str, args)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               encoding='utf-8', errors='replace', env=_child_env())
    line_holder = []
    def read_line():
        line_holder.append(process.stdout.readline())
    thread = threading.Thread(target=read_line, daemon=True)
    thread.start()
    thread.join(timeout)
    line = line_holder[0].strip() if line_holder else ''
    if not line:
        stderr = process.stderr.read() if process.poll() is not None else ''
        _terminate(process)
        raise AssertionError(f'child did not report a stage within {timeout}s: {stderr[-1000:]}')
    return process, Path(line)


CREATE_CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
stage = create_export_staging(sys.argv[2], 'mp3')
(stage.path / 'payload.bin').write_bytes(b'owned payload')
print(stage.path, flush=True)
os._exit(76)
'''

RECOVER_CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from export_staging import reclaim_export_staging
print(json.dumps(reclaim_export_staging([sys.argv[2]])), flush=True)
'''

HOLD_CHILD = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
stage = create_export_staging(sys.argv[2], 'mp3')
(stage.path / 'payload.bin').write_bytes(b'owned payload')
print(stage.path, flush=True)
time.sleep(60)
'''

HOLD_HANDLE_CHILD = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
from export_staging import create_export_staging
from export_staging_directory import stage_directory
stage = create_export_staging(sys.argv[2], 'mp3')
(stage.path / 'payload.bin').write_bytes(b'owned payload')
with stage_directory(stage.path, stage._directory_identity):
    print(stage.path, flush=True)
    time.sleep(60)
'''

PARTIAL_CREATE_CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import export_staging
from unittest.mock import patch
from export_staging_retirement import PosixRetirement, DIRECTORY
original = PosixRetirement._write_receipt
def write(self, name, value):
    if name == 'owner.json':
        print(self.parent / DIRECTORY / self.slot, flush=True)
        os._exit(76)
    return original(self, name, value)
with patch.object(PosixRetirement, '_write_receipt', new=write):
    export_staging.create_export_staging(sys.argv[2], 'mp3')
'''

RETIRE_EXIT_CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import export_staging
from unittest.mock import patch
from export_staging_retirement import PosixRetirement, DIRECTORY
stage = export_staging._create_public_export_staging(sys.argv[2], 'mp3')
(stage.path / 'payload.bin').write_bytes(b'retirement payload')
def exit_after_verified(self, *args, **kwargs):
    print(self.parent / DIRECTORY / self.slot, flush=True)
    os._exit(76)
with patch.object(PosixRetirement, 'cleanup_verified_stage', new=exit_after_verified):
    stage.finish()
'''

RETIRE_UNVERIFIED_CHILD = r'''
import os, sys
sys.path.insert(0, sys.argv[1])
import export_staging
from unittest.mock import patch
from export_staging_retirement import PosixRetirement, DIRECTORY
stage = export_staging._create_public_export_staging(sys.argv[2], 'mp3')
(stage.path / 'payload.bin').write_bytes(b'unverified retirement payload')
original = PosixRetirement._write_receipt
def write(self, name, value):
    if name == 'verified.json':
        print(self.parent / DIRECTORY / self.slot, flush=True)
        os._exit(76)
    return original(self, name, value)
with patch.object(PosixRetirement, '_write_receipt', new=write):
    stage.finish()
'''


def _terminate(process):
    if process.poll() is None:
        process.kill()
        process.wait(timeout=10)


def check_hard_termination(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        stage = Path(result.stdout.strip())
        assert stage.is_dir(), 'hard-exit stage is missing before recovery'
        reclaimed = reclaim_export_staging([root])
        assert reclaimed['removed'], reclaimed
        assert not stage.exists(), 'abandoned stage survived reclamation'
        return {'stage': str(stage), 'result': reclaimed}


def check_restart_recovery(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        stage = Path(result.stdout.strip())
        recovered = child(RECOVER_CHILD, root)
        assert recovered.returncode == 0, recovered.stderr[-2000:]
        payload = json.loads(recovered.stdout.strip().splitlines()[-1])
        assert payload['removed'], payload
        assert not stage.exists(), 'restart recovery left the abandoned stage'
        return {'stage': str(stage), 'result': payload}


def check_partial_creation_recovery(platform, restart):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(PARTIAL_CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        slot = Path(result.stdout.strip().splitlines()[-1])
        assert slot.is_dir(), 'partial creation slot is missing'
        assert (slot / 'allocation.json').exists()
        assert (slot / 'creation.json').exists()
        assert not (slot / 'owner.json').exists()
        if restart:
            recovered = child(RECOVER_CHILD, root)
            assert recovered.returncode == 0, recovered.stderr[-2000:]
            payload = json.loads(recovered.stdout.strip().splitlines()[-1])
        else:
            payload = reclaim_export_staging([root])
        assert payload['removed'], payload
        assert not slot.exists(), 'partial creation slot survived recovery'
        return {'slot': str(slot), 'result': payload}


def check_finalization_recovery(platform, restart, unverified=False):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        script = RETIRE_UNVERIFIED_CHILD if unverified else RETIRE_EXIT_CHILD
        result = child(script, root)
        assert result.returncode == 76, result.stderr[-2000:]
        slot = Path(result.stdout.strip().splitlines()[-1])
        assert slot.is_dir(), 'retirement slot is missing'
        assert (slot / 'reserved.json').exists()
        if unverified:
            assert not (slot / 'verified.json').exists()
        else:
            assert (slot / 'verified.json').exists()
        assert (slot / 'stage').is_dir()
        if restart:
            recovered = child(RECOVER_CHILD, root)
            assert recovered.returncode == 0, recovered.stderr[-2000:]
            payload = json.loads(recovered.stdout.strip().splitlines()[-1])
        else:
            payload = reclaim_export_staging([root])
        assert payload['removed'], payload
        assert not slot.exists(), 'retirement slot survived recovery'
        assert not list(root.rglob('payload.bin')), 'finalization payload survived'
        return {'slot': str(slot), 'result': payload}


def check_live_owner(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        process, stage = child_hold(HOLD_CHILD, root)
        try:
            assert stage.is_dir()
            reclaimed = reclaim_export_staging([root])
            assert str(stage) in reclaimed['preserved'] or str(stage.parent) in reclaimed['preserved'], reclaimed
            assert (stage / 'payload.bin').read_bytes() == b'owned payload'
        finally:
            _terminate(process)
        reclaimed = reclaim_export_staging([root])
        assert reclaimed['removed'], reclaimed
        assert not stage.exists(), 'released stage survived reclamation'
        return {'stage': str(stage), 'preserved_while_live': True,
                'removed_after_exit': reclaimed}


def check_parent_replacement(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(RETIRE_EXIT_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        slot = Path(result.stdout.strip().splitlines()[-1])
        relative = slot.relative_to(root.resolve()) / 'stage'
        old = Path(base) / 'book-old'
        root.rename(old)
        replacement = Path(base) / 'book'
        replacement.mkdir()
        registry = old / DIRECTORY
        assert registry.exists()
        registry.rename(replacement / DIRECTORY)
        stage = replacement / relative
        reclaimed = reclaim_export_staging([replacement])
        assert not reclaimed['removed'], reclaimed
        assert stage.is_dir() and (stage / 'payload.bin').exists(), 'replacement deleted retired stage'
        assert any(str(slot.name) in item for item in reclaimed['preserved'] + reclaimed['errors']), reclaimed
        return {'old_stage': str(stage), 'result': reclaimed}


def check_malformed_receipt(platform):
    details = {}
    for label in ('truncated', 'duplicate_key'):
        with tempfile.TemporaryDirectory() as base:
            root = Path(base) / 'book'
            root.mkdir()
            result = child(RETIRE_EXIT_CHILD, root)
            assert result.returncode == 76, result.stderr[-2000:]
            slot = Path(result.stdout.strip().splitlines()[-1])
            raw = (slot / 'reserved.json').read_text(encoding='utf-8')
            if label == 'truncated':
                text = raw[:max(1, len(raw) // 2)]
            else:
                assert '"schema":1' in raw
                text = raw.replace('"schema":1', '"schema":1,"schema":1', 1)
            (slot / 'reserved.json').write_text(text, encoding='utf-8')
            os.chmod(slot / 'reserved.json', 0o600)
            reclaimed = reclaim_export_staging([root])
            assert not reclaimed['removed'], reclaimed
            assert (slot / 'stage' / 'payload.bin').exists(), f'{label}: malformed receipt deleted payload'
            assert (slot / 'reserved.json').exists(), f'{label}: malformed receipt metadata was destroyed'
            assert any(str(slot) in item for item in reclaimed['preserved'] + reclaimed['errors']), reclaimed
            details[label] = reclaimed
    return details


def check_failed_deletion(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        stage = Path(result.stdout.strip())
        with patch('export_staging._remove_contents',
                   side_effect=PermissionError('synthetic deletion failure')) as failure:
            reclaimed = reclaim_export_staging([root])
        assert failure.called, 'failed-deletion probe did not reach payload deletion'
        assert not reclaimed['removed'], reclaimed
        assert reclaimed['errors'], reclaimed
        assert stage.exists() and (stage / 'payload.bin').exists(), 'failed deletion removed payload'
        return {'stage': str(stage), 'result': reclaimed}


def check_reparse_point(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        victim = Path(base) / 'victim'
        victim.mkdir()
        (victim / 'foreign.txt').write_bytes(b'foreign')
        link = root / '.mp3-merge-reparse'
        try:
            os.symlink(victim, link, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            raise UnsupportedCheck(f'cannot create symlink on this host: {error}')
        with PosixRetirement(parent=root) as registry:
            registry.allocate_slot()
            slot = root / DIRECTORY / registry.slot
            slot_name = registry.slot
        slot.rmdir()
        os.symlink(victim, slot, target_is_directory=True)
        with PosixRetirement(parent=root) as registry:
            try:
                registry.open_slot(slot_name)
            except (OSError, StageDirectoryChanged):
                pass
            else:
                raise AssertionError('private retirement slot symlink was followed')
        reclaimed = reclaim_export_staging([root])
        assert (victim / 'foreign.txt').read_bytes() == b'foreign'
        assert slot.is_symlink(), 'reparse slot link was deleted'
        assert not reclaimed['removed'], reclaimed
        assert any(str(slot) in item for item in reclaimed['preserved'] + reclaimed['errors']), reclaimed
        return {'link': str(slot), 'result': reclaimed}


def check_windows_current(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        stage = Path(result.stdout.strip())
        reclaimed = reclaim_export_staging([root])
        assert reclaimed['removed'], reclaimed
        assert not stage.exists(), 'Windows abandoned stage survived'
        return {'stage': str(stage), 'result': reclaimed}


def check_windows_live_owner(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        process, stage = child_hold(HOLD_CHILD, root)
        try:
            reclaimed = reclaim_export_staging([root])
            assert str(stage) in reclaimed['preserved'] or str(stage.parent) in reclaimed['preserved'], reclaimed
            assert not reclaimed['errors'], reclaimed
            assert (stage / 'payload.bin').read_bytes() == b'owned payload'
        finally:
            _terminate(process)
        reclaimed = reclaim_export_staging([root])
        assert reclaimed['removed'], reclaimed
        return {'stage': str(stage), 'result': reclaimed}


def check_windows_parent_replacement(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        moved = Path(base) / 'book-moved'
        # Establish that ordinary permissions allow this exact rename.
        root.rename(moved)
        moved.rename(root)
        process, stage = child_hold(HOLD_HANDLE_CHILD, root)
        try:
            assert process.poll() is None, 'child exited before rename probe'
            assert stage.is_dir() and (stage / 'payload.bin').exists()
            try:
                root.rename(moved)
            except OSError as error:
                winerror = getattr(error, 'winerror', None)
                if winerror == 5:
                    raise UnsupportedCheck('access denied does not establish a sharing violation') from error
                assert winerror == 32, f'unexpected rename error: {error}'
            else:
                raise AssertionError('parent rename was not refused while ancestor/stage handles were live')
        finally:
            _terminate(process)
        # A stable access denial would not demonstrate protection by the handle.
        root.rename(moved)
        moved.rename(root)
        return {'rename_refused': winerror, 'rename_without_handle': True,
                'rename_after_release': True}


def check_windows_reparse(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        victim = Path(base) / 'victim'
        victim.mkdir()
        (victim / 'foreign.txt').write_bytes(b'foreign')
        link = root / '.mp3-merge-reparse'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(victim)],
                                capture_output=True, encoding='utf-8', errors='replace')
        if result.returncode != 0:
            raise UnsupportedCheck(f'cannot create Windows junction: {result.stderr[-500:]}')
        assert os.path.lexists(link), 'junction was not created'
        from windows_export_staging_directory import WindowsStageDirectory, _identity
        from export_staging_directory import identity
        control = root / 'ordinary-directory'
        control.mkdir()
        with WindowsStageDirectory(control, identity(os.stat(control, follow_symlinks=False))):
            pass

        class TargetProbe(WindowsStageDirectory):
            saw_target_reparse = False

            def check(self):
                info = self.stage.information()
                self.saw_target_reparse = bool(info.attributes & 0x400)
                if self.saw_target_reparse:
                    assert info.attributes & 0x10, 'reparse object is not a directory'
                    assert _identity(info) == self.expected, 'reparse identity changed during admission'
                return super().check()

        probe = TargetProbe(link, identity(os.stat(link, follow_symlinks=False)))
        try:
            with probe:
                raise AssertionError('WindowsStageDirectory followed a junction')
        except StageDirectoryChanged:
            assert probe.saw_target_reparse, 'admission failed before checking the junction target'
        else:
            raise AssertionError('WindowsStageDirectory followed a junction')
        reclaimed = reclaim_export_staging([root])
        assert (victim / 'foreign.txt').read_bytes() == b'foreign'
        assert os.path.lexists(link), 'junction link was deleted'
        assert not reclaimed['removed'], reclaimed
        assert str(link) in reclaimed['preserved'] or any(str(link) in item for item in reclaimed['errors']), reclaimed
        return {'link': str(link), 'result': reclaimed}


def check_windows_malformed_marker(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        stage = root / '.mp3-merge-malformed'
        stage.mkdir()
        (stage / MARKER).write_text('{not-json', encoding='utf-8')
        reclaimed = reclaim_export_staging([root])
        assert not reclaimed['removed'], reclaimed
        assert stage.exists(), 'malformed marker stage was deleted'
        assert (stage / MARKER).exists(), 'malformed marker was deleted'
        assert str(stage) in reclaimed['preserved'] or any(str(stage) in item for item in reclaimed['errors']), reclaimed
        return {'stage': str(stage), 'result': reclaimed}


def check_windows_readonly_deletion(platform):
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        stage = Path(result.stdout.strip())
        payload = stage / 'payload.bin'
        os.chmod(payload, stat.S_IREAD)
        reclaimed = reclaim_export_staging([root])
        assert not reclaimed['removed'], reclaimed
        assert reclaimed['errors'] or str(stage) in reclaimed['preserved'], reclaimed
        assert payload.exists(), 'read-only deletion failure removed payload'
        return {'stage': str(stage), 'result': reclaimed}


def check_windows_partial_creation_missing(platform):
    raise UnsupportedCheck(
        'current Windows creation writes no durable allocation/creation receipt; '
        'hard termination before marker/owner publication is not implemented or qualified')


def check_windows_failed_deletion(platform):
    from windows_export_staging_directory import WindowsStageDirectory
    with tempfile.TemporaryDirectory() as base:
        root = Path(base) / 'book'
        root.mkdir()
        result = child(CREATE_CHILD, root)
        assert result.returncode == 76, result.stderr[-2000:]
        stage = Path(result.stdout.strip())
        with patch.object(WindowsStageDirectory, 'remove_contents',
                          side_effect=PermissionError('synthetic Windows deletion failure')) as failure:
            reclaimed = reclaim_export_staging([root])
        assert failure.called, 'failed Windows deletion probe did not reach payload deletion'
        assert not reclaimed['removed'], reclaimed
        assert reclaimed['errors'], reclaimed
        assert stage.exists() and (stage / 'payload.bin').exists(), 'failed Windows deletion removed payload'
        return {'stage': str(stage), 'result': reclaimed}


def check_native_abi(platform):
    import windows_export_staging_directory as win
    api = win._api()
    with tempfile.TemporaryDirectory() as base:
        path = Path(base) / 'abi-probe.bin'
        path.write_bytes(b'x')
        handle = win._Handle(api, path, delete=True)
        try:
            info = handle.information()
            assert info.links >= 1
            actual = os.stat(path)
            assert win._identity(info) == (actual.st_dev, actual.st_ino), 'Windows identity mapping mismatch'
            handle.delete()
        finally:
            handle.close()
        assert not path.exists(), 'native delete disposition did not remove the probe file'
    return {'module': win.__name__}


def check_private_retirement(platform):
    if importlib.util.find_spec('windows_export_staging_retirement') is None:
        raise UnsupportedCheck(
            'private Windows retirement module is not implemented in this handoff')
    raise UnsupportedCheck(
        'private Windows retirement module exists but this harness has no bound adapter; update the harness')


def check_power_loss(platform):
    raise UnsupportedCheck(
        'directory-entry power-loss durability is a documented limitation and remains unqualified')


def check_windows_finalization_missing(platform):
    raise UnsupportedCheck(
        'current Windows cleanup has no durable retirement/finalization state; '
        'hard termination during cleanup is not implemented or qualified')


def _git_state():
    try:
        head = subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'],
                                       text=True, stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ['git', '-C', str(REPO), 'status', '--porcelain', '--untracked-files=all'],
            text=True, stderr=subprocess.DEVNULL).strip())
        return {'head': head, 'dirty': dirty}
    except (OSError, subprocess.SubprocessError):
        return {'head': None, 'dirty': None}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', metavar='PATH', help='write the result JSON here')
    parser.add_argument('--require-windows', action='store_true',
                        help='fail if any native Windows check is unsupported')
    args = parser.parse_args(argv)

    run_check('posix_hard_termination_recovery', 'posix', lambda: check_hard_termination('posix'))
    run_check('posix_restart_recovery', 'posix', lambda: check_restart_recovery('posix'))
    run_check('posix_live_owner_preserved', 'posix', lambda: check_live_owner('posix'))
    run_check('posix_parent_replacement', 'posix', lambda: check_parent_replacement('posix'))
    run_check('posix_malformed_receipt', 'posix', lambda: check_malformed_receipt('posix'))
    run_check('posix_failed_deletion_preserves_payload', 'posix', lambda: check_failed_deletion('posix'))
    run_check('posix_reparse_point_refused', 'posix', lambda: check_reparse_point('posix'))
    run_check('posix_hard_termination_finalization', 'posix',
              lambda: check_finalization_recovery('posix', False))
    run_check('posix_restart_finalization', 'posix',
              lambda: check_finalization_recovery('posix', True))
    run_check('posix_partial_creation_recovery', 'posix',
              lambda: check_partial_creation_recovery('posix', False))
    run_check('posix_partial_creation_restart', 'posix',
              lambda: check_partial_creation_recovery('posix', True))
    run_check('posix_moved_unverified_recovery', 'posix',
              lambda: check_finalization_recovery('posix', False, unverified=True))

    run_check('windows_partial_creation_recovery', 'windows',
              lambda: check_windows_partial_creation_missing('windows'), required=True)
    run_check('windows_hard_termination_recovery', 'windows',
              lambda: check_windows_current('windows'), required=True)
    run_check('windows_restart_recovery', 'windows',
              lambda: check_restart_recovery('windows'), required=True)
    run_check('windows_live_owner_preserved', 'windows',
              lambda: check_windows_live_owner('windows'), required=True)
    run_check('windows_parent_replacement_refused', 'windows',
              lambda: check_windows_parent_replacement('windows'), required=True)
    run_check('windows_reparse_point_refused', 'windows',
              lambda: check_windows_reparse('windows'), required=True)
    run_check('windows_malformed_marker_preserved', 'windows',
              lambda: check_windows_malformed_marker('windows'), required=True)
    run_check('windows_failed_deletion_preserves_payload', 'windows',
              lambda: check_windows_failed_deletion('windows'), required=True)
    run_check('windows_readonly_deletion_preserved', 'windows',
              lambda: check_windows_readonly_deletion('windows'), required=True)
    run_check('windows_native_abi', 'windows',
              lambda: check_native_abi('windows'), required=True)

    run_check('windows_hard_termination_finalization', 'windows',
              lambda: check_windows_finalization_missing('windows'), required=True)
    run_check('windows_restart_finalization', 'windows',
              lambda: check_windows_finalization_missing('windows'), required=True)
    run_check('windows_private_retirement_state_machine', 'windows',
              lambda: check_private_retirement('windows'), required=True)
    run_check('windows_private_retirement_live_owner', 'windows',
              lambda: check_private_retirement('windows'), required=True)
    run_check('windows_private_retirement_malformed_receipt', 'windows',
              lambda: check_private_retirement('windows'), required=True)
    run_check('windows_private_retirement_failed_deletion', 'windows',
              lambda: check_private_retirement('windows'), required=True)
    run_check('windows_power_loss_directory_durability', 'both',
              lambda: check_power_loss('both'), required=True)

    passed = sum(item['status'] == 'passed' for item in CHECKS)
    failed = sum(item['status'] == 'failed' for item in CHECKS)
    unsupported = sum(item['status'] == 'unsupported' for item in CHECKS)
    required_unsupported = sum(item['status'] == 'unsupported' and item['required']
                               for item in CHECKS)
    result = {
        'platform': 'windows' if IS_WINDOWS else 'posix',
        'python': sys.version,
        'repository': str(REPO),
        'git': _git_state(),
        'environment': {
            'platform': platform_module.platform(),
            'machine': platform_module.machine(),
            'tempdir': tempfile.gettempdir(),
            'windows_version': (list(sys.getwindowsversion())
                                if IS_WINDOWS else None),
        },
        'summary': {'passed': passed, 'failed': failed, 'unsupported': unsupported,
                    'required_unsupported': required_unsupported,
                    'total': len(CHECKS),
                    'qualified': failed == 0 and required_unsupported == 0},
        'checks': CHECKS,
        'limitations': [
            'Unsupported checks are never passes.',
            'No native Windows runner was available for this run unless platform=windows.',
            'Private Windows retirement and power-loss directory durability remain unimplemented/unqualified.',
            'Synthetic temp files only; no application, model, GPU or user project data.',
        ],
    }
    encoded = json.dumps(result, indent=2, sort_keys=True)
    if args.json:
        destination = Path(args.json)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(encoded + '\n', encoding='utf-8')
    print(encoded)
    if failed:
        return 1
    if args.require_windows and result['summary']['required_unsupported']:
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
