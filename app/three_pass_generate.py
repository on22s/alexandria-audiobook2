"""Three-pass script generation orchestrator (segment -> attribute -> instruct).
A side-by-side alternative to generate_script.py's single pass; the single-pass
path is untouched. See docs/superpowers/specs/2026-07-21-three-pass-script-generation-design.md."""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import replace

from openai import OpenAI

from generate_script import (call_llm_for_entries, split_into_chunks,
                             fix_mojibake, LLMGenParams,
                             split_failed_chunk, is_trigram_only_near_miss)
from source_normalization import (normalize_known_source_corruptions,
                                  strip_known_front_matter)
from speaker_identity import stabilize_speaker_identities
from script_repair import build_deterministic_repair
from default_prompts import (load_segment_prompts, load_attribute_prompts,
                             load_instruct_prompts)
from pass_quality import (validate_segment_quality, validate_attribution,
                          validate_instruct, index_head_check)
from review_script import normalize_text
from config_settings import load_app_config
from lmstudio_settings import ensure_ideal_settings
from utils import (get_runtime_data_dir, get_app_config_path,
                   atomic_json_write, safe_load_json)

BATCH_SIZE = 25
NARRATOR_DEFAULT_INSTRUCT = "Neutral, even narration."
CHARACTER_DEFAULT_INSTRUCT = "Natural, in-character delivery."


def _record_resolution(sink, value):
    """Append a per-chunk pass-1 resolution to the telemetry sink, if one is
    provided. `sink` is a per-chunk list; callers read its last entry."""
    if sink is not None:
        sink.append(value)


def resolve_chunk_size(cli_value, config_value):
    """Resolve the effective chunk size (CLI overrides config) and validate it.
    Guards BOTH sources (finding #14): a bad config chunk_size previously slipped
    through because only the CLI value was checked. Raises ValueError on < 1."""
    chunk_size = cli_value if cli_value is not None else config_value
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError(f"chunk_size must be an integer >= 1 (got {chunk_size!r})")
    return chunk_size


def iter_unique_entry_batches(entries, batch_size=BATCH_SIZE):
    """Yield index/entry batches with unique normalized text.

    Each consecutive `batch_size` window is greedily colored into the fewest
    duplicate-free calls. Unlike stopping at the first repeated short line, this
    keeps the other entries in the window batched and preserves bounded source
    locality. Returned indices let callers restore source order."""
    for window_start in range(0, len(entries), batch_size):
        batches = []
        for index in range(window_start, min(window_start + batch_size, len(entries))):
            entry = entries[index]
            key = normalize_text(str(entry.get("text") or ""))
            for batch, seen in batches:
                if key not in seen:
                    batch.append((index, entry))
                    seen.add(key)
                    break
            else:
                batches.append(([(index, entry)], {key}))
        for batch, _ in batches:
            yield batch


def build_roster(entries):
    """Ordered unique UPPERCASE speaker names seen so far, excluding NARRATOR and
    the UNKNOWN placeholder — fed to pass 2 for naming consistency."""
    roster = []
    for entry in entries:
        speaker = (entry.get("speaker") or "").strip().upper()
        if speaker and speaker not in ("NARRATOR", "UNKNOWN") and speaker not in roster:
            roster.append(speaker)
    return roster


def default_instruct(entry):
    speaker = (entry.get("speaker") or "").strip().upper()
    return NARRATOR_DEFAULT_INSTRUCT if speaker == "NARRATOR" else CHARACTER_DEFAULT_INSTRUCT


class PassExhausted(Exception):
    """A pass-2/3 batch could not produce valid output within its retry budget.
    In testing mode (on_exhaustion='fail') this aborts the book so the real
    failure rate is visible."""


