'''Native Windows checks that aliased temporary roots reach sound-asset admission.'''
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

MODULES = ('tests.test_sound_design_stage_ownership',
           'tests.test_sound_design_upload_ownership',
           'tests.test_source_upload_stage',
           'tests.test_sound_design_asset_routes')
EXPECTED_TEST_COUNT = 55
EXPECTED_TEST_IDS_SHA256 = 'a66342ba6f37ee457d7705cfa9bbf51efb6779cad810fc2b535e22f0252bc025'
WORKER_FLAG = '--run-modules-under-alias'
BASE_PREFIX = 'aa2-sound-alias-'
HOSTILE_VARIABLE = 'AA2_HOSTILE_EXPANSION'
HOSTILE_SEGMENT = '& aa2 %AA2_HOSTILE_EXPANSION% ! caret ^ (paren)'
HOSTILE_PAYLOAD = 'payload.txt'


def derived_test_ids():
    ids = []
    for module in MODULES:
        path = Path(__file__).resolve().parent / (module.rsplit('.', 1)[1] + '.py')
        tree = ast.parse(path.read_text(encoding='utf-8'))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith('test_'):
                    ids.append(f'{module}.{node.name}.{item.name}')
    return sorted(ids)


def identity_digest(ids):
    return hashlib.sha256('\n'.join(ids).encode('utf-8')).hexdigest()


def _source_identity_matches(ids):
    return len(ids) == EXPECTED_TEST_COUNT and identity_digest(ids) == EXPECTED_TEST_IDS_SHA256


def _loaded_test_ids(suite):
    ids = []
    stack = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(item)
        else:
            ids.append(item.id())
    return sorted(ids)


def _store_refusal(message):
    return 'without aliases' in message


def _receipt(outcome, **fields):
    print(json.dumps({'outcome': outcome, **fields}, sort_keys=True))


def _run_modules_under_alias(alias_root):
    alias = os.path.abspath(alias_root)
    derived = derived_test_ids()
    if not _source_identity_matches(derived):
        _receipt('source-identity-mismatch', source=len(derived), expected=EXPECTED_TEST_COUNT)
        return 1
    tempfile.tempdir = alias
    observed = os.path.abspath(tempfile.gettempdir())
    if os.path.normcase(os.path.realpath(observed)) == os.path.normcase(observed):
        _receipt('root-not-aliased', tests=0)
        return 2
    with tempfile.TemporaryDirectory() as directory:
        raw = os.path.abspath(directory)
        if os.path.normcase(os.path.realpath(raw)) == os.path.normcase(raw):
            _receipt('child-not-aliased', tests=0)
            return 2
        if not os.path.normcase(raw).startswith(os.path.normcase(alias) + os.sep):
            _receipt('child-escaped', tests=0)
            return 2
    from sound_design_assets import SoundDesignAssetStore
    SoundDesignAssetStore(os.path.realpath(alias))
    try:
        SoundDesignAssetStore(alias)
    except ValueError as error:
        store_refused_alias = _store_refusal(str(error))
        if not store_refused_alias:
            raise
    else:
        _receipt('store-accepted-alias', tests=0)
        return 1
    suite = unittest.defaultTestLoader.loadTestsFromNames(MODULES)
    loaded = _loaded_test_ids(suite)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    source_identity = _source_identity_matches(loaded)
    outcome = 'passed'
    if not source_identity:
        outcome = 'identity-mismatch'
    elif not result.wasSuccessful() or result.testsRun != EXPECTED_TEST_COUNT or result.skipped:
        outcome = 'suite-failed'
    _receipt(outcome, tests=result.testsRun, expected=EXPECTED_TEST_COUNT,
             identity_sha256=identity_digest(loaded),
             failures=len(result.failures), errors=len(result.errors),
             skips=len(result.skipped), success=result.wasSuccessful(),
             store_refused_alias=store_refused_alias, source_identity=source_identity)
    return 0 if outcome == 'passed' else 1


def _remove_aliased_roots(base, alias):
    absolute = os.path.abspath(str(base))
    parent = os.path.normcase(os.path.realpath(str(base.parent)))
    host = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    if (absolute != str(base) or not base.name.startswith(BASE_PREFIX)
            or parent != host or alias != base / 'aliased-root'):
        raise RuntimeError(f'Refusing to remove an unverified path: {base}')
    if os.path.lexists(str(alias)):
        os.rmdir(str(alias))
    if base.exists():
        shutil.rmtree(base)


def _create_junction(target, alias):
    try:
        from _winapi import CreateJunction
    except ImportError as error:
        raise RuntimeError('The native _winapi.CreateJunction primitive is unavailable, '
                           'so no real junction can be created: ' + str(error)) from error
    try:
        CreateJunction(str(target), str(alias))
    except OSError as error:
        raise RuntimeError('Could not create a real junction: ' + str(error)) from error


