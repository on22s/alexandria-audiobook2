"""Three-pass script generation orchestrator (segment -> attribute -> instruct).
See docs/superpowers/specs/2026-07-21-three-pass-script-generation-design.md."""

import argparse
import hashlib
import json
import math
from generation_checkpoint_deltas import GenerationCheckpointDeltas, load_generation_delta_checkpoint
from chunk_status_journal import remove_chunk_snapshot
import os
import re
import sys
import time
from pathlib import Path
from collections import Counter
from dataclasses import replace
from types import MappingProxyType

from core import llm_timeout_seconds
from source_encoding import read_source_text, get_normalized_source_newlines
from llm_provider import make_llm_client, make_run_client, get_run_model_binding, get_run_fingerprint_identity

from judge_reason_log import record_judge_run
from generate_script import (call_llm_for_entries, split_into_chunks,
                             split_into_chunk_records,
                             get_preprocessed_source, LLMGenParams,
                             split_failed_chunk, is_trigram_only_near_miss,
                             ensure_run_request_params)
from dialogue_spans import apply_dialogue_map
from script_preflight import (audit_unicode_text,
                              replacement_load_is_acceptable,
                              replacement_repair_hint)
from speaker_identity import stabilize_speaker_identities
from speaker_traits import get_established_traits
from repair_source_encoding import preflight_source
from script_repair import build_deterministic_repair
from default_prompts import (load_segment_prompts, load_attribute_prompts,
                             load_instruct_prompts)
from narrator_prompt import (add_narrator_prior, get_valid_narrator_name,
                             is_narrator_attested, normalize_narrator_name)
from pass_quality import (is_attested_name, strip_roster_alias_echo,
                          classify_lexical_quote_regions,
                          validate_segment_quality, validate_attribution,
                          validate_instruct, index_head_check,
                          analyze_outer_quote_regions, split_outer_quote_regions,
                          QUOTE_MARKS)
from review_script import normalize_text
from config_settings import load_app_config, get_generation_config
from lmstudio_settings import (ensure_ideal_settings, get_active_llm_config,
                               get_effective_max_tokens, TokenBudgetError)
from utils import (get_runtime_data_dir, get_app_config_path,
                   atomic_json_write, safe_load_json, is_nonverbal_text)

BATCH_SIZE = 25
NARRATOR_DEFAULT_INSTRUCT = "Neutral, even narration."
CHARACTER_DEFAULT_INSTRUCT = "Natural, in-character delivery."


def _record_resolution(sink, value):
    """Append a per-chunk pass-1 resolution to the telemetry sink, if one is
    provided. `sink` is a per-chunk list; callers read its last entry."""
    if sink is not None:
        sink.append(value)


def as_profile_mapping(profile):
    """Return a plain mapping for one three_pass_model_profiles entry.

    load_app_config validates that section into ThreePassModelProfile objects,
    but this module reads profiles with .get(), so a configured profile
    previously crashed the run with AttributeError. None-valued fields are
    dropped so an unset profile key falls through to the caller's default
    instead of overriding it with None.
    """
    if profile is None:
        return {}
    if hasattr(profile, "model_dump"):
        return {key: value for key, value in profile.model_dump().items()
                if value is not None}
    return profile


DEFAULT_MODEL_PROFILES_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "three_pass_model_profiles.json")


def load_default_model_profiles(path=None):
    """Return the measured per-model profiles checked into the repo.

    app/config.json is gitignored and machine-local, so profiles set only
    there would not reproduce on another machine. These defaults ship with the
    code; config.json still wins per key for local experiments.
    """
    path = path or DEFAULT_MODEL_PROFILES_PATH
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load three-pass model profiles from {path}: {exc}") from exc
    if not isinstance(loaded, dict) or any(not isinstance(profile, dict)
                                           for profile in loaded.values()):
        raise ValueError(f"Invalid three-pass model profiles in {path}: expected model-to-profile objects")
    return loaded


def resolve_model_profile(model_name, config_profiles, defaults):
    """Merge the checked-in profile for a model with any config override."""
    merged = dict(as_profile_mapping((defaults or {}).get(model_name)))
    merged.update(as_profile_mapping((config_profiles or {}).get(model_name)))
    return merged


def resolve_chunk_size(cli_value, config_value, model_value=None):
    """Resolve the effective chunk size (CLI overrides config) and validate it.
    Guards BOTH sources (finding #14): a bad config chunk_size previously slipped
    through because only the CLI value was checked. Raises ValueError on < 1."""
    chunk_size = (cli_value if cli_value is not None else
                  model_value if model_value is not None else config_value)
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError(f"chunk_size must be an integer >= 1 (got {chunk_size!r})")
    return chunk_size


def resolve_hard_max_tokens(configured_max_tokens):
    """-> the escalation ceiling for a run whose Setup budget is `configured_max_tokens`.

    Never below the dataclass default, so local setups keep their headroom; a
    larger configured budget raises the ceiling with it. get_effective_max_tokens
    still clamps every call to the server's real context when one is known.
    """
    return max(LLMGenParams.hard_max_tokens, int(configured_max_tokens))


def resolve_three_pass_generation_settings(config, chunk_size_override=None,
                                           segmentation_override=None):
    """Resolve model-profile-sensitive settings shared by runtime and preflight."""
    llm = get_active_llm_config(config)
    gen = config.get("generation") or {}
    model_name = llm.get("model_name")
    model_profile = resolve_model_profile(
        model_name, gen.get("three_pass_model_profiles"),
        load_default_model_profiles())
    return {
        "model_profile": model_profile,
        "chunk_size": resolve_chunk_size(
            chunk_size_override, gen.get("three_pass_chunk_size", 3000),
            model_profile.get("chunk_size")),
        "max_tokens": gen.get("max_tokens", 10000),
        "segment_output_ratio": model_profile.get(
            "segment_output_ratio", gen.get("three_pass_segment_output_ratio", 3.0)),
        "segmentation": segmentation_override or model_profile.get(
            "segmentation", gen.get("three_pass_segmentation") or "auto"),
        "quoted_must_be_spoken": gen.get(
            "three_pass_quoted_must_be_spoken", True) is not False,
        "unquoted_must_be_narrator": gen.get(
            "three_pass_unquoted_must_be_narrator", True) is not False,
        "keep_scope": "batch" if gen.get("three_pass_keep_whole_batch") is True else "line",
        "attribute_batch_size": int(gen.get("three_pass_attribute_batch_size", BATCH_SIZE)),
        "attribute_context_chars": int(gen.get("three_pass_attribute_context_chars", 2000)),
        "attribute_prompt_variant": gen.get("three_pass_attribute_prompt_variant") or "michel2_full",
    }


def get_three_pass_run_params(config, lm_status, reasoning_effort=None, generation_settings=None):
    """Build the shared native generation/retry policy without changing config."""
    generation_settings = (generation_settings if generation_settings is not None
                           else resolve_three_pass_generation_settings(config))
    gen = config.get("generation") or {}
    llm = get_active_llm_config(config)
    model_profile = generation_settings["model_profile"]
    params = LLMGenParams(
        max_tokens=generation_settings["max_tokens"],
        # The escalation ceiling follows the configured budget. It used to stay
        # at the dataclass default (16384) whatever Setup said, so a hosted
        # reasoning model that thinks for 16k tokens was cut off at exactly
        # that point with "cannot grow beyond 16384" while the user had set
        # 65536 (reported 2026-09-17 against an OpenRouter model via a
        # GPT-Load gateway). Never below the default, so local setups keep
        # their headroom; get_effective_max_tokens still clamps to the
        # server's real context when one is known.
        hard_max_tokens=resolve_hard_max_tokens(generation_settings["max_tokens"]),
        temperature=gen.get("temperature", 0.6),
        top_p=gen.get("top_p", 0.8),
        top_k=gen.get("top_k"), min_p=gen.get("min_p"),
        context_length=lm_status.get("context_length"),
        segment_temperature=model_profile.get(
            # Segmentation and attribution are classification, not writing:
            # each has one right answer, so sampling only adds noise. Measured
            # on mushoku16, sending an identical attribution batch twice at
            # temperature 0.1 changed 23.6% of speakers; at 0.0 it changed 0%.
            # That noise was most of the 37.4% run-to-run disagreement that
            # made model comparison impossible, and it also meant regenerating
            # a book produced materially different speakers each time.
            # instruct stays at 0.1: it is the one genuinely generative pass,
            # writing delivery direction rather than choosing a label.
            "segment_temperature", gen.get("three_pass_segment_temperature", 0.0)),
        attribute_temperature=model_profile.get(
            "attribute_temperature", gen.get("three_pass_attribute_temperature", 0.0)),
        instruct_temperature=model_profile.get(
            "instruct_temperature", gen.get("three_pass_instruct_temperature", 0.1)),
        segment_output_ratio=generation_settings["segment_output_ratio"],
        segmentation=generation_settings["segmentation"],
        quoted_must_be_spoken=generation_settings["quoted_must_be_spoken"],
        unquoted_must_be_narrator=generation_settings["unquoted_must_be_narrator"],
        reasoning_effort=reasoning_effort,
        provider_extra_body=llm.get("provider_extra_body"),
        structured_output=llm.get("structured_output", "auto"),
        api_retry_limit=llm.get("api_retry_limit"),
        retry_initial_delay_seconds=llm.get("retry_initial_delay_seconds", 1),
        retry_multiplier=llm.get("retry_multiplier", 2),
        retry_max_delay_seconds=llm.get("retry_max_delay_seconds", 30),
        retry_jitter=llm.get("retry_jitter", 0.2),
        on_api_exhaustion=llm.get("on_api_exhaustion", "fail"))
    params.segment_system_prompt, params.segment_user_prompt_template = resolve_three_pass_prompt(
        config, "pass1")
    params.instruct_system_prompt, params.instruct_user_prompt_template = resolve_three_pass_prompt(
        config, "pass3")
    return params


def resolve_attribute_prompt(config, variant_override=None):
    """-> (variant, texts) the run sends: the active preset (Setup -> Prompt
    Customization), or with --prompt-variant the builtin of that variant."""
    from attribution_prompt_variants import (builtin_texts, get_group_rule_system,
                                             resolve_attribution_preset)
    if variant_override:
        variant, texts = variant_override, None
    else:
        variant, texts, _ = resolve_attribution_preset(config)
    if (config.get("generation") or {}).get("three_pass_group_rule") is True:
        # Setup switch (#653). Sent as preset text, so the changed prompt is
        # part of the checkpoint identity like any edited preset.
        system = (texts or {}).get("system") or builtin_texts(variant)["system"]
        system, applied = get_group_rule_system(system)
        if applied:
            texts = dict(texts or {}, system=system)
    if (config.get("generation") or {}).get("three_pass_speaker_traits") is True:
        # Setup switch (#653): every pass-2 answer also carries the speaker's
        # gender, age group and ageless flag (speaker_traits.TRAITS_RULE).
        # Only the michel2 prompts name their answer shape; any other prompt is
        # left alone and the run says so (is_speaker_traits_prompt).
        from speaker_traits import TRAITS_FIELDS, TRAITS_RULE
        base = builtin_texts(variant)
        user = (texts or {}).get("user") or base["user"]
        if '{"n", "speaker"}' in user:
            system = (texts or {}).get("system") or base["system"]
            texts = dict(texts or {}, system=system + TRAITS_RULE,
                         user=user.replace('{"n", "speaker"}', TRAITS_FIELDS))
    return variant, texts


def is_speaker_traits_prompt(texts):
    """Whether the resolved pass-2 texts ask for the per-line traits."""
    from speaker_traits import TRAITS_FIELDS
    return TRAITS_FIELDS in ((texts or {}).get("user") or "")


def resolve_three_pass_prompt(config, pass_name):
    """Return custom (system, user) text for a numbered pass, if selected."""
    prompts = config.get("prompts") or {}
    active = prompts.get(f"{pass_name}_preset") or "default"
    if active == "default":
        return None, None
    for preset in prompts.get(f"{pass_name}_prompt_presets") or []:
        if isinstance(preset, dict) and preset.get("name") == active:
            return preset.get("system_prompt") or None, preset.get("user_prompt") or None
    return None, None


def build_window_surround(segmented, window_indices, chars):
    """Up to `chars` characters of the segmented text on each side of a window,
    as {"before", "after"} strings for the attribution prompt. SPOKEN entries
    get their quote marks back (the segmenter strips them) so the model can
    tell speech from narration in the evidence. Empty strings when chars is 0
    or the window touches the book's edge."""
    if not chars or not window_indices:
        return {"before": "", "after": ""}

    def gather(indices, take_from_end):
        out, total = [], 0
        for i in indices:
            entry = segmented[i]
            text = entry.get("text") or ""
            if entry.get("type") == "SPOKEN":
                text = f"\u201c{text}\u201d"
            if total + len(text) > chars:
                continue
            out.append(text)
            total += len(text) + 1
        return " ".join(reversed(out) if take_from_end else out)

    first, last = window_indices[0], window_indices[-1]
    return {"before": gather(range(first - 1, -1, -1), True) if first else "",
            "after": gather(range(last + 1, len(segmented)), False)}


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
            if not isinstance(entry, dict):
                continue
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


def get_missing_attribute_contexts(entries, indices, batch_size):
    """Read-only adjacent evidence omitted from the current source-window batch."""
    if not indices:
        return []
    included = set(indices)
    start = indices[0] // batch_size * batch_size
    end = min(start + batch_size, len(entries))
    contexts = []
    for index in indices:
        context = {}
        for key, neighbor in (("previous_context", index - 1),
                              ("next_context", index + 1)):
            if start <= neighbor < end and neighbor not in included:
                context[key] = entries[neighbor]
        contexts.append(context)
    return contexts


MIN_ROSTER_ATTESTATIONS = 3


def build_roster(entries, source_text=None):
    """Ordered unique UPPERCASE speaker names seen so far, excluding NARRATOR and
    the UNKNOWN placeholder — fed to pass 2 for naming consistency.

    When source_text is given, a name must also be attested in the prose. The
    roster is the propagation vector for a hallucinated speaker: once a bad name
    is in it, every later batch is told that name is an established character.
    On mushoku16 a single invention at entry 11 spread to entry 1,106. Gating
    admission contains the damage to the one entry that produced it.
    """
    return attested_new_speakers(entries, set(), source_text)


def attested_new_speakers(entries, roster_seen, source_text):
    """Speakers in ``entries`` that belong in the roster and are not in it yet.

    The incremental counterpart to build_roster, which rescans everything. Both
    ask the same question with the same threshold, so the running roster and a
    rebuilt one cannot disagree - they previously did, because the incremental
    path applied no gate at all and admitted any accepted speaker.

    Returns the names to add; the caller owns the roster and updates it, so
    nothing here mutates what it is given.
    """
    new = []
    for entry in entries:
        speaker = (entry.get("speaker") or "").strip().upper()
        if (not speaker or speaker in ("NARRATOR", "UNKNOWN")
                or speaker in roster_seen or speaker in new
                or entry.get("attribution_unchecked")):
            continue
        if not is_attested_name(speaker, source_text, MIN_ROSTER_ATTESTATIONS):
            continue
        new.append(speaker)
    return new


def load_cast(path):
    """A supplied cast list: [{"name": str, "aliases": [str, ...]}, ...], bare or
    under "cast" as build_cast_list.py writes it.

    Returns {"names", "alias_groups", "alias_to_name", "known_names", "sha256"},
    all upper-case. The names go on the pass-2 roster ahead of the attested
    ones, bypassing MIN_ROSTER_ATTESTATIONS, because a person or a separate pass
    (experiments/build_cast_list.py) supplied them: the capitalisation gate can
    never admit "the stranger", which is how The Invisible Man's protagonist
    lost 99 lines to UNKNOWN in stage 0 (2026-09-28). A malformed file raises.
    """
    raw = open(path, "rb").read()
    try:
        cast = get_cast_from_data(json.loads(raw.decode("utf-8")))
    except ValueError as exc:
        raise ValueError(f"cast file {path}: {exc}") from exc
    return {**cast, "sha256": hashlib.sha256(raw).hexdigest()}


