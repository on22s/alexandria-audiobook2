"""Shared config.json models and shape-safe loading boundary."""

import copy
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Dict, List, Literal, Optional, Union, get_args, get_origin
from types import UnionType

from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ConfigWarning:
    field: str
    message: str


@dataclass(frozen=True)
class AppConfigLoadResult:
    data: dict
    warnings: tuple[ConfigWarning, ...]
    needs_backup: bool = False


class LLMConfig(BaseModel):
    base_url: str
    api_key: str
    model_name: str
    provider_headers: Dict[str, str] = Field(default_factory=dict)
    provider_extra_body: Dict[str, JsonValue] = Field(default_factory=dict)
    # Does this LLM run on this machine's GPU? None = decide from the
    # endpoint (a remote endpoint is not on this GPU; a local one is). When
    # it is not, LLM-only tasks (script, review, personas) no longer hold the
    # GPU lock against audio work, so a hosted-API or CPU-served LLM can
    # annotate one book while the card renders another.
    on_this_gpu: Optional[bool] = None
    # "manual": no HTTP at all - every request the pipeline would send is
    # handed to the user on the Script tab to answer elsewhere and paste back
    # (issue #593). The pipeline's own validation and retries gate the paste.
    transport: Literal["http", "manual"] = "http"
    # How much the model thinks before answering, sent as `reasoning_effort`
    # on every chat completion for this profile (None = the server's default).
    # Folded into the profile request body by llm_provider.get_provider_extra_body,
    # so it reaches script generation, review, personas and nicknames alike.
    reasoning_effort: Optional[Literal["none", "low", "medium", "high", "xhigh"]] = None
    request_timeout_seconds: Optional[float] = Field(default=None, gt=0, le=3600)
    connect_timeout_seconds: Optional[float] = Field(default=None, gt=0, le=300)
    request_interval_seconds: float = Field(default=0, ge=0, le=3600)
    api_retry_limit: Optional[int] = Field(default=None, ge=0, le=10)
    # When the retry budget runs out on a rate limit / 5xx / timeout: give the
    # chunk up ("fail", today's behaviour) or freeze the run for the operator
    # to fix the provider and press Resume ("pause").
    on_api_exhaustion: Literal["fail", "pause"] = "fail"
    # Ask the server to constrain attribution replies to the {n, speaker}
    # JSON schema ("auto": send it, fall back for the run if the server
    # rejects response_format; "off": never). Measured 2026-09-14: on
    # Qwen3.8 at batch 25 it recovered every blank window (54 -> 0).
    structured_output: Literal["auto", "off"] = "auto"
    retry_initial_delay_seconds: float = Field(default=1, ge=0, le=60)
    retry_multiplier: float = Field(default=2, ge=1, le=10)
    retry_max_delay_seconds: float = Field(default=30, ge=0, le=300)
    # Fraction of each retry delay randomised either side (0.2 = +-20%), so
    # parallel workers do not retry a rate-limited provider in lockstep.
    retry_jitter: float = Field(default=0.2, ge=0, le=1)


class TTSConfig(BaseModel):
    mode: Literal["local", "external"] = "local"
    url: str = "http://127.0.0.1:7860"
    # External mode only: a pool of Gradio TTS servers. Empty means [url].
    # Batch rendering runs parallel_workers lines per endpoint concurrently.
    external_urls: List[str] = Field(default_factory=list)
    external_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    device: str = "auto"
    language: str = "English"
    parallel_workers: int = Field(default=2, ge=1)
    batch_seed: Optional[int] = None
    compile_codec: bool = False
    max_new_tokens: int = Field(default=2048, ge=256, le=8192)
    sub_batch_enabled: bool = True
    sub_batch_min_size: int = Field(default=4, ge=1)
    sub_batch_ratio: float = Field(default=5.0, ge=1)
    sub_batch_max_items: int = Field(default=0, ge=0)
    batch_group_by_type: bool = False
    pause_between_speakers_ms: int = Field(default=500, ge=0)
    pause_same_speaker_ms: int = Field(default=250, ge=0)


PromptVariant = Literal[
    "default", "aliases", "passage", "incremental", "michel", "continuity",
    "michel2", "michel2_full", "michel2_shot"]

