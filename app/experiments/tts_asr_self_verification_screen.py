"""Evaluate frozen TTS candidates with bounded ASR self-verification.

This offline experiment reproduces the observable portion of the best-of-N
policy in Asaria et al. (2026): reject ASR dropouts or WER above 0.5, then
identify the lowest-WER surviving candidate. Codec-token counts are not
available in the input manifest, so this report is deliberately incomplete
and cannot authorize production selection, retry, rejection, or publication.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from experiments.asr_backends import is_cjk, normalise, word_error_rate
from experiments.provenance import provenance
from experiments.tts_endpoint_content_screen import build_content_report
from experiments.tts_endpoint_screen import EndpointScreenError, write_new_report
from experiments.tts_endpoint_retry_screen import _read_manifest, _safe_path


DESCRIPTION = "Evaluate frozen TTS candidates with bounded ASR self-verification."
CONTENT_FAILURE_WER = 0.5
MAX_DROPOUT_WORDS = 1
METHOD_URL = "https://arxiv.org/abs/2606.18323"


def build_self_verification_report(manifest_path):
    content = build_content_report(manifest_path)
    _, raw, manifest = _read_manifest(manifest_path)
    if hashlib.sha256(raw).hexdigest() != content["manifest_sha256"]:
        raise EndpointScreenError("manifest changed during self-verification")
    source_cases = manifest["cases"]
    if len(source_cases) != len(content["cases"]):
        raise EndpointScreenError("content report does not match manifest cases")

    counts = {
        "cases": 0,
        "attempts": 0,
        "catastrophic_attempts": 0,
        "first_attempt_catastrophic": 0,
        "all_attempts_catastrophic": 0,
        "recovered_by_second_attempt": 0,
        "selected_attempt_1": 0,
        "selected_attempt_2": 0,
    }
    cases = []
    for source_case, content_case in zip(source_cases, content["cases"]):
        if source_case["id"] != content_case["id"]:
            raise EndpointScreenError("content report case order changed")
        expected = source_case["input"]["text"]
        if is_cjk(expected):
            raise EndpointScreenError(
                f"case {source_case['id']} is outside this English WER screen")
        expected_words = len(normalise(expected, char_level=False).split())
        if expected_words <= MAX_DROPOUT_WORDS:
            raise EndpointScreenError(
                f"case {source_case['id']} is too short for the dropout rule")
        source_attempts = source_case.get("attempts")
        content_attempts = content_case.get("attempts")
        if (not isinstance(source_attempts, list)
                or not isinstance(content_attempts, list)
                or len(source_attempts) != 2 or len(content_attempts) != 2):
            raise EndpointScreenError(
                f"case {source_case['id']} does not contain two candidates")
        source_identity = [(attempt.get("attempt"), attempt.get("take_id"),
                            attempt.get("seed"), attempt.get("audio"))
                           for attempt in source_attempts]
        content_identity = [(attempt.get("attempt"), attempt.get("take_id"),
                             attempt.get("seed"), attempt.get("audio"))
                            for attempt in content_attempts]
        if source_identity != content_identity or [row[0] for row in source_identity] != [1, 2]:
            raise EndpointScreenError(
                f"case {source_case['id']} candidate identity changed")
        candidates = []
        for source_attempt, content_attempt in zip(
                source_attempts, content_attempts):
            transcript = source_attempt["primary_transcript"]
            wer = word_error_rate(expected, transcript, char_level=False)
            if wer is None:
                raise EndpointScreenError(
                    f"case {source_case['id']} has no WER reference words")
            asr_words = len(normalise(transcript, char_level=False).split())
            asr_dropout = asr_words <= MAX_DROPOUT_WORDS
            content_failure = wer > CONTENT_FAILURE_WER
            catastrophic = asr_dropout or content_failure
            candidates.append({
                "attempt": source_attempt["attempt"],
                "take_id": source_attempt["take_id"],
                "seed": source_attempt["seed"],
                "audio": source_attempt["audio"],
                "primary_transcript_sha256": content_attempt[
                    "primary_transcript_sha256"],
                "asr_word_count": asr_words,
                "word_error_rate": wer,
                "asr_dropout_observed": asr_dropout,
                "content_failure_observed": content_failure,
                "catastrophic_observed": catastrophic,
                "codec_token_count": None,
                "codec_token_dropout_observed": None,
            })
        eligible = [candidate for candidate in candidates
                    if not candidate["catastrophic_observed"]]
        selected = (min(eligible, key=lambda candidate: (
            candidate["word_error_rate"], candidate["attempt"]))
            if eligible else None)
        first_failed = candidates[0]["catastrophic_observed"]
        all_failed = not eligible
        recovered = first_failed and not all_failed
        counts["cases"] += 1
        counts["attempts"] += len(candidates)
        counts["catastrophic_attempts"] += sum(
            candidate["catastrophic_observed"] for candidate in candidates)
        counts["first_attempt_catastrophic"] += int(first_failed)
        counts["all_attempts_catastrophic"] += int(all_failed)
        counts["recovered_by_second_attempt"] += int(recovered)
        if selected:
            counts[f"selected_attempt_{selected['attempt']}"] += 1
        cases.append({
            "id": source_case["id"],
            "input_sha256": source_case["input_sha256"],
            "voice_sha256": source_case["voice_sha256"],
            "settings_sha256": source_case["settings_sha256"],
            "first_attempt_catastrophic": first_failed,
            "all_attempts_catastrophic": all_failed,
            "recovered_by_second_attempt": recovered,
            "selected_attempt": None if selected is None else selected["attempt"],
            "candidates": candidates,
        })
    return {
        "version": 1,
        "status": "observed_asr_policy_not_production_qualified",
        "method": {
            "source": METHOD_URL,
            "language_scope": "English",
            "content_failure_rule": {"metric": "word_error_rate",
                                     "operator": ">",
                                     "threshold": CONTENT_FAILURE_WER},
            "asr_dropout_rule": {"metric": "asr_word_count",
                                 "operator": "<=",
                                 "threshold": MAX_DROPOUT_WORDS},
            "selection": "lowest WER among candidates without an observed failure",
        },
        "limitations": [
            "Codec speech-token counts are unavailable, so the paper's token-count dropout rule is not reproduced.",
            "ASR is fallible and WER does not measure pronunciation, prosody, voice identity, or listening quality.",
            "Numbers, dates, rare words, and languages need separate measurement; the source paper documents WER confounds and capability ceilings.",
            "This frozen best-of-two observation does not select, retry, reject, or publish production audio.",
        ],
        "manifest_sha256": content["manifest_sha256"],
        "counts": counts,
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = build_self_verification_report(args.manifest)
    except EndpointScreenError as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"ASR self-verification screen refused input: {message}", file=sys.stderr)
        return 2
    report["provenance"] = provenance(
        __file__, argparse.Namespace(manifest=_safe_path(args.manifest),
                                     out=_safe_path(args.out)))
    try:
        write_new_report(args.out, report)
    except (EndpointScreenError, OSError, UnicodeError) as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"ASR self-verification screen could not publish report: {message}",
              file=sys.stderr)
        return 3
    print(json.dumps({"status": report["status"], "counts": report["counts"]},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