def get_cast_from_data(data):
    """The one cast validator, for a file (load_cast) and an edited list (the
    app's PUT /api/cast_list) alike. Returns load_cast's dict without the
    file hash, which only a file has."""
    if isinstance(data, dict):          # cast_list.py output: {"cast": [...], "provenance"}
        data = data.get("cast")
    if not isinstance(data, list) or not data:
        raise ValueError("must be a non-empty JSON list")
    names, groups, alias_to_name = [], [], {}
    for item in data:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not item["name"].strip()
                or not isinstance(item.get("aliases", []), list)
                or not all(isinstance(a, str) for a in item.get("aliases", []))):
            raise ValueError(f"bad entry {item!r}")
        name = item["name"].strip().upper()
        if name in names:
            continue
        aliases = [a.strip().upper() for a in item.get("aliases", [])
                   if a.strip() and a.strip().upper() != name]
        names.append(name)
        groups.append([name] + aliases)
        for alias in aliases:
            alias_to_name.setdefault(alias, name)
    return {"names": names, "alias_groups": groups, "alias_to_name": alias_to_name,
            "known_names": frozenset(names) | frozenset(alias_to_name)}


def default_instruct(entry):
    speaker = (entry.get("speaker") or "").strip().upper()
    return NARRATOR_DEFAULT_INSTRUCT if speaker == "NARRATOR" else CHARACTER_DEFAULT_INSTRUCT


def get_deterministic_named_entry(entry):
    """Resolve entries whose speaker is explicit without invoking the LLM."""
    if entry.get("type") == "NARRATOR":
        return {"speaker": "NARRATOR", "text": entry["text"]}
    if is_nonverbal_text(entry.get("text")):
        return {"speaker": "NARRATOR", "text": entry["text"]}
    if entry.get("source_label"):
        label = str(entry["source_label"]).strip().rstrip(":")
        return {"speaker": (label.upper() if label.strip("?") else "UNKNOWN"),
                "text": entry["text"]}
    return None


def get_attribute_pending_entries(indexed_batch, named, deterministic, diagnostic_failures):
    """Return the exact still-pending attribution targets and narrator context."""
    return [(index, entry) for index, entry in indexed_batch
            if (named[index] is None or index in deterministic)
            and not any(failure["pass"] == "attribute" and failure.get("entry") == index
                        for failure in diagnostic_failures)]


class PassExhausted(Exception):
    """A pass-2/3 batch could not produce valid output within its retry budget.
    In testing mode (on_exhaustion='fail') this aborts the book so the real
    failure rate is visible. `last_entries` is the final attempt's parsed
    response (or None): it failed validation, so it must never be used as a
    result, but an evaluator may score it row by row rather than lose the
    whole window."""

    def __init__(self, *args, last_entries=None):
        super().__init__(*args)
        self.last_entries = last_entries


SURROUND_BEFORE_HEADER = ("TEXT BEFORE THIS WINDOW (evidence only; do not attribute "
                          "or return it):")
SURROUND_AFTER_HEADER = ("TEXT AFTER THIS WINDOW (evidence only; do not attribute "
                         "or return it):")


def build_attribute_request(frozen_batch, params, roster,
                            neighbor_contexts=None, surround=None):
    """Build the canonical pass-2 system and user prompts. `surround`
    ({"before", "after"} text from build_window_surround) wraps the unchanged
    body in evidence blocks; None or empty strings leave the prompt exactly as
    it was."""
    sys_prompt, usr_template = load_attribute_prompts()
    if params.attribute_system_prompt:
        sys_prompt = params.attribute_system_prompt
    elif params.system_prompt:
        sys_prompt = params.system_prompt
    if params.user_prompt_template:
        usr_template = params.user_prompt_template
    neighbor_contexts = neighbor_contexts or [{} for _ in frozen_batch]
    batch_json = json.dumps([
        {"n": i, "type": e["type"], "text": e["text"], **neighbor_contexts[i]}
        for i, e in enumerate(frozen_batch)], ensure_ascii=False)
    user_prompt = usr_template.format(
        roster=", ".join(roster) or "(none yet)", batch=batch_json)
    before = (surround or {}).get("before") or ""
    after = (surround or {}).get("after") or ""
    if before:
        user_prompt = f"{SURROUND_BEFORE_HEADER}\n{before}\n\n{user_prompt}"
    if after:
        user_prompt = f"{user_prompt}\n\n{SURROUND_AFTER_HEADER}\n{after}"
    return sys_prompt, user_prompt


# What the attribution pass asks the server to constrain replies to, when
# the profile's structured_output is "auto" (issue #522 s9.1). Validation of
# the CONTENT (index head check, text freeze, roster membership) is unchanged:
# the schema only guarantees the shape.
ATTRIBUTION_RESPONSE_SCHEMA = {
    "name": "speaker_attribution",
    "schema": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"n": {"type": "integer"}, "speaker": {"type": "string"}},
            "required": ["n", "speaker"],
            "additionalProperties": False,
        },
    },
}


def get_attribution_response_schema(speaker_traits=False):
    """The pass-2 response schema; with per-line traits on, the three optional
    fields are allowed (speaker_traits.get_traits_response_schema)."""
    if not speaker_traits:
        return ATTRIBUTION_RESPONSE_SCHEMA
    from speaker_traits import get_traits_response_schema
    return get_traits_response_schema(ATTRIBUTION_RESPONSE_SCHEMA)


# Checks whose refusal of a single line is not evidence the answer is wrong.
# Audited 2026-09-28 against PDNC on the stage-0 books: of the clear cases,
# speaker_not_in_source refused the PDNC speaker every time (Silas Durgan, named
# once; Louisa Gradgrind) and spoken_not_named was wrong 9 of 32, each a quoted
# phrase pass 1 took for speech ("the Hands," / "Scarlet Coat") where the model
# correctly answered that nobody speaks. narrator_renamed was right 29 of 29 and
# is deliberately NOT here.
KEEPABLE_ATTRIBUTION_FAILURES = frozenset({"speaker_not_in_source", "spoken_not_named"})


PASS2_KEEP_SCOPES = ("line", "batch")


def is_keepable_exhaustion(frozen_batch, last_entries, last_report):
    """Whether a batch's last answer is aligned and failed ONLY keepable checks.

    The one predicate for both keep scopes: 'line' asks it of a single entry,
    'batch' of a whole batch, so the two cannot disagree about what is keepable.
    """
    findings = (last_report or {}).get("findings", [])
    codes = {finding.get("code") or "unknown" for finding in findings}
    if not last_entries or not codes or not codes <= KEEPABLE_ATTRIBUTION_FAILURES:
        return False
    if not all(isinstance(finding.get("entry_number"), int) for finding in findings):
        return False
    return index_head_check(frozen_batch, last_entries)[0]


def get_named_from_answer(frozen_batch, ordered, cast=None, speaker_traits=False):
    """The accepted answer as named entries - the one conversion for a passed
    batch and a kept one: frozen text, the model's speaker, a cast alias folded
    to its name ("GRIFFIN" -> THE STRANGER, one voice per character)."""
    alias_to_name = (cast or {}).get("alias_to_name") or {}
    out = []
    for f, item in zip(frozen_batch, ordered):
        speaker = strip_roster_alias_echo(item.get("speaker"))
        if isinstance(speaker, str):
            speaker = alias_to_name.get(speaker.strip().upper(), speaker)
        named = {**{k: v for k, v in f.items() if k != "type"}, "speaker": speaker}
        # Traits describe a character: a spoken line the model gave to the
        # narrator (kept unchecked) carries none, so NARRATOR gets no tag.
        if (speaker_traits and f.get("type") == "SPOKEN"
                and str(speaker or "").strip().upper() != "NARRATOR"):
            from speaker_traits import get_traits_from_answer
            named.update(get_traits_from_answer(item))
        out.append(named)
    return out


def keep_exhausted_answer(frozen_batch, last_entries, last_report, roster, cast=None,
                          speaker_traits=False):
    """What on_exhaustion='keep' returns for a batch that ran out of retries.

    A batch whose last answer failed ONLY keepable checks keeps that answer:
    every line takes the model's speaker, and only the lines a check named are
    flagged `attribution_unchecked` with their codes. Anything else (unparsed,
    misaligned, an unkeepable check) gets the fallback labels, all flagged.
    The roster gates skip flagged entries, so a kept name cannot spread to
    later batches. The caller decides whether a multi-entry batch may be kept
    (keep scope 'batch') or must be subdivided first (scope 'line').
    """
    codes = sorted({finding.get("code") or "unknown"
                    for finding in (last_report or {}).get("findings", [])}) or ["unparsed"]
    if is_keepable_exhaustion(frozen_batch, last_entries, last_report):
        ordered = index_head_check(frozen_batch, last_entries)[2]
        flagged = {}
        for finding in last_report["findings"]:
            flagged.setdefault(finding["entry_number"] - 1, set()).add(finding["code"])
        kept = get_named_from_answer(frozen_batch, ordered, cast, speaker_traits)
        for index, entry_codes in flagged.items():
            speaker = kept[index].get("speaker")
            kept[index] = {**kept[index],
                           "speaker": speaker.strip() if isinstance(speaker, str)
                           and speaker.strip() else "UNKNOWN",
                           "attribution_unchecked": sorted(entry_codes)}
        print(f"  Attribution exhausted; kept the model's last answer for "
              f"{len(frozen_batch)} entr{'y' if len(frozen_batch) == 1 else 'ies'}, "
              f"{len(flagged)} unchecked ({', '.join(codes)})")
        return kept
    print(f"  Attribution exhausted; labelled {len(frozen_batch)} entr"
          f"{'y' if len(frozen_batch) == 1 else 'ies'} with the fallback ({', '.join(codes)})")
    seeded = [{**{k: v for k, v in e.items() if k != "type"},
               "speaker": "NARRATOR" if e["type"] == "NARRATOR" else "UNKNOWN"}
              for e in frozen_batch]
    return [{**entry, "attribution_unchecked": codes} for entry in
            stabilize_speaker_identities(seeded, established_speakers=roster)["entries"]]


def get_exhausted_runtime_batch_ranges(client, batch, attempts):
    """Return bounded halves when the fallback runtime cannot serve this batch."""
    if (len(batch) > 1 and getattr(client, "switched", False)
            and any(attempt.get("error_category") == "context_budget"
                    or attempt.get("finish_reason") == "length" for attempt in attempts)):
        middle = len(batch) // 2
        return [(0, middle), (middle, len(batch))]
    return []


def attribute_batch(client, model_name, frozen_batch, params, roster,
                    max_retries=3, on_exhaustion="fail", neighbor_contexts=None,
                    attempt_observer=None, source_text=None,
                    exhaustion_sink=None, entries_provider=None, surround=None,
                    cast=None, keep_scope="line", speaker_traits=False, roster_traits=None):
    """Assign speakers to one batch of frozen {type,text} entries. Enforces the
    text freeze; retries on invalid output. On exhaustion: 'fail' raises
    PassExhausted (testing default); 'fallback' keeps frozen text and labels
    unresolved SPOKEN spans UNKNOWN via stabilize_speaker_identities; 'keep'
    raises for a multi-entry batch (so the caller subdivides) and, at one
    entry, returns keep_exhausted_answer instead of aborting the book. With
    keep_scope='batch' a multi-entry batch whose last answer failed only
    keepable checks is kept whole instead of subdivided (#668: one rejected
    label otherwise costs a halving per level, ~25 requests for 25 lines)."""
    params = ensure_run_request_params(client, params)
    sys_prompt, user_prompt = build_attribute_request(
        frozen_batch, params, roster, neighbor_contexts, surround)
    validated = {}
    attempts = []

    def observe(attempt):
        attempts.append(attempt)
        if attempt_observer:
            attempt_observer(attempt)

    def validate(entries):
        validated["last"] = entries
        report = validate_attribution(frozen_batch, entries, source_text,
                                      known_names=(cast or {}).get("known_names"))
        validated["last_report"] = report
        if report["passed"]:
            validated["ordered"] = index_head_check(frozen_batch, entries)[2]
        return report

    call_params = replace(params, temperature=(params.attribute_temperature
                                               if params.attribute_temperature is not None
                                               else params.temperature),
                          response_schema=get_attribution_response_schema(speaker_traits))
    # entries_provider REPLACES ONLY THE LLM CALL. Everything that makes this
    # function safe - validate_attribution's text freeze, the index_head_check
    # binding, the exhaustion path - is shared by any provider, so an
    # alternative attribution strategy cannot quietly skip them. Default None
    # is the production path, byte-identical to before.
    if entries_provider is None:
        named = call_llm_for_entries(
            client, model_name, sys_prompt, user_prompt, call_params,
            log_name="llm_responses.log", label="ATTRIBUTE",
            max_retries=max_retries, validate_entries=validate,
            attempt_observer=observe)
    else:
        # A provider also receives frozen_batch, because a strategy that
        # serialises WITHOUT a second model has to build {n, head, speaker}
        # itself. It still returns through the same validator below.
        named = entries_provider(
            client, model_name, sys_prompt, user_prompt, call_params,
            log_name="llm_responses.log", label="ATTRIBUTE",
            max_retries=max_retries, validate_entries=validate,
            attempt_observer=observe, frozen_batch=frozen_batch,
            # what a provider that rebuilds the prompt its own way needs: the
            # roster and contexts the canonical prompt was built from, and the
            # surrounding text when the user asked for it
            roster=roster, neighbor_contexts=neighbor_contexts, surround=surround,
            # established [gender, age] per speaker, only with per-line traits on
            **({"roster_traits": roster_traits} if roster_traits else {}))
    if named:
        # The model returned only {n, head, speaker} (never full text, so it can't
        # corrupt it). Bind by the validated index order and keep the frozen text
        # byte-exact; take only the assigned speaker.
        ordered = validated.get("ordered")
        if ordered is None:
            raise RuntimeError("validated attribution response lost its index binding")
        return get_named_from_answer(frozen_batch, ordered, cast, speaker_traits)
    ranges = get_exhausted_runtime_batch_ranges(client, frozen_batch, attempts)
    if ranges:
        combined = []
        for start, end in ranges:
            combined.extend(attribute_batch(
                client, model_name, frozen_batch[start:end], params, roster,
                max_retries=max_retries, on_exhaustion=on_exhaustion,
                neighbor_contexts=(neighbor_contexts[start:end] if neighbor_contexts else None),
                attempt_observer=attempt_observer, source_text=source_text,
                exhaustion_sink=exhaustion_sink, entries_provider=entries_provider,
                surround=surround, cast=cast, keep_scope=keep_scope,
                speaker_traits=speaker_traits, roster_traits=roster_traits))
        return combined
    if exhaustion_sink is not None:
        exhaustion_sink.append(True)
    if on_exhaustion == "keep" and (
            len(frozen_batch) == 1
            or (keep_scope == "batch" and is_keepable_exhaustion(
                frozen_batch, validated.get("last"), validated.get("last_report")))):
        return keep_exhausted_answer(frozen_batch, validated.get("last"),
                                     validated.get("last_report"), roster, cast, speaker_traits)
    if on_exhaustion in ("fail", "keep"):
        raise PassExhausted(f"attribution failed for a {len(frozen_batch)}-entry batch",
                            last_entries=validated.get("last"))
    seeded = [{**{k: v for k, v in e.items() if k != "type"},
               "speaker": "NARRATOR" if e["type"] == "NARRATOR" else "UNKNOWN"}
              for e in frozen_batch]
    return stabilize_speaker_identities(seeded, established_speakers=roster)["entries"]


def build_instruct_request(prior_batch, params, neighbor_contexts=None):
    """Build the canonical pass-3 system and user prompts."""
    sys_prompt, usr_template = load_instruct_prompts()
    if params.instruct_system_prompt:
        sys_prompt = params.instruct_system_prompt
    elif params.system_prompt:
        sys_prompt = params.system_prompt
    if params.instruct_user_prompt_template:
        usr_template = params.instruct_user_prompt_template
    elif params.user_prompt_template:
        usr_template = params.user_prompt_template
    neighbor_contexts = neighbor_contexts or [{} for _ in prior_batch]
    batch_json = json.dumps([
        {"n": i, "speaker": e["speaker"], "text": e["text"], **neighbor_contexts[i]}
        for i, e in enumerate(prior_batch)], ensure_ascii=False)
    return sys_prompt, usr_template.format(batch=batch_json)