# How pass 1 decides what is speech. "auto": quote marks where the chunk makes
# them unambiguous, the model for the rest (the behaviour every three-pass
# result was measured with). "quotes": never ask the model - quoted text is
# dialogue, the rest narration, no pass-1 calls (issue #588; a plain quote
# segmenter finds 99.84% of PDNC's labelled quotations,
# quote_segmenter_pdnc_20260918.json). "llm": always ask the model.
# User-facing names for these stored values are in RECIPES.md ("Pass 1"): auto is shown as
# "Quote marks first, model for the rest (recommended)", quotes as "Quote marks only", lexical as
# "Quote-aware", llm as "Model only". The stored values must not change without a migration.
SegmentationMode = Literal["auto", "quotes", "lexical", "llm"]


class ThreePassModelProfile(BaseModel):
    chunk_size: Optional[int] = Field(default=None, ge=500, le=30000)
    segment_temperature: Optional[float] = Field(default=None, ge=0, le=2)
    attribute_temperature: Optional[float] = Field(default=None, ge=0, le=2)
    instruct_temperature: Optional[float] = Field(default=None, ge=0, le=2)
    segment_output_ratio: Optional[float] = Field(default=None, ge=1.25, le=6.0)
    segmentation: Optional[SegmentationMode] = None


class GenerationConfig(BaseModel):
    chunk_size: int = Field(default=3000, ge=500)
    max_tokens: int = Field(default=4096, ge=256)
    temperature: float = Field(default=0.6, ge=0, le=2)
    top_p: float = Field(default=0.8, ge=0, le=1)
    top_k: int = Field(default=0, ge=0, le=200)
    min_p: float = Field(default=0, ge=0, le=1)
    presence_penalty: float = Field(default=0.0, ge=-2, le=2)
    banned_tokens: List[str] = Field(default_factory=list)
    merge_narrators: bool = False
    context_rescue_windows: List[Annotated[int, Field(gt=0)]] = Field(
        default_factory=lambda: [2000, 4000, 6000], min_length=1)
    context_rescue_retries: int = Field(default=2, ge=0)
    three_pass_segment_temperature: float = Field(default=0.1, ge=0, le=2)
    three_pass_attribute_temperature: float = Field(default=0.1, ge=0, le=2)
    three_pass_instruct_temperature: float = Field(default=0.1, ge=0, le=2)
    three_pass_segment_output_ratio: float = Field(default=3.0, ge=1.25, le=6.0)
    three_pass_chunk_size: int = Field(default=3000, ge=500, le=1000000)
    # How much text pass 2 sees: entries per attribution window, and characters
    # of the book before/after the window shown as evidence. 2000 is the
    # michel2_full setting that won on every base measured (RECIPES, "Prompt
    # variants x bases"); 0 is what the pre-2026-09-19 scores were taken with.
    three_pass_attribute_batch_size: int = Field(default=25, ge=5, le=100)
    three_pass_attribute_context_chars: int = Field(default=2000, ge=0, le=1000000)
    # Zero retains the saved line-based behavior; positive values opt in.
    three_pass_attribute_target_chars: int = Field(default=0, ge=0, le=1000000)
    three_pass_instruct_target_chars: int = Field(default=0, ge=0, le=1000000)
    # Which way the attribution question is asked (attribution_prompt_variants;
    # measured results per variant in RECIPES.md). michel2_full since
    # 2026-09-19, when Muse's cell (90.5 vs michel2 86.6) made it the winner
    # on every base. "default" is the shape the Qwen3-14B rights-clean
    # adapters were trained on - serve those under it. A saved config keeps
    # whatever it holds; only a fresh one gets the new default.
    three_pass_attribute_prompt_variant: PromptVariant = "michel2_full"
    three_pass_segmentation: SegmentationMode = "auto"
    three_pass_quoted_must_be_spoken: bool = True
    three_pass_unquoted_must_be_narrator: bool = True
    # Pass 2: when a batch runs out of retries and the ONLY failures are a
    # name the book never capitalises or a quoted line left unnamed, keep the
    # whole batch and flag just those lines, instead of halving it down to
    # them (#668: one refused label costs a halving per level). Off by default:
    # the A/B (#729) saved 17% of requests but could not separate its accuracy
    # effect from DeepSeek's run-to-run noise.
    three_pass_keep_whole_batch: bool = False
    # Pass 2: a line said by an unnamed member of a group on the cast list
    # (THE STUDENTS) takes the group's name instead of UNKNOWN
    # (attribution_prompt_variants.GROUP_RULE_7). Off by default: measured on
    # one light-novel chapter and five PDNC novels (#653), see that constant.
    three_pass_group_rule: bool = False
    # Pass 2: every answer also gives the speaker's gender, age group (as they
    # look and act) and an ageless flag (speaker_traits.py). Off by default;
    # measured in character_traits.json (+2.6% tokens, who-speaks unchanged).
    three_pass_speaker_traits: bool = False
    three_pass_model_profiles: Dict[str, ThreePassModelProfile] = Field(default_factory=dict)


