"""Cached Gradio clients in owned interpreters with locally enforceable deadlines."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref

from diagnostics import redact_text
from subprocess_ownership import (start_owned_subprocess, stop_owned_subprocess,
                                  is_subprocess_tree_running)
from utils import atomic_json_write
import external_tts_capabilities


_REQUEST_CONTROL = ContextVar("external_tts_request_control", default=None)
PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 2 * 1024**2
MAX_RESPONSE_BYTES = external_tts_capabilities.MAX_PROTOCOL_RESPONSE_BYTES


def get_external_tts_settings(tts_settings):
    from config_settings import TTSConfig
    settings = {name: tts_settings.get(name, field.get_default(call_default_factory=True))
                for name, field in TTSConfig.model_fields.items()
                if name in ('mode', 'url', 'external_urls', 'external_timeout_seconds')}
    pool = tts_settings.get("external_urls") or []
    if not isinstance(pool, list) or any(not isinstance(url, str) for url in pool):
        raise ValueError("Saved TTS server pool is invalid.")
    settings['external_urls'] = [url.strip() for url in pool if url.strip()]
    settings['external_timeout_seconds'] = int(settings['external_timeout_seconds'] or TTSConfig.model_fields['external_timeout_seconds'].default)
    return settings


def get_external_tts_urls(tts_settings):
    settings = get_external_tts_settings(tts_settings)
    return settings['external_urls'] or [settings['url']]


@contextmanager
def ensure_external_request_control(deadline, cancelled):
    token = _REQUEST_CONTROL.set((deadline, cancelled))
    try:
        yield
    finally:
        _REQUEST_CONTROL.reset(token)


def get_bounded_json(path, limit):
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("External TTS protocol document exceeds its byte limit")
    return json.loads(raw)


def copy_external_inputs(value, directory):
    """Capture public FileData uploads under the SDK process's retained owner."""
    copied = 0
    def capture(item):
        nonlocal copied
        if isinstance(item, dict):
            meta = item.get("meta")
            if isinstance(meta, dict) and meta.get("_type") == "gradio.FileData":
                path = item.get("path")
                if isinstance(path, str) and not path.lower().startswith(("http://", "https://")):
                    source = Path(path)
                    destination = directory / f"input-{copied}{source.suffix}"
                    copied += 1
                    shutil.copyfile(source, destination)
                    return dict(item, path=str(destination))
            return {key: capture(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [capture(entry) for entry in item]
        return item
    return capture(value)


class ExternalPrediction(tuple):
    """A copied provider result whose private files belong to the caller."""

    def __new__(cls, path, directory, owner=None):
        result = super().__new__(cls, (path,))
        result.directory = directory
        result.owner = owner
        return result

    def close(self):
        if self.directory is not None:
            if self.owner is not None:
                self.owner.cleanup()
                self.owner = None
                self.directory = None
                return
            try:
                shutil.rmtree(self.directory)
            except FileNotFoundError:
                pass  # A concurrent client close already removed the private root.
            self.directory = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def stop_external_client_worker(process):
    """Stop a disposable SDK and confirm its owned process scope has exited."""
    owner = getattr(process, "_alexandria_windows_owner", None)
    if owner is None:
        stop_owned_subprocess(process, timeout=1)
        return
    # The existing graceful taskkill request fails for these headless SDK
    # workers. Terminate the admitted Job Object and confirm zero membership.
    owner.terminate()
    deadline = time.monotonic() + 1
    while is_subprocess_tree_running(process):
        if time.monotonic() >= deadline:
            raise TimeoutError("External TTS Windows job did not stop within its deadline")
        time.sleep(.01)
    process.wait(timeout=1)


class _ClientResources:
    def __init__(self):
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.process = None
        self.directory = None
        self.pending_cleanup = False

    def stop(self, *, close=False):
        with self.lock:
            if close:
                self.closed.set()
            self.pending_cleanup = True
            if self.process is not None:
                # Retain both ownership and private files if termination fails.
                stop_external_client_worker(self.process)
                control = getattr(self.process, "_alexandria_control", None)
                if control is not None:
                    control.close()
                self.process = None
            if self.directory is not None:
                self.directory.cleanup()
                self.directory = None
            self.pending_cleanup = False


def _close_resources(resources):
    resources.stop(close=True)


class ExternalTTSClient:
    """One owned endpoint slot; generic calls cache SDKs, compatible calls refresh."""

    def __init__(self, url, timeout=300, *, environment=None):
        self.url = url
        self.timeout = timeout
        self._environment = environment
        self._call_lock = threading.Lock()
        self._resources = _ClientResources()
        self._finalizer = weakref.finalize(self, _close_resources, self._resources)

    def _start(self):
        resources = self._resources
        with resources.lock:
            if resources.closed.is_set():
                raise RuntimeError("External TTS client is closed")
            if resources.pending_cleanup:
                resources.stop()  # Do not queue more work behind an unreaped SDK.
            if resources.process is not None:
                return
            resources.directory = tempfile.TemporaryDirectory(prefix="external-tts-client-")
            command = [sys.executable, str(Path(__file__).resolve()), resources.directory.name]
            environment = dict(os.environ if self._environment is None else self._environment)
            environment['HF_HUB_DISABLE_TELEMETRY'] = '1'
            try:
                resources.process = start_owned_subprocess(
                    command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, env=environment,
                    start_new_session=(os.name == "posix"),
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
                    termination_grace=1)
            except BaseException:
                resources.stop()
                raise

    def predict(self, *args, **kwargs):
        response, directory = self._dispatch("predict", args, kwargs, retire_worker=False)
        return self._prediction_from_response(response, directory)

    def get_capabilities(self):
        response, _directory = self._dispatch("capabilities", (), {}, retire_worker=True)
        return response['capabilities']

    def predict_compatible(self, *args, **kwargs):
        response, directory = self._dispatch("compatible_predict", args, kwargs, retire_worker=True)
        return self._prediction_from_response(response, directory)

    def _prediction_from_response(self, response, directory):
        path = str(directory / "audio.wav") if response.get("audio") else None
        return ExternalPrediction(path, directory, response.get('_output_owner'))

    def _dispatch(self, operation, args, kwargs, retire_worker):
        control = _REQUEST_CONTROL.get()
        deadline, cancelled = (control if control is not None
                               else (time.monotonic() + self.timeout, threading.Event()))
        while not self._call_lock.acquire(timeout=.02):
            self._check_active(deadline, cancelled)
        try:
            self._check_active(deadline, cancelled)
            request_id = uuid.uuid4().hex
            request = {"version": PROTOCOL_VERSION, "id": request_id, "url": self.url, "operation": operation, "args": args, "kwargs": kwargs}
            if len(json.dumps(request).encode("utf-8")) > MAX_REQUEST_BYTES:
                raise ValueError("External TTS request exceeds its byte limit")
            if retire_worker:
                self._resources.stop()
            self._start()
            resources = self._resources
            try:
                with resources.lock:
                    self._check_active(deadline, cancelled)
                    root = Path(resources.directory.name)
                    process = resources.process
                    directory = root / ("request-" + request_id)
                    directory.mkdir()
                # SDK termination can fail. Its uploads must then outlive the
                # render's parent-owned clone snapshot, just like the SDK itself.
                request.update(copy_external_inputs({"args": args, "kwargs": kwargs}, directory))
                if len(json.dumps(request).encode("utf-8")) > MAX_REQUEST_BYTES:
                    raise ValueError("External TTS request exceeds its byte limit")
                with resources.lock:
                    self._check_active(deadline, cancelled)
                    atomic_json_write(request, str(root / "request.json"))
                response_path = directory / "response.json"
                while not response_path.exists():
                    self._check_active(deadline, cancelled)
                    if process.poll() is not None:
                        raise RuntimeError("External TTS worker exited before returning a result")
                    time.sleep(.01)
                response = get_bounded_json(response_path, MAX_RESPONSE_BYTES)
                self._check_active(deadline, cancelled)
                if not isinstance(response, dict) or response.get("version") != PROTOCOL_VERSION:
                    raise ValueError("External TTS worker returned an unsupported protocol version")
                if response.get("id") != request_id:
                    raise ValueError("External TTS worker returned a mismatched response")
                if "error" in response:
                    error = TimeoutError if response.get("type") == "TimeoutError" else RuntimeError
                    raise error(response["error"] or response.get("type") or "External TTS provider failed")
                if operation == "capabilities":
                    if not isinstance(response.get("capabilities"), dict):
                        raise ValueError("External TTS worker returned an incomplete capability report")
                    resources.stop()
                elif "audio" not in response or response["audio"] not in (None, "audio.wav"):
                    raise ValueError("External TTS worker returned an invalid audio response")
                else:
                    owner = tempfile.TemporaryDirectory(prefix='external_prediction_')
                    try:
                        output_directory = Path(owner.name)
                        if response['audio']:
                            shutil.move(str(directory / 'audio.wav'), str(output_directory / 'audio.wav'))
                        shutil.rmtree(directory)
                        if operation == 'compatible_predict':
                            resources.stop()
                    except BaseException:
                        owner.cleanup()
                        raise
                    directory = output_directory
                    response['_output_owner'] = owner
                return response, directory
            except BaseException:
                resources.stop()
                raise
        finally:
            self._call_lock.release()

    def _check_active(self, deadline, cancelled):
        if self._resources.closed.is_set():
            raise RuntimeError("External TTS client is closed")
        if cancelled.is_set() or time.monotonic() >= deadline:
            raise TimeoutError("External TTS request reached its submission deadline or was cancelled")

    def close(self):
        self._resources.stop(close=True)
        self._finalizer.detach()


def run_client_worker(directory):
    """Only the owned worker imports/constructs the SDK and its thread pools."""
    root = Path(directory)
    client = None
    while True:
        request_path = root / "request.json"
        if not request_path.exists():
            time.sleep(.01)
            continue
        request = get_bounded_json(request_path, MAX_REQUEST_BYTES)
        request_path.unlink()
        if not isinstance(request, dict) or request.get("version") != PROTOCOL_VERSION:
            raise ValueError("Unsupported external TTS protocol version")
        request_id = request.get("id")
        if (not isinstance(request_id, str) or len(request_id) != 32
                or any(ch not in "0123456789abcdef" for ch in request_id)):
            raise ValueError("Invalid external TTS request ID")
        workspace = root / ("request-" + request_id)
        if not workspace.is_dir() or workspace.is_symlink():
            raise ValueError("Invalid external TTS request workspace")
        response = {"version": PROTOCOL_VERSION, "id": request_id}
        operation = request.get("operation") or "predict"
        try:
            if operation not in ('predict', 'capabilities', 'compatible_predict'):
                raise ValueError("Unsupported external TTS worker operation")
            if client is None:
                from gradio_client import Client
                client = Client(request["url"])
            if operation == 'capabilities':
                metadata = client.view_api(return_format="dict", print_info=False)
                response['capabilities'] = external_tts_capabilities.build_capability_report(request['url'], metadata)
            else:
                call_args, call_kwargs = tuple(request['args']), dict(request['kwargs'])
                if operation == 'compatible_predict':
                    metadata = client.view_api(return_format="dict", print_info=False)
                    call_args, call_kwargs = external_tts_capabilities.build_compatible_call(request['url'], metadata, call_args, call_kwargs)
                result = client.predict(*call_args, **call_kwargs)
                source = result if isinstance(result, str) else result[0]
                if source and os.path.isfile(source):
                    shutil.copyfile(source, workspace / "audio.wav")
                    response["audio"] = "audio.wav"
                else:
                    response["audio"] = None
        except Exception as error:
            if operation == 'capabilities':
                response['capabilities'] = external_tts_capabilities.make_unknown_capability_report(request['url'], external_tts_capabilities.UNAVAILABLE_ERROR)
            else:
                response.update(error=redact_text(str(error)), type=type(error).__name__)
        if operation == 'capabilities' and len(json.dumps(response).encode('utf-8')) > MAX_RESPONSE_BYTES:
            response['capabilities'] = external_tts_capabilities.make_unknown_capability_report(request['url'], external_tts_capabilities.MALFORMED_ERROR)
        atomic_json_write(response, str(workspace / "response.json"))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Expected a private external TTS worker directory")
    run_client_worker(sys.argv[1])