def instruct_batch(client, model_name, prior_batch, params, max_retries=3,
                   neighbor_contexts=None, exhaustion_sink=None,
                   attempt_observer=None):
    """Add instruct to one batch of {speaker,text} entries. Enforces the freeze
    on text+speaker. On exhaustion, attaches a default instruct per entry so
    pass 3 never fails the book."""
    params = ensure_run_request_params(client, params)
    sys_prompt, user_prompt = build_instruct_request(
        prior_batch, params, neighbor_contexts)
    validated = {}

    def validate(entries):
        report = validate_instruct(prior_batch, entries)
        if report["passed"]:
            validated["ordered"] = index_head_check(prior_batch, entries)[2]
        return report

    call_params = replace(params, temperature=(params.instruct_temperature
                                               if params.instruct_temperature is not None
                                               else params.temperature))
    attempts = []

    def observe(attempt):
        attempts.append(attempt)
        if attempt_observer:
            attempt_observer(attempt)

    annotated = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, call_params,
        log_name="llm_responses.log", label="INSTRUCT", max_retries=max_retries,
        validate_entries=validate, attempt_observer=observe)
    if annotated:
        # The model returned only {n, head, instruct}. Keep speaker+text byte-exact
        # from prior (bound by validated index order); take only the instruct.
        ordered = validated.get("ordered")
        if ordered is None:
            raise RuntimeError("validated instruct response lost its index binding")
        return [{**{key: value for key, value in p.items()
                    if key != "instruct_unchecked"}, "instruct": item.get("instruct")}
                for p, item in zip(prior_batch, ordered)]
    ranges = get_exhausted_runtime_batch_ranges(client, prior_batch, attempts)
    if ranges:
        combined = []
        for start, end in ranges:
            combined.extend(instruct_batch(
                client, model_name, prior_batch[start:end], params,
                max_retries=max_retries,
                neighbor_contexts=(neighbor_contexts[start:end] if neighbor_contexts else None),
                exhaustion_sink=exhaustion_sink, attempt_observer=attempt_observer))
        return combined
    if exhaustion_sink is not None:
        exhaustion_sink.append(True)
    return [{**e, "instruct": default_instruct(e), "instruct_unchecked": True}
            for e in prior_batch]


def get_delivery_review_info(entries):
    """Return 1-based locations of actual pass-3 fallback instructions."""
    indices = [index + 1 for index, entry in enumerate(entries)
               if isinstance(entry, dict) and entry.get("instruct_unchecked") is True]
    return {"count": len(indices), "entries": indices}


def retry_delivery_instructions(client, model_name, entries, params,
                                cancel_check=None, attempt_observer=None):
    """Retry only marked delivery entries, retaining frozen text and context.

    Returns new rows; publication and its book/snapshot guard belong to the
    caller. The original generation passes and checkpoint are not invoked.
    """
    result = [dict(entry) for entry in entries]
    pending = [entry if entry.get("instruct_unchecked") is True else None
               for entry in result]
    for indexed_batch in iter_unique_entry_batches(pending):
        work = [indexed_batch]
        while work:
            if cancel_check:
                cancel_check()
            current = work.pop(0)
            batch = [entry for _, entry in current]
            contexts = [{"previous_context": entries[index - 1] if index else None,
                         "next_context": entries[index + 1]
                         if index + 1 < len(entries) else None}
                        for index, _ in current]
            request_params = ensure_run_request_params(client, params)
            if (len(current) > 1 and not does_instruct_batch_fit_context(
                    batch, request_params, contexts)):
                midpoint = len(current) // 2
                work[0:0] = [current[:midpoint], current[midpoint:]]
                continue
            attempts = []

            def observe(attempt):
                attempts.append(attempt)
                if attempt_observer:
                    attempt_observer(attempt)

            exhausted = []
            updated = instruct_batch(
                client, model_name, batch, request_params,
                neighbor_contexts=contexts, exhaustion_sink=exhausted,
                attempt_observer=observe)
            if cancel_check:
                cancel_check()
            if (exhausted and attempts and all(
                    attempt.get("outcome") == "api_error" for attempt in attempts)):
                raise RuntimeError("instruct LLM unavailable; refusing fallback output")
            for (index, _), entry in zip(current, updated):
                result[index] = entry
    return result