def attribute_batch(client, model_name, frozen_batch, params, roster,
                    max_retries=3, on_exhaustion="fail", neighbor_contexts=None):
    """Assign speakers to one batch of frozen {type,text} entries. Enforces the
    text freeze; retries on invalid output. On exhaustion: 'fail' raises
    PassExhausted (testing default); 'fallback' keeps frozen text and labels
    unresolved SPOKEN spans UNKNOWN via stabilize_speaker_identities."""
    sys_prompt, usr_template = load_attribute_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    neighbor_contexts = neighbor_contexts or [{} for _ in frozen_batch]
    batch_json = json.dumps([
        {"n": i, "type": e["type"], "text": e["text"], **neighbor_contexts[i]}
        for i, e in enumerate(frozen_batch)], ensure_ascii=False)
    user_prompt = usr_template.format(roster=", ".join(roster) or "(none yet)",
                                      batch=batch_json)
    validated = {}

    def validate(entries):
        report = validate_attribution(frozen_batch, entries)
        if report["passed"]:
            validated["ordered"] = index_head_check(frozen_batch, entries)[2]
        return report

    call_params = replace(params, temperature=(params.attribute_temperature
                                               if params.attribute_temperature is not None
                                               else params.temperature))
    named = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, call_params,
        log_name="llm_responses.log", label="ATTRIBUTE", max_retries=max_retries,
        validate_entries=validate)
    if named:
        # The model returned only {n, head, speaker} (never full text, so it can't
        # corrupt it). Bind by the validated index order and keep the frozen text
        # byte-exact; take only the assigned speaker.
        ordered = validated.get("ordered")
        if ordered is None:
            raise RuntimeError("validated attribution response lost its index binding")
        return [{**{k: v for k, v in f.items() if k != "type"},
                 "speaker": item.get("speaker")}
                for f, item in zip(frozen_batch, ordered)]
    if on_exhaustion == "fail":
        raise PassExhausted(f"attribution failed for a {len(frozen_batch)}-entry batch")
    seeded = [{**{k: v for k, v in e.items() if k != "type"},
               "speaker": "NARRATOR" if e["type"] == "NARRATOR" else "UNKNOWN"}
              for e in frozen_batch]
    return stabilize_speaker_identities(seeded, established_speakers=roster)["entries"]


def instruct_batch(client, model_name, prior_batch, params, max_retries=3,
                   neighbor_contexts=None):
    """Add instruct to one batch of {speaker,text} entries. Enforces the freeze
    on text+speaker. On exhaustion, attaches a default instruct per entry so
    pass 3 never fails the book."""
    sys_prompt, usr_template = load_instruct_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    neighbor_contexts = neighbor_contexts or [{} for _ in prior_batch]
    batch_json = json.dumps([
        {"n": i, "speaker": e["speaker"], "text": e["text"], **neighbor_contexts[i]}
        for i, e in enumerate(prior_batch)], ensure_ascii=False)
    user_prompt = usr_template.format(batch=batch_json)
    validated = {}

    def validate(entries):
        report = validate_instruct(prior_batch, entries)
        if report["passed"]:
            validated["ordered"] = index_head_check(prior_batch, entries)[2]
        return report

    call_params = replace(params, temperature=(params.instruct_temperature
                                               if params.instruct_temperature is not None
                                               else params.temperature))
    annotated = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, call_params,
        log_name="llm_responses.log", label="INSTRUCT", max_retries=max_retries,
        validate_entries=validate)
    if annotated:
        # The model returned only {n, head, instruct}. Keep speaker+text byte-exact
        # from prior (bound by validated index order); take only the instruct.
        ordered = validated.get("ordered")
        if ordered is None:
            raise RuntimeError("validated instruct response lost its index binding")
        return [{**p, "instruct": item.get("instruct")}
                for p, item in zip(prior_batch, ordered)]
    return [{**e, "instruct": default_instruct(e)} for e in prior_batch]


def _call_segment(client, model_name, chunk, sys_prompt, user_prompt, params,
                  label, max_retries, near_miss_sink, validate=None):
    """Shared body for every pass-1 segment call (plain and context-rescue):
    the segment repair transform, the segment fidelity gate (optionally wrapped),
    and trigram-only near-miss capture. Callers build sys_prompt/user_prompt so
    the two paths can't diverge in how they invoke the gate (findings #10, #11)."""
    if validate is None:
        validate = lambda entries: validate_segment_quality(chunk, entries)
    # Segmentation only adds small JSON/type overhead around source text. Bound
    # both the first request and retry ceiling so a weak model cannot spend
    # 10k-16k tokens expanding a ~1k-token source chunk.
    source_words = max(1, len(chunk.split()))
    completion_ceiling = max(512, math.ceil(source_words * params.segment_output_ratio))
    bounded_params = replace(
        params, max_tokens=min(params.max_tokens, completion_ceiling),
        hard_max_tokens=min(params.hard_max_tokens, completion_ceiling),
        temperature=(params.segment_temperature
                     if params.segment_temperature is not None else params.temperature))
    def repair(entries):
        repaired = build_deterministic_repair(
            entries, chunk, merge_empty_into_pause=False)
        quote_split = []
        for number, entry in enumerate(repaired["entries"], 1):
            text = str(entry.get("text") or "").strip()
            if entry.get("type") != "SPOKEN":
                if any(char in text for char in ('"', '“', '”')):
                    parts, current, quoted = [], [], False
                    for char in text:
                        opens = char in ('"', '“') and not quoted
                        closes = char in ('"', '”') and quoted
                        if opens or closes:
                            part = "".join(current).strip()
                            if part:
                                parts.append({**entry, "type": "SPOKEN" if quoted
                                              else "NARRATOR", "text": part})
                            current = []
                            quoted = not quoted
                        else:
                            current.append(char)
                    part = "".join(current).strip()
                    if part:
                        parts.append({**entry, "type": "SPOKEN" if quoted
                                      else "NARRATOR", "text": part})
                    if not quoted and len(parts) > 1:
                        quote_split.extend(parts)
                        repaired.setdefault("changes", []).append({
                            "entry_number": number, "code": "split_mixed_quote_regions"})
                        continue
            elif ((text.startswith('"') and text.endswith('"'))
                  or (text.startswith('“') and text.endswith('”'))):
                entry = {**entry, "text": text[1:-1]}
                repaired.setdefault("changes", []).append({
                    "entry_number": number, "code": "stripped_dialogue_delimiters"})
            quote_split.append(entry)
        repaired["entries"] = quote_split
        return repaired

    return call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, bounded_params,
        log_name="llm_responses.log", label=label, max_retries=max_retries,
        # Same deterministic structural repair (unicode-homoglyph fixups) the
        # single-pass path runs before its gate, so pass 1 doesn't waste a retry
        # on issues single-pass silently repairs. build_deterministic_repair is
        # text-only, so it applies unchanged to the {type,text} segment shape.
        # merge_empty_into_pause=False so empty units reach the gate (finding #7).
        transform_entries=repair,
        validate_entries=validate,
        near_miss_sink=near_miss_sink)


