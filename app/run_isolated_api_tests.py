#!/usr/bin/env python3
"""Run the live API suite against disposable application state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from api_test_auth import get_api_test_headers
from subprocess_ownership import start_owned_subprocess, stop_owned_subprocess


SERVER_CODE = "from run_isolated_api_tests import run_isolated_server; run_isolated_server()"


def run_isolated_server():
    """Bind once and serve on the same socket, publishing its assigned port."""
    import uvicorn
    from app import app
    from utils import atomic_json_write
    config = uvicorn.Config(app, host="127.0.0.1", port=0, access_log=False)
    with config.bind_socket() as listener:
        atomic_json_write({"port": listener.getsockname()[1]},
                          Path(os.environ["ALEXANDRIA_DATA_DIR"]) / "server_port.json")
        uvicorn.Server(config).run(sockets=[listener])


def get_isolated_server_port(data_path):
    try:
        with open(data_path / "server_port.json", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except FileNotFoundError:
        return None
    port = metadata.get("port") if isinstance(metadata, dict) else None
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Isolated server published an invalid port")
    return port


def main():
    app_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="alexandria-api-test-") as data_dir:
        data_path = Path(data_dir)
        (data_path / "annotated_script.json").write_text(json.dumps([
            {"speaker": "NARRATOR", "text": "Chapter One", "instruct": ""},
            {"speaker": "Hero", "text": "The isolated test begins.", "instruct": "calm"},
        ]), encoding="utf-8")
        (data_path / "state.json").write_text(
            json.dumps({"active_book_id": "isolated-fixture"}), encoding="utf-8")
        # If a built-in voice is downloaded, assign it to the fixture speakers so
        # the --full GPU generation tests exercise real TTS instead of erroring on
        # an unvoiced speaker. builtin_lora ships at the repo root (not the data
        # dir), so it's reachable by the isolated server. Harmless when absent.
        builtin_dir = app_dir.parent / "builtin_lora"
        voice = next((p.parent.name for p in sorted(builtin_dir.glob("*/adapter_model.safetensors"))), None)
        if voice:
            entry = {"type": "builtin_lora", "adapter_id": voice,
                     "adapter_path": f"builtin_lora/{voice}", "seed": "-1"}
            (data_path / "voice_config.json").write_text(
                json.dumps({"NARRATOR": entry, "Hero": entry}), encoding="utf-8")
        env = dict(os.environ, ALEXANDRIA_DATA_DIR=data_dir,
                   ALEXANDRIA_PORT="0", ALEXANDRIA_HOST="127.0.0.1")
        headers = get_api_test_headers(environ=env)
        server = start_owned_subprocess(
            [sys.executable, "-c", SERVER_CODE], cwd=app_dir, env=env,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError(f"Server exited with status {server.returncode}")
                port = get_isolated_server_port(data_path)
                if port is None:
                    time.sleep(0.1)
                    continue
                readiness_url = f"http://127.0.0.1:{port}/api/config"
                readiness_request = Request(readiness_url, headers=headers) if headers else readiness_url
                try:
                    with urlopen(readiness_request, timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Server did not become ready within 20 seconds")
            # Forward extra CLI args (e.g. --full) straight through to test_api
            # so the GPU/LLM suite can be run against this same disposable state.
            #
            # `-m`, not a path. Running `tests/test_api.py` as a script puts
            # `tests/` on sys.path instead of `app/`, and the suite imports app
            # modules (`utils`, `tts`, ...) by bare name - it failed on the
            # first import the moment the file moved. `-m` with cwd=app_dir
            # puts `app/` on the path, which is what those imports mean.
            result = subprocess.run(
                [sys.executable, "-m", "tests.test_api", "--url", f"http://127.0.0.1:{port}",
                 *sys.argv[1:]],
                cwd=app_dir, env=env,
            )
            return result.returncode
        finally:
            original_error = sys.exc_info()[1]
            try:
                try:
                    stop_owned_subprocess(server, timeout=10)
                finally:
                    control = getattr(server, "_alexandria_control", None)
                    if control is not None:
                        control.close()
            except Exception as error:
                if original_error is None:
                    raise
                print(f"Isolated server cleanup failed: {error}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