def does_instruct_batch_fit_context(prior_batch, params, neighbor_contexts=None):
    """Return whether an instruction request has room for a plausible response."""
    sys_prompt, user_prompt = build_instruct_request(
        prior_batch, params, neighbor_contexts)
    messages = [{"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}]
    try:
        available = get_effective_max_tokens(
            params.max_tokens, params.context_length, messages,
            params.hard_max_tokens, scale_to_context=False)
    except TokenBudgetError:
        return False
    return available >= max(256, 48 * len(prior_batch))


_WS_ENTRY_GAP = re.compile(r'[\s"“”「」『』]*')


def drop_whitespace_entries(entries, chunk):
    """-> (entries without formatting-only units, the drop records).

    A model shown one paragraph per line sometimes returns the line break
    between two quotations as its own unit - {"type": "NARRATOR", "text":
    "\n"} (#628). That is not dropped content, and finding #7 (an empty unit
    must reach the gate, because it may be a lost line) still holds: a unit is
    dropped ONLY when the source text between its neighbours is itself nothing
    but whitespace and quote marks, checked against the chunk. Any unit whose
    neighbours cannot be located, or with real text between them, is kept for
    the gate's empty_text finding. Pure: returns new lists."""
    kept, dropped, cursor = [], [], 0
    texts = [str(e.get("text") or "") if isinstance(e, dict) else "" for e in entries]
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or texts[i].strip():
            kept.append(entry)
            if isinstance(entry, dict) and texts[i].strip():
                found = chunk.find(texts[i].strip(), cursor)
                if found >= 0:
                    cursor = found + len(texts[i].strip())
            continue
        prev_text = texts[i - 1].strip() if i > 0 else ""
        next_text = next((t.strip() for t in texts[i + 1:] if t.strip()), "")
        prev_end = cursor if prev_text else 0
        next_start = chunk.find(next_text, prev_end) if next_text else len(chunk)
        gap = chunk[prev_end:next_start] if next_start >= 0 else None
        if gap is not None and _WS_ENTRY_GAP.fullmatch(gap):
            dropped.append({"entry_number": i + 1, "code": "dropped_whitespace_entry"})
            continue
        kept.append(entry)
    return kept, dropped


def apply_segment_gate_controls(system_prompt, params):
    """Append the run's quote-policy override to the active pass-1 prompt."""
    if params.quoted_must_be_spoken and params.unquoted_must_be_narrator:
        return system_prompt
    rules = [
        ("Quoted text MUST be SPOKEN."
         if params.quoted_must_be_spoken else
         "Quoted text is not required to be SPOKEN; decide from its meaning."),
        ("Unquoted text MUST be NARRATOR."
         if params.unquoted_must_be_narrator else
         "Unquoted text is not required to be NARRATOR; decide from its meaning."),
    ]
    return (system_prompt.rstrip()
            + "\n\nRUN-SPECIFIC FIDELITY GATE CONTROLS (override any conflicting "
              "quote-classification rule above):\n- " + "\n- ".join(rules))


def validate_segment_for_params(source_text, entries, params, quote_analysis=None):
    """Run pass 1's fidelity gate with the current quote controls."""
    return validate_segment_quality(
        source_text, entries, quote_analysis=quote_analysis,
        quoted_must_be_spoken=params.quoted_must_be_spoken,
        unquoted_must_be_narrator=params.unquoted_must_be_narrator)


def _call_segment(client, model_name, chunk, sys_prompt, user_prompt, params,
                  label, max_retries, near_miss_sink, validate=None,
                  attempt_observer=None, retry_decider=None):
    """Shared body for every pass-1 segment call (plain and context-rescue):
    the segment repair transform, the segment fidelity gate (optionally wrapped),
    and trigram-only near-miss capture. Callers build sys_prompt/user_prompt so
    the two paths can't diverge in how they invoke the gate (findings #10, #11)."""
    if validate is None:
        validate = lambda entries: validate_segment_for_params(chunk, entries, params)
    # Segmentation only adds small JSON/type overhead around source text. Bound
    # both the first request and retry ceiling so a weak model cannot spend
    # 10k-16k tokens expanding a ~1k-token source chunk.
    source_words = max(1, len(chunk.split()))
    completion_ceiling = resolve_completion_ceiling(
        source_words, params, reasoning_allowance=params.reasoning_allowance)
    bounded_params = replace(
        params, max_tokens=min(params.max_tokens, completion_ceiling),
        hard_max_tokens=min(params.hard_max_tokens, completion_ceiling),
        temperature=(params.segment_temperature
                     if params.segment_temperature is not None else params.temperature))
    def repair(entries):
        repaired = build_deterministic_repair(
            entries, chunk, merge_empty_into_pause=False)
        without_ws, ws_dropped = drop_whitespace_entries(repaired["entries"], chunk)
        if ws_dropped:
            repaired["entries"] = without_ws
            repaired.setdefault("changes", []).extend(ws_dropped)
        quote_split = []
        for number, entry in enumerate(repaired["entries"], 1):
            text = str(entry.get("text") or "").strip()
            if entry.get("type") != "SPOKEN" and params.quoted_must_be_spoken:
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
        attempt_observer=attempt_observer,
        retry_decider=retry_decider,
        near_miss_sink=near_miss_sink)


def segment_chunk(client, model_name, chunk, params, max_retries=4,
                  near_miss_sink=None, failure_sink=None, attempt_sink=None):
    """Pass 1 single attempt-budget over one chunk -> [{type,text}], via the
    segment fidelity gate. Captures a trigram-only near-miss into near_miss_sink
    (same mechanism call_llm_for_entries uses for single-pass). Returns [] on
    exhaustion."""
    sys_prompt, usr_template = load_segment_prompts()
    if params.segment_system_prompt:
        sys_prompt = params.segment_system_prompt
    elif params.system_prompt:
        sys_prompt = params.system_prompt
    if params.segment_user_prompt_template:
        usr_template = params.segment_user_prompt_template
    elif params.user_prompt_template:
        usr_template = params.user_prompt_template
    sys_prompt = apply_segment_gate_controls(sys_prompt, params)
    user_prompt = usr_template.format(chunk=chunk)
    quote_analysis = (classify_lexical_quote_regions(
        chunk, analyze_outer_quote_regions(chunk))
        if params.segmentation == "lexical" else None)
    attempts = []
    def observe(attempt):
        attempts.append(attempt)
        if attempt_sink is not None:
            attempt_sink.append(attempt)
    def decide(quality, repeat_evidence):
        codes = {finding.get("code") for finding in quality.get("findings", [])}
        splittable = {"low_source_token_recall", "low_ordered_trigram_recall",
                      "output_source_ratio", "mixed_quote_region",
                      "quote_region_misclassified", "crosses_quote_boundary"}
        return "split" if repeat_evidence >= 2 and codes & splittable else "retry"
    entries = _call_segment(
        client, model_name, chunk, sys_prompt, user_prompt, params, "SEGMENT",
        max_retries, near_miss_sink, attempt_observer=observe,
        retry_decider=decide,
        validate=lambda candidate: validate_segment_for_params(
            chunk, candidate, params, quote_analysis=quote_analysis))
    if not entries and failure_sink is not None and attempts:
        failure_sink[:] = [set(attempts[-1].get("failure_codes") or [])]
    return entries


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


# Stored values and the names users see (RECIPES.md, "Pass 1"; the Setup dropdown uses the right-hand side):
#   auto    -> "Quote marks first, model for the rest (recommended)"
#   quotes  -> "Quote marks only (no Step 1 model calls)"
#   lexical -> "Quote-aware (quoted terms as narration)"
#   llm     -> "Model only"
# Do not rename a stored value: config.json, model profiles and old results contain them.
SEGMENTATION_MODES = ("auto", "quotes", "lexical", "llm")
LEXICAL_QUOTE_CLASSIFIER_VERSION = 2


def quote_regions_decision(mode, chunk, analysis, quoted_must_be_spoken=True,
                           unquoted_must_be_narrator=True):
    """-> (entries, resolution) when the chunk's quote marks settle pass 1
    without the model, else (None, None).

    "auto": only when the marks split the chunk into more than one region AND
    the segment gate passes on them - the rule every three-pass result was
    measured with; otherwise the model decides. If either quote gate control is
    disabled, auto also asks the model so that the control is not bypassed by
    deterministic pre-segmentation. "quotes" (issue #588): the
    model is never asked. A chunk auto would accept is accepted the same way;
    any other chunk is what its marks say - the regions if there are any, else
    the whole chunk as one entry typed by whether a quote was open when the
    chunk began - and is recorded as `quote_forced` so the manifest says the
    gate did not vouch for it. "llm": never."""
    if mode == "llm" or (mode == "auto" and not (
            quoted_must_be_spoken and unquoted_must_be_narrator)):
        return None, None
    if mode == "lexical":
        analysis = classify_lexical_quote_regions(chunk, analysis)
    regions = analysis["regions"]
    if len(regions) > 1:
        quality = validate_segment_quality(
            chunk, regions, quote_analysis=analysis,
            quoted_must_be_spoken=quoted_must_be_spoken,
            unquoted_must_be_narrator=unquoted_must_be_narrator)
        if quality["passed"]:
            # Outer quotes already answer the only pass-1 question: inside is
            # spoken, outside is narration. Do not ask the model to rewrite
            # tiny attribution regions; live testing showed that invites
            # hallucinated expansion despite perfect source coverage.
            return regions, ("quote_presegmented_repaired" if analysis["repairs"]
                             else "quote_presegmented_continuation"
                             if (analysis.get("initial_depth") or analysis.get("final_depth"))
                             else "quote_presegmented")
        if mode == "quotes":
            print("  quote marks only: segment gate not met, keeping the quote regions "
                  f"unverified ({sorted({f.get('code') for f in quality['findings']})})")
    if mode != "quotes":
        return None, None
    if regions:
        return regions, "quote_forced"
    return [{"type": "SPOKEN" if analysis.get("initial_depth") else "NARRATOR",
             "text": chunk.strip()}], "quote_forced"


# Progress lines the Script tab's activity row and core._compute_eta both
# read (#588: a slow reply looked like an idle run). "announce" says what is
# about to be waited on, in prose so the ETA parser ignores it; "finish"
# carries the current/total marker the ETA parser counts as done.
STEP_NAMES = {1: "split", 2: "speakers", 3: "delivery"}


def announce_step(step, unit, number, total):
    print(f"Step {step} ({STEP_NAMES[step]}): {unit} {number} of {total} - asking the model",
          flush=True)


def finish_step(step, unit, number, total, note):
    print(f"Step {step} ({STEP_NAMES[step]}): {unit} {number}/{total} done - {note}", flush=True)


def planned_calls_from_preflight(report):
    """{step: predicted model calls} from build_three_pass_request_preflight's
    request list - one entry per predicted call, context-rescue retries not
    counted (they are the exception, not the plan)."""
    counts = {1: 0, 2: 0, 3: 0}
    for request in report["requests"]:
        step = {"segment": 1, "attribute": 2, "instruct": 3}.get(request["stage"])
        if step:
            counts[step] += 1
    return counts


def plan_line(planned):
    return (f"Plan: Step 1 ~ {planned[1]} model calls, Step 2 ~ {planned[2]} windows, "
            f"Step 3 ~ {planned[3]} windows")


def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


class RunProgress:
    """Where the run is in model calls, and the ETA it prints after every
    finished unit. One estimate for the whole job (Rule 15): the pipeline is
    the only thing that knows the plan, the actual counts as each pass
    starts, and when the first call went out. Average seconds per call so
    far, over the calls left - an estimate, and the line says "about".
    Restored work counts toward completion, but only newly completed work is
    paired with this run's elapsed interval to estimate its rate.

    `planned` = {step: predicted calls} from the preflight; each step's actual
    total replaces its estimate when known (set_total). Quote-mark chunks are
    not calls. The machine-readable tail is what core._compute_eta reads."""

    def __init__(self, planned):
        self.planned = dict(planned)
        self.totals = {}
        self.done = {1: 0, 2: 0, 3: 0}
        self.restored_done = {1: 0, 2: 0, 3: 0}
        self.started = None

    def set_total(self, step, total):
        self.totals[step] = total

    def restore_done(self, step, count):
        self.done[step] = count
        self.restored_done[step] = count

    def note_call_started(self):
        if self.started is None:
            self.started = time.time()

    def note_done(self, step):
        self.done[step] += 1

    def total_calls(self):
        return sum(self.totals.get(step, self.planned.get(step, 0)) for step in (1, 2, 3))

    def eta_line(self, step, now=None):
        done = sum(self.done.values())
        total = max(self.total_calls(), done)
        fraction = done / total if total else 1.0
        elapsed = ((now if now is not None else time.time()) - self.started
                   if self.started is not None else 0.0)
        measured_done = done - sum(self.restored_done.values())
        remaining = (elapsed / measured_done) * (total - done) if measured_done else 0.0
        return (f"ETA: about {format_duration(remaining)} left "
                f"(Step {step} of 3, {done} of {total} model calls done) "
                f"[eta_seconds={int(round(remaining))} fraction={fraction:.3f}]")


def segment_chunk_adaptively(client, model_name, chunk, params,
                             resolution_sink=None, failure_sink=None,
                             attempt_sink=None, quote_analysis=None,
                             quote_decision=None):
    """Pass 1 with the full safety net: full-chunk attempt, then a
    natural-boundary split whose halves each recurse, and exhaustion-only
    trigram-only near-miss acceptance. Mirrors process_chunk_adaptively but for
    the segment gate. Returns [{type,text}] or [] (book failure). When
    resolution_sink is given, appends exactly one resolution string describing
    how the chunk was handled (clean / adaptive_split / recombination_near_miss /
    near_miss / fail). Only the top-level call should pass a sink; recursive
    part-calls do not, so inner resolutions don't pollute the record."""
    if params.segmentation != "llm":
        regions, resolution = (quote_decision if quote_decision is not None else
            quote_regions_decision(
                params.segmentation, chunk,
                quote_analysis or analyze_outer_quote_regions(chunk),
                params.quoted_must_be_spoken, params.unquoted_must_be_narrator))
        if regions is not None:
            _record_resolution(resolution_sink, resolution)
            return regions
    near_miss = []
    local_failures = []
    entries = segment_chunk(client, model_name, chunk, params,
                            near_miss_sink=near_miss, failure_sink=local_failures,
                            attempt_sink=attempt_sink)
    if entries:
        _record_resolution(resolution_sink, "clean")
        return entries
    parts = split_failed_chunk(chunk)
    if not parts:
        if failure_sink is not None:
            failure_sink[:] = local_failures
        return _resolved_near_miss(near_miss, resolution_sink)
    print(f"  Adaptive split (segment): -> {len(parts[0])} + {len(parts[1])} chars")
    combined, any_failed = [], False
    for part in parts:
        part_entries = segment_chunk_adaptively(
            client, model_name, part, params, attempt_sink=attempt_sink)
        if not part_entries:
            any_failed = True
            continue
        combined.extend(part_entries)
    if any_failed:
        if failure_sink is not None:
            failure_sink[:] = local_failures
        return _resolved_near_miss(near_miss, resolution_sink)
    combined_analysis = (classify_lexical_quote_regions(
        chunk, quote_analysis or analyze_outer_quote_regions(chunk))
        if params.segmentation == "lexical" else None)
    combined_quality = validate_segment_for_params(
        chunk, combined, params, quote_analysis=combined_analysis)
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


def should_rescue_with_context(failure_codes):
    """Context is not a remedy for omission, truncation, or quote structure."""
    return bool(set(failure_codes or ()) & {"context_required"})


def select_preflight_chunks(source_text, chunk_size):
    """Select distinct first, middle, and dialogue-dense real-book chunks."""
    chunks = split_into_chunks(source_text, max_size=chunk_size)
    if not chunks:
        return []
    selected = [("first", 0)]
    middle = len(chunks) // 2
    if middle:
        selected.append(("middle", middle))
    # Endnote/reference sections often contain more quoted terms than the story
    # itself. They are useful source material, but are not a representative
    # dialogue qualification sample.
    prose_candidates = [i for i, chunk in enumerate(chunks)
                        if chunk.count("←") <= 2]
    dialogue_pool = prose_candidates or list(range(len(chunks)))
    dialogue = max(dialogue_pool, key=lambda i: sum(
        chunks[i].count(mark) for mark in ('"', '“', '”')))
    if dialogue not in {index for _, index in selected}:
        selected.append(("dialogue", dialogue))
    return [(label, index, chunks[index]) for label, index in selected]


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


def get_context_rescue_windows(windows=None):
    """Return positive source-context widths; only None selects defaults."""
    if windows is None:
        return _CONTEXT_RESCUE_WINDOWS
    if (not isinstance(windows, (list, tuple)) or not windows
            or any(isinstance(window, bool) or not isinstance(window, int)
                   or window < 1 for window in windows)):
        raise ValueError("context rescue windows must be a nonempty list of positive integers")
    return tuple(windows)


def validate_attribution_vote_settings(votes, temperature):
    """Reject invalid sampling controls before planning or dispatching calls."""
    if isinstance(votes, bool) or not isinstance(votes, int) or votes < 1:
        raise ValueError("attribution votes must be a positive integer")
    if (isinstance(temperature, bool) or not isinstance(temperature, (int, float))
            or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("vote temperature must be a finite number between 0 and 2")


def get_three_pass_planning_entries(source_text, settings, params):
    """Predict entries once using the execution quote-boundary policy."""
    chunk_size = settings["chunk_size"]
    records = split_into_chunk_records(source_text, max_size=chunk_size)
    chunks = [record["text"] for record in records]
    predicted_entries = []
    unresolved_chunks = []
    quote_depth = 0
    chunks_without_quote_marks = 0
    for index, chunk in enumerate(chunks):
        analysis = analyze_outer_quote_regions(
            chunk, initial_depth=quote_depth,
            allow_open_end=index < len(chunks) - 1)
        quote_depth = analysis["final_depth"]
        chunks_without_quote_marks += not any(mark in chunk for mark in QUOTE_MARKS)
        regions, _ = quote_regions_decision(
            settings["segmentation"], chunk, analysis,
            params.quoted_must_be_spoken, params.unquoted_must_be_narrator)
        if regions is not None:
            predicted_entries.extend(regions)
        else:
            # Unknown pass-1 output: SPOKEN exercises both later LLM passes and
            # is more conservative than assuming deterministic narration.
            predicted_entries.append({"type": "SPOKEN", "text": chunk})
            unresolved_chunks.append(chunk)

    return chunks, predicted_entries, unresolved_chunks, chunks_without_quote_marks


def get_three_pass_planned_calls(source_text, settings, params):
    """Count predicted calls without formatting request prompts or token reports."""
    validate_attribution_vote_settings(settings.get("attribution_votes", 1),
                                       settings.get("vote_temperature", 0.3))
    _, predicted, unresolved, _ = get_three_pass_planning_entries(source_text, settings, params)
    batch_size = int(settings.get("attribute_batch_size") or BATCH_SIZE)
    attribute_calls = sum(
        any(entry.get("type") == "SPOKEN" for _, entry in batch)
        for batch in iter_unique_entry_batches(predicted, batch_size))
    named = [{"speaker": ("UNKNOWN" if entry.get("type") == "SPOKEN" else "NARRATOR"),
              "text": entry["text"]} for entry in predicted]
    return {1: len(unresolved),
            2: attribute_calls * settings.get("attribution_votes", 1),
            3: sum(1 for _ in iter_unique_entry_batches(named))}


def build_three_pass_request_preflight(source_text, settings, context_length,
                                       parallel, context_windows=None,
                                       reserve=512, params=None):
    """Estimate the real three-pass prompt shapes for context-slot planning."""
    context_windows = get_context_rescue_windows(context_windows)
    validate_attribution_vote_settings(settings.get("attribution_votes", 1),
                                       settings.get("vote_temperature", 0.3))
    chunk_size = settings["chunk_size"]
    params = params or LLMGenParams(
        max_tokens=settings["max_tokens"], context_length=context_length,
        segment_output_ratio=settings["segment_output_ratio"],
        segmentation=settings["segmentation"],
        quoted_must_be_spoken=settings.get("quoted_must_be_spoken", True),
        unquoted_must_be_narrator=settings.get("unquoted_must_be_narrator", True),
        segment_system_prompt=settings.get("segment_system_prompt"),
        segment_user_prompt_template=settings.get("segment_user_prompt_template"),
        instruct_system_prompt=settings.get("instruct_system_prompt"),
        instruct_user_prompt_template=settings.get("instruct_user_prompt_template"))
    chunks, predicted_entries, unresolved_chunks, chunks_without_quote_marks = (
        get_three_pass_planning_entries(source_text, settings, params))

    requests = []

    def add_request(stage, system_prompt, user_prompt, completion_tokens):
        prompt_tokens = math.ceil((len(system_prompt) + len(user_prompt)) / 3)
        total = prompt_tokens + int(completion_tokens) + reserve
        requests.append({"stage": stage, "prompt_tokens": prompt_tokens,
                         "predicted_completion_tokens": int(completion_tokens),
                         "predicted_total_tokens": total})

    segment_system, segment_template = load_segment_prompts()
    if params.segment_system_prompt:
        segment_system = params.segment_system_prompt
    segment_system = apply_segment_gate_controls(segment_system, params)
    if params.segment_user_prompt_template:
        segment_template = params.segment_user_prompt_template
    # Pass 1 re-emits the chunk verbatim, so its output grows with the chunk.
    # The run's escalation ceiling is the most it can ever ask for; a chunk
    # whose predicted output exceeds it cannot succeed at any retry, and the
    # caller should refuse the run rather than let it fail chunk by chunk.
    output_ceiling = resolve_hard_max_tokens(int(settings["max_tokens"]))
    largest_completion = 0
    for chunk in chunks:
        largest_completion = max(largest_completion, resolve_completion_ceiling(
            max(1, len(chunk.split())), params))
    for chunk in unresolved_chunks:
        completion = min(
            int(settings["max_tokens"]),
            resolve_completion_ceiling(
                max(1, len(chunk.split())), params))
        add_request("segment", segment_system,
                    segment_template.format(chunk=chunk), completion)
        windows = context_windows
        if windows:
            window = max(windows)
            rescue_user = _CONTEXT_SEGMENT_USER.format(
                before="x" * window, after="x" * window, chunk=chunk)
            add_request("segment_context_rescue", segment_system,
                        rescue_user, completion)

    roster_chars = min(4096, 32 * sum(
        entry.get("type") == "SPOKEN" for entry in predicted_entries))
    estimated_roster = ["R" * roster_chars] if roster_chars else []
    attribute_batch_size = int(settings.get("attribute_batch_size") or BATCH_SIZE)
    context_chars = int(settings.get("attribute_context_chars") or 0)
    for indexed_batch in iter_unique_entry_batches(predicted_entries, attribute_batch_size):
        pending = [(index, entry) for index, entry in indexed_batch
                   if entry.get("type") == "SPOKEN"]
        if not pending:
            continue
        batch = [entry for _, entry in pending]
        contexts = get_missing_attribute_contexts(
            predicted_entries, [index for index, _ in pending], attribute_batch_size)
        surround = {"before": "x" * context_chars, "after": "x" * context_chars}
        variant = settings.get("attribute_prompt_variant") or "default"
        texts = settings.get("attribute_prompt_texts")
        if variant != "default" or texts:
            from attribution_prompt_variants import build_variant_request
            system_prompt, user_prompt = build_variant_request(
                variant, batch, params, estimated_roster,
                alias_groups=(settings.get("cast") or {}).get("alias_groups"),
                neighbor_contexts=contexts, surround=surround, texts=texts)
        else:
            system_prompt, user_prompt = build_attribute_request(
                batch, params, estimated_roster, contexts, surround)
        for _ in range(settings.get("attribution_votes", 1)):
            add_request("attribute", system_prompt, user_prompt,
                        max(256, 24 * len(batch)))

    named_entries = [{"speaker": ("UNKNOWN" if entry.get("type") == "SPOKEN"
                                   else "NARRATOR"),
                      "text": entry["text"]}
                     for entry in predicted_entries]
    for indexed_batch in iter_unique_entry_batches(named_entries):
        batch = [entry for _, entry in indexed_batch]
        contexts = [{
            "previous_context": named_entries[index - 1] if index else None,
            "next_context": (named_entries[index + 1]
                             if index + 1 < len(named_entries) else None),
        } for index, _ in indexed_batch]
        system_prompt, user_prompt = build_instruct_request(
            batch, params, contexts)
        add_request("instruct", system_prompt, user_prompt,
                    max(256, 48 * len(batch)))

    totals = sorted(request["predicted_total_tokens"] for request in requests)
    worst = totals[-1] if totals else 0
    p95 = totals[max(0, math.ceil(len(totals) * 0.95) - 1)] if totals else 0
    return get_three_pass_preflight_capacity({
        "chunk_count": len(chunks),
        # what pass 1 will cost in model calls, and whether this book marks
        # its dialogue at all (the quotes-only refusal reads these)
        "segmentation": {
            "mode": settings["segmentation"], "chunks": len(chunks),
            "quote_presegmented": len(chunks) - len(unresolved_chunks),
            "llm_chunks": len(unresolved_chunks),
            "chunks_without_quote_marks": chunks_without_quote_marks},
        "worst_predicted_tokens": worst, "p95_predicted_tokens": p95,
        "average_predicted_tokens": (
            round(sum(totals) / len(totals), 1) if totals else 0),
        "output_ceiling": output_ceiling,
        "largest_predicted_completion": largest_completion,
        "exceeds_output_ceiling": largest_completion > output_ceiling,
        # the largest chunk size whose predicted output would fit, for the
        # refusal message; None when the current one already fits
        "suggested_chunk_size": (
            max(500, int(chunk_size * output_ceiling / largest_completion) // 100 * 100)
            if largest_completion > output_ceiling else None),
        "requests": requests,
    }, context_length, parallel)


def get_three_pass_preflight_capacity(report, context_length, parallel):
    """Return a sizing report with the current context-slot capacity."""
    per_slot = int(context_length or 0) // max(1, int(parallel or 1))
    return {**report, "context_length": context_length, "parallel": parallel,
            "per_slot_context": per_slot,
            "predicted_fits": bool(per_slot and report["worst_predicted_tokens"] <= per_slot)}


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
                               max_retries=2, near_miss_sink=None,
                               quote_analysis=None, attempt_observer=None):
    """Last-resort pass-1 retry: give the model surrounding SOURCE text (before /
    after the failing chunk, reference-only) for narrative flow, but validate that
    the output still covers ONLY the target chunk. Captures a trigram-only
    near-miss into near_miss_sink like the normal segment path. Returns
    [{type,text}] or []."""
    sys_prompt, _ = load_segment_prompts()
    if params.segment_system_prompt:
        sys_prompt = params.segment_system_prompt
    elif params.system_prompt:
        sys_prompt = params.system_prompt
    sys_prompt = apply_segment_gate_controls(sys_prompt, params)
    user_prompt = _CONTEXT_SEGMENT_USER.format(before=before or "(start of book)",
                                               after=after or "(end of book)",
                                               chunk=chunk)

    def validate(entries):
        # Fidelity gate PLUS a context-bleed guard: a target-correct output that
        # also pastes a reference-context sentence can otherwise pass recall /
        # trigram / ratio (the leaked sentence adds output but doesn't drop source
        # recall), so reject clear context-only entries as a validation failure.
        lexical_analysis = (classify_lexical_quote_regions(
            chunk, quote_analysis if quote_analysis is not None
            else analyze_outer_quote_regions(chunk))
            if params.segmentation == "lexical" else None)
        report = validate_segment_for_params(
            chunk, entries, params, quote_analysis=lexical_analysis)
        if _output_has_context_bleed(entries, chunk, before, after):
            report = dict(report)
            report["passed"] = False
            report["findings"] = list(report["findings"]) + [{
                "code": "context_bleed",
                "message": "An entry reproduced reference-context text absent from the target chunk."}]
        return report

    return _call_segment(client, model_name, chunk, sys_prompt, user_prompt,
                         params, "SEGMENT+CTX", max_retries, near_miss_sink,
                         validate=validate, attempt_observer=attempt_observer)


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
                              resolution_sink=None, windows=None, max_retries=None,
                              quote_analysis=None, attempt_observer=None):
    """When chunk `index` fails normal segmentation, retry it with escalating
    surrounding-source context. Accepts a clean pass, else the best trigram-only
    near-miss any window produced. Returns entries or []. When resolution_sink is
    given, appends the resolution (context_rescue:<window> /
    context_rescue_near_miss / fail). Windows whose prompt would exceed the
    model's context budget are skipped (finding #4). `windows` and `max_retries`
    default to the module constants when None (finding #12: config-tunable)."""
    windows = get_context_rescue_windows(windows)
    if max_retries is None:
        max_retries = _CONTEXT_RESCUE_MAX_RETRIES
    max_window = max(windows)
    before_all = _tail_join(chunks[:index], max_window)
    after_all = _head_join(chunks[index + 1:], max_window)
    sys_prompt, _ = load_segment_prompts()
    if params.segment_system_prompt:
        sys_prompt = params.segment_system_prompt
    elif params.system_prompt:
        sys_prompt = params.system_prompt
    sys_prompt = apply_segment_gate_controls(sys_prompt, params)
    overhead_chars = len(sys_prompt) + len(_CONTEXT_SEGMENT_USER)
    best_near_miss = []  # holds the single best [(entries, quality)] seen so far
    for window in windows:
        before, after = before_all[-window:], after_all[:window]
        request_params = ensure_run_request_params(client, params)
        if not _rescue_prompt_fits(chunks[index], before, after, overhead_chars, request_params):
            print(f"  context rescue {window}-char window skipped "
                  "(prompt would exceed context budget)")
            continue
        near_miss = []
        seg = segment_chunk_with_context(
            client, model_name, chunks[index],
            before, after, request_params, max_retries=max_retries, near_miss_sink=near_miss,
            quote_analysis=quote_analysis, attempt_observer=attempt_observer)
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



def get_preflight_sample_info(label, index, output_path, planned_calls,
                              entries=None, error=None):
    """Read the sample's native failure evidence for the preflight receipt."""
    manifest = safe_load_json(three_pass_manifest_path(output_path), {})
    info = {"label": label, "chunk_index": index,
            "planned_calls": dict(planned_calls),
            "status": "failed" if error or manifest.get("status") in {
                "failed", "incomplete"} else "complete",
            "failure_codes": manifest.get("progress", {}).get("failure_codes", {})}
    if entries is not None:
        info["entries"] = len(entries)
    if error:
        info["error"] = error
    elif info["status"] == "failed":
        info["error"] = "The sample pipeline did not complete."
    for key in ("failed_pass", "failed_chunk"):
        if key in manifest:
            info[key] = manifest[key]
    return info


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
        "quote_repairs": sum(r == "quote_presegmented_repaired"
                             for r in resolutions),
        "quote_continuations": sum(r == "quote_presegmented_continuation"
                                   for r in resolutions),
        "quote_forced": sum(r == "quote_forced" for r in resolutions),
    }