def segment_chunk(client, model_name, chunk, params, max_retries=4, near_miss_sink=None):
    """Pass 1 single attempt-budget over one chunk -> [{type,text}], via the
    segment fidelity gate. Captures a trigram-only near-miss into near_miss_sink
    (same mechanism call_llm_for_entries uses for single-pass). Returns [] on
    exhaustion."""
    sys_prompt, usr_template = load_segment_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    user_prompt = usr_template.format(chunk=chunk)
    return _call_segment(client, model_name, chunk, sys_prompt, user_prompt,
                         params, "SEGMENT", max_retries, near_miss_sink)


def _accept_segment_near_miss(near_miss):
    if not near_miss:
        return []
    entries, quality = near_miss[0]
    print("  SEGMENT accepted as trigram-only near-miss "
          f"(ordered_trigram_recall={quality['metrics']['ordered_trigram_recall']})")
    return entries


def _resolved_near_miss(near_miss, resolution_sink):
    """Accept the exhaustion near-miss (if any) and record the resolution."""
    entries = _accept_segment_near_miss(near_miss)
    _record_resolution(resolution_sink, "near_miss" if entries else "fail")
    return entries


def segment_chunk_adaptively(client, model_name, chunk, params, resolution_sink=None):
    """Pass 1 with the full safety net: full-chunk attempt, then a
    natural-boundary split whose halves each recurse, and exhaustion-only
    trigram-only near-miss acceptance. Mirrors process_chunk_adaptively but for
    the segment gate. Returns [{type,text}] or [] (book failure). When
    resolution_sink is given, appends exactly one resolution string describing
    how the chunk was handled (clean / adaptive_split / recombination_near_miss /
    near_miss / fail). Only the top-level call should pass a sink; recursive
    part-calls do not, so inner resolutions don't pollute the record."""
    near_miss = []
    entries = segment_chunk(client, model_name, chunk, params, near_miss_sink=near_miss)
    if entries:
        _record_resolution(resolution_sink, "clean")
        return entries
    parts = split_failed_chunk(chunk)
    if not parts:
        return _resolved_near_miss(near_miss, resolution_sink)
    print(f"  Adaptive split (segment): -> {len(parts[0])} + {len(parts[1])} chars")
    combined, any_failed = [], False
    for part in parts:
        part_entries = segment_chunk_adaptively(client, model_name, part, params)
        if not part_entries:
            any_failed = True
            continue
        combined.extend(part_entries)
    if any_failed:
        return _resolved_near_miss(near_miss, resolution_sink)
    combined_quality = validate_segment_quality(chunk, combined)
    if not combined_quality["passed"]:
        codes = {f.get("code") for f in combined_quality["findings"]}
        m = combined_quality["metrics"]
        # Both halves already passed their own segment gate (we only reach here
        # when any_failed is False), so the recombined whole has adequate content
        # coverage. A whole-chunk trigram dip when trigram is the ONLY defect is a
        # split-seam artifact, not lost content - accept it if it still clears the
        # trigram-only near-miss floor rather than discarding two good halves.
        # Recall / ratio / cyrillic / duplicate defects are NOT waived (real).
        if is_trigram_only_near_miss(combined_quality):
            print(f"  Adaptive split (segment) recombination accepted: both halves "
                  f"passed, trigram-only near-miss at seam "
                  f"(trigram={m['ordered_trigram_recall']} recall={m['source_token_recall']})")
            _record_resolution(resolution_sink, "recombination_near_miss")
            return combined
        # Diagnostic: log exactly why a recombination was rejected so we can tell
        # trigram-seam brittleness from real content loss / duplication.
        print(f"  Adaptive split (segment) recombination REJECTED: codes={sorted(codes)} "
              f"metrics={m}")
        return _resolved_near_miss(near_miss, resolution_sink)
    _record_resolution(resolution_sink, "adaptive_split")
    return combined


