"""Three-pass script generation orchestrator (segment -> attribute -> instruct).
A side-by-side alternative to generate_script.py's single pass; the single-pass
path is untouched. See docs/superpowers/specs/2026-07-21-three-pass-script-generation-design.md."""

import argparse
import json
import os
import sys

from openai import OpenAI

from generate_script import (call_llm_for_entries, split_into_chunks,
                             fix_mojibake, LLMGenParams)
from source_normalization import (normalize_known_source_corruptions,
                                  strip_known_front_matter)
from speaker_identity import stabilize_speaker_identities
from default_prompts import (load_segment_prompts, load_attribute_prompts,
                             load_instruct_prompts)
from pass_quality import (validate_segment_quality, validate_attribution,
                          validate_instruct)
from config_settings import load_app_config
from utils import get_runtime_data_dir, get_app_config_path

BATCH_SIZE = 25
NARRATOR_DEFAULT_INSTRUCT = "Neutral, even narration."
CHARACTER_DEFAULT_INSTRUCT = "Natural, in-character delivery."


def iter_entry_batches(entries, batch_size=BATCH_SIZE):
    for start in range(0, len(entries), batch_size):
        yield entries[start:start + batch_size]


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
                    max_retries=3, on_exhaustion="fail"):
    """Assign speakers to one batch of frozen {type,text} entries. Enforces the
    text freeze; retries on invalid output. On exhaustion: 'fail' raises
    PassExhausted (testing default); 'fallback' keeps frozen text and labels
    unresolved SPOKEN spans UNKNOWN via stabilize_speaker_identities."""
    sys_prompt, usr_template = load_attribute_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    batch_json = json.dumps([{"type": e["type"], "text": e["text"]}
                             for e in frozen_batch], ensure_ascii=False)
    user_prompt = usr_template.format(roster=", ".join(roster) or "(none yet)",
                                      batch=batch_json)
    named = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log", label="ATTRIBUTE", max_retries=max_retries,
        validate_entries=lambda entries: validate_attribution(frozen_batch, entries))
    if named:
        return named
    if on_exhaustion == "fail":
        raise PassExhausted(f"attribution failed for a {len(frozen_batch)}-entry batch")
    seeded = [{"speaker": "NARRATOR" if e["type"] == "NARRATOR" else "UNKNOWN",
               "text": e["text"]} for e in frozen_batch]
    return stabilize_speaker_identities(seeded, established_speakers=roster)["entries"]


def instruct_batch(client, model_name, prior_batch, params, max_retries=3):
    """Add instruct to one batch of {speaker,text} entries. Enforces the freeze
    on text+speaker. On exhaustion, attaches a default instruct per entry so
    pass 3 never fails the book."""
    sys_prompt, usr_template = load_instruct_prompts()
    if params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    batch_json = json.dumps([{"speaker": e["speaker"], "text": e["text"]}
                             for e in prior_batch], ensure_ascii=False)
    user_prompt = usr_template.format(batch=batch_json)
    annotated = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log", label="INSTRUCT", max_retries=max_retries,
        validate_entries=lambda entries: validate_instruct(prior_batch, entries))
    if annotated:
        return annotated
    return [{"speaker": e["speaker"], "text": e["text"],
             "instruct": default_instruct(e)} for e in prior_batch]
