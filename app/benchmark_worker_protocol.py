"""Shared wire format for benchmark workers; stage execution stays with callers."""
import base64
import json
from benchmark_execution import BenchmarkCancelled


def get_encoded_worker_payload(payload):
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def get_decoded_worker_payload(encoded):
    return json.loads(base64.b64decode(encoded).decode("utf-8"))


def emit_benchmark_worker_result(marker, execute, *, metrics_only=False):
    """Emit one result, preserving stage success shapes and cancellation signals."""
    try:
        result = execute()
        if metrics_only:
            result = {"status": "passed", "metrics": result, "error": None}
        encoded = json.dumps(result, separators=(",", ":"))
    except BenchmarkCancelled:
        raise
    except Exception as exc:
        encoded = json.dumps({"status": "failed", "metrics": {}, "error": str(exc)},
                             separators=(",", ":"))
    print(marker + encoded)


def get_benchmark_worker_result(result, marker, failure_message, *, error_limit=4000,
                                raise_failed=False):
    lines = [line for line in result.stdout.splitlines() if line.startswith(marker)]
    if result.returncode or not lines:
        detail = result.stderr.strip() or result.stdout.strip() or failure_message
        raise RuntimeError(detail[-error_limit:])
    value = json.loads(lines[-1][len(marker):])
    if raise_failed and isinstance(value, dict) and value.get("status") == "failed":
        raise RuntimeError(str(value.get("error") or failure_message)[-error_limit:])
    return value