# Escalating context windows (chars of surrounding source) tried, in order, as a
# last resort when a chunk exhausts normal retries + adaptive split. Defaults;
# overridable via generation config (context_rescue_windows / _retries).
_CONTEXT_RESCUE_WINDOWS = (2000, 4000, 6000)
_CONTEXT_RESCUE_MAX_RETRIES = 2
_CONTEXT_SEGMENT_USER = (
    "The text between the CONTEXT markers below is surrounding material from the "
    "same book, given ONLY as reference for narrative flow and continuity. DO NOT "
    "convert it and DO NOT include any of it in your output.\n\n"
    "=== CONTEXT BEFORE (reference only) ===\n{before}\n=== END CONTEXT ===\n\n"
    "=== CONTEXT AFTER (reference only) ===\n{after}\n=== END CONTEXT ===\n\n"
    "Now convert ONLY the SOURCE TEXT below into the JSON array of "
    '{{"type","text"}} units. Your output must cover exactly the SOURCE TEXT and '
    "nothing from the context.\n\nSOURCE TEXT:\n{chunk}"
)


_CONTEXT_BLEED_MIN_CHARS = 40


def _output_has_context_bleed(entries, chunk, before, after):
    """True if any entry's text clearly leaked from the reference context: it
    appears (normalized) in before+after but NOT in the target chunk. Conservative
    - only entries with >= _CONTEXT_BLEED_MIN_CHARS of normalized text count, so a
    short generic line ("Yes.") that legitimately recurs in both context and chunk
    doesn't trip a false rejection."""
    chunk_norm = normalize_text(chunk)
    context_norm = normalize_text((before or "") + " " + (after or ""))
    if not context_norm:
        return False
    context_tokens = context_norm.split()
    chunk_tokens = chunk_norm.split()
    chunk_spans = {tuple(chunk_tokens[i:i + 8])
                   for i in range(max(0, len(chunk_tokens) - 7))}
    context_spans = {tuple(context_tokens[i:i + 8])
                     for i in range(max(0, len(context_tokens) - 7))}
    for entry in entries:
        text_norm = normalize_text(str((entry or {}).get("text") or "")
                                   if isinstance(entry, dict) else "")
        if (len(text_norm) >= _CONTEXT_BLEED_MIN_CHARS
                and text_norm in context_norm and text_norm not in chunk_norm):
            return True
        entry_tokens = text_norm.split()
        entry_spans = {tuple(entry_tokens[i:i + 8])
                       for i in range(max(0, len(entry_tokens) - 7))}
        if (entry_spans & context_spans) - chunk_spans:
            return True
    return False


def segment_chunk_with_context(client, model_name, chunk, before, after, params,
                               max_retries=2, near_miss_sink=None):
    """Last-resort pass-1 retry: give the model surrounding SOURCE text (before /
    after the failing chunk, reference-only) for narrative flow, but validate that
    the output still covers ONLY the target chunk. Captures a trigram-only
    near-miss into near_miss_sink like the normal segment path. Returns
    [{type,text}] or []."""
    sys_prompt, _ = load_segment_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    user_prompt = _CONTEXT_SEGMENT_USER.format(before=before or "(start of book)",
                                               after=after or "(end of book)",
                                               chunk=chunk)

    def validate(entries):
        # Fidelity gate PLUS a context-bleed guard: a target-correct output that
        # also pastes a reference-context sentence can otherwise pass recall /
        # trigram / ratio (the leaked sentence adds output but doesn't drop source
        # recall), so reject clear context-only entries as a validation failure.
        report = validate_segment_quality(chunk, entries)
        if _output_has_context_bleed(entries, chunk, before, after):
            report = dict(report)
            report["passed"] = False
            report["findings"] = list(report["findings"]) + [{
                "code": "context_bleed",
                "message": "An entry reproduced reference-context text absent from the target chunk."}]
        return report

    return _call_segment(client, model_name, chunk, sys_prompt, user_prompt,
                         params, "SEGMENT+CTX", max_retries, near_miss_sink,
                         validate=validate)


