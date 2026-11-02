import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


PROBES = {
    "independent_124": 'import sys; print("timeout: sending signal INT to command", file=sys.stderr); sys.exit(124)',
    "independent_137": 'raise SystemExit(137)',
    "cooperative_int": '''import signal, sys, time
def cleanup(*args):
    time.sleep(0.6)
    print("cooperative_cleanup", flush=True)
    sys.exit(0)
signal.signal(signal.SIGINT, cleanup)
print("worker_ready", flush=True)
time.sleep(5)
''',
    "ignores_int": 'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); print("worker_ready", flush=True); time.sleep(5)',
}
EXPECTED_RETURNCODES = {
    "independent_124": 124, "independent_137": 137,
    "cooperative_int": 124, "ignores_int": -9,
}


def get_contract_errors(observation):
    name = observation["probe"]
    errors = []
    if observation["returncode"] != EXPECTED_RETURNCODES[name]:
        errors.append("unexpected raw Python subprocess returncode")
    if name in ("cooperative_int", "ignores_int"):
        if "worker_ready" not in observation["stdout"]:
            errors.append("worker did not reach signal setup")
        if "sending signal INT" not in observation["stderr"]:
            errors.append("missing supervisor INT diagnostic")
    if name == "cooperative_int":
        if "cooperative_cleanup" not in observation["stdout"]:
            errors.append("cooperative cleanup did not finish")
        if observation["elapsed_seconds"] < 1.0:
            errors.append("cleanup grace was shortened")
        if "sending signal KILL" in observation["stderr"]:
            errors.append("cooperative worker required KILL")
    if name == "ignores_int":
        if observation["elapsed_seconds"] < 1.35:
            errors.append("additional one-second KILL grace was shortened")
        if "sending signal KILL" not in observation["stderr"]:
            errors.append("missing supervisor KILL diagnostic")
    return errors


def run_probes(output, observe_only=False):
    executable = Path(shutil.which("timeout") or "timeout").resolve(strict=True)
    with Path(output).open("w", encoding="utf-8") as stream:
        version = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=10)
        identity = {"executable": str(executable),
                    "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                    "version": version.stdout, "version_stderr": version.stderr,
                    "version_returncode": version.returncode,
                    "observe_only": observe_only}
        stream.write(json.dumps(identity) + "\n")
        stream.flush()
        failed = version.returncode != 0 or "GNU coreutils" not in version.stdout
        for name, source in PROBES.items():
            started = time.monotonic()
            result = subprocess.run(
                [str(executable), "--verbose", "--signal=INT", "--kill-after=1s", "0.5s",
                 sys.executable, "-u", "-c", source],
                capture_output=True, text=True, timeout=10, start_new_session=True,
            )
            observation = {"probe": name, "returncode": result.returncode,
                           "reference_python_returncode": EXPECTED_RETURNCODES[name],
                           "elapsed_seconds": time.monotonic() - started,
                           "stdout": result.stdout, "stderr": result.stderr}
            observation["errors"] = get_contract_errors(observation)
            failed = failed or bool(observation["errors"])
            stream.write(json.dumps(observation) + "\n")
            stream.flush()
    return 1 if failed and not observe_only else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--observe-only", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_probes(args.output, args.observe_only))
