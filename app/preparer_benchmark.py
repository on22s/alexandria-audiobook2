"""Hash-verified Voice Lab preparer ASR benchmark worker."""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import time


def execute_fixture(fixture, python_executable, preparer_script):
    audio_path = os.path.abspath(os.path.join(fixture["root_dir"], fixture["audio_path"]))
    with open(audio_path, "rb") as audio_file:
        if hashlib.sha256(audio_file.read()).hexdigest() != fixture["audio_sha256"]:
            raise ValueError("preparer audio hash changed")
    with tempfile.TemporaryDirectory(prefix="alexandria-preparer-benchmark-") as scratch:
        asr_path = os.path.join(scratch, "asr.json")
        command = [python_executable, "-u", preparer_script, "--phase", "asr",
                   "--audio", audio_path, "--limit", str(fixture["limit"]),
                   "--lang", fixture["language"], "--asr-output", asr_path,
                   "--asr-model-revision", fixture["model_revision"],
                   "--scratch-audio", os.path.join(scratch, "audio24.wav")]
        started = time.monotonic()
        result = subprocess.run(command, cwd=scratch, capture_output=True, text=True,
                                timeout=3600, check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            raise RuntimeError((result.stdout + "\n" + result.stderr)[-4000:])
        with open(asr_path, "rb") as asr_file:
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
    payload = json.loads(base64.b64decode(args.payload).decode("utf-8"))
    try:
        metrics = execute_fixture(payload["fixture"], payload["python"],
                                  payload["preparer_script"])
        result = {"status": "passed", "metrics": metrics, "error": None}
    except Exception as exc:
        result = {"status": "failed", "metrics": {}, "error": str(exc)}
    print("PREPARER_BENCHMARK_RESULT=" + json.dumps(result, separators=(",", ":")))


if __name__ == "__main__":
    main()