def _tail_join(parts, limit):
    """Join just enough trailing parts to cover `limit` chars from the end,
    instead of materializing the whole list (finding #9). The result may slightly
    exceed limit (the boundary part isn't cut); callers slice [-window:]."""
    acc, total = [], 0
    for part in reversed(parts):
        acc.append(part)
        total += len(part)
        if total >= limit:
            break
    return "".join(reversed(acc))


def _head_join(parts, limit):
    """Join just enough leading parts to cover `limit` chars from the start."""
    acc, total = [], 0
    for part in parts:
        acc.append(part)
        total += len(part)
        if total >= limit:
            break
    return "".join(acc)


def _rescue_prompt_fits(chunk, before, after, overhead_chars, params):
    """Estimate whether a context-rescue prompt for this window fits the model's
    context, leaving room to emit the chunk. Uses the pipeline's chars//3 token
    estimate. Returns True when context_length is unknown (keep prior behavior)."""
    ctx_len = getattr(params, "context_length", None)
    if not ctx_len:
        return True
    prompt_tokens = math.ceil((overhead_chars + len(chunk) + len(before) + len(after)) / 3)
    # Segment output reproduces the chunk text wrapped in JSON; reserve ~1.5x the
    # chunk's token estimate plus a small structural margin as the output budget.
    output_budget = math.ceil(len(chunk) / 3 * 1.5) + 64
    return prompt_tokens + output_budget <= ctx_len


def rescue_chunk_with_context(client, model_name, chunks, index, params,
                              resolution_sink=None, windows=None, max_retries=None):
    """When chunk `index` fails normal segmentation, retry it with escalating
    surrounding-source context. Accepts a clean pass, else the best trigram-only
    near-miss any window produced. Returns entries or []. When resolution_sink is
    given, appends the resolution (context_rescue:<window> /
    context_rescue_near_miss / fail). Windows whose prompt would exceed the
    model's context budget are skipped (finding #4). `windows` and `max_retries`
    default to the module constants when None (finding #12: config-tunable)."""
    windows = windows or _CONTEXT_RESCUE_WINDOWS
    if max_retries is None:
        max_retries = _CONTEXT_RESCUE_MAX_RETRIES
    max_window = max(windows)
    before_all = _tail_join(chunks[:index], max_window)
    after_all = _head_join(chunks[index + 1:], max_window)
    sys_prompt, _ = load_segment_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    overhead_chars = len(sys_prompt) + len(_CONTEXT_SEGMENT_USER)
    best_near_miss = []  # holds the single best [(entries, quality)] seen so far
    for window in windows:
        before, after = before_all[-window:], after_all[:window]
        if not _rescue_prompt_fits(chunks[index], before, after, overhead_chars, params):
            print(f"  context rescue {window}-char window skipped "
                  "(prompt would exceed context budget)")
            continue
        near_miss = []
        seg = segment_chunk_with_context(
            client, model_name, chunks[index],
            before, after, params, max_retries=max_retries, near_miss_sink=near_miss)
        if seg:
            print(f"  chunk {index + 1}/{len(chunks)} rescued with "
                  f"{window}-char surrounding context (clean pass)")
            _record_resolution(resolution_sink, f"context_rescue:{window}")
            return seg
        if near_miss:
            trig = near_miss[0][1]["metrics"]["ordered_trigram_recall"]
            best = (best_near_miss[0][1]["metrics"]["ordered_trigram_recall"]
                    if best_near_miss else -1.0)
            if trig > best:
                best_near_miss = near_miss
            print(f"  context rescue at {window} chars: trigram-only near-miss "
                  f"{trig} captured; escalating")
        else:
            print(f"  context rescue at {window} chars did not pass; escalating")
    if best_near_miss:
        entries, quality = best_near_miss[0]
        print(f"  chunk {index + 1}/{len(chunks)} rescued with context as "
              f"trigram-only near-miss "
              f"(ordered_trigram_recall={quality['metrics']['ordered_trigram_recall']})")
        _record_resolution(resolution_sink, "context_rescue_near_miss")
        return entries
    _record_resolution(resolution_sink, "fail")
    return []


def three_pass_checkpoint_path(output_path):
    return output_path + ".threepass_checkpoint.json"


def three_pass_manifest_path(output_path):
    return output_path + ".threepass_manifest.json"


def _resolution_counts(resolutions):
    """Roll per-chunk resolution strings up into summary counts."""
    return {
        "near_miss_accepted": sum(r == "near_miss" for r in resolutions),
        "context_rescued": sum(r.startswith("context_rescue") for r in resolutions),
        "split_recombined": sum(r in ("adaptive_split", "recombination_near_miss")
                                for r in resolutions),
    }


