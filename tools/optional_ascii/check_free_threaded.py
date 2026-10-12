"""Actual free-threaded rejection checks; never qualify native execution."""
import argparse
import ast
import hashlib
import importlib.abc
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig


BUILD_REJECTION = (
    'aa2_ascii_native requires a GIL-enabled CPython build; '
    'free-threaded builds are unsupported'
)


def get_free_threaded_state():
    flag = sysconfig.get_config_var('Py_GIL_DISABLED')
    probe = getattr(sys, '_is_gil_enabled', None)
    if type(flag) is not int or flag != 1 or not callable(probe):
        raise RuntimeError('A real free-threaded CPython interpreter is required')
    enabled = probe()
    if type(enabled) is not bool:
        raise RuntimeError('Unknown actual GIL state')
    return {'python': sys.version, 'build_flag': flag, 'gil_enabled': enabled}


def probe_runtime(runtime, forced_gil):
    state = get_free_threaded_state()
    if state['gil_enabled'] != (forced_gil == '1'):
        raise AssertionError('The requested actual GIL mode was not applied')
    attempts = []

    class RefuseNativeImport(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'aa2_ascii_native' or fullname.startswith('aa2_ascii_native.'):
                attempts.append(fullname)
                raise AssertionError('Unsupported runtime attempted native import')
            return None

    sys.meta_path.insert(0, RefuseNativeImport())
    sys.path.insert(0, str(runtime))
    import speech_text as speech
    import ascii_speech_native as loader
    node = next(n for n in ast.parse((runtime / 'speech_text.py').read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name == 'verbalize_symbols')
    node.name = '_reference'
    # Retain the existing Python loop, removing only its native dispatch block.
    if (not isinstance(node.body[1], ast.If)
            or ast.unparse(node.body[1].test) != 'ascii_speech_native.IS_REQUESTED'):
        raise AssertionError('Unexpected reference-loop layout')
    node.body = [node.body[0]] + node.body[2:]
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'reference-loop', 'exec'), speech.__dict__)
    cases = ['', 'A' * 63, 'A' * 64, 'A' * 4096, 'A' * 4097,
             '&\x00' * 64, '日本語' * 64, 'a' * 64 + '\ud800']
    for text in cases:
        if speech.verbalize_symbols(text) != speech._reference(text):
            raise AssertionError('Python fallback parity mismatch')
    requested = os.environ['AA2_EXPERIMENTAL_ASCII_NATIVE'] == '1'
    expected = ('unsupported_runtime_python_fallback' if requested else 'disabled')
    status = loader.get_ascii_native_status()
    if (status['state'] != expected
            or status['runtime_rejection'] != ('free_threaded_build' if requested else None)
            or attempts or 'aa2_ascii_native' in sys.modules
            or any(k.startswith('aa2_ascii_native.') for k in sys.modules)):
        raise AssertionError('Fallback did not reject before native import')
    # Check again after imports: nothing silently enabled the GIL in off mode.
    if get_free_threaded_state()['gil_enabled'] != state['gil_enabled']:
        raise AssertionError('GIL state changed during fallback checks')
    return {**state, 'requested': requested, 'state': status['state'],
            'native_import_attempts': attempts, 'parity_cases': len(cases)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work')
    parser.add_argument('--runtime')
    parser.add_argument('--forced-gil', choices=['0', '1'])
    args = parser.parse_args()
    actual = get_free_threaded_state()  # Classic builds fail; never simulate it.
    if args.runtime:
        if args.forced_gil is None:
            parser.error('--forced-gil is required for the isolated runtime probe')
        print(json.dumps(probe_runtime(Path(args.runtime), args.forced_gil)))
        return
    if not args.work:
        parser.error('--work is required')
    crate = Path(__file__).resolve().parent
    repo = crate.parents[1]
    work = Path(args.work).resolve()
    if work == repo or repo in work.parents:
        raise ValueError('Work must be outside the source tree')
    work.mkdir(parents=True, exist_ok=False)
    runtime = work / 'runtime'
    runtime.mkdir()
    source_hashes = {}
    for filename in ['speech_text.py', 'speech_policy.py', 'ascii_speech_native.py']:
        source = repo / 'app' / filename
        shutil.copyfile(source, runtime / filename)
        source_hashes['app/' + filename] = hashlib.sha256(source.read_bytes()).hexdigest()
    for filename in ['Cargo.toml', 'Cargo.lock', 'build.rs', 'src/lib.rs']:
        source_hashes['tools/optional_ascii/' + filename] = hashlib.sha256((crate / filename).read_bytes()).hexdigest()
    builds, probes = [], []
    for forced_gil in ['1', '0']:
        env = dict(os.environ, PYTHON_GIL=forced_gil, PYO3_PYTHON=sys.executable,
                   CARGO_NET_OFFLINE='true', CARGO_BUILD_JOBS='1', CARGO_TERM_COLOR='never',
                   CARGO_TARGET_DIR=str(work / ('target-gil-' + forced_gil)))
        result = subprocess.run(
            ['cargo', 'build', '--release', '--locked', '--offline',
             '--manifest-path', str(crate / 'Cargo.toml')],
            env=env, text=True, capture_output=True, timeout=300, check=False)
        output = result.stdout + result.stderr
        (work / ('build-gil-' + forced_gil + '.log')).write_text(output)
        if (result.returncode != 101 or BUILD_REJECTION not in output
                or 'failed to run custom build command for `aa2_ascii_native' not in output
                or 'panicked at' not in output):
            raise AssertionError('Source build did not reach the real free-threaded guard: '
                                 + output[-4096:])
        builds.append({'forced_gil': forced_gil, 'returncode': result.returncode,
                       'actual_guard_message_verified': True})
        for requested in ['0', '1']:
            probe_env = dict(env, AA2_EXPERIMENTAL_ASCII_NATIVE=requested)
            probe_env.pop('PYTHONPATH', None)
            output = subprocess.check_output(
                [sys.executable, '-I', '-X', 'gil=' + forced_gil, str(Path(__file__).resolve()),
                 '--runtime', str(runtime), '--forced-gil', forced_gil],
                env=probe_env, cwd=work, text=True, timeout=30)
            probes.append(json.loads(output))
    result = {'status': 'passed', 'actual_interpreter': actual,
              'source_sha256': source_hashes, 'source_build_rejections': builds,
              'pre_import_fallback_probes': probes, 'native_execution': 'never attempted',
              'qualification': 'Linux CPython3.13t rejection only; not whole-app/Python3.13 support'}
    (work / 'free-threaded-rejection.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
