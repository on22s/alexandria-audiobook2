"""Three-pass script generation orchestrator (segment -> attribute -> instruct).
A side-by-side alternative to generate_script.py's single pass; the single-pass
path is untouched. See docs/superpowers/specs/2026-07-21-three-pass-script-generation-design.md."""

import argparse
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
