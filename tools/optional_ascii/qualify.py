"""Bounded optional-package qualification using only staged synthetic inputs."""
import argparse
import base64
import csv
from email.parser import Parser
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile

import build
from packaging.specifiers import SpecifierSet
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run(command, **kwargs):
    return subprocess.check_output(command, text=True, timeout=300, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--work', required=True)
    parser.add_argument('--architecture', choices=['x86_64', 'arm64'], required=True)
    args = parser.parse_args()
    actual = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(platform.machine().lower(), platform.machine().lower())
    if actual != args.architecture:
        raise RuntimeError('Runner architecture does not match the declared lane')
    os.environ['PATH'] = str(Path(sys.executable).parent) + os.pathsep + os.environ['PATH']
    crate = Path(__file__).resolve().parent
    repo = crate.parents[1]
    work = Path(args.work).resolve()
    if work == repo or repo in work.parents:
        raise ValueError('Work must be outside the source tree')
    work.mkdir(parents=True, exist_ok=False)
    target = work / 'target'
    os.environ.update(CARGO_TARGET_DIR=str(target), CARGO_NET_OFFLINE='true',
                      CARGO_BUILD_JOBS='1', PYO3_PYTHON=sys.executable,
                      PIP_NO_INDEX='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    source = {name: digest((crate / name).read_bytes()) for name in
              ['pyproject.toml', 'Cargo.toml', 'Cargo.lock', 'build.rs', 'src/lib.rs', 'README.md', 'PACKAGING.md', 'build-tools.txt', 'qualify.py']}
    sdist_dir = work / 'sdist'
    sdist_dir.mkdir()
    builder = build.ProjectBuilder(str(crate))
    if builder.check_dependencies('sdist'):
        raise RuntimeError('Missing pinned sdist build dependencies')
    sdist = sdist_dir / builder.build('sdist', str(sdist_dir))
    extraction = work / 'extracted'
    extraction.mkdir()
    with tarfile.open(sdist) as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > 10 * 1024 * 1024:
            raise ValueError('Unexpected sdist size')
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe archive member')
            if member.isfile() and (path.suffix in ['.so', '.pyd', '.dll', '.dylib'] or 'target' in path.parts):
                raise ValueError('Prebuilt payload in sdist')
        archive.extractall(extraction)
    roots = list(extraction.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError('Unexpected sdist layout')
    extracted = roots[0]
    for name, expected in source.items():
        if digest((extracted / name).read_bytes()) != expected:
            raise ValueError('Archive source differs: ' + name)
    if not (extracted / 'PKG-INFO').is_file():
        raise ValueError('Missing PKG-INFO')
    # A new target proves this wheel is compiled from the extracted sdist.
    target = work / 'wheel-target'
    os.environ['CARGO_TARGET_DIR'] = str(target)
    if target.exists():
        raise ValueError('Wheel target must be fresh')
    wheel_dir = work / 'wheel'
    wheel_dir.mkdir()
    builder = build.ProjectBuilder(str(extracted))
    if builder.check_dependencies('wheel'):
        raise RuntimeError('Missing pinned wheel build dependencies')
    settings = {'build-args': '--compatibility manylinux_2_34 --auditwheel check'} if sys.platform == 'linux' else {}
    wheel = wheel_dir / builder.build('wheel', str(wheel_dir), config_settings=settings)
    name, version, _, tags = parse_wheel_filename(wheel.name)
    abi = 'cp' + str(sys.version_info.major) + str(sys.version_info.minor)
    if name != 'aa2-ascii-native' or str(version) != '0.1.0' or not (tags & set(sys_tags())):
        raise ValueError('Unexpected distribution or incompatible wheel')
    if any(t.interpreter != abi or t.abi != abi for t in tags):
        raise ValueError('Unexpected Python ABI tags')
    if sys.platform == 'linux' and any(t.platform != 'manylinux_2_34_x86_64' for t in tags):
        raise ValueError('Linux wheel lacks the required audited manylinux tag')
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        metadata = z.read(next(n for n in names if n.endswith('/METADATA'))).decode()
        parsed = Parser().parsestr(metadata)
        if parsed.get_all('Requires-Dist'):
            raise ValueError('Optional extension gained runtime dependencies')
        supported = SpecifierSet(parsed['Requires-Python'])
        if not all(v in supported for v in ['3.10.0', '3.12.0']) or any(v in supported for v in ['3.9.0', '3.11.0', '3.13.0']):
            raise ValueError('Unexpected Python version admission')
        binaries = [n for n in names if n.endswith(('.so', '.pyd'))]
        if len(binaries) != 1:
            raise ValueError('Unexpected extension inventory')
        binary_hash = digest(z.read(binaries[0]))
        record = next(n for n in names if n.endswith('/RECORD'))
        rows = list(csv.reader(io.StringIO(z.read(record).decode())))
        if {row[0] for row in rows} != set(names):
            raise ValueError('Incomplete wheel RECORD')
        for path, hash_value, size in rows:
            if path == record:
                if hash_value or size:
                    raise ValueError('RECORD must not hash itself')
                continue
            data = z.read(path)
            expected = 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            if hash_value != expected or size != str(len(data)):
                raise ValueError('Incorrect wheel RECORD')
    runtime = work / 'runtime'
    runtime.mkdir()
    for filename in ['speech_text.py', 'speech_policy.py', 'ascii_speech_native.py', 'pronunciation.py']:
        shutil.copyfile(repo / 'app' / filename, runtime / filename)
    (runtime / 'lex.json').write_text('{}')
    venv.EnvBuilder(with_pip=True, symlinks=os.name != 'nt').create(work / 'venv')
    python = work / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    probe = '''
import ast,hashlib,importlib,json,os,sys
from pathlib import Path
runtime=Path(sys.argv[1]);sys.path.insert(0,str(runtime))
import speech_text as st, speech_policy as policy, pronunciation, ascii_speech_native as loader
pronunciation.DEFAULT_PATH=str(runtime/'lex.json')
requested=os.environ['AA2_EXPERIMENTAL_ASCII_NATIVE']=='1';installed=sys.argv[2]=='installed'
node=next(x for x in ast.parse((runtime/'speech_text.py').read_text()).body if isinstance(x,ast.FunctionDef) and x.name=='verbalize_symbols')
node.name='_reference';node.body=[node.body[0]]+node.body[2:]
exec(compile(ast.Module(body=[node],type_ignores=[]),'frozen-python-loop','exec'),st.__dict__)
cases=['','A'*63,'A'*64,'A'*4096,'A'*4097,'&\\x00'*64,'日本語'*64,'a'*64+'\\ud800',''.join(chr(i) for i in range(128))]
for text in cases:assert st.verbalize_symbols(text)==st._reference(text)
if requested and installed:
 assert loader.get_ascii_native_status()['state']=='active'
 import aa2_ascii_native
 extension=importlib.import_module('aa2_ascii_native.aa2_ascii_native')
 path=Path(extension.__file__).resolve();assert Path(sys.prefix).resolve() in path.parents
 assert hashlib.sha256(path.read_bytes()).hexdigest()==sys.argv[3]
 policy.SPOKEN_SYMBOLS['A']='alpha'
 assert st.verbalize_symbols('A'*128)==st._reference('A'*128)
 assert loader.get_ascii_native_status()['state']=='invalidated_python_fallback'
else:
 assert 'aa2_ascii_native' not in sys.modules
 assert loader.get_ascii_native_status()['state']==('unavailable_python_fallback' if requested else 'disabled')
print(json.dumps({'state':loader.get_ascii_native_status()['state'],'cases':len(cases),'requested':requested,'installed':installed}))
'''
    checks = []
    def probes(phase):
        for flag in ['0', '1']:
            env = dict(os.environ, AA2_EXPERIMENTAL_ASCII_NATIVE=flag)
            env.pop('PYTHONPATH', None)
            checks.append(json.loads(run([str(python), '-I', '-c', probe, str(runtime), phase, binary_hash], cwd=work, env=env)))
    probes('absent')
    run([str(python), '-I', '-m', 'pip', 'install', '--no-index', '--no-deps', '--no-cache-dir', str(wheel)], cwd=work)
    probes('installed')
    run([str(python), '-I', '-m', 'pip', 'uninstall', '-y', 'aa2-ascii-native'], cwd=work)
    # Verify uninstall removes both the module and distribution metadata.
    run([str(python), '-I', '-c', "import importlib.util,importlib.metadata; assert importlib.util.find_spec('aa2_ascii_native') is None; assert not [d for d in importlib.metadata.distributions() if d.metadata['Name']=='aa2-ascii-native']"], cwd=work)
    probes('absent')
    result = {'status': 'passed', 'source_sha256': source, 'sdist_sha256': digest(sdist.read_bytes()),
              'wheel_sha256': digest(wheel.read_bytes()), 'binary_sha256': binary_hash,
              'wheel': wheel.name, 'python': sys.version, 'host': platform.platform(),
              'architecture': actual, 'rustc': run(['rustc', '-Vv']), 'cargo': run(['cargo', '-V']),
              'maturin': run([sys.executable, '-m', 'maturin', '--version']),
              'build': build.__version__, 'checks': checks,
              'minimum_os_execution': 'unrun', 'genuine_upgrade': 'unrun',
              'actual_free_threaded_execution': 'unrun', 'allocator_exhaustion': 'unrun'}
    (work / 'receipt.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
