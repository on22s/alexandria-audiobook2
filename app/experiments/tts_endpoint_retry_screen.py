"""Simulate a one-retry TTS endpoint policy over frozen labeled attempts.

This is an offline policy experiment, not production retry behavior. Every
case binds the text, voice, and settings identities while its two attempts
carry distinct recorded seeds. The report keeps both attempt labels visible
and refuses to infer ground truth from the endpoint threshold.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys

from experiments.provenance import provenance
from experiments.tts_endpoint_screen import (
    EndpointScreenError,
    MAX_MANIFEST_BYTES,
    MAX_TOTAL_AUDIO_BYTES,
    _file_identity,
    _resolve_audio,
    measure_wav_endpoint,
    write_new_report,
)

VERSION = 1
MAX_CASES = 4096
ATTEMPTS_PER_CASE = 2
DECISION_WINDOW_MS = 5
LABELS = frozenset(("cut", "clean"))


def _read_manifest(path):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        path = path.parent.resolve() / path.name
        if path.is_symlink():
            raise EndpointScreenError("manifest must not be a symlink")
        descriptor = os.open(path, flags)
        try:
            handle = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                raise EndpointScreenError("manifest is not a regular file")
            if facts.st_size > MAX_MANIFEST_BYTES:
                raise EndpointScreenError("manifest is too large")
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
            final = os.fstat(handle.fileno())
            if (len(raw) != facts.st_size
                    or _file_identity(final) != _file_identity(facts)):
                raise EndpointScreenError("manifest changed during read")
    except EndpointScreenError:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise EndpointScreenError("manifest is unavailable") from error
    try:
        return path, raw, json.loads(raw)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise EndpointScreenError("manifest is invalid JSON") from error


def _identity(value, field, case_id):
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise EndpointScreenError(f"case {case_id} has invalid {field}")
    return value


def _bound_object(case, field, case_id):
    value = case.get(field)
    if not isinstance(value, dict):
        raise EndpointScreenError(f"case {case_id} has invalid {field}")
    try:
        canonical = json.dumps(value, ensure_ascii=False, allow_nan=False,
                               sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (RecursionError, TypeError, UnicodeError, ValueError) as error:
        raise EndpointScreenError(f"case {case_id} has invalid {field}") from error
    expected = _identity(case.get(f"{field}_sha256"), f"{field}_sha256", case_id)
    if hashlib.sha256(canonical).hexdigest() != expected:
        raise EndpointScreenError(f"case {case_id} {field} hash does not match")
    return value, expected


def _bound_case(case, case_id):
    input_value, input_sha256 = _bound_object(case, "input", case_id)
    voice, voice_sha256 = _bound_object(case, "voice", case_id)
    settings, settings_sha256 = _bound_object(case, "settings", case_id)
    if (not isinstance(input_value.get("text"), str) or not input_value["text"]
            or not isinstance(input_value.get("speaker_id"), str)
            or not input_value["speaker_id"]
            or not isinstance(input_value.get("delivery_instruction"), str)
            or type(input_value.get("pause_after_ms")) is not int
            or not 0 <= input_value["pause_after_ms"] <= 60_000):
        raise EndpointScreenError(f"case {case_id} has invalid input")
    if not isinstance(voice.get("id"), str) or not voice["id"]:
        raise EndpointScreenError(f"case {case_id} has invalid voice")
    _identity(voice.get("asset_sha256"), "voice.asset_sha256", case_id)
    return {"input": input_value, "input_sha256": input_sha256,
            "voice": voice, "voice_sha256": voice_sha256,
            "settings": settings, "settings_sha256": settings_sha256}


def _confusion_bucket(suspicious, label):
    actual_cut = label == "cut"
    if suspicious:
        return "true_positive" if actual_cut else "false_positive"
    return "false_negative" if actual_cut else "true_negative"


def build_retry_report(manifest_path, threshold_dbfs=-40.0):
    if (type(threshold_dbfs) not in (int, float)
            or not -120.0 <= threshold_dbfs <= 0.0
            or not math.isfinite(threshold_dbfs)):
        raise EndpointScreenError("threshold must be finite and between -120 and 0 dBFS")
    manifest_path, raw, manifest = _read_manifest(manifest_path)
    cases = manifest.get("cases") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest.get("version") != VERSION or not isinstance(cases, list)
            or not cases or len(cases) > MAX_CASES):
        raise EndpointScreenError("manifest must contain 1..4096 version-1 cases")

    root = manifest_path.parent
    seen = set()
    seen_take_ids = set()
    seen_recordings = set()
    seen_audio_hashes = {}
    results = []
    all_counts = {name: 0 for name in (
        "true_positive", "false_positive", "true_negative", "false_negative")}
    used_counts = dict(all_counts)
    policy = {name: 0 for name in (
        "accepted_first", "accepted_retry", "exhausted",
        "false_accept_cases", "false_reject_cases", "false_reject_attempts")}
    total_audio_bytes = 0

    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise EndpointScreenError("every case needs a non-empty string id")
        case_id = case["id"]
        try:
            case_id.encode("utf-8", "strict")
        except UnicodeError as error:
            raise EndpointScreenError("case id is not valid Unicode") from error
        if case_id in seen:
            raise EndpointScreenError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        identity = _bound_case(case, case_id)
        attempts = case.get("attempts")
        if not isinstance(attempts, list) or len(attempts) != ATTEMPTS_PER_CASE:
            raise EndpointScreenError(f"case {case_id} must have exactly two attempts")

        seeds = set()
        measured = []
        for index, attempt in enumerate(attempts, 1):
            if (not isinstance(attempt, dict) or type(attempt.get("attempt")) is not int
                    or attempt["attempt"] != index):
                raise EndpointScreenError(
                    f"case {case_id} attempts must be numbered 1 and 2")
            seed = attempt.get("seed")
            if type(seed) is not int or not 0 <= seed < 2**63 or seed in seeds:
                raise EndpointScreenError(
                    f"case {case_id} needs two distinct nonnegative integer seeds")
            seeds.add(seed)
            take_id = attempt.get("take_id")
            if not isinstance(take_id, str) or not take_id:
                raise EndpointScreenError(f"case {case_id} needs a non-empty take id")
            try:
                take_id.encode("utf-8", "strict")
            except UnicodeError as error:
                raise EndpointScreenError(
                    f"case {case_id} take id is not valid Unicode") from error
            if take_id in seen_take_ids:
                raise EndpointScreenError(f"duplicate take id: {take_id}")
            seen_take_ids.add(take_id)
            label = attempt.get("label")
            if not isinstance(label, str) or label not in LABELS:
                raise EndpointScreenError(f"case {case_id} attempt {index} has invalid label")
            path, facts = _resolve_audio(root, attempt.get("audio"))
            recording = (("inode", facts.st_dev, facts.st_ino)
                         if facts.st_ino else ("path", os.path.normcase(str(path))))
            if recording in seen_recordings:
                raise EndpointScreenError(
                    f"case {case_id} reuses a prior attempt recording")
            seen_recordings.add(recording)
            total_audio_bytes += facts.st_size
            if total_audio_bytes > MAX_TOTAL_AUDIO_BYTES:
                raise EndpointScreenError(
                    f"manifest audio work exceeds {MAX_TOTAL_AUDIO_BYTES} bytes")
            measurement = measure_wav_endpoint(path, facts)
            suspicious = (measurement["tail_rms_dbfs"][str(DECISION_WINDOW_MS)]
                          > threshold_dbfs)
            all_counts[_confusion_bucket(suspicious, label)] += 1
            duplicate_of = seen_audio_hashes.get(measurement["audio_sha256"])
            seen_audio_hashes.setdefault(measurement["audio_sha256"], take_id)
            measured.append({**measurement, "attempt": index, "take_id": take_id,
                             "seed": seed, "label": label,
                             "audio": attempt["audio"], "suspicious": suspicious,
                             "duplicate_audio_of_take_id": duplicate_of})

        used = measured if measured[0]["suspicious"] else measured[:1]
        false_rejects = 0
        for attempt in used:
            used_counts[_confusion_bucket(attempt["suspicious"], attempt["label"])] += 1
            if attempt["suspicious"] and attempt["label"] == "clean":
                policy["false_reject_attempts"] += 1
                false_rejects += 1
        if false_rejects:
            policy["false_reject_cases"] += 1
        accepted = next((attempt for attempt in used if not attempt["suspicious"]), None)
        if accepted is None:
            outcome = "exhausted"
        elif accepted["attempt"] == 1:
            outcome = "accepted_first"
        else:
            outcome = "accepted_retry"
        policy[outcome] += 1
        if accepted is not None and accepted["label"] == "cut":
            policy["false_accept_cases"] += 1
        for attempt in measured:
            attempt["used_by_policy"] = attempt in used
        results.append({"id": case_id, **identity, "outcome": outcome,
                        "accepted_attempt": None if accepted is None else accepted["attempt"],
                        "attempts": measured})

    return {
        "version": VERSION,
        "status": "simulated_not_production_qualified",
        "caveat": ("Offline labeled sequences only; this does not authorize a threshold, "
                   "retry, or production publication policy. Byte-identical distinct "
                   "recordings remain counted and are explicitly flagged."),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "rule": {"window_ms": DECISION_WINDOW_MS, "operator": ">",
                 "threshold_dbfs": threshold_dbfs, "maximum_attempts": 2},
        "all_attempt_confusion_matrix": all_counts,
        "used_attempt_confusion_matrix": used_counts,
        "policy_outcomes": policy,
        "cases": results,
    }


def _safe_path(value):
    return str(value).encode("utf-8", "backslashreplace").decode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--threshold-dbfs", type=float, default=-40.0)
    args = parser.parse_args()
    try:
        report = build_retry_report(args.manifest, args.threshold_dbfs)
    except EndpointScreenError as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"endpoint retry screen refused input: {message}", file=sys.stderr)
        return 2
    report["provenance"] = provenance(
        __file__, argparse.Namespace(manifest=_safe_path(args.manifest),
                                     out=_safe_path(args.out),
                                     threshold_dbfs=args.threshold_dbfs))
    try:
        write_new_report(args.out, report)
    except (EndpointScreenError, FileExistsError, OSError, UnicodeError) as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"endpoint retry screen could not publish report: {message}", file=sys.stderr)
        return 3
    print(json.dumps({"status": report["status"],
                      "policy_outcomes": report["policy_outcomes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
