"""Engine side of the optional Apple MLX backend (#1442).

The worker (mlx_tts_worker.py) runs in its own env because mlx-audio needs
transformers 5.x and app/env pins 4.x. It is an owned subprocess: AA2's
ownership helpers start and stop it, cancellation stops it (nothing partial is
published), and idle release stops it so its unified memory is returned.
"""
from pathlib import Path
import json
import os
import platform
import queue
import subprocess
import sys
import threading
import time

from subprocess_ownership import start_owned_subprocess, stop_owned_subprocess

APP_DIR = Path(__file__).resolve().parent
WORKER = APP_DIR / 'mlx_tts_worker.py'
DEFAULT_MLX_MODELS = {
    'custom': 'mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16',
    'clone': 'mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16',
    'design': 'mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16',
}


class MLXGenerationCancelled(Exception):
    pass


class MLXBackendUnavailable(RuntimeError):
    """MLX mode was selected but this machine cannot run it yet."""


def get_default_mlx_python():
    return str(APP_DIR.parent / 'mlx_env' / 'bin' / 'python')


def is_mlx_platform():
    return sys.platform == 'darwin' and platform.machine() == 'arm64'


def get_mlx_settings(tts_config):
    """Resolved interpreter and per-family model ids or paths; pure read."""
    models = dict(DEFAULT_MLX_MODELS)
    models.update({family: value for family, value in (tts_config.get('mlx_models') or {}).items()
                   if family in DEFAULT_MLX_MODELS and isinstance(value, str) and value.strip()})
    return {'python': tts_config.get('mlx_python') or get_default_mlx_python(), 'models': models}


def get_mlx_backend_status(settings):
    """What is missing for MLX mode, in words a user can act on; never starts anything."""
    problems = []
    if not is_mlx_platform():
        problems.append('MLX mode needs an Apple Silicon Mac (macOS on arm64).')
    if not os.path.isfile(settings['python']):
        problems.append(f"The MLX environment is missing ({settings['python']}). "
                        'Re-run Install on this Mac to create mlx_env, or set tts.mlx_python.')
    else:
        # A venv whose package install failed still has its interpreter.
        lib = Path(settings['python']).parent.parent / 'lib'
        if lib.is_dir() and not any(lib.glob('python3*/site-packages/mlx_audio')):
            problems.append(f"The MLX environment is incomplete: mlx-audio is not installed in {lib.parent}. "
                            'Re-run Install on this Mac.')
    for family, model in settings['models'].items():
        if os.path.isabs(model) and not os.path.isdir(model):
            problems.append(f'The {family} MLX model folder is missing: {model}')
    return {'ready': not problems, 'problems': problems, 'python': settings['python'],
            'models': dict(settings['models'])}


class MLXWorkerClient:
    """One owned worker process, one request at a time."""

    def __init__(self, python, *, worker=WORKER, start_timeout=600):
        self._python = python
        self._worker = str(worker)
        self._lock = threading.Lock()
        self._process = None
        self._replies = None
        self._next_id = 0
        self._start_timeout = start_timeout

    def _ensure_started(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._stop_process()
        if not os.path.isfile(self._python):
            raise FileNotFoundError(f'MLX environment missing: {self._python}')
        process = start_owned_subprocess(
            [self._python, '-u', self._worker], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding='utf-8', start_new_session=True, cwd=str(APP_DIR))
        replies = queue.Queue()

        def read():
            for line in process.stdout:
                replies.put(line)
            replies.put(None)
        threading.Thread(target=read, daemon=True, name='mlx-tts-reply').start()
        self._process, self._replies = process, replies

    def _stop_process(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        stop_owned_subprocess(process, timeout=5, force_after_grace=True)
        if process.stdout:
            process.stdout.close()

    def request(self, payload, cancel_check=lambda: False, timeout=3600):
        """Send one request; on cancellation the worker is stopped, not left running."""
        with self._lock:
            self._ensure_started()
            self._next_id += 1
            payload = dict(payload, id=self._next_id)
            self._process.stdin.write(json.dumps(payload) + '\n')
            self._process.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                if cancel_check():
                    self._stop_process()
                    raise MLXGenerationCancelled('MLX generation cancelled.')
                if time.monotonic() > deadline:
                    self._stop_process()
                    raise TimeoutError(f'MLX generation exceeded {timeout}s.')
                try:
                    line = self._replies.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    code = self._process.poll() if self._process else None
                    self._stop_process()
                    raise RuntimeError(f'MLX worker exited unexpectedly (exit code {code}).')
                reply = json.loads(line)
                if reply.get('id') != payload['id']:
                    continue
                if not reply.get('ok'):
                    raise RuntimeError(f"MLX {reply.get('type', 'error')}: {reply.get('error', '')}")
                return reply

    def ensure_stopped_if_idle(self):
        """Stop the worker unless a request is running; True when it is stopped."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            self._stop_process()
            return True
        finally:
            self._lock.release()
