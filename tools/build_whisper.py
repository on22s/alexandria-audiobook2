"""Build pinned Whisper assets without reporting success after a failed command."""
import importlib.metadata
import os
import re
import subprocess
import sys
from pathlib import Path

from verify_whisper_assets import verify_whisper_assets
from subprocess_ownership import run_owned_capture

ROOT = Path(__file__).resolve().parents[1]
MARKER = ROOT / 'app' / '.whisper-install-ok'
CMAKE_VERSION = '3.31.10'
def run_build_command(command, *, cwd):
    """Keep compilers in the launcher's existing process group and show logs."""
    subprocess.run(command, cwd=str(cwd), check=True)


def build(backend, root=ROOT, marker=MARKER):
    marker.unlink(missing_ok=True)
    verify_whisper_assets(root)
    if importlib.metadata.version('cmake') != CMAKE_VERSION:
        raise RuntimeError('The pinned CMake package is missing; rerun Install.')
    cmake = Path(sys.executable).parent / ('cmake.exe' if os.name == 'nt' else 'cmake')
    if not cmake.is_file():
        raise RuntimeError('CMake is missing from the application environment; rerun Install.')
    source = root / 'whisper.cpp'
    build_dir = source / 'build'
    flags = {'cpu': [], 'metal': ['-DGGML_METAL=ON'],
             'cuda': ['-DGGML_CUDA=ON'], 'hip': ['-DGGML_HIP=ON']}
    if backend not in flags:
        raise ValueError('Unknown Whisper build backend')
    # A retry may reuse a build directory configured for a different backend.
    selected = ['-DGGML_HIP=OFF', '-DGGML_CUDA=OFF', '-DGGML_METAL=OFF', *flags[backend]]
    if backend == 'hip':
        result = run_owned_capture(['rocminfo'], timeout=30, text=True)
        result.check_returncode()
        target = re.search(r'^\s*Name:\s*(gfx[0-9a-z]+)\s*$', result.stdout, re.MULTILINE)
        if not target:
            raise RuntimeError('Could not determine an AMD GPU target from rocminfo.')
        selected.append('-DAMDGPU_TARGETS=' + target.group(1))
    run_build_command([str(cmake), '-S', str(source), '-B', str(build_dir), *selected], cwd=root)
    run_build_command([str(cmake), '--build', str(build_dir), '--config', 'Release', '-j', '4'], cwd=root)
    paths = [build_dir / 'bin' / 'whisper-cli', build_dir / 'bin' / 'Release' / 'whisper-cli.exe',
             build_dir / 'Release' / 'whisper-cli.exe']
    binary = next((p for p in paths if p.is_file() and os.access(p, os.X_OK)), None)
    if binary is None:
        raise RuntimeError('Whisper build completed without an executable whisper-cli.')
    subprocess.run([str(binary), '--help'], cwd=str(root), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    marker.write_text('Whisper build verified\n', encoding='utf-8')
    (marker.parent / '.whisper-install-pending').unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Expected one Whisper backend')
        build(sys.argv[1])
    except Exception as error:
        print(f'Whisper installation failed: {error}', file=sys.stderr)
        sys.exit(1)
