"""Measure acoustic and transcript ending evidence without assigning ground truth.

This offline experiment does not authorize publication, rejection, or retry.
It binds frozen inputs and distinct recordings, reuses the qualified bounded
speech comparison, and keeps acoustic, terminal-token, and corroboration
signals separate so none can silently become a production verdict.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys

from experiments.provenance import provenance
from experiments.tts_endpoint_retry_screen import (
    ATTEMPTS_PER_CASE,
    DECISION_WINDOW_MS,
    MAX_CASES,
    VERSION,
    _bound_case,
    _read_manifest,
    _safe_path,
)
from experiments.tts_endpoint_screen import (
    EndpointScreenError,
    MAX_TOTAL_AUDIO_BYTES,
    _resolve_audio,
    measure_wav_endpoint,
    write_new_report,
)
from speech_verification_compare import (
    SpeechComparisonUnavailable,
    get_comparison_tokens,
    get_corroborated_regions,
    get_material_regions,
    get_speech_comparison,
)

DESCRIPTION = "Measure acoustic and transcript ending evidence without assigning ground truth."


def _transcript(value, field, case_id, attempt):
    if not isinstance(value, str):
        raise EndpointScreenError(
            f"case {case_id} attempt {attempt} has invalid {field}")
    try:
        value.encode("utf-8", "strict")
        get_comparison_tokens(value)
    except (UnicodeError, SpeechComparisonUnavailable) as error:
        raise EndpointScreenError(
            f"case {case_id} attempt {attempt} has invalid {field}") from error
    return value


def _comparison(expected, transcript, field, case_id, attempt):
    try:
        return get_speech_comparison(expected, transcript)
    except SpeechComparisonUnavailable as error:
        raise EndpointScreenError(
            f"case {case_id} attempt {attempt} has unavailable {field}") from error


def build_content_report(manifest_path, threshold_dbfs=-40.0):
    if (type(threshold_dbfs) not in (int, float)
            or not -120.0 <= threshold_dbfs <= 0.0
            or not math.isfinite(threshold_dbfs)):
        raise EndpointScreenError(
            "threshold must be finite and between -120 and 0 dBFS")
    manifest_path, raw, manifest = _read_manifest(manifest_path)
    cases = manifest.get("cases") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest.get("version") != VERSION or not isinstance(cases, list)
            or not cases or len(cases) > MAX_CASES):
        raise EndpointScreenError(
            f"manifest must contain 1..{MAX_CASES} version-{VERSION} cases")

    root = manifest_path.parent
    seen_cases, seen_takes, seen_recordings = set(), set(), set()
    total_audio_bytes = 0
    results = []
    counts = {name: 0 for name in (
        "attempts", "endpoint_suspicious", "primary_terminal_missing",
        "primary_terminal_mismatch", "same_terminal_mismatch", "primary_material_regions",
        "corroborated_content_regions")}
    for case in cases:
        if (not isinstance(case, dict) or not isinstance(case.get("id"), str)
                or not case["id"]):
            raise EndpointScreenError("every case needs a non-empty string id")
        case_id = case["id"]
        try:
            case_id.encode("utf-8", "strict")
        except UnicodeError as error:
            raise EndpointScreenError("case id is not valid Unicode") from error
        if case_id in seen_cases:
            raise EndpointScreenError(f"duplicate case id: {case_id}")
        seen_cases.add(case_id)
        identity = _bound_case(case, case_id)
        try:
            expected_tokens = get_comparison_tokens(identity["input"]["text"])
        except SpeechComparisonUnavailable as error:
            raise EndpointScreenError(
                f"case {case_id} has unavailable expected speech") from error
        if not expected_tokens:
            raise EndpointScreenError(
                f"case {case_id} has no expected speech tokens")
        attempts = case.get("attempts")
        if not isinstance(attempts, list) or len(attempts) != ATTEMPTS_PER_CASE:
            raise EndpointScreenError(
                f"case {case_id} must have exactly {ATTEMPTS_PER_CASE} attempts")
        seeds, measured = set(), []
        for index, attempt in enumerate(attempts, 1):
            if (not isinstance(attempt, dict)
                    or type(attempt.get("attempt")) is not int
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
                raise EndpointScreenError(
                    f"case {case_id} needs a non-empty take id")
            try:
                take_id.encode("utf-8", "strict")
            except UnicodeError as error:
                raise EndpointScreenError(
                    f"case {case_id} take id is not valid Unicode") from error
            if take_id in seen_takes:
                raise EndpointScreenError(f"duplicate take id: {take_id}")
            seen_takes.add(take_id)
            path, facts = _resolve_audio(root, attempt.get("audio"))
            recording = (("inode", facts.st_dev, facts.st_ino) if facts.st_ino
                         else ("path", os.path.normcase(str(path))))
            if recording in seen_recordings:
                raise EndpointScreenError(
                    f"case {case_id} reuses a prior attempt recording")
            seen_recordings.add(recording)
            total_audio_bytes += facts.st_size
            if total_audio_bytes > MAX_TOTAL_AUDIO_BYTES:
                raise EndpointScreenError(
                    f"manifest audio work exceeds {MAX_TOTAL_AUDIO_BYTES} bytes")
            measurement = measure_wav_endpoint(path, facts)
            primary_text = _transcript(
                attempt.get("primary_transcript"), "primary_transcript",
                case_id, index)
            confirmation_raw = attempt.get("confirmation_transcript")
            confirmation_text = (None if confirmation_raw is None else _transcript(
                confirmation_raw, "confirmation_transcript", case_id, index))
            primary = _comparison(identity["input"]["text"], primary_text,
                                  "primary comparison", case_id, index)
            confirmation = (None if confirmation_text is None else
                _comparison(identity["input"]["text"], confirmation_text,
                            "confirmation comparison", case_id, index))
            primary_tokens = get_comparison_tokens(primary_text)
            confirmation_tokens = (None if confirmation_text is None else
                                   get_comparison_tokens(confirmation_text))
            expected_terminal = expected_tokens[-1]
            primary_terminal = primary_tokens[-1] if primary_tokens else None
            confirmation_terminal = (confirmation_tokens[-1]
                                     if confirmation_tokens else None)
            suspicious = (measurement["tail_rms_dbfs"][str(DECISION_WINDOW_MS)]
                          > threshold_dbfs)
            primary_material = get_material_regions(primary)
            corroborated = ([] if confirmation is None else
                            get_corroborated_regions(primary, confirmation))
            primary_missing = primary_terminal is None
            primary_mismatch = (primary_terminal is not None and
                                primary_terminal != expected_terminal)
            same_mismatch = bool(confirmation_tokens and primary_mismatch and
                confirmation_terminal == primary_terminal)
            counts["attempts"] += 1
            counts["endpoint_suspicious"] += int(suspicious)
            counts["primary_terminal_missing"] += int(primary_missing)
            counts["primary_terminal_mismatch"] += int(primary_mismatch)
            counts["same_terminal_mismatch"] += int(same_mismatch)
            counts["primary_material_regions"] += len(primary_material)
            counts["corroborated_content_regions"] += len(corroborated)
            measured.append({
                **measurement,
                "attempt": index,
                "take_id": take_id,
                "seed": seed,
                "audio": attempt["audio"],
                "endpoint_suspicious": suspicious,
                "primary_transcript_sha256": hashlib.sha256(
                    primary_text.encode("utf-8")).hexdigest(),
                "confirmation_transcript_sha256": (None if confirmation_text is None
                    else hashlib.sha256(confirmation_text.encode("utf-8")).hexdigest()),
                "expected_terminal_token": expected_terminal,
                "primary_terminal_token": primary_terminal,
                "confirmation_terminal_token": confirmation_terminal,
                "primary_terminal_missing": primary_missing,
                "primary_terminal_mismatch": primary_mismatch,
                "same_terminal_mismatch": same_mismatch,
                "primary_comparison": primary,
                "confirmation_comparison": confirmation,
                "primary_material_regions": primary_material,
                "corroborated_content_regions": corroborated,
            })
        results.append({"id": case_id, **identity, "attempts": measured})
    return {
        "version": VERSION,
        "status": "measured_not_production_qualified",
        "caveat": ("ASR and endpoint measurements are review evidence only; this "
                   "report assigns no audible ground truth and authorizes no "
                   "acceptance, rejection, retry, or publication policy."),
        "transcript_content_policy": ("Full raw transcripts remain in the input "
            "manifest; the report retains their hashes plus bounded token-level "
            "comparison evidence needed to audit differences."),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "endpoint_rule": {"window_ms": DECISION_WINDOW_MS, "operator": ">",
                          "threshold_dbfs": threshold_dbfs},
        "counts": counts,
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--threshold-dbfs", type=float, default=-40.0)
    args = parser.parse_args()
    try:
        report = build_content_report(args.manifest, args.threshold_dbfs)
    except EndpointScreenError as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"endpoint content screen refused input: {message}", file=sys.stderr)
        return 2
    report["provenance"] = provenance(
        __file__, argparse.Namespace(manifest=_safe_path(args.manifest),
                                     out=_safe_path(args.out),
                                     threshold_dbfs=args.threshold_dbfs))
    try:
        write_new_report(args.out, report)
    except (EndpointScreenError, FileExistsError, OSError, UnicodeError) as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"endpoint content screen could not publish report: {message}", file=sys.stderr)
        return 3
    print(json.dumps({"status": report["status"],
                      "counts": report["counts"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