def _write_manifest(output_path, fingerprint, resolutions, passes, status,
                    failed_pass=None, failed_chunk=None, legacy_resume=False,
                    progress=None, diagnostic_failures=None, telemetry=None,
                    model_binding=None, delivery_review=None):
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
        "diagnostic_failures": diagnostic_failures or [],
        "telemetry": telemetry or {},
        "model_binding": model_binding,
        "delivery_review": delivery_review or {"count": 0, "entries": []},
    }
    if failed_pass is not None:
        manifest["failed_pass"] = failed_pass
    if failed_chunk is not None:
        manifest["failed_chunk"] = failed_chunk
    atomic_json_write(manifest, three_pass_manifest_path(output_path))


def three_pass_fingerprint(source_text, model_name, chunk_size, params=None,
                           on_exhaustion="fail", context_windows=None,
                           context_rescue_retries=None, endpoint=None,
                           collect_all_failures=False, attribute_batch_size=BATCH_SIZE,
                           attribute_context_chars=0, attribute_prompt_variant="default",
                           attribute_prompt_texts=None, cast_sha256=None,
                           attribution_votes=1, vote_temperature=0.3,
                           first_person_narrator=None, speaker_traits=False):
    digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    settings = {
        "model_name": model_name, "chunk_size": chunk_size,
        # 'keep' shares 'fail''s identity: everything a 'fail' run checkpointed
        # passed every check, so it resumes under 'keep' unchanged (the app moved
        # from one to the other; a new identity would restart users' books), and
        # anything 'keep' adds is flagged attribution_unchecked in the output.
        "endpoint": endpoint,
        "on_exhaustion": "fail" if on_exhaustion == "keep" else on_exhaustion,
        "context_windows": context_windows,
        "context_rescue_retries": context_rescue_retries,
        "collect_all_failures": collect_all_failures,
        # Older checkpoints cannot prove the voting/seed/reasoning controls.
        "pipeline_version": 9,
        "attribution_votes": attribution_votes,
        "vote_temperature": vote_temperature,
        "first_person_narrator": normalize_narrator_name(first_person_narrator),
        # Only present when moved off the default, so every checkpoint written
        # before these knobs existed keeps its identity and resumes.
        **({"attribute_batch_size": attribute_batch_size}
           if attribute_batch_size != BATCH_SIZE else {}),
        **({"attribute_context_chars": attribute_context_chars}
           if attribute_context_chars else {}),
        **({"attribute_prompt_variant": attribute_prompt_variant}
           if attribute_prompt_variant not in (None, "default") else {}),
        # a preset with its own text is a different prompt; a builtin is not
        **({"attribute_prompt_texts": attribute_prompt_texts} if attribute_prompt_texts else {}),
        # a supplied cast changes pass 2's roster and its name check
        **({"cast_sha256": cast_sha256} if cast_sha256 else {}),
        "default_prompts_sha256": hashlib.sha256("\n".join(
            sum((list(load_segment_prompts()), list(load_attribute_prompts()),
                 list(load_instruct_prompts())), [])).encode("utf-8")).hexdigest(),
    }
    if params is not None:
        settings.update({name: getattr(params, name, None) for name in (
            "system_prompt", "attribute_system_prompt", "user_prompt_template",
            "max_tokens", "temperature", "seed", "reasoning_effort",
            "reasoning_allowance", "output_format", "provider_extra_body",
            "top_p", "top_k", "min_p", "presence_penalty", "banned_tokens",
            "context_length", "hard_max_tokens", "segment_temperature",
            "attribute_temperature", "instruct_temperature",
            "segment_output_ratio", "structured_output",
            "segment_system_prompt", "segment_user_prompt_template",
            "instruct_system_prompt", "instruct_user_prompt_template",
            "response_schema")})
        if not getattr(params, "quoted_must_be_spoken", True):
            settings["quoted_must_be_spoken"] = False
        if not getattr(params, "unquoted_must_be_narrator", True):
            settings["unquoted_must_be_narrator"] = False
        # The pass-1 knob used to be the bool `presegment_quotes`; keep that key
        # with the same meaning so every checkpoint written before the
        # three-way mode existed still resumes, and add the mode only in
        # "quotes", where pass 1 is a different contract.
        settings["presegment_quotes"] = getattr(params, "segmentation", "llm") != "llm"
        if getattr(params, "segmentation", None) in ("quotes", "lexical"):
            settings["segmentation"] = getattr(params, "segmentation")
        if getattr(params, "segmentation", None) == "lexical":
            settings["lexical_quote_classifier_version"] = LEXICAL_QUOTE_CLASSIFIER_VERSION
        # The attribution schema is installed inside attribute_batch rather
        # than on the caller's params object. Include it in the checkpoint
        # identity so changing the request contract cannot resume old output.
        if getattr(params, "structured_output", "auto") != "off":
            settings["response_schema"] = get_attribution_response_schema(speaker_traits)
    encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {"source_sha256": digest, "settings_sha256": hashlib.sha256(encoded).hexdigest(),
            "model_name": model_name, "pipeline": "three_pass"}


def validate_three_pass_checkpoint(data, chunk_count=None):
    """Validate persisted resume shapes and cursors before consuming state."""
    def require(condition, field):
        if not condition:
            raise ValueError(f"invalid {field}")

    def is_cursor(value, maximum=None):
        return (isinstance(value, int) and not isinstance(value, bool)
                and value >= 0 and (maximum is None or value <= maximum))

    require(isinstance(data.get("stage"), str) and data["stage"] in {
        "segment", "segment_failed", "segment_incomplete", "attribute",
        "attribute_failed", "attribute_incomplete", "attribute_unavailable", "instruct",
        "instruct_failed", "instruct_unavailable", "done"}, "stage")
    require(is_cursor(data.get("chunks_done"), chunk_count), "chunks_done")
    segmented, named, annotated = (data.get(key) for key in ("segmented", "named", "annotated"))
    require(isinstance(segmented, list), "segmented")
    for index, entry in enumerate(segmented):
        require(isinstance(entry, dict) and isinstance(entry.get("type"), str)
                and entry["type"] in {"SPOKEN", "NARRATOR"}
                and isinstance(entry.get("text"), str), f"segmented[{index}]")
    require(data["chunks_done"] > 0 or not segmented, "segmented with zero chunks_done")
    require(isinstance(named, list) and len(named) <= len(segmented), "named length")
    for index, entry in enumerate(named):
        require(entry is None or (isinstance(entry, dict)
                and isinstance(entry.get("speaker"), str)
                and entry.get("text") == segmented[index]["text"]), f"named[{index}]")
    require(isinstance(annotated, list) and len(annotated) <= len(named), "annotated length")
    for index, entry in enumerate(annotated):
        require(entry is None or (isinstance(entry, dict) and isinstance(named[index], dict)
                and isinstance(entry.get("speaker"), str) and isinstance(entry.get("instruct"), str)
                and entry.get("text") == named[index]["text"]), f"annotated[{index}]")
    resolutions = data.get("resolutions", [])
    require(isinstance(resolutions, list) and len(resolutions) <= data["chunks_done"] + 1
            and all(isinstance(value, str) for value in resolutions), "resolutions")
    elapsed = data.get("elapsed_s", {})
    require(isinstance(elapsed, dict) and all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and (isinstance(value, int) or math.isfinite(value))
        and value >= 0 for value in elapsed.values()), "elapsed_s")
    failures = data.get("diagnostic_failures", [])
    require(isinstance(failures, list), "diagnostic_failures")
    for index, failure in enumerate(failures):
        require(isinstance(failure, dict) and isinstance(failure.get("pass"), str),
                f"diagnostic_failures[{index}]")
        if failure["pass"] == "segment":
            require(is_cursor(failure.get("chunk"), data["chunks_done"])
                    and failure["chunk"] > 0, f"diagnostic_failures[{index}].chunk")
    for field in ("failed", "model_binding"):
        require(data.get(field) is None or isinstance(data[field], dict), field)
    resume = data.get("diagnostic_segment_resume")
    if resume is not None:
        require(isinstance(resume, dict), "diagnostic_segment_resume")
        require(is_cursor(resume.get("chunks_done"), data["chunks_done"])
                and is_cursor(resume.get("segmented_entries"), len(segmented)),
                "diagnostic_segment_resume cursors")
        failed_chunks = [failure["chunk"] for failure in failures if failure["pass"] == "segment"]
        require(failed_chunks and min(failed_chunks) == resume["chunks_done"] + 1,
                "diagnostic_segment_resume first failed chunk")


def _load_three_pass_checkpoint(output_path, fingerprint, chunk_count=None):
    path = three_pass_checkpoint_path(output_path)
    if not os.path.exists(path):
        return None
    try:
        data = load_generation_delta_checkpoint(path)
        if not isinstance(data, dict):
            raise ValueError("expected an object")
        if (get_run_fingerprint_identity(data.get("fingerprint"))
                != get_run_fingerprint_identity(fingerprint)):
            return None
        validate_three_pass_checkpoint(data, chunk_count)
    except (OSError, ValueError, UnicodeError) as exc:
        raise RuntimeError(f"Invalid three-pass checkpoint {path}: {exc}") from exc
    return data


def _save_three_pass_checkpoint(output_path, fingerprint, stage, segmented,
                                chunks_done, named, annotated, resolutions=None,
                                elapsed_s=None, diagnostic_failures=None,
                                failed=None, model_binding=None,
                                diagnostic_segment_resume=None, checkpoint_writer=None):
    """`failed` (only on a pass-1 fail-fast) carries what the recovery panel
    needs to show and to validate a hand-supplied segmentation: the failed
    chunk's number, its exact source text, how many chunks there are, and the
    attempt records for that chunk (issue #522 s23 / s4.4)."""
    data = {"fingerprint": fingerprint, "stage": stage,
                       "chunks_done": chunks_done, "segmented": segmented,
                       "named": named, "annotated": annotated,
                       "resolutions": resolutions or [],
                       "elapsed_s": elapsed_s or {},
                       "diagnostic_failures": diagnostic_failures or [],
                       "failed": failed or None,
                       "model_binding": model_binding,
                       "diagnostic_segment_resume": diagnostic_segment_resume}
    path = three_pass_checkpoint_path(output_path)
    if checkpoint_writer is None:
        atomic_json_write(data, path)
    else:
        if checkpoint_writer.path.absolute() != Path(path).absolute():
            raise ValueError("Checkpoint writer belongs to a different output")
        checkpoint_writer.save_checkpoint(data, compact=stage not in (
            "segment", "segment_incomplete", "attribute", "attribute_incomplete", "instruct"))


def require_nonempty_source(source_text):
    if not source_text or not source_text.strip():
        raise ValueError("prepared source contains no text to generate")