def _write_manifest(output_path, fingerprint, resolutions, passes, status,
                    failed_pass=None, failed_chunk=None, legacy_resume=False,
                    progress=None):
    """Persist the run manifest next to the output so results are analyzable from
    structured data instead of log-grepping."""
    if not output_path:
        return
    manifest = {
        "fingerprint": fingerprint,
        "status": status,
        "chunks": [{"index": i + 1, "resolution": r}
                   for i, r in enumerate(resolutions)],
        "counts": _resolution_counts(resolutions),
        "passes": passes,
        "legacy_resume": legacy_resume,
        "progress": progress or {},
    }
    if failed_pass is not None:
        manifest["failed_pass"] = failed_pass
    if failed_chunk is not None:
        manifest["failed_chunk"] = failed_chunk
    atomic_json_write(manifest, three_pass_manifest_path(output_path))


def three_pass_fingerprint(source_text, model_name, chunk_size, params=None,
                           on_exhaustion="fail", context_windows=None,
                           context_rescue_retries=None, endpoint=None):
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    settings = {
        "model_name": model_name, "chunk_size": chunk_size,
        "endpoint": endpoint, "on_exhaustion": on_exhaustion,
        "context_windows": context_windows,
        "context_rescue_retries": context_rescue_retries,
        "pipeline_version": 2,
    }
    if params is not None:
        settings.update({name: getattr(params, name, None) for name in (
            "system_prompt", "user_prompt_template", "max_tokens", "temperature",
            "top_p", "top_k", "min_p", "presence_penalty", "banned_tokens",
            "context_length", "hard_max_tokens", "segment_temperature",
            "attribute_temperature", "instruct_temperature",
            "segment_output_ratio")})
    encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {"source_sha256": digest, "settings_sha256": hashlib.sha256(encoded).hexdigest(),
            "model_name": model_name, "pipeline": "three_pass"}


def _load_three_pass_checkpoint(output_path, fingerprint):
    data = safe_load_json(three_pass_checkpoint_path(output_path), None)
    if not isinstance(data, dict) or data.get("fingerprint") != fingerprint:
        return None
    return data


def _save_three_pass_checkpoint(output_path, fingerprint, stage, segmented,
                                chunks_done, named, annotated, resolutions=None,
                                elapsed_s=None):
    atomic_json_write({"fingerprint": fingerprint, "stage": stage,
                       "chunks_done": chunks_done, "segmented": segmented,
                       "named": named, "annotated": annotated,
                       "resolutions": resolutions or [],
                       "elapsed_s": elapsed_s or {}},
                      three_pass_checkpoint_path(output_path))