class TextPromptPreset(BaseModel):
    """Named system/user text for one numbered generation pass."""
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(default="", max_length=100_000)
    user_prompt: str = Field(default="", max_length=100_000)


class PromptConfig(BaseModel):
    system_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    # name of the active attribution preset (a builtin variant name or a
    # user preset); generation.three_pass_attribute_prompt_variant is derived
    # from it on save so the CLI keeps working
    attribution_preset: str = Field(default="michel2_full", min_length=1, max_length=80)
    review_system_prompt: Optional[str] = None
    review_user_prompt: Optional[str] = None
    persona_system_prompt: Optional[str] = None
    persona_user_prompt: Optional[str] = None
    persona_advanced_prompt: Optional[str] = None
    persona_reference_chars: int = Field(default=12000, ge=12000, le=1_000_000)
    persona_allow_unknown_network_context: bool = False
    pass1_preset: str = Field(default="default", min_length=1, max_length=80)
    pass1_prompt_presets: List[TextPromptPreset] = Field(default_factory=list, max_length=50)
    pass3_preset: str = Field(default="default", min_length=1, max_length=80)
    pass3_prompt_presets: List[TextPromptPreset] = Field(default_factory=list, max_length=50)


class PromptPreset(BaseModel):
    """A named, user-editable attribution prompt: which variant (the prompt's
    shape) plus the texts the variant sends - system prompt, user
    instruction/template, and for michel2_shot the worked example."""
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    variant: PromptVariant = "default"
    system_prompt: str = Field(default="", max_length=100_000)
    user_prompt: str = Field(default="", max_length=100_000)
    example: str = Field(default="", max_length=100_000)
    builtin: bool = False


class AppConfig(BaseModel):
    llm: LLMConfig
    llm_mode: Literal["local", "remote"] = "local"
    llm_local: Optional[LLMConfig] = None
    llm_remote: Optional[LLMConfig] = None
    llm_remote_ssh: Optional[str] = None
    # When the active profile's retries run out (or it refuses on content
    # policy), switch this run to the OTHER profile for the rest of the run.
    # Off by default: the other profile may be a rented endpoint that bills.
    llm_failover: bool = False
    tts: TTSConfig
    prompts: Optional[PromptConfig] = None
    prompt_presets: List[PromptPreset] = Field(default_factory=list, max_length=50)
    generation: Optional[GenerationConfig] = None


def get_config_section_model(field):
    """Read section and nullability policy directly from a schema field."""
    annotation = field.annotation
    nullable = False
    if get_origin(annotation) in (Union, UnionType):
        arguments = get_args(annotation)
        nullable = type(None) in arguments
        members = [member for member in arguments if member is not type(None)]
        if len(members) == 1:
            annotation = members[0]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation, nullable
    return None, False


def _get_field_adapter(model: type[BaseModel], field_name: str) -> TypeAdapter:
    field = model.model_fields[field_name]
    annotation = field.annotation
    if field.metadata:
        annotation = Annotated.__class_getitem__((annotation, *field.metadata))
    return TypeAdapter(annotation)


def _migrate_presegment_quotes(config: dict) -> None:
    """The pass-1 knob was a bool, `three_pass_presegment_quotes` (and
    `presegment_quotes` per model profile), until 2026-09-18; it is now the
    three-way `segmentation`. A saved config keeps its meaning: True was the
    automatic rule, False was "always the model". The old key is dropped so
    GET /api/config never shows both."""
    gen = config.get("generation")
    if not isinstance(gen, dict):
        return
    if "three_pass_presegment_quotes" in gen:
        old = gen.pop("three_pass_presegment_quotes")
        gen.setdefault("three_pass_segmentation", "auto" if old is not False else "llm")
    profiles = gen.get("three_pass_model_profiles")
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict) and "presegment_quotes" in profile:
                old = profile.pop("presegment_quotes")
                if old is not None:
                    profile.setdefault("segmentation", "auto" if old else "llm")


def _validate_present_fields(section_name: str, data: dict,
                             model: type[BaseModel], warnings: list) -> dict:
    validated = dict(data)
    for field_name in model.model_fields:
        if field_name not in validated:
            continue
        try:
            validated[field_name] = _get_field_adapter(model, field_name).validate_python(
                validated[field_name]
            )
        except ValidationError:
            path = f"{section_name}.{field_name}"
            warning = ConfigWarning(path, "Invalid stored value ignored")
            warnings.append(warning)
            logger.warning("Invalid stored config value '%s', ignoring it", path)
            del validated[field_name]
    return validated