@record_judge_run()
def run_three_pass(client, model_name, source_text, params, chunk_size,
                   on_exhaustion="fail", output_path=None,
                   context_windows=None, context_rescue_retries=None, endpoint=None,
                   collect_all_failures=False, thinking_mode=None,
                   unicode_report=None, attribution_votes=1,
                   vote_temperature=0.3, first_person_narrator=None,
                   attribute_batch_size=BATCH_SIZE, attribute_context_chars=0,
                   attribute_prompt_variant="default", attribute_prompt_texts=None,
                   planned_calls=None, cast=None, keep_scope="line", speaker_traits=False):
    """Full flow. Returns the assembled [{speaker,text,instruct}] list, or raises
    RuntimeError if pass 1 exhausts a chunk. first_person_narrator optionally
    seeds that exact character into the pass-2 roster. When output_path is given, saves a
    checkpoint after each pass-1 chunk and each pass-2/3 batch and resumes from
    it; when None, runs purely in memory. context_windows / context_rescue_retries
    override the context-rescue defaults (finding #12). attribute_batch_size
    is the pass-2 window (entries per request); attribute_context_chars is how
    much of the book either side of the window is shown as evidence (0 = the
    window alone, the measured default); attribute_prompt_variant names one
    of attribution_prompt_variants.VARIANTS ("default" = the shipped prompt)
    and attribute_prompt_texts is the active preset's {"system", "user",
    "example"} (None = the variant's builtin texts). A preset with edited
    text is sent through the variant provider for every variant, default
    included, so what Setup shows is what the model gets."""
    require_nonempty_source(source_text)
    # Preserve None in checkpoint identity while validating explicit controls.
    get_context_rescue_windows(context_windows)
    validate_attribution_vote_settings(attribution_votes, vote_temperature)
    unavailable_passes = set()
    entries_provider = None
    if (attribute_prompt_variant and attribute_prompt_variant != "default") or attribute_prompt_texts:
        from attribution_prompt_variants import make_provider
        texts = dict(attribute_prompt_texts or {})
        if params.attribute_system_prompt and not texts.get("system"):
            # the narrator prior was folded into params by main(); keep it
            texts["system"] = params.attribute_system_prompt
        entries_provider = make_provider(attribute_prompt_variant or "default",
                                         alias_groups=(cast or {}).get("alias_groups"),
                                         texts=texts or None)
    narrator = normalize_narrator_name(first_person_narrator)
    chunk_records = split_into_chunk_records(source_text, max_size=chunk_size)
    chunks = [record["text"] for record in chunk_records]
    quote_analyses = []
    quote_depth = 0
    for index, record in enumerate(chunk_records):
        analysis = analyze_outer_quote_regions(
            record["text"], initial_depth=quote_depth,
            allow_open_end=index < len(chunk_records) - 1)
        quote_analyses.append(analysis)
        quote_depth = analysis["final_depth"]
    fingerprint = three_pass_fingerprint(
        source_text, model_name, chunk_size, params, on_exhaustion,
        context_windows, context_rescue_retries, endpoint, collect_all_failures,
        attribute_batch_size=attribute_batch_size,
        attribute_context_chars=attribute_context_chars,
        attribute_prompt_variant=attribute_prompt_variant,
        attribute_prompt_texts=attribute_prompt_texts,
        cast_sha256=(cast or {}).get("sha256"),
        attribution_votes=attribution_votes, vote_temperature=vote_temperature,
        first_person_narrator=narrator, speaker_traits=speaker_traits)
    initial_binding = get_run_model_binding(client, model_name)
    if initial_binding["failover_model"] is not None:
        fingerprint["model_binding"] = initial_binding
    state = _load_three_pass_checkpoint(output_path, fingerprint, len(chunks)) if output_path else None
    checkpoint_writer = (GenerationCheckpointDeltas(three_pass_checkpoint_path(output_path))
                         if output_path else None)
    resumed_binding = (state or {}).get("model_binding")
    segmented = state["segmented"] if state else []
    chunks_done = state["chunks_done"] if state else 0
    if state:
        # Say so: a run that silently picks up mid-book reads as "it skipped
        # the start" to someone who cancelled and pressed Generate again (#597).
        print(f"Resuming saved progress for this text and these settings: "
              f"{chunks_done}/{len(chunks)} chunks already split, stage '{state.get('stage')}'. "
              "Use 'Start over' to begin at chunk 1.", flush=True)
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
    diagnostic_failures = list(state.get("diagnostic_failures", [])) if state else []
    diagnostic_segment_resume = None
    if any(failure.get("pass") == "segment" for failure in diagnostic_failures):
        resume = state.get("diagnostic_segment_resume")
        if resume is None:
            print("Incomplete legacy checkpoint cannot identify an accepted segmentation prefix; "
                  "retrying segmentation from chunk 1.", flush=True)
            chunks_done, segmented = 0, []
        else:
            chunks_done = resume["chunks_done"]
            segmented = segmented[:resume["segmented_entries"]]
            print(f"Retrying diagnostic segmentation from failed chunk {chunks_done + 1}; "
                  f"retaining {chunks_done} accepted source chunks.", flush=True)
        # Repairing missing source changes downstream batch context and indices.
        named, annotated = [], []
        resolutions = resolutions[:chunks_done]
        diagnostic_failures = []
    # Latest attempt seen by call_llm_for_entries, so a failure record can say
    # why the batch failed instead of only which entry it was.
    last_attempts = {}
    attempts = []
    attempt_passes = {}

    reasoning_allowance = ReasoningAllowance()

    def record_attempt(pass_name, attempt):
        # The shared helper completes this record after notifying observers.
        # Keep its reference so final outcomes reach diagnostics and manifests.
        attempts.append(attempt)
        attempt_passes[id(attempt)] = pass_name
        last_attempts["latest"] = attempt
        # Size the next call from what this model has actually shown. Stays at
        # zero for a model that never reports reasoning_tokens, so a
        # non-reasoning model keeps exactly today's ceiling.
        reasoning_allowance.observe(
            attempt.get("reasoning_tokens"),
            truncated=attempt.get("finish_reason") == "length")
        params.reasoning_allowance = reasoning_allowance.current()

    def last_attempt_for(_index):
        return last_attempts.get("latest")
    def save(stage, failed=None):
        if output_path:
            _save_three_pass_checkpoint(output_path, fingerprint, stage,
                                        segmented, chunks_done, named, annotated,
                                        resolutions, elapsed_s, diagnostic_failures,
                                        failed=failed,
                                        model_binding=get_run_model_binding(
                                            client, model_name, resumed_binding),
                                        diagnostic_segment_resume=diagnostic_segment_resume,
                                        checkpoint_writer=checkpoint_writer)
    passes = {}

    def emit_manifest(status, failed_pass=None, failed_chunk=None):
        failure_counts = Counter(
            code for attempt in attempts
            for code in (attempt.get("failure_codes") or []))
        _write_manifest(output_path, fingerprint, resolutions, passes, status,
                        delivery_review=get_delivery_review_info(annotated),
                        model_binding=get_run_model_binding(client, model_name, resumed_binding),
                        telemetry={
                            "model_name": model_name,
                            "first_person_narrator": narrator or None,
                            "thinking_mode": thinking_mode or "default",
                            "pass2_keep_scope": keep_scope,
                            "cast": ({"sha256": cast["sha256"], "names": len(cast["names"])}
                                     if cast else None),
                            "unicode": dict(unicode_report or {}),
                            "failure_reasons": dict(Counter(
                                f.get("reason") or "unknown"
                                for f in diagnostic_failures)),
                            "truncations": sum(
                                f.get("finish_reason") == "length"
                                for f in diagnostic_failures),
                        },
                        failed_pass=failed_pass, failed_chunk=failed_chunk,
                        legacy_resume=legacy_resume, progress={
                            "source_words": len(source_text.split()),
                            "source_words_total": len(source_text.split()),
                            "source_words_completed": sum(
                                len(chunks[j].split()) for j, resolution in
                                enumerate(resolutions) if resolution != "fail"),
                            "chunks_total": len(chunks),
                            "chunks_attempted": chunks_done,
                            "chunks_completed": sum(
                                resolution != "fail" for resolution in resolutions),
                            "segmented_entries": len(segmented),
                            "attributed_entries": sum(isinstance(e, dict) for e in named),
                            "instructed_entries": sum(isinstance(e, dict) for e in annotated),
                            "llm_calls": len(attempts),
                            "repeated_responses": sum(
                                a.get("response_repeat_count", 0) > 1 for a in attempts),
                            "completion_tokens": sum(
                                a.get("completion_tokens") or 0 for a in attempts),
                            "failure_codes": dict(sorted(failure_counts.items())),
                            "response_fingerprints": len({
                                a.get("response_fingerprint") for a in attempts
                                if a.get("response_fingerprint")}),
                        }, diagnostic_failures=diagnostic_failures)

    # Pass 1 — resume from chunks_done.
    seg_start = time.time()
    seg_base = elapsed_s.get("segment", 0)
    # resolve_completion_ceiling is consumed ONLY by pass 1, so pass 1 has to be
    # what feeds the allowance. Wiring it to the pass-2/3 observers alone left it
    # at zero for the pass that uses it, and thinking-on segmentation kept
    # reporting "cannot grow beyond 2700" - the visible-output budget with no
    # room for reasoning at all.
    observed_attempts = 0
    progress = RunProgress(planned_calls) if planned_calls else None
    quote_decisions = [quote_regions_decision(
        params.segmentation, chunk, analysis,
        params.quoted_must_be_spoken, params.unquoted_must_be_narrator)
                       for chunk, analysis in zip(chunks, quote_analyses)]
    needs_model = [regions is None for regions, _ in quote_decisions]
    if progress:
        progress.set_total(1, sum(needs_model))
        progress.restore_done(1, sum(needs_model[:chunks_done]))
    for i in range(chunks_done, len(chunks)):
        sink = []
        failures = []
        attempts_before = len(attempts)
        by_marks = not needs_model[i]
        if not by_marks:
            announce_step(1, "chunk", i + 1, len(chunks))
            if progress:
                progress.note_call_started()
        seg = segment_chunk_adaptively(client, model_name, chunks[i], params,
                                       resolution_sink=sink, failure_sink=failures,
                                       attempt_sink=attempts,
                                       quote_analysis=quote_analyses[i],
                                       quote_decision=quote_decisions[i])
        finish_step(1, "chunk", i + 1, len(chunks),
                    "from quote marks" if by_marks else "from the model")
        if progress and not by_marks:
            progress.note_done(1)
            print(progress.eta_line(1), flush=True)
        for attempt in attempts[observed_attempts:]:
            attempt.setdefault("pass", "segment")
            reasoning_allowance.observe(
                attempt.get("reasoning_tokens"),
                truncated=attempt.get("finish_reason") == "length")
        observed_attempts = len(attempts)
        params.reasoning_allowance = reasoning_allowance.current()
        if not seg and should_rescue_with_context(failures[0] if failures else set()):
            # Last resort: retry with escalating surrounding-source context.
            print(f"  chunk {i + 1}/{len(chunks)} failed normal segmentation; "
                  "trying escalating surrounding-source context")
            seg = rescue_chunk_with_context(client, model_name, chunks, i, params,
                                            resolution_sink=sink,
                                            windows=context_windows,
                                            max_retries=context_rescue_retries,
                                            quote_analysis=quote_analyses[i],
                                            attempt_observer=lambda attempt: record_attempt(
                                                "segment_context_rescue", attempt))
            # Rescue observers already updated the reasoning allowance live.
            observed_attempts = len(attempts)
        resolutions.append(sink[-1] if sink else ("clean" if seg else "fail"))
        if not seg:
            if collect_all_failures:
                if diagnostic_segment_resume is None:
                    diagnostic_segment_resume = {
                        "chunks_done": i, "segmented_entries": len(segmented)}
                diagnostic_failures.append(build_segment_failure_record(
                    i + 1, chunks[i], failures[0] if failures else []))
                chunks_done = i + 1
                elapsed_s["segment"] = seg_base + time.time() - seg_start
                save("segment_incomplete")
                continue
            elapsed_s["segment"] = seg_base + time.time() - seg_start
            passes["segment"] = {"elapsed_s": round(elapsed_s["segment"], 3),
                                 "status": "failed"}
            save("segment_failed", failed={
                "pass": "segment", "chunk": i + 1, "chunks_total": len(chunks),
                "source": chunks[i],
                "quoted_must_be_spoken": params.quoted_must_be_spoken,
                "unquoted_must_be_narrator": params.unquoted_must_be_narrator,
                "failure_codes": sorted(failures[0]) if failures else [],
                "attempts": [{**attempt, "pass": attempt_passes.get(
                    id(attempt), attempt.get("pass", "segment"))}
                    for attempt in attempts[attempts_before:][-20:]]})
            emit_manifest("failed", failed_pass="segment", failed_chunk=i + 1)
            raise RuntimeError(f"pass 1 (segment) failed on chunk {i + 1}/{len(chunks)}")
        segmented.extend(seg)
        chunks_done = i + 1
        elapsed_s["segment"] = seg_base + time.time() - seg_start
        save("segment")
    elapsed_s["segment"] = seg_base + time.time() - seg_start
    passes["segment"] = {"elapsed_s": round(elapsed_s["segment"], 3),
                         "status": ("incomplete" if any(
                             f["pass"] == "segment" for f in diagnostic_failures)
                             else "complete")}
    # Pass 2 — deterministic duplicate-free batches, restored to source order.
    # Maintain a running roster (set for O(1) membership + list for order) updated
    # per batch, instead of rescanning the whole `named` prefix every batch.
    named.extend([None] * (len(segmented) - len(named)))
    deterministic = {}
    for index, entry in enumerate(segmented):
        resolved = get_deterministic_named_entry(entry)
        # Narration needs no LLM to resolve, but it is where the dialogue tags
        # live ("Lilia spoke up quietly"), and pass 2 used to drop it from the
        # batch entirely - the model saw a wall of bare quotes. Measured on the
        # mushoku16 gold set, keeping it in the batch moved attribution from
        # 29.9% to 37.0%. It is sent for company, not for an answer: whatever
        # the model says about these lines is discarded below.
        #
        # Resolved for every index, not just unnamed ones. A resumed run has
        # `named` already populated for the entries it finished, and the old
        # `if named[index] is not None: continue` would skip them - leaving
        # `deterministic` empty on resume, so the narration silently stopped
        # being sent as context for exactly the runs that were restarted.
        if resolved is not None and resolved.get("speaker") == "NARRATOR":
            deterministic[index] = resolved
        if named[index] is None:
            named[index] = resolved

    eligible = set(build_roster(
        (entry for entry in named if isinstance(entry, dict)), source_text))
    roster_positions = {}
    for index, entry in enumerate(named):
        if not isinstance(entry, dict) or entry.get("attribution_unchecked"):
            continue
        speaker = (entry.get("speaker") or "").strip().upper()
        if speaker in eligible:
            roster_positions.setdefault(speaker, index)

    def get_attribution_roster():
        current = sorted(roster_positions, key=roster_positions.get)
        if cast:
            current = list(cast["names"]) + [n for n in current if n not in cast["names"]]
        if narrator and narrator not in current:
            current.insert(0, narrator)
        return current

    roster = get_attribution_roster()
    roster_seen = set(roster)
    attr_start = time.time()
    attr_base = elapsed_s.get("attribute", 0)
    # The batch in flight, so a fail-fast can record exactly what failed for
    # the recovery panel (issue #522 s23 / s4.4).
    in_flight = {"current": None, "attempt_start": 0}
    window_total = sum(1 for _ in iter_unique_entry_batches(segmented, attribute_batch_size))
    window_number = 0
    if progress:
        model_total, model_done = 0, 0
        for indexed_batch in iter_unique_entry_batches(segmented, attribute_batch_size):
            if not any(get_deterministic_named_entry(entry) is None for _, entry in indexed_batch):
                continue
            model_total += 1
            pending = get_attribute_pending_entries(
                indexed_batch, named, deterministic, diagnostic_failures)
            model_done += not any(index not in deterministic for index, _ in pending)
        progress.set_total(2, model_total)
        progress.restore_done(2, model_done)
    try:
        for indexed_batch in iter_unique_entry_batches(segmented, attribute_batch_size):
            window_number += 1
            pending = get_attribute_pending_entries(
                indexed_batch, named, deterministic, diagnostic_failures)
            # Narration alone is not work - without a line to attribute there is
            # nothing to give it context for.
            if not any(index not in deterministic for index, _ in pending):
                continue
            work = [pending]
            while work:
                current = work.pop(0)
                batch = [entry for _, entry in current]
                # Duplicate coloring, resume and subdivision can omit adjacent
                # entries. Include only missing neighbors inside this source
                # window; outer evidence remains controlled by the context knob.
                contexts = get_missing_attribute_contexts(
                    segmented, [index for index, _ in current], attribute_batch_size)
                surround = build_window_surround(
                    segmented, [index for index, _ in current], attribute_context_chars)
                announce_step(2, "window", window_number, window_total)
                if progress:
                    progress.note_call_started()
                try:
                    attempt_start = len(attempts)
                    in_flight["current"], in_flight["attempt_start"] = current, attempt_start
                    exhausted = []
                    new_named, vote_confidences = attribute_batch_voted(
                        client, model_name, batch, params, roster=roster,
                        votes=attribution_votes,
                        vote_temperature=vote_temperature,
                        on_exhaustion=on_exhaustion, keep_scope=keep_scope,
                        speaker_traits=speaker_traits,
                        roster_traits=(get_established_traits(named) if speaker_traits else None),
                        attempt_observer=lambda attempt: record_attempt(
                            "attribute", attempt),
                        exhaustion_sink=exhausted,
                        source_text=source_text, surround=surround,
                        neighbor_contexts=contexts,
                        entries_provider=entries_provider, cast=cast)
                except PassExhausted:
                    if len(current) == 1:
                        if collect_all_failures:
                            index, entry = current[0]
                            diagnostic_failures.append(build_failure_record(
                                "attribute", index, entry["text"],
                                last_attempt_for(index)))
                            save("attribute_incomplete")
                            continue
                        raise
                    midpoint = len(current) // 2
                    print(f"  Attribution batch exhausted; subdividing "
                          f"{len(current)} -> {midpoint} + {len(current) - midpoint}")
                    work[0:0] = [current[:midpoint], current[midpoint:]]
                    continue
                batch_attempts = attempts[attempt_start:]
                if (exhausted and batch_attempts
                        and all(attempt.get("outcome") == "api_error"
                                for attempt in batch_attempts)):
                    unavailable_passes.add("attribute")
                    if not collect_all_failures:
                        raise PassExhausted(
                            "attribute LLM unavailable; refusing fallback output")
                for position, ((index, _), entry) in enumerate(
                        zip(current, new_named)):
                    if attribution_votes > 1 and position < len(vote_confidences):
                        # Only present when voting, so a default run's output
                        # shape is unchanged.
                        entry = {**entry,
                                 "attribution_confidence": round(
                                     vote_confidences[position], 3)}
                    # Narration was sent as context only; its speaker is known
                    # without asking, so the model's answer never overwrites it.
                    named[index] = deterministic.get(index, entry)
                if on_exhaustion == "fallback":
                    admitted = set(attested_new_speakers(
                        (named[index] for index, _ in current), set(), source_text))
                    for index, _ in current:
                        speaker = (named[index].get("speaker") or "").strip().upper()
                        if speaker in admitted and not named[index].get("attribution_unchecked"):
                            roster_positions[speaker] = min(index, roster_positions.get(speaker, index))
                    roster = get_attribution_roster()
                    roster_seen = set(roster)
                else:
                    # Same admission gate as build_roster above, applied
                    # incrementally so a batch does not rescan every prior entry.
                    for speaker in attested_new_speakers(
                            new_named, roster_seen, source_text):
                        roster_seen.add(speaker)
                        roster.append(speaker)
                elapsed_s["attribute"] = attr_base + time.time() - attr_start
                # Each accepted subdivision is durable; a later single-entry
                # failure resumes after this work instead of replaying the batch.
                save("attribute")
            finish_step(2, "window", window_number, window_total, "speakers assigned")
            if progress:
                progress.note_done(2)
                print(progress.eta_line(2), flush=True)
    except PassExhausted as exc:
        elapsed_s["attribute"] = attr_base + time.time() - attr_start
        passes["attribute"] = {"elapsed_s": round(elapsed_s["attribute"], 3),
                               "status": "failed"}
        batch = in_flight["current"] or []
        save("attribute_failed", failed={
            "pass": "attribute",
            "indices": [index for index, _ in batch],
            "entries": [{"type": entry.get("type"), "text": entry.get("text")}
                        for _, entry in batch],
            "roster": list(roster),
            "reason": str(exc),
            "attempts": attempts[in_flight["attempt_start"]:][-20:]})
        emit_manifest("failed", failed_pass="attribute")
        if "LLM unavailable" in str(exc):
            raise RuntimeError(str(exc)) from exc
        raise
    elapsed_s["attribute"] = attr_base + time.time() - attr_start
    passes["attribute"] = {"elapsed_s": round(elapsed_s["attribute"], 3),
                           "status": ("incomplete" if any(
                               f["pass"] == "attribute" for f in diagnostic_failures)
                               else "complete"),
                           "unchecked_entries": sum(
                               1 for entry in named
                               if entry and entry.get("attribution_unchecked"))}
    # Pass 3 uses the same duplicate-free scheduling so ambiguous heads cannot
    # slip through there either (finding #5).
    annotated.extend([None] * (len(named) - len(annotated)))
    for index, entry in enumerate(named):
        if (annotated[index] is None and isinstance(entry, dict)
                and is_nonverbal_text(entry.get("text"))):
            annotated[index] = {**entry, "instruct": default_instruct(entry)}
    inst_start = time.time()
    inst_base = elapsed_s.get("instruct", 0)
    window_total = sum(1 for _ in iter_unique_entry_batches(named))
    window_number = 0
    instruction_progress_reported = False
    if progress:
        model_total, model_done = 0, 0
        for indexed_batch in iter_unique_entry_batches(named):
            model_indices = [index for index, entry in indexed_batch
                             if not is_nonverbal_text(entry.get("text"))]
            if not model_indices:
                continue
            model_total += 1
            model_done += all(annotated[index] is not None for index in model_indices)
        progress.set_total(3, model_total)
        progress.restore_done(3, model_done)
    for indexed_batch in iter_unique_entry_batches(named):
        window_number += 1
        pending = [(index, entry) for index, entry in indexed_batch
                   if annotated[index] is None]
        if not pending:
            continue
        work = [pending]
        while work:
            current = work.pop(0)
            batch = [entry for _, entry in current]
            contexts = [{"previous_context": named[index - 1] if index else None,
                         "next_context": named[index + 1]
                         if index + 1 < len(named) else None}
                        for index, _ in current]
            request_params = ensure_run_request_params(client, params)
            if (len(current) > 1
                    and not does_instruct_batch_fit_context(batch, request_params, contexts)):
                midpoint = len(current) // 2
                print(f"  Instruction batch exceeds context budget; subdividing "
                      f"{len(current)} -> {midpoint} + {len(current) - midpoint}")
                work[0:0] = [current[:midpoint], current[midpoint:]]
                continue
            exhausted = []
            attempt_start = len(attempts)
            announce_step(3, "window", window_number, window_total)
            if progress:
                progress.note_call_started()
            new_annotated = instruct_batch(
                client, model_name, batch, request_params, neighbor_contexts=contexts,
                exhaustion_sink=exhausted,
                attempt_observer=lambda attempt: record_attempt(
                    "instruct", attempt))
            batch_attempts = attempts[attempt_start:]
            if (exhausted and batch_attempts
                    and all(attempt.get("outcome") == "api_error"
                            for attempt in batch_attempts)):
                unavailable_passes.add("instruct")
                if not collect_all_failures:
                    elapsed_s["instruct"] = inst_base + time.time() - inst_start
                    passes["instruct"] = {
                        "elapsed_s": round(elapsed_s["instruct"], 3),
                        "status": "failed"}
                    save("instruct_failed", failed={
                        "pass": "instruct",
                        "indices": [index for index, _ in current],
                        "entries": [{"speaker": entry.get("speaker"), "text": entry.get("text")}
                                    for _, entry in current],
                        "reason": "instruct LLM unavailable; refusing fallback output",
                        "attempts": batch_attempts[-20:]})
                    emit_manifest("failed", failed_pass="instruct")
                    raise RuntimeError(
                        "instruct LLM unavailable; refusing fallback output")
            if exhausted and collect_all_failures and len(current) > 1:
                midpoint = len(current) // 2
                print(f"  Instruction batch exhausted; subdividing "
                      f"{len(current)} -> {midpoint} + {len(current) - midpoint}")
                work[0:0] = [current[:midpoint], current[midpoint:]]
                continue
            if exhausted and collect_all_failures:
                index, entry = current[0]
                diagnostic_failures.append(build_failure_record(
                    "instruct", index, entry["text"], last_attempt_for(index)))
            for (index, _), entry in zip(current, new_annotated):
                annotated[index] = entry
            elapsed_s["instruct"] = inst_base + time.time() - inst_start
            save("instruct")
        finish_step(3, "window", window_number, window_total, "delivery notes written")
        if progress:
            progress.note_done(3)
            print(progress.eta_line(3), flush=True)
            instruction_progress_reported = True
    if progress and not instruction_progress_reported:
        print(progress.eta_line(3), flush=True)
    elapsed_s["instruct"] = inst_base + time.time() - inst_start
    passes["instruct"] = {"elapsed_s": round(elapsed_s["instruct"], 3),
                          "status": ("incomplete" if any(
                              f["pass"] == "instruct" for f in diagnostic_failures)
                              else "complete")}
    if unavailable_passes:
        failed_pass = next(pass_name for pass_name in ("attribute", "instruct")
                           if pass_name in unavailable_passes)
        if collect_all_failures:
            # Diagnostic mode deliberately returns partial output so callers
            # can inspect every recorded failure. Keep it visibly incomplete;
            # production mode below still refuses fallback-only publication.
            passes[failed_pass]["status"] = "incomplete"
        else:
            passes[failed_pass]["status"] = "failed"
            save(f"{failed_pass}_unavailable")
            emit_manifest("failed", failed_pass=failed_pass)
            raise RuntimeError(
                f"{failed_pass} LLM unavailable; refusing to publish fallback-only output")
    try:
        dialogue = apply_dialogue_map(annotated, source_text)
        annotated = dialogue["entries"]
        if dialogue["convention"]:
            print(f"Dialogue map: {dialogue['convention']}, "
                  f"{dialogue['spoken']} spoken lines, "
                  f"{dialogue['located']}/{len(annotated)} entries located in the source")
            if dialogue["speaker_changes"]:
                print(f"Source speaker labels applied to "
                      f"{len(dialogue['speaker_changes'])} entries")
        else:
            print("Dialogue map: convention could not be determined; entries "
                  "carry no `spoken` key rather than a guessed one")
    except Exception as exc:                                   # noqa: BLE001
        print(f"Dialogue map FAILED ({exc}); entries carry no `spoken` key")
        diagnostic_failures.append({
            "pass": "dialogue_map", "reason": type(exc).__name__,
            "error": str(exc)[:500]})
    save("done")
    emit_manifest("incomplete" if diagnostic_failures or unavailable_passes
                  else "complete")
    return [entry for entry in annotated if isinstance(entry, dict)]


