"""Hash-verified Voice Lab preparer ASR benchmark worker."""

from benchmark_worker_protocol import get_decoded_worker_payload, emit_benchmark_worker_result
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time

from benchmark_validation import get_benchmark_file_path, get_benchmark_output_path
from lora_evidence import get_file_sha256


def execute_fixture(fixture, python_executable, preparer_script):
    root_dir = os.path.abspath(fixture["root_dir"])
    try:
        audio_path = get_benchmark_file_path(root_dir, fixture["audio_path"])
    except ValueError as exc:
        raise ValueError("preparer audio must be inside fixture root and exist") from exc
    if not isinstance(python_executable, str) or not python_executable:
        raise ValueError("python executable must be a non-empty path")
    if not isinstance(preparer_script, str) or not preparer_script:
        raise ValueError("preparer script must be a non-empty path")
    if get_file_sha256(audio_path) != fixture["audio_sha256"]:
        raise ValueError("preparer audio hash changed")
    with tempfile.TemporaryDirectory(prefix="alexandria-preparer-benchmark-") as scratch:
        asr_path = get_benchmark_output_path(scratch, "asr.json")
        command = [python_executable, "-u", preparer_script, "--phase", "asr",
                   "--audio", audio_path, "--limit", str(fixture["limit"]),
                   "--lang", fixture["language"], "--asr-output", asr_path,
                   "--asr-model-revision", fixture["model_revision"],
                   "--scratch-audio", get_benchmark_output_path(scratch, "audio24.wav")]
        started = time.monotonic()
        result = subprocess.run(command, cwd=scratch, capture_output=True, text=True,
                                timeout=3600, check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr)[-4000:])
        with open(get_benchmark_output_path(scratch, "asr.json"), "rb") as asr_file:
            asr_raw = asr_file.read()
    asr = json.loads(asr_raw)
    words = asr.get("word_segments") or []
    transcript_text = " ".join(str(word.get("word", "")).strip() for word in words)
    return {"elapsed_seconds": round(elapsed, 3), "word_count": len(words),
            "audio_duration_seconds": asr.get("audio_duration"),
            "detected_language": asr.get("detected_lang"),
            "transcript_text_sha256": hashlib.sha256(
                transcript_text.encode("utf-8")).hexdigest(),
            "alignment_sha256": hashlib.sha256(json.dumps(
                words, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    args = parser.parse_args()
    def execute():
        payload = get_decoded_worker_payload(args.payload)
        return execute_fixture(payload["fixture"], payload["python"], payload["preparer_script"])
    emit_benchmark_worker_result('PREPARER_BENCHMARK_RESULT=',
                                 execute, metrics_only=True)


if __name__ == "__main__":
    main()