def run_three_pass(client, model_name, source_text, params, chunk_size,
                   on_exhaustion="fail", output_path=None,
                   context_windows=None, context_rescue_retries=None, endpoint=None):
    """Full flow. Returns the assembled [{speaker,text,instruct}] list, or raises
    RuntimeError if pass 1 exhausts a chunk. When output_path is given, saves a
    checkpoint after each pass-1 chunk and each pass-2/3 batch and resumes from
    it; when None, runs purely in memory. context_windows / context_rescue_retries
    override the context-rescue defaults (finding #12)."""
    chunks = split_into_chunks(source_text, max_size=chunk_size)
    fingerprint = three_pass_fingerprint(
        source_text, model_name, chunk_size, params, on_exhaustion,
        context_windows, context_rescue_retries, endpoint)
    state = _load_three_pass_checkpoint(output_path, fingerprint) if output_path else None
    segmented = state["segmented"] if state else []
    chunks_done = state["chunks_done"] if state else 0
    named = state["named"] if state else []
    annotated = state["annotated"] if state else []
    legacy_resume = bool(state and "resolutions" not in state)
    resolutions = (list(state.get("resolutions", [])) if state
                   else [])
    # A failed chunk may have been checkpointed for timing. It is retried on
    # resume, so discard its provisional resolution and replace it with the
    # eventual outcome instead of emitting two manifest rows for one chunk.
    resolutions = resolutions[:chunks_done]
    if len(resolutions) < chunks_done:
        resolutions.extend(["resumed"] * (chunks_done - len(resolutions)))
    elapsed_s = dict(state.get("elapsed_s", {})) if state else {}

    def save(stage):
        if output_path:
            _save_three_pass_checkpoint(output_path, fingerprint, stage,
                                        segmented, chunks_done, named, annotated,
                                        resolutions, elapsed_s)
    passes = {}

    def emit_manifest(status, failed_pass=None, failed_chunk=None):
        _write_manifest(output_path, fingerprint, resolutions, passes, status,
                        failed_pass=failed_pass, failed_chunk=failed_chunk,
                        legacy_resume=legacy_resume, progress={
                            "source_words": len(source_text.split()),
                            "chunks_total": len(chunks),
                            "chunks_completed": chunks_done,
                            "segmented_entries": len(segmented),
                            "attributed_entries": sum(isinstance(e, dict) for e in named),
                            "instructed_entries": sum(isinstance(e, dict) for e in annotated),
                        })

    # Pass 1 — resume from chunks_done.
    seg_start = time.time()
    seg_base = elapsed_s.get("segment", 0)
    for i in range(chunks_done, len(chunks)):
        sink = []
        seg = segment_chunk_adaptively(client, model_name, chunks[i], params,
                                       resolution_sink=sink)
        if not seg:
            # Last resort: retry with escalating surrounding-source context.
            print(f"  chunk {i + 1}/{len(chunks)} failed normal segmentation; "
                  "trying escalating surrounding-source context")
            seg = rescue_chunk_with_context(client, model_name, chunks, i, params,
                                            resolution_sink=sink,
                                            windows=context_windows,
                                            max_retries=context_rescue_retries)
        resolutions.append(sink[-1] if sink else "fail")
        if not seg:
            elapsed_s["segment"] = seg_base + time.time() - seg_start
            passes["segment"] = {"elapsed_s": round(elapsed_s["segment"], 3),
                                 "status": "failed"}
            save("segment_failed")
            emit_manifest("failed", failed_pass="segment", failed_chunk=i + 1)
            raise RuntimeError(f"pass 1 (segment) failed on chunk {i + 1}/{len(chunks)}")
        segmented.extend(seg)
        chunks_done = i + 1
        elapsed_s["segment"] = seg_base + time.time() - seg_start
        save("segment")
    elapsed_s["segment"] = seg_base + time.time() - seg_start
    passes["segment"] = {"elapsed_s": round(elapsed_s["segment"], 3),
                         "status": "complete"}
    # Pass 2 — deterministic duplicate-free batches, restored to source order.
    # Maintain a running roster (set for O(1) membership + list for order) updated
    # per batch, instead of rescanning the whole `named` prefix every batch.
    named.extend([None] * (len(segmented) - len(named)))
    roster = build_roster(entry for entry in named if isinstance(entry, dict))
    roster_seen = set(roster)
    attr_start = time.time()
    attr_base = elapsed_s.get("attribute", 0)
    try:
        for indexed_batch in iter_unique_entry_batches(segmented):
            pending = [(index, entry) for index, entry in indexed_batch
                       if named[index] is None]
            if not pending:
                continue
            work = [pending]
            while work:
                current = work.pop(0)
                batch = [entry for _, entry in current]
                contexts = [{"previous_context": segmented[index - 1] if index else None,
                             "next_context": segmented[index + 1]
                             if index + 1 < len(segmented) else None}
                            for index, _ in current]
                try:
                    new_named = attribute_batch(
                        client, model_name, batch, params, roster=roster,
                        on_exhaustion=on_exhaustion, neighbor_contexts=contexts)
                except PassExhausted:
                    if len(current) == 1:
                        raise
                    midpoint = len(current) // 2
                    print(f"  Attribution batch exhausted; subdividing "
                          f"{len(current)} -> {midpoint} + {len(current) - midpoint}")
                    work[0:0] = [current[:midpoint], current[midpoint:]]
                    continue
                for (index, _), entry in zip(current, new_named):
                    named[index] = entry
                if on_exhaustion == "fallback":
                    roster = build_roster(
                        entry for entry in named if isinstance(entry, dict))
                    roster_seen = set(roster)
                else:
                    for entry in new_named:
                        speaker = (entry.get("speaker") or "").strip().upper()
                        if (speaker and speaker not in ("NARRATOR", "UNKNOWN")
                                and speaker not in roster_seen):
                            roster_seen.add(speaker)
                            roster.append(speaker)
                elapsed_s["attribute"] = attr_base + time.time() - attr_start
                # Each accepted subdivision is durable; a later single-entry
                # failure resumes after this work instead of replaying the batch.
                save("attribute")
    except PassExhausted:
        elapsed_s["attribute"] = attr_base + time.time() - attr_start
        passes["attribute"] = {"elapsed_s": round(elapsed_s["attribute"], 3),
                               "status": "failed"}
        save("attribute_failed")
        emit_manifest("failed", failed_pass="attribute")
        raise
    elapsed_s["attribute"] = attr_base + time.time() - attr_start
    passes["attribute"] = {"elapsed_s": round(elapsed_s["attribute"], 3),
                           "status": "complete"}
    # Pass 3 uses the same duplicate-free scheduling so ambiguous heads cannot
    # slip through there either (finding #5).
    annotated.extend([None] * (len(named) - len(annotated)))
    inst_start = time.time()
    inst_base = elapsed_s.get("instruct", 0)
    for indexed_batch in iter_unique_entry_batches(named):
        pending = [(index, entry) for index, entry in indexed_batch
                   if annotated[index] is None]
        if not pending:
            continue
        batch = [entry for _, entry in pending]
        contexts = [{"previous_context": named[index - 1] if index else None,
                     "next_context": named[index + 1]
                     if index + 1 < len(named) else None}
                    for index, _ in pending]
        new_annotated = instruct_batch(client, model_name, batch, params,
                                       neighbor_contexts=contexts)
        for (index, _), entry in zip(pending, new_annotated):
            annotated[index] = entry
        elapsed_s["instruct"] = inst_base + time.time() - inst_start
        save("instruct")
    elapsed_s["instruct"] = inst_base + time.time() - inst_start
    passes["instruct"] = {"elapsed_s": round(elapsed_s["instruct"], 3),
                          "status": "complete"}
    save("done")
    emit_manifest("complete")
    return annotated