def load_app_config_result(path: str) -> AppConfigLoadResult:
    """Load and sanitize present known values without defaults or file writes."""
    warnings = []
    needs_backup = False
    if not os.path.exists(path):
        return AppConfigLoadResult({}, ())
    try:
        with open(path, "r", encoding="utf-8") as config_file:
            loaded = json.load(config_file)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Corrupted/unreadable JSON at %s, using defaults: %s", path, exc)
        return AppConfigLoadResult(
            {}, (ConfigWarning("$", "Configuration file could not be read"),), True
        )
    if not isinstance(loaded, dict):
        logger.warning("Invalid top-level config shape '%s', using defaults", type(loaded).__name__)
        return AppConfigLoadResult(
            {}, (ConfigWarning("$", "Configuration must be a JSON object"),), True
        )
    config = dict(loaded)
    _migrate_presegment_quotes(config)

    for field_name, field in AppConfig.model_fields.items():
        if field_name not in config:
            continue
        model, nullable = get_config_section_model(field)
        if model is not None:
            if nullable and config[field_name] is None:
                continue
            if not isinstance(config[field_name], dict):
                warnings.append(ConfigWarning(field_name, "Invalid section ignored"))
                needs_backup = True
                logger.warning("Invalid '%s' section in config, ignoring it", field_name)
                if nullable:
                    config[field_name] = None
                else:
                    del config[field_name]
                continue
            config[field_name] = _validate_present_fields(
                field_name, config[field_name], model, warnings)
            continue
        try:
            adapter = _get_field_adapter(AppConfig, field_name)
            validated = adapter.validate_python(config[field_name])
            # Collection fields such as prompt_presets remain JSON-native.
            config[field_name] = adapter.dump_python(validated, mode="json")
        except ValidationError:
            warnings.append(ConfigWarning(field_name, "Invalid stored value ignored"))
            logger.warning("Invalid stored config value '%s', ignoring it", field_name)
            del config[field_name]

    return AppConfigLoadResult(config, tuple(warnings), needs_backup)


def _to_plain(value):
    """Recursively convert validated models back to JSON-native data.

    WHY THIS EXISTS. The sanitizer validates some sections into pydantic
    models - `generation.three_pass_model_profiles` is a
    Dict[str, ThreePassModelProfile]. Ten modules load the config and write it
    back (the concurrency cache in llm_bench, personas, nicknames, review,
    project, ...), and every one of them raised
    "Object of type ThreePassModelProfile is not JSON serializable" as soon as
    a profile was configured. Running --dedupe-speakers on index18 hit it.

    three_pass_generate already worked around this locally with
    `as_profile_mapping`, whose docstring records the same class of crash. A
    per-module workaround leaves the other nine broken, so the conversion
    belongs here, where the data is produced.

    None-valued fields are dropped, preserving the semantics
    `as_profile_mapping` established: an unset profile key must fall through to
    the caller's default rather than override it with None.
    """
    if hasattr(value, "model_dump"):
        return {key: _to_plain(item)
                for key, item in value.model_dump().items()
                if item is not None}
    if isinstance(value, dict):
        return {key: _to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def load_app_config(path: str) -> dict:
    """Return the sanitized config data from :func:`load_app_config_result`.

    The result is JSON-native, so a caller may write it back unchanged.
    """
    return _to_plain(load_app_config_result(path).data)


def backup_damaged_app_config(path: str) -> str:
    """Create an atomic, metadata-preserving backup beside a damaged config."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = f"{path}.damaged-{stamp}-{uuid.uuid4().hex[:8]}.bak"
    temp_path = f"{backup_path}.tmp"
    try:
        shutil.copy2(path, temp_path)
        os.replace(temp_path, backup_path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return backup_path


def get_generation_config(config, model_override=None):
    """Return isolated run settings with one validated active generation model."""
    from lmstudio_settings import get_active_llm_config
    selected = (model_override if model_override is not None else
                get_active_llm_config(config).get(
                    "model_name", "richardyoung/qwen3-14b-abliterated:Q8_0"))
    if (not isinstance(selected, str) or not selected.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in selected)):
        raise ValueError("generation model must be a nonempty name without control characters")
    result = copy.deepcopy(config)
    active = {**get_active_llm_config(result), "model_name": selected.strip()}
    mode = result.get("llm_mode") or "local"
    result[f"llm_{mode}"] = active
    result["llm"] = copy.deepcopy(active)
    return result