def build_segment_failure_record(chunk_number, chunk_text, failure_codes):
    """Build a pass-1 failure record.

    Pass 1 fails per source chunk rather than per entry, so it cannot reuse
    build_failure_record's entry shape. It carries the same "reason" key so the
    manifest's failure_reasons rollup counts pass-1 failures instead of
    bucketing them all as unknown.
    """
    codes = sorted(failure_codes or [])
    return {"pass": "segment", "chunk": chunk_number,
            "source_sha256": hashlib.sha256(chunk_text.encode()).hexdigest(),
            "source_characters": len(chunk_text),
            "source_preview": chunk_text[:500],
            "failure_codes": codes,
            "reason": codes[0] if codes else "segment_exhausted"}


# Fixed, distinct seeds so a vote is reproducible: the same input yields the
# same samples and therefore the same majority on every run.
_VOTE_SEED_BASE = 1000


def vote_seeds(count):
    """Return `count` distinct, stable seeds for one voted attribution."""
    return [_VOTE_SEED_BASE + index for index in range(count)]


def majority_vote(votes):
    """Return (winner, confidence) for one entry's ballots.

    Confidence is the winning share, which is the signal greedy decoding cannot
    produce: measured on mushoku16, greedy agreed with a unanimous vote 81% of
    the time but with a split vote only 42%, so a split reliably marks a
    contested line. With no majority the first sample wins, keeping the result
    deterministic rather than dependent on tie ordering.
    """
    real = [v for v in votes if v is not None]
    if not real:
        return None, 0.0
    tally = Counter(real)
    best = max(tally.values())
    for vote in real:                      # first sample with the top count
        if tally[vote] == best:
            return vote, best / len(real)
    return real[0], best / len(real)


def attribute_batch_voted(client, model_name, frozen_batch, params, roster,
                          votes=1, vote_temperature=0.3, **kwargs):
    """Attribute one batch, optionally by majority vote across seeded samples.

    votes=1 is the greedy path and is byte-identical to calling attribute_batch
    directly, so this is inert unless voting is switched on.

    Above 1, the batch is attributed once per fixed seed at vote_temperature
    and the majority wins per entry. Greedy commits to a single path with no
    way to correct itself: measured on mushoku16, in 30 of 49 disagreements it
    chose a speaker that not one of three samples picked, and in one scene it
    scattered four lines addressed to Rudi across two characters while every
    sample said ROXY.

    Returns (entries, confidences). Each confidence is the winning share, so a
    caller can flag contested lines - a signal greedy cannot produce.
    """
    validate_attribution_vote_settings(votes, vote_temperature)
    if votes == 1:
        result = attribute_batch(client, model_name, frozen_batch, params,
                                 roster, **kwargs)
        return result, [1.0] * len(result or [])

    ballots = []
    fallback = None
    any_exhausted = False
    for seed in vote_seeds(votes):
        sampled = replace(params, seed=seed, temperature=vote_temperature,
                          attribute_temperature=vote_temperature)
        sample_exhausted = []
        sample_kwargs = {**kwargs, "exhaustion_sink": sample_exhausted}
        result = attribute_batch(client, model_name, frozen_batch, sampled,
                                 roster, **sample_kwargs)
        if sample_exhausted:
            any_exhausted = True
            if fallback is None and result and len(result) == len(frozen_batch):
                fallback = result
            continue
        if result and len(result) == len(frozen_batch):
            ballots.append(result)
    if not ballots:
        if any_exhausted and kwargs.get("exhaustion_sink") is not None:
            kwargs["exhaustion_sink"].append(True)
        return (fallback, [0.0] * len(fallback)) if fallback else ([], [])
    if len(ballots) == 1:
        return ballots[0], [1.0] * len(ballots[0])

    entries, confidences = [], []
    for position in range(len(ballots[0])):
        speakers = [ballot[position].get("speaker") for ballot in ballots]
        winner, confidence = majority_vote(speakers)
        entry = dict(next(ballot[position] for ballot in ballots
                          if ballot[position].get("speaker") == winner))
        entry["speaker"] = winner
        entries.append(entry)
        confidences.append(confidence)
    return entries, confidences


def build_failure_record(pass_name, index, text, last_attempt=None):
    """Build a diagnostic failure record carrying why the batch failed.

    The earlier record shape (pass/entry/text_sha256/text_preview) said which
    entry failed but never why, so causes had to be recovered by grepping run
    logs. last_attempt is the final observed attempt dict from
    generate_script's attempt_observer, or None when no attempt was recorded.
    """
    attempt = last_attempt or {}
    codes = attempt.get("failure_codes") or []
    return {
        "pass": pass_name,
        "entry": index,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "text_preview": text[:500],
        "reason": codes[0] if codes else (attempt.get("outcome") or "unknown"),
        "finish_reason": attempt.get("finish_reason"),
        "prompt_tokens": attempt.get("prompt_tokens"),
        "completion_tokens": attempt.get("completion_tokens"),
        "reasoning_tokens": attempt.get("reasoning_tokens"),
        "effective_max_tokens": attempt.get("effective_max_tokens"),
        "attempt": attempt.get("attempt"),
    }


REASONING_ALLOWANCE_FLOOR = 1024


class ReasoningAllowance:
    """Track a model's observed thinking-token cost.

    Reasoning tokens bill to completion_tokens but are returned in
    message.reasoning_content, so a ceiling sized on visible output truncates a
    reasoning model mid-thought. A model that never reports reasoning_tokens
    keeps an allowance of zero, so non-reasoning behaviour is unchanged.
    """

    def __init__(self):
        self._observations = []

    def observe(self, reasoning_tokens, truncated=False):
        """Record one call's thinking cost.

        A truncated response reports only the reasoning it managed to emit
        before hitting the ceiling, not what it wanted. Taking that at face
        value makes the allowance converge upward one small step per chunk,
        truncating every chunk on the way, so a censored observation is
        treated as a lower bound and inflated instead.
        """
        if not reasoning_tokens:
            return
        tokens = int(reasoning_tokens)
        if truncated:
            tokens *= 2
        self._observations.append(tokens)

    def current(self):
        if not self._observations:
            return 0
        ordered = sorted(self._observations)
        index = min(len(ordered) - 1, int(len(ordered) * 0.95))
        return max(REASONING_ALLOWANCE_FLOOR, ordered[index])


def resolve_completion_ceiling(source_words, params, reasoning_allowance=0):
    """Bound segmentation output, leaving room for invisible reasoning.

    The visible-output bound is unchanged from the original: it stops a weak
    model spending 10k-16k tokens expanding a ~1k-token chunk. The reasoning
    allowance is added on top rather than carved out of it, so a reasoning
    model gets the same visible budget as everyone else.
    """
    visible = max(512, math.ceil(max(1, source_words) * params.segment_output_ratio))
    return visible + max(0, int(reasoning_allowance))