def main():
    parser = argparse.ArgumentParser(description="Three-pass annotated script generation.")
    parser.add_argument("input_file")
    parser.add_argument("--output", default=None)
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--strip-front-matter", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--pass2-on-exhaustion", choices=["fail", "fallback"],
                        default="fail",
                        help="testing default 'fail' surfaces pass-2 failures; "
                             "'fallback' degrades gracefully (production).")
    args = parser.parse_args()

    with open(args.input_file, encoding="utf-8", errors="replace") as fh:
        book = fh.read()
    book = fix_mojibake(book)
    book, _ = normalize_known_source_corruptions(book)
    if args.strip_front_matter:
        book, _ = strip_known_front_matter(book)

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    config = load_app_config(get_app_config_path(data_dir, root, app_dir))
    llm = config.get("llm", {})
    gen = config.get("generation") or {}
    try:
        chunk_size = resolve_chunk_size(args.chunk_size, gen.get("chunk_size", 6000))
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    base_url = llm.get("base_url", "http://localhost:1234/v1")
    model_name = llm.get("model_name")
    llm_mode = config.get("llm_mode", "local")
    model_profile = (gen.get("three_pass_model_profiles") or {}).get(model_name, {})
    # Self-heal LM Studio: load model_name at its verified context if nothing is
    # loaded / settings are stale, mirroring generate_script.py. Without this a
    # fresh `lms unload` leaves no model loaded and every call 400s.
    _, lm_status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"))
    print(heal_msg)
    params = LLMGenParams(
        max_tokens=gen.get("max_tokens", 10000),
        temperature=gen.get("temperature", 0.6),
        top_p=gen.get("top_p", 0.8),
        top_k=gen.get("top_k"), min_p=gen.get("min_p"),
        context_length=lm_status.get("context_length"),
        segment_temperature=model_profile.get(
            "segment_temperature", gen.get("three_pass_segment_temperature", 0.1)),
        attribute_temperature=model_profile.get(
            "attribute_temperature", gen.get("three_pass_attribute_temperature", 0.1)),
        instruct_temperature=model_profile.get(
            "instruct_temperature", gen.get("three_pass_instruct_temperature", 0.1)),
        segment_output_ratio=model_profile.get(
            "segment_output_ratio", gen.get("three_pass_segment_output_ratio", 3.0)))
    client = OpenAI(base_url=base_url, api_key=llm.get("api_key", "local"))

    # Context-rescue tuning (finding #12): config-overridable, else defaults.
    cfg_windows = gen.get("context_rescue_windows")
    context_windows = tuple(cfg_windows) if cfg_windows else None
    context_rescue_retries = gen.get("context_rescue_retries")

    output_path = args.output or os.path.join(root, "annotated_script.json")
    print(f"Three-pass generation: {len(book)} chars, chunk_size={chunk_size}, "
          f"model={model_name}, pass2_on_exhaustion={args.pass2_on_exhaustion}")
    try:
        entries = run_three_pass(client, model_name, book, params, chunk_size,
                                 on_exhaustion=args.pass2_on_exhaustion,
                                 output_path=output_path,
                                 context_windows=context_windows,
                                 context_rescue_retries=context_rescue_retries,
                                 endpoint=base_url)
    except (RuntimeError, PassExhausted) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    atomic_json_write(entries, output_path)
    print(f"Wrote {len(entries)} entries to {output_path}")


if __name__ == "__main__":
    main()