def _assert_real_junction(target, alias):
    canonical = os.path.normcase(os.path.realpath(str(target)))
    junction = os.path.normcase(os.path.realpath(str(alias)))
    if junction != canonical or junction == os.path.normcase(str(alias)):
        raise RuntimeError('The created path is not a real junction')
    info = os.lstat(str(alias))
    if not stat.S_ISDIR(info.st_mode) or info.st_reparse_tag != stat.IO_REPARSE_TAG_MOUNT_POINT:
        raise RuntimeError('The created path is not a real junction')


def _create_aliased_roots():
    base = Path(tempfile.mkdtemp(prefix=BASE_PREFIX))
    target = base / 'canonical-root'
    alias = base / 'aliased-root'
    try:
        target.mkdir()
        _create_junction(target, alias)
        _assert_real_junction(target, alias)
    except BaseException:
        _remove_aliased_roots(base, alias)
        raise
    return base, target, alias


def _verify_hostile_junction(target, alias):
    escaped = target.parent.parent / (target.parent.name + '-escaped-outside-owned-base')
    previous = os.environ.get(HOSTILE_VARIABLE)
    os.environ[HOSTILE_VARIABLE] = str(escaped)
    try:
        target.mkdir()
        _create_junction(target, alias)
        _assert_real_junction(target, alias)
        if os.path.lexists(str(escaped)):
            raise RuntimeError('A hostile junction expanded an environment variable '
                               'outside its owned base: ' + str(escaped))
        link = os.readlink(str(alias))
        if HOSTILE_SEGMENT not in link:
            raise RuntimeError('A hostile junction name was rewritten before reaching '
                               'the filesystem: ' + link)
        (target / HOSTILE_PAYLOAD).write_text(HOSTILE_PAYLOAD, encoding='utf-8')
        if not os.path.isfile(str(alias / HOSTILE_PAYLOAD)):
            raise RuntimeError('A hostile junction did not reach its target: ' + str(alias))
    finally:
        if previous is None:
            os.environ.pop(HOSTILE_VARIABLE, None)
        else:
            os.environ[HOSTILE_VARIABLE] = previous


def _read_receipt(stdout):
    for line in reversed(stdout.strip().splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'outcome' in value:
            return value
    return None


def _run_checks():
    base, target, alias = _create_aliased_roots()
    hostile_target = base / HOSTILE_SEGMENT
    hostile_alias = base / (HOSTILE_SEGMENT + '-aliased-root')
    try:
        try:
            _verify_hostile_junction(hostile_target, hostile_alias)
            env = {**os.environ,
                   'PYTHONPATH': os.pathsep.join(filter(None, (
                       str(Path(__file__).resolve().parents[1]), os.environ.get('PYTHONPATH')))),
                   'TEMP': str(alias), 'TMP': str(alias)}
            completed = subprocess.run(
                [sys.executable, '-B', str(Path(__file__).resolve()), WORKER_FLAG, str(alias)],
                capture_output=True, text=True, env=env, timeout=600)
        finally:
            try:
                if os.path.lexists(str(hostile_alias)):
                    os.rmdir(str(hostile_alias))
            finally:
                _remove_aliased_roots(base, alias)
    except subprocess.TimeoutExpired as error:
        _receipt('child-timeout', timeout_seconds=error.timeout,
                 alias=str(alias), target=str(target))
        return 1
    sys.stderr.write(completed.stderr)
    child = _read_receipt(completed.stdout)
    fields = {'child_exit': completed.returncode, 'child': child,
              'alias': str(alias), 'target': str(target)}
    if completed.returncode != 0:
        _receipt('child-failed', **fields)
        return 1
    if child is None:
        _receipt('no-receipt', **fields)
        return 1
    if (child.get('outcome') != 'passed'
            or child.get('tests') != EXPECTED_TEST_COUNT
            or child.get('identity_sha256') != EXPECTED_TEST_IDS_SHA256
            or child.get('skips') != 0 or child.get('failures') != 0
            or child.get('errors') != 0 or child.get('success') is not True
            or child.get('store_refused_alias') is not True):
        _receipt('receipt-invalid', **fields)
        return 1
    _receipt('passed', **fields)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and (argv[0] != WORKER_FLAG or len(argv) != 2 or not argv[1]):
        print(f'Usage: {Path(__file__).name} [{WORKER_FLAG} <aliased-root>]', file=sys.stderr)
        return 2
    if sys.platform != 'win32':
        print('Native Windows aliased temporary-root checks require Windows; '
              'nothing was verified.', file=sys.stderr)
        return 2
    if argv:
        return _run_modules_under_alias(argv[1])
    return _run_checks()


if __name__ == '__main__':
    raise SystemExit(main())