MAX_REPLACEMENT_DENSITY = 0.02




def prepare_source_text(book):
    """Apply the shared conservative source repair and audit before LLM calls.

    Mirrors production's gate in generate_script.main (audit_unicode_text then
    hard failure) so the diagnostic CLI cannot spend hours on a source that
    production would reject outright. Raises ValueError rather than exiting so
    the behaviour is testable; main() turns it into a non-zero exit.
    """
    damaged = book.count("�")
    if damaged and damaged / max(len(book), 1) > MAX_REPLACEMENT_DENSITY:
        raise ValueError(
            f"source replacement-character density "
            f"{damaged / len(book):.1%} exceeds the "
            f"{MAX_REPLACEMENT_DENSITY:.0%} ceiling; refusing to process it")
    # Inspect destroyed characters with the same engine as single-pass before
    # any fallback can consume the evidence and invent a different punctuation.
    preflight = preflight_source(book)
    for line in preflight["messages"]:
        print(line)
    book = preflight["text"]
    report = audit_unicode_text(book)
    if report["unsafe_controls"]:
        raise ValueError("source contains unsafe control characters: "
                         f"{report['unsafe_controls']}")
    # Same policy as the single-pass path, from one definition. These two
    # used to disagree: a repaired book generated single-pass and was refused
    # here, for the same input.
    if not replacement_load_is_acceptable(
            report["replacement_character_count"], len(book)):
        raise ValueError(
            f"source is {report['replacement_character_count'] / max(1, len(book)):.2%} "
            "replacement characters, above the shared limit.\n"
            + replacement_repair_hint())
    unresolved = report["replacement_character_count"]
    residual = preflight["applied"].get("last_resort_dash", 0)
    return book, {"repaired": max(0, damaged - unresolved - residual),
                  "residual": residual, "unresolved": unresolved,
                  "scripts": report["scripts"], "is_nfc": report["is_nfc"]}


def get_output_paths(data_dir, requested_output=None):
    """Resolve output and stale-chunk paths for CLI and application runs."""
    if requested_output is not None:
        return requested_output, None
    return (os.path.join(data_dir, "annotated_script.json"),
            os.path.join(data_dir, "chunks.json"))


def get_prepared_source(path, strip_front_matter=True, report=lambda message: None):
    """-> (text, unicode_report): the source exactly as generation sees it.

    One sequence for every caller that sends the book to a model - generation
    and the whole-book cast list (cast_list.py) - so a cast is never built from
    text pass 2 does not receive (front matter, repaired encoding). `report`
    receives the same notices main() has always printed. Raises ValueError for
    a source the shared gate refuses.
    """
    book, source_encoding = read_source_text(path)
    book = get_normalized_source_newlines(book)
    if source_encoding != "utf-8":
        report(f"Read {path} as {source_encoding} (not valid UTF-8)")
    book, preprocessing = get_preprocessed_source(
        book, strip_front_matter=strip_front_matter)
    if preprocessing["source_normalizations"]:
        report(f"Normalized {len(preprocessing['source_normalizations'])} known source "
               "corruption(s) in memory; the upload was not modified.")
    publisher = preprocessing["publisher_matter"]
    if publisher["front_paragraphs"] or publisher["back_paragraphs"]:
        report(f"Stripped publisher matter: {publisher['front_paragraphs']} "
               f"paragraph(s) from the front, {publisher['back_paragraphs']} "
               "from the back (copyright page / colophon, not narration)")
    book, unicode_report = prepare_source_text(book)
    require_nonempty_source(book)
    if unicode_report["repaired"] or unicode_report["residual"]:
        report(f"Repaired {unicode_report['repaired']} destroyed character(s); "
               f"neutralized {unicode_report['residual']} unrecoverable one(s). "
               "The source file was not modified.")
    if unicode_report["unresolved"]:
        report(f"Retained {unicode_report['unresolved']} unresolved replacement character(s); "
               "the shared source gate admits this remaining load.")
    return book, unicode_report


def main():
    parser = argparse.ArgumentParser(description="Three-pass annotated script generation.")
    parser.add_argument("input_file")
    parser.add_argument("--output", default=None)
    parser.add_argument("--model", default=None, help="Generation model for this run; config.json is unchanged.")
    parser.add_argument("--chunk-size", type=int, default=None)
    parser.add_argument("--attribute-batch-size", type=int, default=None,
                        help="Override generation.three_pass_attribute_batch_size "
                             "(entries per pass-2 request)")
    parser.add_argument("--attribute-context-chars", type=int, default=None,
                        help="Override generation.three_pass_attribute_context_chars "
                             "(characters of the book shown either side of each pass-2 window)")
    parser.add_argument("--prompt-variant", default=None,
                        help="Override generation.three_pass_attribute_prompt_variant "
                             "(one of attribution_prompt_variants.VARIANTS)")
    parser.add_argument("--segmentation", choices=SEGMENTATION_MODES, default=None,
                        help="Override generation.three_pass_segmentation: auto "
                             "(shown to users as 'Quote marks first, model for the rest'; "
                             "quote marks where unambiguous, else the model), quotes "
                             "(never the model), lexical (quoted terms as narration), "
                             "llm (always the model)")
    parser.add_argument("--strip-front-matter", action=argparse.BooleanOptionalAction,
                        default=True)
    parser.add_argument("--cast-file", default=None,
                        help="JSON cast list [{name, aliases}] put on the pass-2 roster "
                             "ahead of attested names (experiments/build_cast_list.py).")
    parser.add_argument("--pass2-keep-scope", choices=PASS2_KEEP_SCOPES, default=None,
                        help="With --pass2-on-exhaustion keep: 'line' subdivides an exhausted "
                             "batch down to the failing line before keeping it; 'batch' keeps a "
                             "batch whose only failures are keepable checks whole, flagging just "
                             "those lines (#668). Default: the Setup switch "
                             "(generation.three_pass_keep_whole_batch), else 'line'.")
    parser.add_argument("--pass2-on-exhaustion", choices=["fail", "fallback", "keep"],
                        default="fail",
                        help="testing default 'fail' surfaces pass-2 failures; "
                             "'fallback' degrades gracefully; 'keep' (the app's) "
                             "keeps a single line's last answer when only a "
                             "keepable check refused it, flagged unchecked.")
    parser.add_argument("--preflight", action="store_true",
                        help="Run first/middle/dialogue-heavy samples only.")
    parser.add_argument("--attribution-votes", type=int, default=1,
                        help="Attribute each batch this many times with fixed "
                             "seeds and take the majority (default 1 = greedy). "
                             "Greedy commits to one path and cannot recover; "
                             "measured on mushoku16 it chose a speaker no "
                             "sample agreed with in 61%% of disagreements.")
    parser.add_argument("--vote-temperature", type=float, default=0.3,
                        help="Sampling temperature for voted attribution "
                             "(default 0.3). At 0.7 the samples abstain to "
                             "UNKNOWN instead of committing.")
    parser.add_argument("--reasoning-effort", default=None,
                        help="Pass through to the model (e.g. 'none' to "
                             "disable thinking on a reasoning model).")
    parser.add_argument(
        "--first-person-narrator", default=None,
        help="Exact character name of this book's first-person narrator.")
    parser.add_argument("--collect-all-failures", action="store_true",
                        help="Diagnostic mode: record exhausted work, continue, "
                             "write only a .partial.json result, and exit nonzero.")
    args = parser.parse_args()
    if args.preflight and args.collect_all_failures:
        parser.error("--collect-all-failures cannot be combined with --preflight")

    try:
        book, unicode_report = get_prepared_source(
            args.input_file, args.strip_front_matter, report=print)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    try:
        narrator = get_valid_narrator_name(args.first_person_narrator)
    except ValueError as exc:
        parser.error(str(exc))
    if narrator and not is_narrator_attested(narrator, book):
        parser.error(
            "first-person narrator must appear by name at least three times "
            "in the prepared source")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    try:
        config = get_generation_config(
            load_app_config(get_app_config_path(data_dir, root, app_dir)), args.model)
    except ValueError as exc:
        parser.error(str(exc))
    llm = get_active_llm_config(config)
    gen = config.get("generation") or {}
    model_name = llm.get("model_name")
    cast = None
    if args.cast_file:
        try:
            cast = load_cast(args.cast_file)
        except (OSError, ValueError) as exc:
            parser.error(f"--cast-file: {exc}")
        print(f"Cast list: {len(cast['names'])} characters from {args.cast_file} "
              f"(sha256 {cast['sha256'][:12]})")
    try:
        generation_settings = resolve_three_pass_generation_settings(
            config, args.chunk_size, segmentation_override=args.segmentation)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    model_profile = generation_settings["model_profile"]
    chunk_size = generation_settings["chunk_size"]
    attribute_batch_size = (args.attribute_batch_size if args.attribute_batch_size is not None
                            else generation_settings["attribute_batch_size"])
    attribute_context_chars = (args.attribute_context_chars if args.attribute_context_chars is not None
                               else generation_settings["attribute_context_chars"])
    if attribute_batch_size < 1 or attribute_context_chars < 0:
        raise SystemExit("attribute batch size must be >= 1 and context chars >= 0")
    try:
        configured_windows = gen.get("context_rescue_windows")
        validated_windows = get_context_rescue_windows(configured_windows)
        context_windows = validated_windows if configured_windows is not None else None
        validate_attribution_vote_settings(args.attribution_votes, args.vote_temperature)
    except ValueError as exc:
        parser.error(str(exc))
    context_rescue_retries = gen.get("context_rescue_retries")
    base_url = llm.get("base_url", "http://localhost:1234/v1")
    llm_mode = config.get("llm_mode", "local")
    # Self-heal LM Studio: load model_name at its verified context if nothing is
    # loaded / settings are stale, mirroring generate_script.py. Without this a
    # fresh `lms unload` leaves no model loaded and every call 400s.
    _, lm_status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"), api_key=llm.get("api_key"))
    print(heal_msg)
    params = get_three_pass_run_params(config, lm_status, args.reasoning_effort, generation_settings)
    generation_settings.update({
        "segment_system_prompt": params.segment_system_prompt,
        "segment_user_prompt_template": params.segment_user_prompt_template,
        "instruct_system_prompt": params.instruct_system_prompt,
        "instruct_user_prompt_template": params.instruct_user_prompt_template,
    })
    attribute_prompt_variant, attribute_prompt_texts = resolve_attribute_prompt(
        config, args.prompt_variant)
    from attribution_prompt_variants import (GROUP_RULE_7, VARIANTS, builtin_texts,
                                             validate_preset_texts)
    if (config.get("generation") or {}).get("three_pass_group_rule") is True:
        print("Group rule: on" if GROUP_RULE_7 in ((attribute_prompt_texts or {}).get("system") or "")
              else "Group rule: on, but this prompt has no standard rule 7 to replace; not applied")
    speaker_traits = is_speaker_traits_prompt(attribute_prompt_texts)
    if (config.get("generation") or {}).get("three_pass_speaker_traits") is True:
        print("Speaker gender/age per line: on" if speaker_traits else
              "Speaker gender/age per line: on, but this prompt has no standard answer shape "
              "to extend; not applied")
    if attribute_prompt_variant not in VARIANTS:
        raise SystemExit(f"unknown prompt variant {attribute_prompt_variant!r}; expected one of {VARIANTS}")
    problem = validate_preset_texts(attribute_prompt_variant, attribute_prompt_texts)
    if problem:
        raise SystemExit(f"attribution preset: {problem}")
    if narrator:
        # the first-person narrator prior goes on top of whatever system text
        # the active preset sends (the file's, or the user's edit of it)
        system_text = ((attribute_prompt_texts or {}).get("system")
                       or builtin_texts(attribute_prompt_variant)["system"])
        params.attribute_system_prompt = add_narrator_prior(system_text, narrator)
        if attribute_prompt_texts:
            attribute_prompt_texts = dict(attribute_prompt_texts, system=params.attribute_system_prompt)
    elif attribute_prompt_texts and attribute_prompt_variant == "default":
        params.attribute_system_prompt = attribute_prompt_texts.get("system") or None
        params.user_prompt_template = attribute_prompt_texts.get("user") or None
    client = make_run_client(config, llm, llm_timeout_seconds())

    # The CLI flag overrides the Setup switch; neither set means 'line'.
    keep_scope = args.pass2_keep_scope or generation_settings["keep_scope"]
    output_path, chunks_path = get_output_paths(data_dir, args.output)
    print(f"Three-pass generation: {len(book)} chars, chunk_size={chunk_size}, "
          f"attribute_batch_size={attribute_batch_size}, "
          f"attribute_context_chars={attribute_context_chars}, "
          f"attribute_prompt_variant={attribute_prompt_variant}, "
          f"model={model_name}, pass2_on_exhaustion={args.pass2_on_exhaustion}, "
          f"pass2_keep_scope={keep_scope}")
    # Resolve execution controls once; samples and full runs share this policy.
    run_options = MappingProxyType({
        "on_exhaustion": args.pass2_on_exhaustion,
        "keep_scope": keep_scope,
        "speaker_traits": speaker_traits,
        "context_windows": context_windows,
        "context_rescue_retries": context_rescue_retries,
        "endpoint": base_url,
        "collect_all_failures": args.collect_all_failures,
        "unicode_report": unicode_report,
        "thinking_mode": args.reasoning_effort,
        "attribution_votes": args.attribution_votes,
        "vote_temperature": args.vote_temperature,
        "first_person_narrator": narrator,
        "attribute_batch_size": attribute_batch_size,
        "attribute_context_chars": attribute_context_chars,
        "attribute_prompt_variant": attribute_prompt_variant,
        "attribute_prompt_texts": attribute_prompt_texts,
        "cast": cast,
    })
    planning_settings = MappingProxyType({**generation_settings, **run_options})

    def get_planned_calls(source):
        return get_three_pass_planned_calls(source, planning_settings, params)

    planned_calls = get_planned_calls(book)
    print(plan_line(planned_calls), flush=True)
    if args.preflight:
        summary = {"status": "complete", "model_name": model_name,
                   "planned_calls": dict(planned_calls), "samples": []}
        for label, index, sample in select_preflight_chunks(book, chunk_size):
            sample_out = f"{output_path}.preflight_{label}.json"
            sample_plan = get_planned_calls(sample)
            try:
                sample_entries = run_three_pass(
                    client, model_name, sample, params, chunk_size,
                    output_path=sample_out,
                    planned_calls=sample_plan, **run_options)
                atomic_json_write(sample_entries, sample_out)
                info = get_preflight_sample_info(label, index, sample_out,
                                                 sample_plan, entries=sample_entries)
            except (RuntimeError, PassExhausted) as exc:
                info = get_preflight_sample_info(label, index, sample_out,
                                                 sample_plan, error=str(exc))
            summary["samples"].append(info)
            if info["status"] != "complete":
                summary["status"] = "failed"
                break
        atomic_json_write(summary, output_path + ".preflight_manifest.json")
        sys.exit(0 if summary["status"] == "complete" else 1)
    try:
        entries = run_three_pass(client, model_name, book, params, chunk_size,
                                 output_path=output_path,
                                 planned_calls=planned_calls, **run_options)
    except (RuntimeError, PassExhausted) as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    manifest = safe_load_json(three_pass_manifest_path(output_path), {})
    if manifest.get("status") == "incomplete":
        partial_path = output_path + ".partial.json"
        atomic_json_write(entries, partial_path)
        print(f"Diagnostic run found {len(manifest.get('diagnostic_failures', []))} "
              f"failure(s); wrote {len(entries)} successful entries to {partial_path}")
        sys.exit(1)
    # Dialogue mapping already ran inside run_three_pass before the final
    # checkpoint and manifest were written.
    atomic_json_write(entries, output_path)
    print(f"Wrote {len(entries)} entries to {output_path}")
    if chunks_path is not None and remove_chunk_snapshot(chunks_path):
        print("Cleared old chunks.json")


if __name__ == "__main__":
    main()
