#!/usr/bin/env python3
"""Run the live API suite against disposable application state."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def get_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
        port = get_free_port()
        env = dict(os.environ, ALEXANDRIA_DATA_DIR=data_dir,
                   ALEXANDRIA_PORT=str(port))
        server = subprocess.Popen(
            [sys.executable, "app.py"], cwd=app_dir, env=env,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError(f"Server exited with status {server.returncode}")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Server did not become ready within 20 seconds")
            result = subprocess.run(
                [sys.executable, "test_api.py", "--url", f"http://127.0.0.1:{port}"],
                cwd=app_dir,
            )
            return result.returncode
        finally:
            if server.poll() is None:
                os.killpg(server.pid, signal.SIGTERM)
                try:
                    server.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(server.pid, signal.SIGKILL)
                    server.wait()


if __name__ == "__main__":
    raise SystemExit(main())
