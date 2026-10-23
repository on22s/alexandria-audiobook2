import os
import copy
import ntpath
import zlib
import hashlib
import io
import re
import json
import logging
import tempfile
import sys
import threading
import time
import weakref
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import partial, wraps
from types import MethodType
import shutil
import uuid

import numpy as np
import soundfile as sf

import device_utils
from tts_diagnostics import (capture_tts_operation, measure_tts_stage, record_tts_audio,
                             record_tts_cache, record_tts_failure, measure_tts_stage_time,
                             record_tts_reference_cache)
import clone_reference_cache
from adapter_checkpoint_transaction import (ensure_adapter_generation_snapshot, get_adapter_generation_sha256,
                                            get_adapter_serving_metadata)
from lora_evidence import get_file_sha256, get_tts_model_settings
from audio_validation import GeneratedAudioError, publish_audio_output
from speech_text import normalize_for_speech, get_speech_preparation
from audio_generation_owner import (is_audio_generation_cancelled, ensure_audio_generation_active,
                                    AudioGenerationCancelled)
from synthesis_render import (ensure_synthesis_receipts, get_synthesis_window_plan,
                              render_synthesis_windows)
from synthesis_recovery_context import ensure_synthesis_recovery_batch_parent
from synthesis_settings import get_enabled_synthesis_recovery_settings
from speech_verification_settings import get_enabled_speech_verification_settings
from synthesis_input import (ensure_speech_input_capture, is_speech_input_capture_active,
    capture_speech_input, get_captured_speech_input, promote_speech_input, capture_composite_speech_input)
from synthesis_recovery_serving import (ensure_native_recovery_serving_current,
                                        SynthesisRecoveryDependencyChanged)
from pronunciation import ensure_occurrence_pronunciation_context, get_pronunciation_model_context
from pydub import AudioSegment
from audio_assembly import AudioSegmentBuilder
from export_limits import ensure_pause_allowed
from audio_pcm_producer import (get_pcm_source_shape, get_audio_duration_ms,
                                get_joined_pcm_shape, get_promoted_pcm_shape, get_silence_pcm_shape)
from voice_manifest import get_resolved_adapter_path, get_adapter_asset_snapshot
from external_tts_executor import ExternalTTSExecutor
from external_tts_client import (ExternalTTSClient, ExternalPrediction,
                                 ensure_external_request_control)

try:
    from .utils import secure_filename as _secure_filename, get_runtime_data_dir as _get_runtime_data_dir, is_path_inside
except ImportError:
    from utils import secure_filename as _secure_filename, get_runtime_data_dir as _get_runtime_data_dir, is_path_inside



# Torch RNG and loaded model operations are shared across local worker threads,
# including distinct engine instances. Reentrancy permits prompt/load helpers
# and fallback generation to use the same admission as their outer render.
_LOCAL_TTS_LOCK = threading.RLock()
_EXTERNAL_RUNTIME_LOCK = threading.Lock()


def create_external_client(url, timeout):
    return ExternalTTSClient(url, timeout)


def _close_external_runtime(lock, calls, clients, executor):
    with lock:
        cancelled = list(calls)
        cached = list(clients.values())
    for event in cancelled:
        event.set()
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    failure = None
    for client in cached:
        close = getattr(client, "close", None)
        if callable(close):
            try:
                close()
            except Exception as error:
                failure = failure or error
    if failure is not None:
        raise failure
    if executor is not None:
        # Closing the owned SDK wakes workers into diagnostics and private-file
        # cleanup. Keep that work alive until it finishes, before finalization
        # can tear down buffered output. A stuck caller retains failed ownership.
        executor.shutdown(wait=True, cancel_futures=True, timeout=2)


@dataclass(frozen=True)
class TTSFailure:
    category: str
    detail: str
    next_action: str

    def get_message(self):
        return f"{self.detail} {self.next_action}"


@dataclass(frozen=True)
class TTSGenerationResult:
    success: bool
    failure: TTSFailure | None = None
    synthesis_receipt: dict | None = None
    speech_input: dict | None = None


_TTS_FAILURES = ContextVar("tts_generation_failures", default=None)


def capture_batch_speech_inputs(render):
    @wraps(render)
    def wrapped(self, chunks, voice_config, output_dir, batch_seed=-1, *, capture_input=False):
        enabled = capture_input is True or bool(getattr(self, '_speech_verification', None))
        with ensure_speech_input_capture(enabled):
            result = render(self, chunks, voice_config, output_dir, batch_seed)
            if not enabled:
                return result
            inputs = {}
            for index in result['completed']:
                record = get_captured_speech_input(os.path.join(output_dir, f'temp_batch_{index}.wav'))
                if record is not None:
                    inputs[index] = record
            return {**result, 'speech_inputs': inputs}
    return wrapped


def apply_tts_failure(error, category=None, next_action=None):
    """Record a terminal failure only for the current structured render call."""
    failures = _TTS_FAILURES.get()
    if category is None:
        if isinstance(error, UnsupportedVoiceBackendError):
            category = "unsupported_backend"
            next_action = "Select local TTS for this voice or choose a supported external voice."
        elif device_utils.is_allocation_failure(error):
            category = "out_of_memory"
            next_action = "Free GPU memory or reduce the workload before retrying."
        elif isinstance(error, GeneratedAudioError):
            category = "invalid_audio"
            next_action = "Check the TTS output and server logs before retrying."
        elif isinstance(error, FileNotFoundError):
            category = "missing_asset"
            next_action = "Restore the missing voice/model asset before retrying."
        elif isinstance(error, TimeoutError):
            category = "timeout"
            next_action = "Check the TTS server and its logs before retrying."
        else:
            category = "generation_error"
    record_tts_failure(category)
    if failures is None:
        return
    from diagnostics import redact_text
    detail = redact_text(str(error) or type(error).__name__)
    failures.append(TTSFailure(category, detail,
        next_action or "Check the TTS logs and voice settings before retrying."))


def ensure_local_tts_serialized(method):
    """Serialize local model loading, prompt construction and seeded inference."""
    @wraps(method)
    def run(*args, **kwargs):
        with _LOCAL_TTS_LOCK:
            owner = args[0] if args else None
            tracked = hasattr(owner, '_local_model_active_leases')
            if tracked:
                owner._local_model_active_leases += 1
            try:
                return method(*args, **kwargs)
            finally:
                if tracked:
                    owner._local_model_active_leases -= 1
    return run


def _resolve_asset_path(rel_path):
    """Resolve a voice-config relative path (adapter_path / ref_audio) or an
    output subdir to an absolute path, honoring ALEXANDRIA_DATA_DIR.

    Built-in adapters ship read-only at the repo root; all other voice data
    (lora_models, clone_voices, designed_voices) lives under the runtime data
    dir. A no-op when the data dir IS the repo root (the default layout), so
    existing installs are unaffected — it only corrects paths once data is
    relocated (e.g. the Docker /alexandria/runtime volume)."""
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = os.fspath(rel_path)
    norm = raw.replace("\\", "/")
    if (not norm or norm.startswith("/") or ntpath.splitdrive(norm)[0]
            or ".." in norm.split("/")):
        raise ValueError("TTS relative asset path must not be absolute or traverse parents")
    norm = os.path.normpath(norm)
    if norm == ".":
        raise ValueError("TTS relative asset path must name an asset")
    base = root_dir if norm.split(os.sep, 1)[0] == "builtin_lora" else _get_runtime_data_dir(root_dir)
    path = os.path.join(base, norm)
    if not is_path_inside(path, base):
        raise ValueError("TTS relative asset path escapes its data root")
    return path


DEFAULT_PAUSE_MS = 500  # Pause between different speakers
SAME_SPEAKER_PAUSE_MS = 250  # Shorter pause for same speaker continuing


def get_style_timeline_index(point):
    """Read a nonnegative integer timeline position, rejecting malformed data."""
    value = point.get("from_index", 0)
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("style_timeline from_index must be a nonnegative integer") from exc
    if isinstance(value, bool) or index < 0 or (not isinstance(value, str) and value != index):
        raise ValueError("style_timeline from_index must be a nonnegative integer")
    return index


def active_character_style(voice_data, chunk_index=None):
    """The identity anchor in force at a line: the entry's `character_style`,
    replaced by the last `style_timeline` point at or before `chunk_index`.
    A point is {"from_index": N, "character_style": "..."} - a character can
    change from a place in the book (a time skip, an aged character) without
    becoming a different speaker. No index -> the base anchor."""
    data = voice_data or {}
    style = (data.get("character_style") or data.get("default_style") or "").strip()
    if chunk_index is None:
        return style
    points = [(get_style_timeline_index(point), point)
              for point in data.get("style_timeline") or [] if isinstance(point, dict)]
    for index, point in sorted(points, key=lambda item: item[0]):
        if index <= chunk_index:
            style = (point.get("character_style") or "").strip()
    return style


# Bookkeeping a voice version carries that must never overlay the entry it is
# applied to: its own label, and the entry's version/timeline structure.
VERSION_OVERLAY_EXCLUDED = ("age_group", "versions", "version_timeline", "style_timeline",
                            "candidates", "active_version", "active_candidate",
                            # a state version's own review bookkeeping (#1040 review C4)
                            "persona_state", "persona_status", "voice_status", "persona_voice_audit")


def get_version_fields(voice_data, version_id):
    """-> the fields one of a character's voice versions puts over its entry,
    or None when the version does not exist. One overlay for the whole-book
    select route and the per-chunk version timeline (Rule 15)."""
    version = ((voice_data or {}).get("versions") or {}).get(version_id)
    if not isinstance(version, dict):
        return None
    return {key: value for key, value in version.items() if key not in VERSION_OVERLAY_EXCLUDED}


def active_version_id(voice_data, chunk_index):
    """The version a `version_timeline` puts in force at a chunk, or None.
    A point is {"from_index": N, "version_id": "..."}: from chunk N on, the
    character speaks with that version (a settled age or gender state, #653)."""
    if chunk_index is None:
        return None
    current = None
    points = [(get_style_timeline_index(point), point)
              for point in (voice_data or {}).get("version_timeline") or [] if isinstance(point, dict)]
    for index, point in sorted(points, key=lambda item: item[0]):
        if index <= chunk_index:
            current = point.get("version_id")
    return current


def get_resolved_speaker(speaker, voice_config):
    """Follow speaker aliases, refusing cycles or an excessive chain."""
    name, seen = speaker, set()
    for _ in range(16):
        if name in seen:
            raise ValueError(f"Alias cycle detected for speaker '{speaker}'")
        seen.add(name)
        entry = voice_config.get(name) or {}
        alias = entry.get("alias_of") or entry.get("alias")
        if not isinstance(alias, str) or not alias.strip():
            return name
        name = alias
    raise ValueError(f"Alias chain for '{speaker}' exceeded 16 iterations")


def voice_config_for_chunk(voice_config, speaker, chunk_index):
    """Apply selected versions before walking their members and aliases.
    Every reachable voice uses the containing line's source index."""
    if not isinstance((voice_config or {}).get(speaker), dict):
        return voice_config
    pending, seen, updates = [speaker], set(), {}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        data = voice_config.get(name)
        if not isinstance(data, dict):
            continue
        version_id = active_version_id(data, chunk_index) if data.get("version_timeline") else None
        fields = get_version_fields(data, version_id) if version_id else None
        if fields:
            data = {**data, **fields}
            updates[name] = data
        if data.get("style_timeline"):
            data = {**data, "character_style": active_character_style(data, chunk_index)}
            updates[name] = data
        alias = data.get("alias_of") or data.get("alias")
        if isinstance(alias, str) and alias.strip():
            pending.append(alias)
        if voice_category(data) == "ensemble" and isinstance(data.get("members"), list):
            pending.extend(member for member in data["members"] if isinstance(member, str))
    return {**voice_config, **updates} if updates else voice_config


def get_chunk_voice_inputs(voices, chunk, index, resolve_alias=None):
    """Read effective inputs with the selected root first, then member voices."""
    resolve_alias = resolve_alias or get_resolved_speaker
    speaker = resolve_alias(chunk.get("speaker"), voices)
    effective = voice_config_for_chunk(
        resolve_narrator_voice_config(speaker, voices, chunk), speaker, index)
    fields = ("type", "voice", "character_style", "default_style", "seed",
              "ref_audio", "ref_text", "description", "adapter_id", "adapter_path",
              "adapter_generation_sha256", "members")
    inputs, pending = {}, [speaker]
    while pending:
        name = resolve_alias(pending.pop(), effective)
        if name in inputs:
            continue
        config = effective.get(name) or {}
        inputs[name] = {field: copy.deepcopy(config.get(field)) for field in fields}
        if voice_category(config) == "ensemble":
            pending.extend(member for member in config.get("members") or [] if isinstance(member, str))
    return inputs


def get_pronunciation_render_context(voices, chunk, index, model_settings, *, source_speaker=None):
    """Identify the selected root/group assignment and actual speech mode."""
    inputs = get_chunk_voice_inputs(voices, chunk, index)
    identity = {name: {key: value for key, value in fields.items()
                       if key not in {"character_style", "default_style", "seed"}}
                for name, fields in inputs.items()}
    encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8", errors="surrogatepass")
    context = {"speaker": chunk.get("speaker") if source_speaker is None else source_speaker,
               "voice_sha256": hashlib.sha256(encoded).hexdigest(),
               **get_pronunciation_model_context(model_settings)}
    return {key: value if isinstance(value, str) and len(value) <= 256 else None
            for key, value in context.items()}


CHUNK_SEED_PRIME = 1_000_003


def get_chunk_seed(text, take=0):
    """Return a stable, non-negative seed for normalized text and a take."""
    normalized = " ".join(str(text).split())
    return (zlib.crc32(normalized.encode("utf-8")) + int(take) * CHUNK_SEED_PRIME) % 2**31


def get_reproducible_voice_config(resolved, seed, *, take=0):
    """Copy voices, retaining pinned originals and offsetting numbered takes."""
    reproducible = {}
    for name, data in resolved.items():
        if not isinstance(data, dict):
            reproducible[name] = copy.deepcopy(data)
            continue
        entry = copy.deepcopy(data)
        try:
            current_seed = int(entry.get("seed", -1))
        except (TypeError, ValueError):
            current_seed = -1
        if current_seed < 0:
            entry["seed"] = seed
        elif int(take) > 0:
            entry["seed"] = (current_seed + int(take) * CHUNK_SEED_PRIME) % 2**31
        reproducible[name] = entry
    return reproducible


def get_batch_voice_config(voice_config, speaker, chunk_index, batch_seed):
    """Override seeds after timeline selection without mutating stored voices."""
    resolved = voice_config_for_chunk(voice_config, speaker, chunk_index)
    if batch_seed < 0:
        return resolved
    return {name: {**data, "seed": batch_seed} if isinstance(data, dict) else data
            for name, data in resolved.items()}


def anchored_instruct(voice_data, instruct_text):
    """CustomVoice: the identity anchor first, the line's own emotion after.
    Measured 2026-09-18 (custom_voice_instruct_drift, 120 real lines, Ryan):
    the per-line instructs alone wander 3.5 semitones in pitch over a run at
    ECAPA 0.74 to the voice's own opening; a constant anchor in front of them
    holds it to 2.5 st at 0.77 (anchor alone: 2.4 st, 0.82, but no emotion).
    This path ignored `character_style` entirely before."""
    anchor = (voice_data.get("character_style") or "").strip()
    line = (instruct_text or "").strip()
    if anchor and line:
        return f"{anchor} {line}"
    return anchor or line or (voice_data.get("default_style") or "").strip() or "neutral"


@dataclass(frozen=True)
class CustomVoiceRequest:
    voice: str
    seed: int
    instruct: str


def get_custom_voice_request(voice_config, speaker, instruct_text):
    """The actual native speaker, seed and anchored instruction selection."""
    voice_data = voice_config.get(speaker)
    if not voice_data:
        return None
    return CustomVoiceRequest(voice_data.get("voice", "Ryan"), int(voice_data.get("seed", -1)),
                              anchored_instruct(voice_data, instruct_text))


def voice_is_set(voice_data):
    """Does this character have a voice yet? True for any assigned LoRA /
    clone / designed / ensemble voice, and for a custom entry that carries a
    persona (a description or reference audio). False for the bare default
    the Voices tab writes for every character as soon as it renders - which
    is why "has an entry in voice_config.json" is not the test (#602: that
    made "only characters without a voice" select nobody)."""
    data = voice_data or {}
    if voice_category(data) != "custom":
        return True
    return bool((data.get("description") or "").strip() or data.get("ref_audio"))


def is_voice_config_present(speaker, voice_config):
    """Whether single and batch generation have an entry for this speaker."""
    return bool((voice_config or {}).get(speaker))


def voice_category(voice_data):
    """Normalize a voice config entry's type into a routing category.

    Collapses the "lora"/"builtin_lora" pair into a single "lora" category so
    the membership test isn't duplicated at every call site. Returns one of
    "clone", "lora", "design", "ensemble", or "custom".
    """
    voice_type = (voice_data or {}).get("type", "custom")
    if voice_type == "clone":
        return "clone"
    if voice_type in ("lora", "builtin_lora"):
        return "lora"
    if voice_type == "design":
        return "design"
    if voice_type == "ensemble":
        return "ensemble"
    return "custom"


def resolve_narrator_voice_config(speaker, voice_config, chunk=None):
    """Resolve an optional dynamic narrator strategy for one chunk.

    Strategies are opt-in and fall back to the configured NARRATOR voice when
    the chunk has no matching focus/version metadata.
    """
    if str(speaker or "").upper() != "NARRATOR":
        return voice_config
    narrator = voice_config.get("NARRATOR") or voice_config.get("Narrator") or {}
    strategy = narrator.get("narrator_strategy", "global")
    chunk = chunk or {}
    selected = None
    if strategy in {"focus", "character", "character_gender", "character_age", "character_gender_age"}:
        focus = chunk.get("focus_speaker") or chunk.get("character_focus")
        if focus and focus in voice_config:
            focus_voice = voice_config[focus]
            wanted_gender = chunk.get("narrator_gender") or chunk.get("focus_gender")
            wanted_age = chunk.get("narrator_age_group") or chunk.get("focus_age_group")
            gender_ok = strategy not in {"character_gender", "character_gender_age"} or not wanted_gender or focus_voice.get("gender") == wanted_gender
            age_ok = strategy not in {"character_age", "character_gender_age"} or not wanted_age or focus_voice.get("age_group") == wanted_age
            if gender_ok and age_ok:
                selected = focus_voice
    elif strategy == "chapter":
        version_id = chunk.get("narrator_version") or chunk.get("chapter_narrator_version")
        selected = (narrator.get("versions") or {}).get(version_id) if version_id else None
        if selected is None:
            selected = narrator
    elif strategy not in {"gender", "age", "gender_age"}:
        selected = narrator
    if selected is None and strategy in {"gender", "age", "gender_age"}:
        wanted_gender = chunk.get("narrator_gender") or chunk.get("focus_gender")
        wanted_age = chunk.get("narrator_age_group") or chunk.get("focus_age_group")
        for version in (narrator.get("versions") or {}).values():
            if not isinstance(version, dict):
                continue
            gender_ok = strategy == "age" or not wanted_gender or version.get("gender") == wanted_gender
            age_ok = strategy == "gender" or not wanted_age or version.get("age_group") == wanted_age
            if gender_ok and age_ok:
                selected = version
                break
    if not isinstance(selected, dict):
        selected = narrator
    if selected is narrator:
        return voice_config
    resolved = dict(voice_config)
    resolved["NARRATOR"] = dict(selected)
    resolved[speaker] = resolved["NARRATOR"]
    return resolved


def mix_to_unison(wav_paths, output_path, max_stretch=1.35):
    """Align same-text clips to a common length and mix them into one track.

    For an "ensemble" speaker, each member renders the same line separately, so
    the clips differ in length and prosody. Time-stretching every clip to the
    LONGEST one makes them start and end together (nobody is sped up). Words
    still drift mid-line — this is a chorus/doubling effect, not true unison.
    Word-level alignment would need forced alignment and is out of scope.

    A clip needing more than `max_stretch` is left alone: stretching that far
    smears it audibly, and letting it drift sounds better than destroying it.

    Returns True on success.
    """
    import librosa

    clips, rates = [], set()
    for path in wav_paths:
        audio, sr = sf.read(path)
        if not len(audio):
            raise ValueError(f"ensemble clip has no audio frames: {path}")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)  # force mono before mixing
        clips.append(np.asarray(audio, dtype=np.float32))
        rates.add(sr)
    if not clips:
        raise ValueError("mix_to_unison needs at least one clip")
    if len(rates) > 1:
        raise ValueError(f"cannot mix clips with different sample rates: {sorted(rates)}")
    sample_rate = rates.pop()

    target_len = max(len(c) for c in clips)
    aligned = []
    for clip in clips:
        stretch = target_len / len(clip)
        if stretch > max_stretch:
            print(f"  Unison: clip is {stretch:.2f}x shorter than the longest "
                  f"(> {max_stretch}x); leaving it unstretched to avoid smearing")
            out = clip
        else:
            # librosa rate>1 speeds up; to reach target_len use len/target_len.
            out = librosa.effects.time_stretch(clip, rate=len(clip) / target_len)
        if len(out) < target_len:
            out = np.pad(out, (0, target_len - len(out)))
        aligned.append(out[:target_len])

    # Power-preserving sum: /N would duck the line, straight sum would clip.
    mixed = np.sum(aligned, axis=0) / np.sqrt(len(aligned))

    # Match the loudest input so the line neither jumps out nor hides.
    loudest = max(float(np.abs(c).max()) for c in clips)
    peak = float(np.abs(mixed).max())
    if peak > 0 and loudest > 0:
        mixed = mixed * (loudest / peak)

    # The ensemble path writes directly rather than through _save_wav, so it
    # needs the same check - a mixed file can be truncated exactly as a
    # generated one can.
    from audio_validation import save_generated_wav
    save_generated_wav(mixed, sample_rate, output_path, "ensemble mix")
    return True


def sanitize_filename(name):
    """Make a string safe for use in filenames. Uses secure_filename to prevent path
    traversal, then collapses remaining spaces/dots/etc. to underscores to match the
    naming convention existing on-disk files were written with."""
    safe = _secure_filename(name) or "unnamed"
    return re.sub(r'[^\w\-]', '_', safe).lower()


# Dead air the model emits around each line. Measured 2026-09-18 over 6,550
# LoRA-path lines (ab_test_runtime/chapter_audio + voice_drift): median
# 310-340 ms of leading silence (p90 540-600 ms) and 80 ms trailing, i.e.
# 4-5% of a book, ~2.5-2.9 minutes per hour - and it sits on top of the
# configured pause, so a 250 ms same-speaker gap was really ~670 ms. Trimmed
# at join time (not at generation, so nothing on disk changes and the
# editor's per-line files are untouched), keeping a short head and tail so no
# onset is clipped. The CustomVoice path emits ~80 ms and is unaffected.
EDGE_SILENCE_THRESHOLD_DBFS = -45.0
EDGE_SILENCE_KEEP_HEAD_MS = 40
EDGE_SILENCE_KEEP_TAIL_MS = 80
_EDGE_SILENCE_STEP_MS = 10


class ExportCancelled(Exception):
    """Raised when the caller cancels audio assembly or export."""


def ensure_audio_export_active(cancel_check):
    if cancel_check and cancel_check():
        raise ExportCancelled('Export cancelled')


def _silent_edge_ms(segment, from_end, threshold_db, cancel_check=None):
    """Milliseconds of silence at the start (or end) of a segment."""
    length = len(segment)
    silent = 0
    while silent < length:
        ensure_audio_export_active(cancel_check)
        start = length - silent - _EDGE_SILENCE_STEP_MS if from_end else silent
        slice_ = segment[max(0, start):max(0, start) + _EDGE_SILENCE_STEP_MS]
        if len(slice_) == 0 or slice_.dBFS > threshold_db:
            break
        silent += _EDGE_SILENCE_STEP_MS
    return min(silent, length)


def trim_edge_silence(segment, threshold_db=EDGE_SILENCE_THRESHOLD_DBFS,
                      keep_head_ms=EDGE_SILENCE_KEEP_HEAD_MS,
                      keep_tail_ms=EDGE_SILENCE_KEEP_TAIL_MS, cancel_check=None):
    """-> the segment with leading/trailing silence cut back to keep_head_ms /
    keep_tail_ms. A segment that is silent throughout is returned unchanged
    (a missing line must stay visible as its full length, not vanish)."""
    ensure_audio_export_active(cancel_check)
    if segment is None or len(segment) == 0:
        return segment
    lead = _silent_edge_ms(segment, False, threshold_db, cancel_check)
    if lead >= len(segment):
        return segment
    tail = _silent_edge_ms(segment, True, threshold_db, cancel_check)
    start = max(0, lead - keep_head_ms)
    end = len(segment) - max(0, tail - keep_tail_ms)
    return segment[start:end] if (start, end) != (0, len(segment)) else segment


def get_pause_duration_ms(previous_chunk, next_chunk, pause_ms=DEFAULT_PAUSE_MS,
                          same_speaker_pause_ms=SAME_SPEAKER_PAUSE_MS):
    """Return the pause rule, rejecting values above the shared export limit."""
    override = previous_chunk.get("pause_after")
    if override is not None:
        duration = int(override)
    elif next_chunk.get("speaker") == previous_chunk.get("speaker"):
        duration = same_speaker_pause_ms
    else:
        duration = pause_ms
    ensure_pause_allowed(duration)
    return duration


def combine_audio_with_pauses(audio_segments, speakers, pause_ms=DEFAULT_PAUSE_MS,
                              same_speaker_pause_ms=SAME_SPEAKER_PAUSE_MS,
                              pause_overrides=None, cancel_check=None):
    """Combine audio segments with pauses between them.

    Args:
        pause_overrides: Optional list aligned with audio_segments. Each entry is
            the pause (ms) to insert *after* that segment, or None to use the
            default speaker-change logic. The last entry is ignored.
    """
    if not audio_segments:
        return None

    ensure_audio_export_active(cancel_check)
    audio_segments = [trim_edge_silence(s, cancel_check=cancel_check) for s in audio_segments]
    prev_speaker = speakers[0]
    if len(audio_segments) == 1:
        ensure_audio_export_active(cancel_check)
        return audio_segments[0]
    combined = AudioSegmentBuilder(audio_segments[0])

    for i, (segment, speaker) in enumerate(zip(audio_segments[1:], speakers[1:])):
        ensure_audio_export_active(cancel_check)
        override = pause_overrides[i] if pause_overrides else None
        gap = AudioSegment.silent(duration=get_pause_duration_ms(
            {"speaker": prev_speaker, "pause_after": override}, {"speaker": speaker},
            pause_ms, same_speaker_pause_ms))
        combined.append(gap + segment)
        prev_speaker = speaker

    combined = combined.finish()
    ensure_audio_export_active(cancel_check)
    return combined


def compute_timeline(chunks_with_audio, pause_ms=DEFAULT_PAUSE_MS,
                     same_speaker_pause_ms=SAME_SPEAKER_PAUSE_MS, cancel_check=None):
    """Compute a timeline of (chunk, segment, abs_start_ms) tuples.

    Args:
        chunks_with_audio: list of (chunk_dict, AudioSegment) tuples.
            Each chunk_dict may have an optional 'pause_after' key (int ms)
            that overrides the default pause inserted after that chunk.
        pause_ms: Default pause between different speakers.
        same_speaker_pause_ms: Default pause when same speaker continues.

    Returns:
        list of (chunk_dict, AudioSegment, abs_start_ms) tuples.
    """
    return list(iter_timeline(chunks_with_audio, pause_ms, same_speaker_pause_ms,
                              cancel_check=cancel_check))


def iter_timeline(chunks_with_audio, pause_ms=DEFAULT_PAUSE_MS,
                  same_speaker_pause_ms=SAME_SPEAKER_PAUSE_MS, cancel_check=None,
                  already_trimmed=False):
    """Yield the existing trimmed timeline without retaining preceding audio."""
    cursor_ms = 0
    prefix_shape = None
    prev_chunk = None

    for chunk, segment in chunks_with_audio:
        ensure_audio_export_active(cancel_check)
        if not already_trimmed:
            segment = trim_edge_silence(segment, cancel_check=cancel_check)   # same cut the combined audio gets
        segment_shape = get_pcm_source_shape(segment)
        if prev_chunk is not None:
            pause = get_pause_duration_ms(
                prev_chunk, chunk, pause_ms, same_speaker_pause_ms)
            # Mirror combine's (gap + segment), then prefix promotion. The
            # integer PCM clock retains fractional milliseconds across a book.
            piece = get_joined_pcm_shape(get_silence_pcm_shape(pause), segment_shape)
            joined = get_joined_pcm_shape(prefix_shape, piece)
            if joined is not None:
                promoted = get_promoted_pcm_shape(segment_shape, joined[0])
                cursor_ms = 1000 * (joined[1] - promoted[1]) / joined[0][1]
            else:
                cursor_ms += pause
            prefix_shape = joined
        else:
            prefix_shape = segment_shape

        yield chunk, segment, cursor_ms
        cursor_ms = (1000 * prefix_shape[1] / prefix_shape[0][1]
                     if prefix_shape is not None else cursor_ms + get_audio_duration_ms(segment))
        prev_chunk = chunk
        del segment



class UnsupportedVoiceBackendError(ValueError):
    """The selected backend cannot serve this voice category."""


from tts_capabilities import get_tts_backend_capabilities


def get_memory_bounded_codec_decode(decode):
    """Keep Qwen's normal decode; bound only its activation peak after OOM."""
    @wraps(decode)
    def decode_with_recovery(codes, chunk_size=300, left_context_size=25):
        import gc
        import torch

        while True:
            try:
                return decode(codes, chunk_size=chunk_size, left_context_size=left_context_size)
            except torch.OutOfMemoryError:
                if chunk_size <= 25:
                    raise
                next_size = max(25, min(chunk_size, codes.shape[-1]) // 2)
                if next_size >= chunk_size:
                    raise
                print(f"Audio codec out of memory; retrying the same codes in {next_size}-frame chunks.")
            # Leaving the exception scope releases its traceback/activation tensors.
            gc.collect()
            torch.cuda.empty_cache()
            chunk_size = next_size
    return decode_with_recovery


class TTSEngine:
    """TTS engine supporting local (qwen-tts) and external (Gradio) backends.

    Mode is determined by config["tts"]["mode"]:
      - "local": Loads Qwen3TTSModel directly. No external server needed.
      - "external": Connects via Gradio client to a running TTS server.

    Models and clients are lazily initialized on first use.
    """

    _LOCAL_MODEL_IDS = {
        "custom": "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice",
        "clone": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        "design": "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        "lora": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    }

    def __init__(self, config):
        model_settings = get_tts_model_settings(config)
        tts_config = config.get("tts", {})
        self._mode = model_settings["mode"]
        self._synthesis_windows = copy.deepcopy(model_settings.get("synthesis_windows"))
        # Recovery changes execution/retention, never the acoustic model or LoRA
        # evidence identity returned by get_model_settings.
        self._synthesis_recovery = get_enabled_synthesis_recovery_settings(tts_config.get("synthesis_recovery"))
        self._speech_verification = get_enabled_speech_verification_settings(tts_config.get('speech_verification'))
        self._url = tts_config.get("url", "http://127.0.0.1:7860")
        # Each endpoint has one lazily created client per configured worker.
        # Every client keeps its own lock: gradio_client cannot be shared
        # concurrently. `url` alone is the one-endpoint pool.
        self._external_urls = [u.strip() for u in (tts_config.get("external_urls") or []) if u and u.strip()] or [self._url]
        self._external_timeout = int(tts_config.get("external_timeout_seconds", 300) or 300)
        self._external_parallel_workers = max(1, int(tts_config.get("parallel_workers", 2) or 1))
        self._device = model_settings["device"]
        self._compile_codec_enabled = model_settings["compile_codec"]

        # Cap on generated audio tokens per sequence. One source for both the
        # VRAM estimate and every generate call — if they ever disagree, the
        # estimate under-counts memory per sequence and over-sizes the batch.
        self._max_new_tokens = model_settings["max_new_tokens"]

        # Language setting (passed to Qwen3-TTS)
        self._language = model_settings["language"]

        # Sub-batching config
        self._sub_batch_enabled = tts_config.get("sub_batch_enabled", True)
        self._sub_batch_min_size = max(1, tts_config.get("sub_batch_min_size", 4))
        self._sub_batch_ratio = max(1.0, float(tts_config.get("sub_batch_ratio", 5)))
        self._sub_batch_max_items = int(tts_config.get("sub_batch_max_items", 0))  # 0 = auto

        # Lazy-loaded backends (guarded by _model_lock to prevent concurrent loads)
        self._model_lock = threading.Lock()
        self._local_model_active_leases = 0
        self._local_model_devices = {}
        self._local_release_pending_devices = set()
        self._local_release_pending_families = set()
        self._local_model_release_events = []
        self._local_custom_model = None
        self._local_clone_model = None
        self._local_design_model = None
        self._local_lora_model = None
        self._custom_warmup_needed = True
        self._lora_adapter_path = None  # track which adapter is currently loaded
        self._lora_generation_sha256 = None
        self._gradio_clients = {}       # (url, worker) -> gradio_client.Client
        self._external_locks = {}       # (url, worker) -> threading.Lock
        self._external_pool_lock = threading.Lock()
        self._external_next = 0
        self._external_client_next = {}
        self._external_executor = None
        self._external_calls = set()
        self._external_closed = False

        self._prepared_reference_cache = clone_reference_cache.ReferenceLRU(
            clone_reference_cache.PREPARED_MAX_ENTRIES, clone_reference_cache.PREPARED_MAX_BYTES)
        self._clone_prompt_cache = clone_reference_cache.ReferenceLRU(
            clone_reference_cache.PROMPT_MAX_ENTRIES, clone_reference_cache.PROMPT_MAX_BYTES)
        self._clone_prompt_model_ref = None
        # LoRA clone prompt cache: (adapter_path, serving generation) -> reusable prompt
        self._lora_prompt_cache = {}

    @property
    def mode(self):
        return self._mode

    @staticmethod
    def _concat_audio(wav):
        """Concatenate audio array(s) into a single numpy array."""
        if isinstance(wav, list):
            return np.concatenate(wav) if len(wav) > 1 else wav[0]
        return wav

    def get_memory_device(self, model=None):
        """Return the model's CUDA device, or the configured device before loading."""
        device = getattr(model, "device", None)
        if device is None:
            device = self._resolve_device()
        return device if str(device).startswith("cuda") else None

    def _vram_snapshot(self, label=""):
        """Log and return current VRAM state in GB (allocated/reserved/free/total)."""
        import torch
        if not torch.cuda.is_available():
            return {}
        device = self.get_memory_device()
        if device is None:
            return {}
        alloc = torch.cuda.memory_allocated(device) / 1e9
        reserved = torch.cuda.memory_reserved(device) / 1e9
        free, total = torch.cuda.mem_get_info(device)
        free /= 1e9
        total /= 1e9
        snap = {"allocated_gb": round(alloc, 2), "reserved_gb": round(reserved, 2),
                "free_gb": round(free, 2), "total_gb": round(total, 1)}
        tag = f"[{label}] " if label else ""
        print(f"VRAM {tag}{alloc:.2f} alloc / {reserved:.2f} reserved / {free:.2f} free / {total:.1f} total GB")
        return snap

    @staticmethod
    def _clear_gpu_cache():
        """Free GPU memory: garbage-collect Python objects, then clear CUDA cache.
        A no-op when torch was never imported - external mode holds no GPU
        memory and should not pull torch in just to empty an unused cache."""
        import gc
        gc.collect()
        torch = sys.modules.get("torch")
        if torch is not None:
            torch.cuda.empty_cache()

    @staticmethod
    def _reset_compile_cache():
        """Reset torch.compile dynamo state to prevent guard accumulation.

        torch.compile(dynamic=True) accumulates shape guards across calls.
        With varying batch sizes and sequence lengths, the guard list grows
        and CPU-side guard evaluation becomes a bottleneck, causing
        progressive throughput degradation.  Resetting clears all in-memory
        guards; the next call pays a one-time recompilation cost (fast due
        to inductor disk cache) but prevents the slowdown from compounding.

        Only applied on ROCm (AMD GPUs). On NVIDIA, max-autotune mode
        re-benchmarks all kernel variants after each reset, and the
        benchmarking cost scales with tensor size — causing worse slowdown
        than the guard accumulation it prevents.
        """
        import torch
        if not (hasattr(torch.version, "hip") and torch.version.hip):
            return  # skip on NVIDIA/CPU — recompilation cost outweighs benefit
        torch._dynamo.reset()

    def _estimate_max_batch_size(self, model, clone_prompt_tokens=0,
                                ref_text_chars=0, max_text_chars=0):
        """Estimate how many sequences fit in free VRAM based on KV cache math.

        Uses the talker's architecture (num_layers, num_kv_heads, head_dim) to
        calculate KV cache bytes per token, then estimates total tokens per
        sequence from clone prompt size + text length + max generation length.

        The generation length is read from self._max_new_tokens - the same value
        the generate calls use - rather than taken as an argument, so the two
        cannot drift apart and silently over-size the batch.

        Returns max batch size (>= 1). CPU use has no VRAM limit; an unreadable
        GPU model config conservatively allows one sequence.
        """
        max_new_tokens = self._max_new_tokens
        import torch
        if not torch.cuda.is_available():
            return 9999

        try:
            config = model.model.talker.config
            num_layers = config.num_hidden_layers
            num_kv_heads = config.num_key_value_heads
            head_dim = config.hidden_size // config.num_attention_heads
        except AttributeError:
            return 1  # cannot size GPU memory safely without model dimensions

        device = self.get_memory_device(model)
        if device is None:
            return 9999

        dtype_bytes = 2  # bf16
        kv_per_token = num_layers * 2 * num_kv_heads * head_dim * dtype_bytes

        # Total tokens per sequence (worst case: padded to longest + full generation)
        overhead = 10  # role tokens + prefix + special tokens
        ref_text_tokens = ref_text_chars // 3 if ref_text_chars else 0
        text_tokens = max_text_chars // 3 if max_text_chars else 0
        total_tokens = overhead + clone_prompt_tokens + ref_text_tokens + text_tokens + max_new_tokens

        # Overhead factor covers prefill activations, codec, allocator fragmentation
        OVERHEAD_FACTOR = 2.0
        mem_per_seq = total_tokens * kv_per_token * OVERHEAD_FACTOR

        # Available = driver-level free + PyTorch reserved-but-unallocated
        free_driver, _ = torch.cuda.mem_get_info(device)
        reserved_unused = torch.cuda.memory_reserved(device) - torch.cuda.memory_allocated(device)
        free_total = free_driver + reserved_unused

        budget = int(free_total * 0.8)
        max_batch = max(1, int(budget // mem_per_seq))

        print(f"VRAM estimate: {free_total / 1e9:.1f}GB free, "
              f"{total_tokens} tok/seq ({clone_prompt_tokens} prompt + "
              f"{ref_text_tokens + text_tokens} text + {max_new_tokens} gen), "
              f"{mem_per_seq / 1e6:.0f}MB/seq -> max_batch={max_batch}")

        return max_batch

    def _build_sub_batches(self, texts, max_items=None):
        """Split sorted-by-length texts into sub-batches.

        Splits on three criteria (checked in order):
        1. VRAM item limit: when max_items is set (from _estimate_max_batch_size)
        2. Length ratio: when longest/shortest > sub_batch_ratio
        3. Minimum size: ratio splits only happen after sub_batch_min_size items

        Returns list of (start, end) index tuples.
        """
        if len(texts) <= 1:
            return [(0, len(texts))]

        # Manual cap overrides VRAM estimate when set (take the stricter of the two)
        if self._sub_batch_max_items > 0:
            max_items = min(max_items, self._sub_batch_max_items) if max_items else self._sub_batch_max_items

        if not self._sub_batch_enabled:
            # Ratio-based splitting is disabled, but STILL honor the VRAM/manual
            # item cap so a huge render isn't issued as one OOM-ing batch.
            if max_items and max_items > 0 and len(texts) > max_items:
                return [(s, min(s + max_items, len(texts))) for s in range(0, len(texts), max_items)]
            return [(0, len(texts))]

        sub_batches = []
        batch_start = 0

        for i in range(1, len(texts)):
            shortest = max(len(texts[batch_start]), 1)
            should_split = False

            # VRAM-estimated item limit (highest priority — based on actual
            # free GPU memory and per-sequence KV cache cost)
            if max_items is not None and (i - batch_start) >= max_items:
                should_split = True
            # Ratio split: large length disparity wastes padding —
            # only split after min_size items to preserve parallelism
            elif (i - batch_start) >= self._sub_batch_min_size:
                if len(texts[i]) > self._sub_batch_ratio * shortest:
                    should_split = True

            if should_split:
                sub_batches.append((batch_start, i))
                batch_start = i

        sub_batches.append((batch_start, len(texts)))
        return sub_batches

    # ── Lazy initialization ──────────────────────────────────────

    @ensure_local_tts_serialized
    def _warmup_model(self, model) -> bool:
        """Run a short warmup generation to pre-tune MIOpen/GPU solvers.

        First generation after model load is ~2x slower due to MIOpen autotuning.
        This warmup pays that cost upfront so real generations run at full speed.
        """
        import time
        t0 = time.time()
        try:
            model.generate_custom_voice(
                text="The ancient library stood at the crossroads of two forgotten paths, its weathered stone walls covered in ivy that had been growing for centuries.",
                language=self._language,
                speaker="serena",
                instruct="neutral",
                non_streaming_mode=True,
                max_new_tokens=self._max_new_tokens,
            )
            print(f"Warmup done in {time.time()-t0:.1f}s")
            return True
        except Exception as e:
            print(f"Warmup failed (non-fatal): {e}")
            return False

    @ensure_local_tts_serialized
    def ensure_custom_warmup(self, model=None) -> bool:
        """Warm the CustomVoice backend once, retrying after non-fatal failure."""
        if not self._custom_warmup_needed:
            return True
        if model is None:
            model = self._init_local_custom()
        print("Running batch warmup generation...")
        succeeded = self._warmup_model(model)
        self._custom_warmup_needed = not succeeded
        return succeeded

    def _resolve_device(self):
        """Resolve 'auto' device to the best available."""
        return device_utils.resolve_device(self._device)

    def _enable_rocm_optimizations(self):
        """Apply ROCm-specific optimizations. No-op on NVIDIA/CPU. See
        device_utils.enable_rocm_optimizations for the per-step rationale
        (MIOpen fast-find, flash attention via Triton AMD, triton_key shim)."""
        import torch
        device_utils.enable_rocm_optimizations()

        # Correct under-reported GPU properties on consumer RDNA2/3.
        # ROCm reports half the CU count and warp size 32 instead of 64,
        # causing PyTorch to under-schedule work on RX 6000/7000 GPUs.
        self._patch_rdna_device_properties(torch)


    @staticmethod
    def _patch_rdna_device_properties(torch):
        """Monkey-patch torch.cuda.get_device_properties to report correct
        CU count and wavefront size for consumer RDNA2/3 GPUs.

        ROCm exposes these GPUs with half CU count and warp_size=32
        (matching the CDNA/MI convention). The actual hardware has the
        full CU count and native wavefront64. Under-reporting causes
        PyTorch to generate smaller kernel launches.

        Based on AMD-GPU-BOOST (github.com/Painter3000/AMD-GPU-BOOST).
        """
        if hasattr(torch.cuda, '_rdna_props_patched'):
            return

        # Known RDNA GPU corrections: {name_substring: (true_CUs, true_warp)}
        _rdna_corrections = {
            # RDNA4. Verified on this machine 2026-07-27: rocminfo reports
            # "Compute Unit: 64" while torch reports multi_processor_count=32,
            # i.e. torch is given the WGP count, the same halving this patch
            # exists to undo on RDNA2/3. Warp stays 32 because rocminfo reports
            # "Wavefront Size: 32" for gfx1201 - unlike the entries below, there
            # is no measured basis here for forcing 64.
            "9070 XT":  (64, 32),
            "7900 XTX": (96, 64),
            "7900 XT":  (84, 64),
            "7900 GRE": (80, 64),
            "7800 XT":  (60, 64),
            "7700 XT":  (54, 64),
            "7600":     (32, 64),
            "6950 XT":  (80, 64),
            "6900 XT":  (80, 64),
            "6800 XT":  (72, 64),
            "6800":     (60, 64),
            "6750 XT":  (40, 64),
            "6700 XT":  (40, 64),
            "6700":     (36, 64),
            "6650 XT":  (32, 64),
            "6600 XT":  (32, 64),
            "6600":     (28, 64),
        }

        original_fn = torch.cuda.get_device_properties
        _cache = {}

        def _patched_get_device_properties(device=None):
            if device is None:
                device = torch.cuda.current_device()
            if isinstance(device, int):
                key = device
            else:
                try:
                    dev = torch.device(device) if isinstance(device, str) else device
                    key = dev.index if dev.index is not None else torch.cuda.current_device()
                except Exception:
                    key = 0

            if key in _cache:
                return _cache[key]

            props = original_fn(device)

            # Find matching correction
            correction = None
            for substr, vals in _rdna_corrections.items():
                if substr in props.name:
                    correction = vals
                    break

            if correction:
                true_cus, true_warp = correction
                _ov = {"multi_processor_count": true_cus, "warp_size": true_warp}

                class _RDNADeviceProps:
                    __slots__ = ("_orig", "_ov")

                    def __init__(self, orig, ov):
                        object.__setattr__(self, "_orig", orig)
                        object.__setattr__(self, "_ov", ov)

                    def __getattr__(self, name):
                        ov = object.__getattribute__(self, "_ov")
                        if name in ov:
                            return ov[name]
                        try:
                            return getattr(object.__getattribute__(self, "_orig"), name)
                        except RuntimeError:
                            # Some ROCm/driver combos raise RuntimeError (not
                            # AttributeError) for certain C-extension device
                            # property fields. The SimpleNamespace this proxy
                            # replaced caught (AttributeError, RuntimeError)
                            # per-attribute and just omitted that attribute -
                            # re-raise as AttributeError so callers using
                            # getattr(obj, name, default)/hasattr still get
                            # that same "not available" signal instead of an
                            # unhandled RuntimeError propagating through.
                            raise AttributeError(name)

                    def __repr__(self):
                        return repr(object.__getattribute__(self, "_orig"))

                patched = _RDNADeviceProps(props, _ov)
                # Safely calculate old thread count - handle missing warp_size on unusual ROCm versions
                old_warp = getattr(props, 'warp_size', 32)  # Default to 32 if missing
                old_threads = props.multi_processor_count * old_warp
                new_threads = true_cus * true_warp
                print(f"  [RDNA fix] {props.name}: CUs {props.multi_processor_count}->{true_cus}, "
                      f"warp {old_warp}->{true_warp}, "
                      f"threads {old_threads}->{new_threads}")
                _cache[key] = patched
                return patched

            _cache[key] = props
            return props

        torch.cuda.get_device_properties = _patched_get_device_properties
        torch.cuda._rdna_props_patched = True

    def _compile_codec(self, model):
        """Apply torch.compile to the audio codec for faster decoding.

        CURRENTLY NO EFFECT (#1113, measured 2026-10-12): qwen_tts decodes via
        `speech_tokenizer.model.decode(...)`, but torch.compile on the module
        only compiles forward(), so renders are unchanged and no inductor cache
        is written. Compiling `.decode` instead made compilation run, but on
        ROCm it then stalled CPU-bound in symbolic-shape compilation (0 chunks
        in 23 min). The speed figures below predate that library change.

        The codec decoder has 136 attention modules and many small ops that
        benefit enormously from compilation.  Profiling shows the codec is
        47% of single-gen time and 85% of batch time uncompiled.  With
        torch.compile (dynamic=True, max-autotune), batch throughput
        improves from ~1.3x to ~4.3x real-time and single generation
        drops from ~14s to ~9s.

        max-autotune mode benchmarks GPU kernels to pick the fastest and
        handles varying batch sizes gracefully (unlike reduce-overhead
        which uses CUDA graphs that break on shape changes).
        """
        import torch, time
        vram_before = self._vram_snapshot("pre-compile_codec")
        t0 = time.time()
        try:
            codec = model.model.speech_tokenizer.model
            model.model.speech_tokenizer.model = torch.compile(
                codec, mode="max-autotune", dynamic=True,
            )
            compile_time = time.time() - t0
            vram_after = self._vram_snapshot("post-compile_codec")
            delta = vram_after.get("allocated_gb", 0) - vram_before.get("allocated_gb", 0)
            print(f"Codec compiled OK in {compile_time:.1f}s (VRAM delta: {delta:+.2f} GB).")
        except Exception as e:
            print(f"Codec compilation FAILED after {time.time()-t0:.1f}s: {type(e).__name__}: {e}")

    @staticmethod
    def _resolve_local_model_path(model_id):
        """Check if a HuggingFace model is cached locally and return its snapshot path.

        Uses try_to_load_from_cache to find the local snapshot directory.
        Returns the local path string if cached, or None if not cached.
        """
        from huggingface_hub import try_to_load_from_cache
        result = try_to_load_from_cache(model_id, "config.json")
        if isinstance(result, str):
            # result is the full path to config.json inside the snapshot dir
            return os.path.dirname(result)
        return None

    @staticmethod
    def _load_model(model_cls, model_id, load_kwargs, *, release_for_oom=None):
        """Retry one allocation failure after cleanup, preserving cache repair."""
        for attempt in range(2):
            ensure_audio_generation_active()
            try:
                return TTSEngine._load_model_attempt(model_cls, model_id, load_kwargs)
            except AudioGenerationCancelled:
                raise
            except Exception as error:
                if attempt or not device_utils.is_allocation_failure(error):
                    raise
            # Leave the exception scope before cleanup: failed loader frames may
            # retain partially allocated models. Never interpret a retry differently.
            ensure_audio_generation_active()
            cleanup = release_for_oom or TTSEngine._clear_gpu_cache
            cleanup()

    @staticmethod
    def _load_model_attempt(model_cls, model_id, load_kwargs):
        """Load a model, preferring local cache to avoid network issues.

        Checks if the model snapshot exists in the HF cache and loads from
        the local directory path directly, bypassing all HF Hub network calls.
        Falls back to normal download on first install when cache is empty.
        If loading from local cache fails (e.g. incomplete snapshot), retries
        with the model ID so HF Hub can download any missing files.
        """
        local_path = TTSEngine._resolve_local_model_path(model_id)
        if local_path:
            print(f"  Loading from local cache: {local_path}")
            from synthesis_recovery_native import (capture_native_recovery_load,
                complete_native_recovery_load)
            from synthesis_recovery_identity import RecoveryIdentityUnavailable
            proposal = None
            try:
                proposal = capture_native_recovery_load(model_cls, model_id, local_path, load_kwargs)
            except (RecoveryIdentityUnavailable, OSError, ValueError, RecursionError) as error:
                print(f"  Recovery model identity unavailable: {type(error).__name__}")
            try:
                # Use the exact canonical directory whose names/bytes were
                # captured. A cache ref alias cannot redirect the admitted load.
                source = proposal.snapshot.root if proposal is not None else local_path
                model = model_cls.from_pretrained(source, **load_kwargs)
            except Exception as e:
                if device_utils.is_allocation_failure(e):
                    raise
                proposal = None
                import traceback
                print(f"  Warning: Failed to load from local cache: {e}")
                traceback.print_exc()
                print(f"  Retrying with model ID (may download missing files)...")
                model = model_cls.from_pretrained(model_id, **load_kwargs)
            if proposal is not None:
                try:
                    complete_native_recovery_load(model, proposal)
                except (RecoveryIdentityUnavailable, OSError, ValueError, RecursionError) as error:
                    print(f"  Recovery loaded model is unproven: {type(error).__name__}")
        else:
            print(f"  Model not cached locally, downloading {model_id}...")
            model = model_cls.from_pretrained(model_id, **load_kwargs)
        codec = getattr(getattr(model, "model", None), "speech_tokenizer", None)
        codec_model = getattr(codec, "model", None)
        if getattr(getattr(codec_model, "config", None), "model_type", None) == "qwen3_tts_tokenizer_12hz":
            decoder = codec_model.decoder
            decoder.chunked_decode = get_memory_bounded_codec_decode(decoder.chunked_decode)
        return model

    @staticmethod
    def get_local_model_inventory():
        """Observe known cached base assets; never construct an engine or load."""
        from synthesis_recovery_identity import get_model_asset_observation
        records = []
        for model_id in dict.fromkeys(TTSEngine._LOCAL_MODEL_IDS.values()):
            families = [name for name, value in TTSEngine._LOCAL_MODEL_IDS.items() if value == model_id]
            try:
                path = TTSEngine._resolve_local_model_path(model_id)
                observation = get_model_asset_observation(path) if path else {'state': 'not_cached'}
            except (ImportError, OSError, ValueError, RuntimeError, RecursionError):
                observation = {'state': 'unavailable'}
            records.append({'model_id': model_id, 'families': families, **observation})
        return records

    def get_model_residency(self):
        """Observe this engine without loading models or waiting for inference."""
        if not _LOCAL_TTS_LOCK.acquire(blocking=False):
            return {"state": "busy", "active_leases": None, "residents": None}
        try:
            native = sys.modules.get('synthesis_recovery_native')
            observe_load = getattr(native, 'get_native_recovery_load_observation', None)
            return {"state": "observed", "active_leases": self._local_model_active_leases,
                    "cleanup_pending": bool(self._local_release_pending_devices),
                    "release_events": copy.deepcopy(self._local_model_release_events),
                    "residents": [{"family": name, "device": self._local_model_devices.get(name),
                                   "load_capture": observe_load(getattr(self, f"_local_{name}_model"))
                                   if observe_load is not None else None}
                                  for name in ("custom", "clone", "design", "lora")
                                  if getattr(self, f"_local_{name}_model") is not None]}
        finally:
            _LOCAL_TTS_LOCK.release()

    def _release_local_model_family(self, name):
        """Drop one serialized family and its dependent caches."""
        setattr(self, f"_local_{name}_model", None)
        self._local_model_devices.pop(name, None)
        if name == "custom":
            self._custom_warmup_needed = True
        elif name == "clone":
            self._clear_clone_reference_caches()
        elif name == "lora":
            self._lora_adapter_path = None
            self._lora_generation_sha256 = None
            self._lora_prompt_cache.clear()

    def ensure_idle_local_models_released(self):
        """Release idle residents under the existing serialization boundary.

        Callers still need task/host ownership before admitting GPU work.
        Synchronization failure leaves model references and caches intact.
        """
        if not _LOCAL_TTS_LOCK.acquire(blocking=False):
            return False
        try:
            if self._local_model_active_leases or not self._model_lock.acquire(blocking=False):
                return False
            try:
                families = [name for name in ("custom", "clone", "design", "lora")
                            if getattr(self, f"_local_{name}_model") is not None]
                return self._release_local_model_families(families)
            finally:
                self._model_lock.release()
        finally:
            _LOCAL_TTS_LOCK.release()

    def _release_local_model_families(self, families, *, extra_devices=()):
        """Caller holds local serialization/model lock; failed cleanup stays fenced."""
        families = sorted(set(families) | self._local_release_pending_families)
        devices = ({self._local_model_devices.get(name) for name in families
                    if getattr(self, f"_local_{name}_model") is not None}
                   | self._local_release_pending_devices | set(extra_devices))
        if not families and not devices:
            return True
        if None in devices:
            return False
        devices = sorted(devices)
        started = time.monotonic()
        event = {"families": list(families), "result": "failed", "phase": "synchronize",
                 "before": [device_utils.get_loaded_torch_memory_observation(d) for d in devices]}
        torch = sys.modules.get("torch")
        try:
            for device in devices:
                if device.startswith("cuda"):
                    if torch is None:
                        return False
                    torch.cuda.synchronize(device)
                elif device == "mps":
                    if torch is None:
                        return False
                    torch.mps.synchronize()
                elif device != "cpu":
                    return False
            self._local_release_pending_devices.update(devices)
            self._local_release_pending_families.update(families)
            event["phase"] = "drop_models"
            for name in families:
                self._release_local_model_family(name)
            event["phase"] = "clear_cache"
            self._clear_gpu_cache()
            if "mps" in devices:
                torch.mps.empty_cache()
            self._local_release_pending_devices.clear()
            self._local_release_pending_families.clear()
            event.update(result="released", phase="complete")
            return True
        finally:
            event["after"] = [device_utils.get_loaded_torch_memory_observation(d) for d in devices]
            event["elapsed_seconds"] = max(0, time.monotonic() - started)
            self._local_model_release_events.append(event)
            del self._local_model_release_events[:-8]

    def _release_inactive_models_after_oom(self, family, device):
        families = [name for name in ("custom", "clone", "design", "lora")
                    if name != family and getattr(self, f"_local_{name}_model") is not None]
        if not self._release_local_model_families(families, extra_devices=(device,)):
            raise RuntimeError("Cannot release inactive models with unknown loaded devices")

    def _release_inactive_local_models(self, family, device, dtype):
        """Keep cached families only when another load has ample headroom.

        Called under local serialization and _model_lock, before loading. The
        current 1.7B families need ~4.2 GB bf16 weights including the codec;
        budget 5 GB plus at least an equal amount for inference/fragmentation.
        Scale for fp32 and the configured finite generation window. Unknown
        GPU capacity and CPU/MPS use a single resident family conservatively.
        Existing inference headroom checks still govern each render.
        """
        families = ("custom", "clone", "design", "lora")
        inactive = [name for name in families if name != family
                    and getattr(self, f"_local_{name}_model", None) is not None]
        if not inactive:
            return
        import torch
        if str(device).startswith("cuda"):
            try:
                free, _ = torch.cuda.mem_get_info(device)
                dtype_bytes = torch.finfo(dtype).bits // 8
                load_budget = 5e9 * (dtype_bytes / 2)
                required = load_budget * max(2.0, self._max_new_tokens / 1024)
                # Driver free is conservative: allocator reservations are not
                # counted as available for retaining another full family.
                if free * 0.8 >= required:
                    return
            except Exception as exc:
                print(f"Cannot size local model capacity: {exc}; releasing inactive families.")
        for name in inactive:
            print(f"Releasing inactive local {name} model before loading {family}...")
            self._release_local_model_family(name)
        self._clear_gpu_cache()

    @ensure_local_tts_serialized
    @measure_tts_stage("model_preparation")
    def _init_local_custom(self):
        """Load Qwen3-TTS CustomVoice model on demand."""
        if self._local_custom_model is not None:
            return self._local_custom_model

        with self._model_lock:
            if self._local_custom_model is not None:
                return self._local_custom_model

            self._enable_rocm_optimizations()

            import torch
            from qwen_tts import Qwen3TTSModel

            device = self._resolve_device()
            dtype = device_utils.compute_dtype(device)

            self._release_inactive_local_models("custom", device, dtype)

            print(f"Loading Qwen3-TTS CustomVoice model on {device} ({dtype})...")
            vram_before = self._vram_snapshot("pre-load")
            load_kwargs = {"dtype": dtype}
            if device != "cpu":
                load_kwargs["device_map"] = device
            self._local_custom_model = self._load_model(
                Qwen3TTSModel, self._LOCAL_MODEL_IDS["custom"], load_kwargs,
                release_for_oom=lambda: self._release_inactive_models_after_oom("custom", device),
            )
            self._local_model_devices["custom"] = device
            vram_after = self._vram_snapshot("post-load")
            model_gb = vram_after.get("allocated_gb", 0) - vram_before.get("allocated_gb", 0)
            print(f"CustomVoice model loaded: {model_gb:.2f} GB VRAM footprint")
            if self._compile_codec_enabled:
                self._compile_codec(self._local_custom_model)
            return self._local_custom_model

    @ensure_local_tts_serialized
    @measure_tts_stage("model_preparation")
    def _init_local_clone(self):
        """Load Qwen3-TTS Base model (for voice cloning) on demand."""
        if self._local_clone_model is not None:
            return self._local_clone_model

        with self._model_lock:
            if self._local_clone_model is not None:
                return self._local_clone_model

            self._enable_rocm_optimizations()

            import torch
            from qwen_tts import Qwen3TTSModel

            device = self._resolve_device()
            dtype = device_utils.compute_dtype(device)

            self._release_inactive_local_models("clone", device, dtype)

            print(f"Loading Qwen3-TTS Base model (voice cloning) on {device} ({dtype})...")
            load_kwargs = {"dtype": dtype}
            if device != "cpu":
                load_kwargs["device_map"] = device
            self._local_clone_model = self._load_model(
                Qwen3TTSModel, self._LOCAL_MODEL_IDS["clone"], load_kwargs,
                release_for_oom=lambda: self._release_inactive_models_after_oom("clone", device),
            )
            self._local_model_devices["clone"] = device
            if self._compile_codec_enabled:
                self._compile_codec(self._local_clone_model)
            print("Base model (voice cloning) loaded.")
            return self._local_clone_model

    @ensure_local_tts_serialized
    @measure_tts_stage("model_preparation")
    def _init_local_design(self):
        """Load Qwen3-TTS VoiceDesign model on demand."""
        if self._local_design_model is not None:
            return self._local_design_model

        with self._model_lock:
            if self._local_design_model is not None:
                return self._local_design_model

            self._enable_rocm_optimizations()

            import torch
            from qwen_tts import Qwen3TTSModel

            device = self._resolve_device()
            dtype = device_utils.compute_dtype(device)

            self._release_inactive_local_models("design", device, dtype)

            print(f"Loading Qwen3-TTS VoiceDesign model on {device} ({dtype})...")
            load_kwargs = {"dtype": dtype}
            if device != "cpu":
                load_kwargs["device_map"] = device
            self._local_design_model = self._load_model(
                Qwen3TTSModel, self._LOCAL_MODEL_IDS["design"], load_kwargs,
                release_for_oom=lambda: self._release_inactive_models_after_oom("design", device),
            )
            self._local_model_devices["design"] = device
            if self._compile_codec_enabled:
                self._compile_codec(self._local_design_model)
            print("VoiceDesign model loaded.")
            return self._local_design_model

    @ensure_local_tts_serialized
    @measure_tts_stage("model_preparation")
    def _init_local_lora(self, adapter_path, generation_sha256=None, source_adapter_path=None):
        """Load Qwen3-TTS Base model with a LoRA adapter on demand.

        Caches the model; if a different adapter is requested the old one
        is unloaded first to free VRAM.
        """
        source_adapter_path = source_adapter_path or adapter_path
        if generation_sha256 is None:
            with ensure_adapter_generation_snapshot(adapter_path) as (snapshot, generation):
                return self._init_local_lora(snapshot, generation, source_adapter_path)
        if (self._local_lora_model is not None and self._lora_adapter_path == source_adapter_path
                and self._lora_generation_sha256 == generation_sha256):
            return self._local_lora_model

        with self._model_lock:
            if (self._local_lora_model is not None and self._lora_adapter_path == source_adapter_path
                    and self._lora_generation_sha256 == generation_sha256):
                return self._local_lora_model

            # Unload previous adapter if switching
            if self._local_lora_model is not None:
                print(f"Unloading previous LoRA adapter ({self._lora_adapter_path})...")
                del self._local_lora_model
                self._local_lora_model = None
                self._lora_adapter_path = None
                self._lora_generation_sha256 = None
                self._lora_prompt_cache.clear()
                self._clear_gpu_cache()

            self._enable_rocm_optimizations()

            import torch
            from qwen_tts import Qwen3TTSModel
            from peft import PeftModel

            device = self._resolve_device()
            dtype = device_utils.compute_dtype(device)

            self._release_inactive_local_models("lora", device, dtype)

            print(f"Loading Qwen3-TTS Base model + LoRA adapter on {device} ({dtype})...")
            load_kwargs = {"dtype": dtype}
            if device != "cpu":
                load_kwargs["device_map"] = device

            model = self._load_model(
                Qwen3TTSModel, self._LOCAL_MODEL_IDS["lora"], load_kwargs,
                release_for_oom=lambda: self._release_inactive_models_after_oom("lora", device),
            )

            # Apply the adapter, then fold it into the base weights.
            #
            # Left as a PeftModel, every decoder step computes W.x + B.A.x
            # for every targeted module, and that is the whole of the LoRA
            # path's speed cost: measured on the RX 9070 XT (goal 4.1),
            # unmerged runs at 1.25x realtime, merged at 0.835x -- the stock
            # model's speed. merge_and_unload adds B.A into W once (0.03 s)
            # and returns the plain talker module. The merged weights are
            # bf16-rounded, so the audio is not bit-identical to the unmerged
            # render, but it is the same voice: ECAPA similarity 0.714
            # merged-vs-unmerged on the same line, against 0.538 for the
            # adapter across different lines (2026-09-12, six lines).
            # This model instance is fresh from _load_model and cached only
            # here, so merging in place cannot leak into the stock/clone
            # paths; switching adapters reloads the base anyway.
            model.model.talker = PeftModel.from_pretrained(
                model.model.talker,
                adapter_path,
            ).merge_and_unload()
            model.model.talker.eval()

            if self._compile_codec_enabled:
                self._compile_codec(model)

            self._local_lora_model = model
            self._local_model_devices["lora"] = device
            self._lora_adapter_path = source_adapter_path
            self._lora_generation_sha256 = generation_sha256
            print(f"LoRA adapter loaded from {source_adapter_path}")
            return model

    def external_endpoints(self):
        """The external endpoint pool, in round-robin order."""
        return list(self._external_urls)

    def _next_external_url(self):
        """Round-robin over the pool; the ONE place an endpoint is chosen."""
        with self._external_pool_lock:
            url = self._external_urls[self._external_next % len(self._external_urls)]
            self._external_next += 1
            return url

    def _external_endpoint(self, url=None):
        """-> (client, lock) for the next endpoint worker, creating its client on demand."""
        url = url or self._next_external_url()
        with self._external_pool_lock:
            if self._external_closed:
                raise RuntimeError("External TTS engine is closed")
            worker = self._external_client_next.get(url, 0) % self._external_parallel_workers
            self._external_client_next[url] = worker + 1
            key = (url, worker)
            lock = self._external_locks.setdefault(key, threading.Lock())
            client = self._gradio_clients.get(key)
        if client is None:
            with lock:
                client = self._gradio_clients.get(key)
                if client is None:
                    print(f"Connecting to TTS server at {url}...")
                    client = create_external_client(url, self._external_timeout)
                    with self._external_pool_lock:
                        closed = self._external_closed
                        if not closed:
                            self._gradio_clients[key] = client
                    if closed:
                        close = getattr(client, "close", None)
                        if close is not None:
                            close()
                        raise RuntimeError("External TTS engine is closed")
                    print(f"External TTS client ready for {url}.")
        return client, lock

    def _ensure_external_executor(self):
        with _EXTERNAL_RUNTIME_LOCK:
            if not hasattr(self, "_external_pool_lock"):
                self._external_pool_lock = threading.Lock()
            if not hasattr(self, "_gradio_clients"):
                self._gradio_clients = {}
            if not hasattr(self, "_external_calls"):
                self._external_calls = set()
            if not hasattr(self, "_external_closed"):
                self._external_closed = False
            if self._external_closed:
                raise RuntimeError("External TTS engine is closed")
            executor = getattr(self, "_external_executor", None)
            if executor is None:
                workers = (getattr(self, "_external_parallel_workers", 1)
                           * len(getattr(self, "_external_urls", None) or [None]))
                executor = ExternalTTSExecutor(workers)
                self._external_executor = executor
                self._external_finalizer = weakref.finalize(
                    self, _close_external_runtime, self._external_pool_lock,
                    self._external_calls, self._gradio_clients, executor)
            return executor

    def _submit_external(self, function, cancelled, *args):
        executor = self._ensure_external_executor()
        with self._external_pool_lock:
            if self._external_closed:
                cancelled.set()
                raise RuntimeError("External TTS engine is closed")
            self._external_calls.add(cancelled)
        try:
            future = executor.submit(partial(copy_context().run, function), *args)
        except BaseException:
            with self._external_pool_lock:
                self._external_calls.discard(cancelled)
            cancelled.set()
            raise
        owner = weakref.ref(self)
        def release(future):
            engine = owner()
            if engine is not None:
                with engine._external_pool_lock:
                    engine._external_calls.discard(cancelled)
        future.add_done_callback(release)
        return future

    def close_external(self):
        with _EXTERNAL_RUNTIME_LOCK:
            if not hasattr(self, "_external_pool_lock"):
                return
            with self._external_pool_lock:
                self._external_closed = True
            executor = getattr(self, "_external_executor", None)
        _close_external_runtime(self._external_pool_lock, getattr(self, "_external_calls", set()),
                                self._gradio_clients, executor)
        finalizer = getattr(self, "_external_finalizer", None)
        if finalizer is not None:
            finalizer.detach()

    # ── Clone prompt cache (local mode) ──────────────────────────

    def _clear_clone_reference_caches(self):
        self._clone_prompt_cache.clear()
        prepared = getattr(self, '_prepared_reference_cache', None)
        if prepared is not None:
            prepared.clear()
        self._clone_prompt_model_ref = None

    def _capture_clone_prompt_cache_copy(self, prompt):
        try:
            captured = clone_reference_cache.copy_clone_prompt(prompt, self._clone_prompt_cache.max_bytes)
        except Exception as error:
            # Optional retention must not turn a usable provider result into a
            # render failure. Release cache storage before ordinary generation.
            self._clone_prompt_cache.clear()
            self._prepared_reference_cache.clear()
            logging.warning('Clone reference cache copy bypassed (%s).', type(error).__name__)
            captured = None
        if captured is None:
            self._clone_prompt_cache.record_bypass()
        return captured

    @ensure_local_tts_serialized
    @measure_tts_stage("reference_preparation")
    def _get_clone_prompt(self, speaker, voice_config):
        """Prepare an admitted reference, sharing bounded content/model work."""
        try:
            return self._prepare_clone_prompt(speaker, voice_config)
        finally:
            record_tts_reference_cache(self._prepared_reference_cache.snapshot(),
                                       self._clone_prompt_cache.snapshot())

    def _prepare_clone_prompt(self, speaker, voice_config):
        ensure_audio_generation_active()
        voice_data = voice_config.get(speaker, {})
        ref_audio_path = voice_data.get("ref_audio")
        ref_text = voice_data.get("ref_text")

        if not ref_audio_path or not ref_text:
            raise ValueError(f"Clone voice for '{speaker}' missing ref_audio or ref_text")
        # Resolve relative paths against project root (parent of app/)
        if not os.path.isabs(ref_audio_path):
            ref_audio_path = _resolve_asset_path(ref_audio_path)
        # Hash and decode one captured reference, resolving historical adapter
        # directories under publication admission before reading their bytes.
        ref_audio_path, audio_bytes = get_adapter_asset_snapshot(ref_audio_path)
        digest = hashlib.sha256(audio_bytes).hexdigest()
        # Admission always precedes hits, including a resident-family change.
        model = self._init_local_clone()
        owner = self._clone_prompt_model_ref
        if owner is None or owner() is not model:
            self._clear_clone_reference_caches()
            try:
                self._clone_prompt_model_ref = weakref.ref(model)
            except TypeError:
                # An unknown provider without weak identity remains uncached.
                pass
        prepared_key = (clone_reference_cache.PREPROCESSING_VERSION, digest)
        cache_prompt = self._clone_prompt_model_ref is not None and type(ref_text) is str
        prompt_key = (*prepared_key, ref_text) if cache_prompt else None
        if cache_prompt:
            cached = self._clone_prompt_cache.lookup(prompt_key)
            if cached is not None:
                copied = self._capture_clone_prompt_cache_copy(cached)
                if copied is not None:
                    ensure_audio_generation_active()
                    record_tts_cache(True)
                    return copied[0]
                cached = None  # Release the evicted buffers before rebuilding.
        else:
            self._clone_prompt_cache.record_bypass()
        record_tts_cache(False)

        prepared = self._prepared_reference_cache.lookup(prepared_key)
        audio_array = None
        if prepared is not None:
            try:
                audio_array = prepared[0].copy(order='C')
                sample_rate = prepared[1]
            except MemoryError:
                self._prepared_reference_cache.clear()
                self._prepared_reference_cache.record_bypass()
                prepared = None
        staged_reference = None
        if audio_array is None:
            # Preserve float64 decode/mono mean before the provider's float32
            # conversion. Changing that order changes reference samples.
            audio_array, sample_rate = sf.read(io.BytesIO(audio_bytes))
            if audio_array.ndim > 1:
                audio_array = audio_array.mean(axis=1)
            charge = (audio_array.nbytes + sys.getsizeof(audio_array)
                      + sys.getsizeof(sample_rate) + sys.getsizeof((audio_array, sample_rate)))
            if charge <= self._prepared_reference_cache.max_bytes:
                try:
                    snapshot = audio_array.copy(order='C')
                    snapshot.flags.writeable = False
                    staged_reference = (snapshot, sample_rate)
                except MemoryError:
                    self._prepared_reference_cache.clear()
                    self._prepared_reference_cache.record_bypass()
            else:
                self._prepared_reference_cache.record_bypass()

        print(f"Creating clone prompt for '{speaker}'...")
        ensure_audio_generation_active()
        prompt = model.create_voice_clone_prompt(
            ref_audio=(audio_array, sample_rate),
            ref_text=ref_text,
        )
        ensure_audio_generation_active()
        if staged_reference is not None:
            self._prepared_reference_cache.store(prepared_key, staged_reference, charge)
        if cache_prompt:
            captured = self._capture_clone_prompt_cache_copy(prompt)
            if captured is not None:
                self._clone_prompt_cache.store(prompt_key, captured[0], captured[1])
        ensure_audio_generation_active()
        print(f"Clone prompt prepared for '{speaker}'.")
        return prompt

    def get_voice_backend(self, category):
        """Return the selected backend or reject unsupported local-model fallback."""
        try:
            capabilities = get_tts_backend_capabilities(self._mode)
        except ValueError as error:
            raise UnsupportedVoiceBackendError(str(error)) from error
        if category not in capabilities['voice_categories']:
            raise UnsupportedVoiceBackendError(
                f"{category} voices require local TTS mode; external TTS supports custom and clone voices.")
        return self._mode

    def get_model_settings(self):
        """Return the actual model settings used by this initialized engine."""
        settings = {"mode": self._mode, "device": self._device,
                    "compile_codec": self._compile_codec_enabled,
                    "max_new_tokens": self._max_new_tokens, "language": self._language}
        if getattr(self, "_synthesis_windows", None) is not None:
            settings["synthesis_windows"] = copy.deepcopy(self._synthesis_windows)
        return settings

    def get_synthesis_recovery_policy(self):
        return copy.deepcopy(getattr(self, "_synthesis_recovery", None))

    def get_speech_verification_policy(self):
        return copy.deepcopy(getattr(self, '_speech_verification', None))

    def _get_synthesis_window_plan(self, text, category, voice_data=None, voice_config=None):
        settings = getattr(self, "_synthesis_windows", None)
        if settings is None:
            return None
        keys = [f"{self.get_voice_backend(category)}.{category}"]
        if category == "ensemble":
            for member in self._get_ensemble_members(voice_data, voice_config):
                member_category = voice_category(voice_config[member])
                keys.append(f"{self.get_voice_backend(member_category)}.{member_category}")
        return get_synthesis_window_plan(text, settings, keys)

    def _render_synthesis_windows(self, text, category, render, output_path,
                                  voice_data=None, voice_config=None, recovery_request=None):
        try:
            ensure_audio_generation_active()
            plan = self._get_synthesis_window_plan(text, category, voice_data, voice_config)
            if plan is None:
                return None
            from synthesis_recovery import ensure_synthesis_recovery
            with ensure_synthesis_recovery(self, category, plan, self._synthesis_windows,
                                           recovery_request) as recovery:
                from synthesis_workspace import is_synthesis_workspace_supported
                from synthesis_recovery_context import get_synthesis_recovery_parent
                # Only this synchronous built-in output writer is initially
                # qualified. Unknown/external providers can outlive their caller.
                methods_known = all(getattr(getattr(self, name), '__func__', getattr(self, name)) is method
                                    for name, method in _RAW_RECOVERY_METHODS.items())
                workspace_parent = (get_synthesis_recovery_parent()
                                    if category == 'custom' and methods_known
                                    and self.get_voice_backend(category) == 'local'
                                    and is_synthesis_workspace_supported() else None)
                options = {} if workspace_parent is None else {'workspace_parent': workspace_parent}
                render_synthesis_windows(plan, render, output_path, self._synthesis_windows,
                                         cancel_check=is_audio_generation_cancelled, recovery=recovery, **options)
            return True
        except Exception as error:
            apply_tts_failure(error)
            return False

    # ── Core generation methods ──────────────────────────────────

    def _external_single(self, generate, output_path):
        """Bound one external request without publishing late audio."""
        from concurrent.futures import TimeoutError as FutureTimeout
        staging_path = f"{output_path}.pending.{uuid.uuid4().hex}"
        cancelled = threading.Event()
        deadline = time.monotonic() + self._external_timeout
        capture_inputs = is_speech_input_capture_active()

        def work():
            try:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    return False, time.monotonic(), None
                with (ensure_external_request_control(deadline, cancelled),
                      ensure_speech_input_capture(capture_inputs)):
                    success = generate(staging_path, cancelled)
                    proof = get_captured_speech_input(staging_path) if success else None
                    return success, time.monotonic(), proof
            except Exception as error:
                import traceback
                traceback.print_exc()
                apply_tts_failure(error)
                return False, time.monotonic(), None
            finally:
                if cancelled.is_set():
                    try:
                        os.remove(staging_path)
                    except FileNotFoundError:
                        pass
        future = self._submit_external(work, cancelled)
        try:
            success, finished_at, speech_input = future.result(timeout=max(0, deadline - time.monotonic()))
            if finished_at > deadline:
                raise FutureTimeout()
        except FutureTimeout:
            cancelled.set()
            future.cancel()
            try:
                os.remove(staging_path)
            except FileNotFoundError:
                pass
            print(f"External TTS timed out after {self._external_timeout}s")
            apply_tts_failure(TimeoutError(f"External TTS timed out after {self._external_timeout}s"))
            return False
        except Exception as error:
            apply_tts_failure(error)
            success = False
        except BaseException:
            cancelled.set()
            future.cancel()
            try:
                os.remove(staging_path)
            except FileNotFoundError:
                pass
            raise
        if (success and os.path.exists(staging_path)
                and publish_audio_output(staging_path, output_path, cancelled)):
            promote_speech_input(speech_input, output_path)
            return True
        try:
            os.remove(staging_path)
        except FileNotFoundError:
            pass
        return False

    @capture_tts_operation("generate_custom_voice")
    def generate_custom_voice(self, text, instruct_text, speaker, voice_config, output_path):
        """Generate audio using CustomVoice model. Returns True on success."""
        prepared = get_speech_preparation(text, instruct_text)
        return self._generate_custom_voice_prepared(
            prepared["text"], prepared["instruct"], speaker, voice_config, output_path)

    def _generate_custom_voice_prepared(self, text, instruct_text, speaker, voice_config, output_path):
        """Render explicitly prepared input; public callers use the raw wrapper."""
        windowed = self._render_synthesis_windows(
            text, "custom", lambda span, path: self._generate_custom_voice_prepared(
                span, instruct_text, speaker, voice_config, path), output_path,
            recovery_request=(get_custom_voice_request(voice_config, speaker, instruct_text)
                              if getattr(self, '_synthesis_recovery', None) else None))
        if windowed is not None:
            return windowed
        if self.get_voice_backend("custom") == "local":
            return self._local_generate_custom(text, instruct_text, speaker, voice_config, output_path)
        else:
            return self._external_single(
                lambda staged, cancelled: self._external_generate_custom(
                    text, instruct_text, speaker, voice_config, staged, cancelled=cancelled),
                output_path)

    @capture_tts_operation("generate_clone_voice")
    def generate_clone_voice(self, text, speaker, voice_config, output_path):
        """Generate audio using voice cloning. Returns True on success."""
        return self._generate_clone_voice_prepared(
            normalize_for_speech(text), speaker, voice_config, output_path)

    def _generate_clone_voice_prepared(self, text, speaker, voice_config, output_path):
        """Render explicitly prepared input; public callers use the raw wrapper."""
        windowed = self._render_synthesis_windows(
            text, "clone", lambda span, path: self._generate_clone_voice_prepared(
                span, speaker, voice_config, path), output_path)
        if windowed is not None:
            return windowed
        if self.get_voice_backend("clone") == "local":
            return self._local_generate_clone(text, speaker, voice_config, output_path)
        else:
            return self._external_single(
                lambda staged, cancelled: self._external_generate_clone(
                    text, speaker, voice_config, staged, cancelled=cancelled),
                output_path)

    @capture_tts_operation("generate_voice_result")
    def generate_voice_result(self, text, instruct_text, speaker, voice_config, output_path, *, capture_input=False):
        """Return call-local diagnostics without changing legacy boolean methods."""
        failures = []
        token = _TTS_FAILURES.set(failures)
        try:
            with (ensure_synthesis_receipts() as receipts,
                  ensure_speech_input_capture(capture_input is True or bool(getattr(self, '_speech_verification', None)))):
                try:
                    success = bool(self.generate_voice(
                        text, instruct_text, speaker, voice_config, output_path))
                except Exception as error:
                    apply_tts_failure(error)
                    success = False
                speech_input = get_captured_speech_input(output_path) if success else None
            # An ensemble can wrap the original member failure; retain its cause.
            return TTSGenerationResult(success, None if success or not failures else failures[0],
                                       receipts[-1] if success and receipts else None, speech_input)
        finally:
            _TTS_FAILURES.reset(token)

    @capture_tts_operation("generate_voice")
    def generate_voice(self, text, instruct_text, speaker, voice_config, output_path):
        """Generate audio using the appropriate method based on voice type config."""
        prepared = get_speech_preparation(text, instruct_text)
        return self._generate_voice_prepared(
            prepared["text"], prepared["instruct"], speaker, voice_config, output_path)

    def _dispatch_prepared_voice(self, text, instruct_text, speaker, voice_config, output_path):
        """Retain public dispatcher overrides without re-preparing native input."""
        render = self.generate_voice
        if (isinstance(render, MethodType) and render.__self__ is self
                and render.__func__ is _RAW_GENERATE_VOICE):
            return self._generate_voice_prepared(text, instruct_text, speaker, voice_config, output_path)
        return render(text, instruct_text, speaker, voice_config, output_path)

    def _generate_voice_prepared(self, text, instruct_text, speaker, voice_config, output_path):
        """Dispatch input whose simultaneous speech preparation has already run."""
        speaker = get_resolved_speaker(speaker, voice_config)
        voice_config = resolve_narrator_voice_config(speaker, voice_config)
        voice_data = voice_config.get(speaker)
        if not is_voice_config_present(speaker, voice_config):
            print(f"Warning: No voice configuration for '{speaker}'. Skipping.")
            apply_tts_failure(f"No voice configuration for '{speaker}'.", "missing_configuration",
                              "Assign a voice to this speaker before retrying.")
            return False

        category = voice_category(voice_data)
        try:
            self.get_voice_backend(category)
        except UnsupportedVoiceBackendError as error:
            print(f"Error: {error}")
            apply_tts_failure(error)
            return False

        if category == "clone":
            render = self.generate_clone_voice
            if (isinstance(render, MethodType) and render.__self__ is self
                    and render.__func__ is _RAW_CLONE_VOICE):
                return self._generate_clone_voice_prepared(text, speaker, voice_config, output_path)
            # Preserve historical override input and preparation semantics.
            return render(text, speaker, voice_config, output_path)
        elif category == "lora":
            render = self.generate_lora_voice
            if (isinstance(render, MethodType) and render.__self__ is self
                    and render.__func__ is _RAW_LORA_VOICE):
                return self._generate_lora_voice_prepared(text, instruct_text, voice_data, output_path)
            return render(text, instruct_text, voice_data, output_path)
        elif category == "design":
            render = self.generate_design_voice
            if (isinstance(render, MethodType) and render.__self__ is self
                    and render.__func__ is _RAW_DESIGN_VOICE):
                return self._generate_design_voice_prepared(text, instruct_text, voice_data, output_path)
            return render(text, instruct_text, voice_data, output_path)
        elif category == "ensemble":
            render = self.generate_ensemble_voice
            if (isinstance(render, MethodType) and render.__self__ is self
                    and render.__func__ is _RAW_ENSEMBLE_VOICE):
                return self._generate_ensemble_voice_prepared(text, instruct_text, voice_data, voice_config, output_path)
            return render(text, instruct_text, voice_data, voice_config, output_path)
        else:
            render = self.generate_custom_voice
            if (isinstance(render, MethodType) and render.__self__ is self
                    and render.__func__ is _RAW_CUSTOM_VOICE):
                return self._generate_custom_voice_prepared(
                    text, instruct_text, speaker, voice_config, output_path)
            # Preserve historical override input and preparation semantics.
            return render(text, instruct_text, speaker, voice_config, output_path)

    @capture_tts_operation("generate_ensemble_voice")
    def generate_ensemble_voice(self, text, instruct_text, voice_data, voice_config,
                                output_path):
        """Render one line as several characters speaking at once.

        Members are ordinary speaker keys, so each is rendered through
        generate_voice with whatever voice it already has — a member with a LoRA
        assigned keeps that LoRA. The clips are then aligned and mixed by
        mix_to_unison.
        """
        prepared = get_speech_preparation(text, instruct_text)
        return self._generate_ensemble_voice_prepared(
            prepared["text"], prepared["instruct"], voice_data, voice_config, output_path)

    def _get_ensemble_members(self, voice_data, voice_config):
        members = voice_data.get("members") or []
        if not members:
            raise ValueError("Ensemble voice has no members configured.")

        resolved_members = [get_resolved_speaker(member, voice_config) for member in members]
        for member in resolved_members:
            member_data = voice_config.get(member)
            if not member_data:
                raise ValueError(
                    f"Ensemble member '{member}' has no voice configuration.")
            # One level only: an ensemble of ensembles would recurse forever.
            if voice_category(member_data) == "ensemble":
                raise ValueError(
                    f"Ensemble member '{member}' is itself an ensemble; "
                    f"ensembles cannot be nested.")
            self.get_voice_backend(voice_category(member_data))
        return resolved_members

    def _generate_ensemble_voice_prepared(self, text, instruct_text, voice_data, voice_config, output_path):
        resolved_members = self._get_ensemble_members(voice_data, voice_config)
        windowed = self._render_synthesis_windows(
            text, "ensemble", lambda span, path: self._generate_ensemble_voice_prepared(
                span, instruct_text, voice_data, voice_config, path), output_path,
            voice_data=voice_data, voice_config=voice_config)
        if windowed is not None:
            return windowed
        members = voice_data.get("members") or []
        temp_dir = tempfile.mkdtemp(prefix="unison_")
        try:
            member_paths = []
            for i, member in enumerate(resolved_members):
                member_path = os.path.join(temp_dir, f"member_{i}.wav")
                if not self._dispatch_prepared_voice(text, instruct_text, member,
                                                    voice_config, member_path):
                    raise RuntimeError(
                        f"Ensemble member '{member}' failed to generate.")
                member_paths.append(member_path)

            print(f"  Unison: mixing {len(member_paths)} voices ({', '.join(members)})")
            success = mix_to_unison(member_paths, output_path)
            if success and mix_to_unison is _RAW_MIX_TO_UNISON:
                capture_composite_speech_input(member_paths, output_path, text, 'ensemble')
            return success
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ── Voice design generation ──────────────────────────────────

    @ensure_local_tts_serialized
    @capture_tts_operation("generate_voice_design")
    def generate_voice_design(self, description, sample_text, language=None, seed=-1):
        """Generate a voice from a text description using the VoiceDesign model.

        Args:
            description: Natural language description of the desired voice
            sample_text: Text to synthesize with the designed voice
            language: Language code (defaults to engine's configured language)
            seed: Random seed (-1 for random, >= 0 for reproducible)

        Returns:
            (wav_path, sample_rate) on success

        Raises:
            RuntimeError: If generation fails
        """
        self.get_voice_backend("design")
        import time
        import torch

        lang = language or self._language
        print(f"VoiceDesign: generating preview for description='{description[:80]}...'"
              f"{f', seed={seed}' if seed >= 0 else ''}")

        model = self._init_local_design()

        if seed >= 0:
            torch.manual_seed(seed)

        t_start = time.time()
        with measure_tts_stage_time("inference_decode"):
            wavs, sr = model.generate_voice_design(
                text=sample_text,
                instruct=description,
                language=lang,
                non_streaming_mode=True,
                max_new_tokens=self._max_new_tokens,
            )
        gen_time = time.time() - t_start

        if wavs is None or len(wavs) == 0:
            raise RuntimeError("VoiceDesign model returned no audio")

        audio = np.concatenate(wavs) if len(wavs) > 1 else wavs[0]
        duration = len(audio) / sr
        print(f"VoiceDesign: done in {gen_time:.1f}s -> {duration:.1f}s audio")

        # Save to previews directory (under the runtime data dir, matching where
        # app.py's /api/voice_design/save reads it back from).
        previews_dir = _resolve_asset_path(os.path.join("designed_voices", "previews"))
        os.makedirs(previews_dir, exist_ok=True)

        filename = f"preview_{uuid.uuid4().hex}.wav"
        wav_path = os.path.join(previews_dir, filename)
        self._save_wav(audio, sr, wav_path)
        capture_speech_input(wav_path, sample_text, lang, 'local_design_api')

        return wav_path, sr

    @capture_tts_operation("generate_design_voice")
    def generate_design_voice(self, text, instruct_text, voice_data, output_path):
        """Generate audio using VoiceDesign model with combined description + instruct.

        The voice_data 'description' field provides the base voice identity,
        and the per-line instruct_text is appended for delivery/emotion direction.
        """
        prepared = get_speech_preparation(text, instruct_text)
        return self._generate_design_voice_prepared(
            prepared["text"], prepared["instruct"], voice_data, output_path)

    def _generate_design_voice_prepared(self, text, instruct_text, voice_data, output_path):
        self.get_voice_backend("design")
        windowed = self._render_synthesis_windows(
            text, "design", lambda span, path: self._generate_design_voice_prepared(
                span, instruct_text, voice_data, path), output_path)
        if windowed is not None:
            return windowed
        import shutil

        base_desc = (voice_data.get("description") or "").strip()
        instruct = (anchored_instruct(voice_data, instruct_text)
                    if (voice_data.get("character_style") or "").strip()
                    else (instruct_text or "").strip())

        if base_desc and instruct:
            description = f"{base_desc}, {instruct}"
        elif base_desc:
            description = base_desc
        elif instruct:
            description = instruct
        else:
            print("Warning: Design voice has no description or instruct. Using generic.")
            description = "A clear, natural speaking voice"

        seed = int(voice_data.get("seed", -1))
        wav_path, sr = self.generate_voice_design(description=description, sample_text=text, seed=seed)
        speech_input = get_captured_speech_input(wav_path)
        staging_path = f"{output_path}.pending.{uuid.uuid4().hex}"
        try:
            shutil.copy2(wav_path, staging_path)
            success = publish_audio_output(staging_path, output_path)
            if success:
                promote_speech_input(speech_input, output_path)
            return success
        finally:
            try:
                os.remove(staging_path)
            except FileNotFoundError:
                pass
            except OSError as error:
                logging.getLogger(__name__).warning("Design staging cleanup failed at %s: %s", staging_path, error)
            # A generated preview is throwaway even when output copying fails.
            if os.path.abspath(wav_path) != os.path.abspath(output_path):
                try:
                    os.remove(wav_path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    logging.getLogger(__name__).warning("Design preview cleanup failed at %s: %s", wav_path, error)

    # ── LoRA voice generation ────────────────────────────────────

    @capture_tts_operation("generate_lora_voice")
    def generate_lora_voice(self, text, instruct_text, voice_data, output_path):
        """Generate audio using a LoRA-finetuned Base model.

        The adapter directory must contain:
          - PEFT adapter weights (adapter_model.safetensors / adapter_config.json)
          - ref_sample.wav (reference audio for voice cloning prompt)
          - training_meta.json (with ref_sample_text)

        The LoRA weights refine voice identity beyond what the reference alone provides.
        """
        prepared = get_speech_preparation(text, instruct_text)
        return self._generate_lora_voice_prepared(
            prepared["text"], prepared["instruct"], voice_data, output_path)

    @ensure_local_tts_serialized
    def _generate_lora_voice_prepared(self, text, instruct_text, voice_data, output_path):
        try:
            self.get_voice_backend("lora")
            windowed = self._render_synthesis_windows(
                text, "lora", lambda span, path: self._generate_lora_voice_prepared(
                    span, instruct_text, voice_data, path), output_path)
            if windowed is not None:
                return windowed
            import time

            adapter_path = voice_data.get("adapter_path")
            if not adapter_path:
                print(f"Error: No adapter_path in voice_data")
                apply_tts_failure("LoRA voice has no adapter_path.", "missing_configuration",
                                  "Assign a trained adapter before retrying.")
                return False

            # Resolve relative paths against project root
            if not os.path.isabs(adapter_path):
                adapter_path = _resolve_asset_path(adapter_path)
            adapter_path = get_resolved_adapter_path(adapter_path)

            if (not os.path.isdir(adapter_path)
                    and os.path.basename(adapter_path).startswith("builtin_")):
                # Auto-download built-in adapters from HF
                adapter_id = os.path.basename(adapter_path)
                if adapter_id.startswith("builtin_"):
                    print(f"Adapter {adapter_id} not downloaded, attempting auto-download...")
                    try:
                        try:
                            from .hf_utils import download_builtin_adapter
                        except ImportError:
                            from hf_utils import download_builtin_adapter
                        builtin_dir = os.path.dirname(adapter_path)
                        download_builtin_adapter(adapter_id, builtin_dir)
                    except Exception as e:
                        print(f"Error: Auto-download failed for {adapter_id}: {e}")
                        apply_tts_failure(e)
                        return False

            # User adapters are checked inside snapshot admission, where an ID
            # renamed after the lookup above can still resolve to its bundle.
            model, prompt = self._ensure_local_lora_generation(adapter_path, voice_data.get("adapter_generation_sha256"))
            print(f"TTS [local lora] generating for adapter={os.path.basename(adapter_path)}, "
                  f"text='{text[:50]}...'")

            # Build instruct_ids so the Base model can follow style prompts
            gen_extra = {}
            instruct = instruct_text or ""
            character_style = voice_data.get("character_style", "") or voice_data.get("default_style", "")
            if character_style:
                instruct = f"{instruct} {character_style}".strip()
            if instruct:
                instruct_formatted = f"<|im_start|>user\n{instruct}<|im_end|>\n"
                gen_extra["instruct_ids"] = model._tokenize_texts([instruct_formatted])

            # THE LORA PATH NEVER SEEDED. generate_voice_design,
            # _local_generate_custom and _local_generate_clone all read
            # voice_data["seed"] and call torch.manual_seed; this one did not,
            # in 121 lines, so the seed field was silently ignored for every
            # `lora` voice - 22 characters including NARRATOR, which speaks
            # 1,581 of 2,606 lines. Each line was an independent draw of the
            # voice, which is audible as the narrator changing between
            # paragraphs, and it made every A/B on this path uncontrolled:
            # the same input produced an 18% swing in clip length.
            #
            # Verified before fixing: torch.manual_seed alone IS sufficient
            # here. Seeding externally before three calls gave byte-identical
            # output, so neither the cached clone prompt nor the ROCm kernels
            # add nondeterminism of their own.
            import torch
            seed = int(voice_data.get("seed", -1))
            if seed >= 0:
                torch.manual_seed(seed)

            t_start = time.time()
            with measure_tts_stage_time("inference_decode"):
                wavs, sr = model.generate_voice_clone(
                    text=text,
                    language=self._language,
                    voice_clone_prompt=prompt,
                    non_streaming_mode=True,
                    max_new_tokens=self._max_new_tokens,
                    **gen_extra,
                )
            gen_time = time.time() - t_start

            if wavs is None or len(wavs) == 0:
                print(f"Error: No audio generated for: '{text[:50]}...'")
                apply_tts_failure("TTS returned no audio.", "invalid_audio")
                return False

            audio = np.concatenate(wavs) if len(wavs) > 1 else wavs[0]
            duration = len(audio) / sr
            rtf = duration / gen_time if gen_time > 0 else 0
            print(f"TTS [local lora] done: {gen_time:.1f}s -> {duration:.1f}s audio ({rtf:.2f}x real-time)")
            self._save_wav(audio, sr, output_path)
            capture_speech_input(output_path, text, self._language, 'local_lora_api')
            return True

        except Exception as e:
            import traceback
            print(f"Error generating LoRA voice: {e}")
            traceback.print_exc()
            apply_tts_failure(e)
            return False

    @ensure_local_tts_serialized
    def _ensure_local_lora_generation(self, adapter_path, expected_generation=None):
        """Load metadata, model and prompt from one admitted private serving snapshot."""
        with ensure_adapter_generation_snapshot(adapter_path) as (snapshot, generation):
            if expected_generation is not None and generation != expected_generation:
                raise ValueError('Adapter generation changed since benchmark fixture admission')
            meta = get_adapter_serving_metadata(snapshot)
            model = self._init_local_lora(snapshot, generation_sha256=generation, source_adapter_path=adapter_path)
            prompt = self._ensure_lora_prompt(snapshot, model, meta['ref_sample_text'], generation_sha256=generation, source_adapter_path=adapter_path)
            # A provider must not mutate the captured serving evidence while loading.
            if get_adapter_generation_sha256(snapshot) != generation:
                self._lora_prompt_cache.pop((adapter_path, generation), None)
                with self._model_lock:
                    if self._lora_adapter_path == adapter_path and self._lora_generation_sha256 == generation:
                        self._lora_generation_sha256 = None
                raise ValueError('Captured adapter generation changed while loading')
            return model, prompt

    @ensure_local_tts_serialized
    @measure_tts_stage("reference_preparation")
    def _ensure_lora_prompt(self, adapter_path, model, ref_text, generation_sha256=None, source_adapter_path=None):
        """Build and cache the clone prompt used by LoRA inference."""
        source_adapter_path = source_adapter_path or adapter_path
        if generation_sha256 is None:
            with ensure_adapter_generation_snapshot(adapter_path) as (snapshot, generation):
                return self._ensure_lora_prompt(snapshot, model, ref_text, generation, source_adapter_path)
        cache_key = (source_adapter_path, generation_sha256)
        record_tts_cache(cache_key in self._lora_prompt_cache)
        if cache_key not in self._lora_prompt_cache:
            audio_array, sample_rate = sf.read(os.path.join(adapter_path, "ref_sample.wav"))
            if audio_array.ndim > 1:
                audio_array = audio_array.mean(axis=1)
            print("Creating clone prompt for LoRA adapter...")
            prompt = model.create_voice_clone_prompt(
                ref_audio=(audio_array, sample_rate),
                ref_text=ref_text,
                x_vector_only_mode=True,
            )
            self._lora_prompt_cache[cache_key] = prompt
            print("Clone prompt cached for LoRA adapter.")
        return self._lora_prompt_cache[cache_key]

    def _generate_prepared_batch_parent(self, chunk, voice_config, output_dir, batch_seed):
        idx = chunk["index"]
        output_path = os.path.join(output_dir, f"temp_batch_{idx}.wav")
        resolved = get_batch_voice_config(voice_config, chunk.get("speaker"), idx, batch_seed)
        with (ensure_synthesis_recovery_batch_parent(chunk.get("uid")),
              ensure_synthesis_receipts() as receipts):
            success = self._dispatch_prepared_voice(
                chunk["text"], chunk.get("instruct", ""), chunk.get("speaker"), resolved, output_path)
        return success, receipts[-1] if success and receipts else None

    # ── Batch generation ─────────────────────────────────────────

    @capture_tts_operation("generate_batch")
    @capture_batch_speech_inputs
    def generate_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Generate multiple audio files.

        Local mode: uses native list-based batch API for custom voices.
        External mode: sequential individual calls.

        Args:
            chunks: List of dicts with 'text', 'instruct', 'speaker', 'index' keys
            voice_config: Voice configuration dict
            output_dir: Directory to save output files
            batch_seed: Single seed for all generations (-1 for random)

        Returns:
            dict with 'completed' (list of indices) and 'failed' (list of (index, error) tuples)
        """
        results = {"completed": [], "failed": []}

        if not chunks or is_audio_generation_cancelled():
            return results

        # Resolve each row before its one canonical speech preparation. Stable
        # UID/rules remain on copies through native, external and fallback paths.
        dynamic_chunks = []
        for chunk in chunks:
            source_speaker = chunk.get("_source_speaker", chunk.get("speaker"))
            try:
                speaker = get_resolved_speaker(chunk.get("speaker"), voice_config)
                chunk = {**chunk, "speaker": speaker, "_source_speaker": source_speaker}
                resolved = resolve_narrator_voice_config(speaker, voice_config, chunk)
                if not is_voice_config_present(speaker, resolved):
                    results["failed"].append((chunk["index"],
                        f"No voice configuration for '{speaker}'."))
                    continue
                effective_config = voice_config_for_chunk(resolved, speaker, chunk["index"])
                self.get_voice_backend(voice_category(effective_config.get(speaker)))
            except (UnsupportedVoiceBackendError, ValueError, TypeError, OverflowError) as error:
                results["failed"].append((chunk["index"], str(error)))
                continue
            context = (get_pronunciation_render_context(
                voice_config, chunk, chunk["index"],
                {"mode": self._mode, "language": self._language},
                source_speaker=chunk.get("_source_speaker", chunk.get("speaker")))
                if chunk.get("pronunciation_rules") else None)
            with ensure_occurrence_pronunciation_context(chunk, context):
                prepared = get_speech_preparation(chunk.get("text"), chunk.get("instruct", ""))
            dynamic_chunks.append(({**chunk, **prepared}, resolved))
        chunks = [chunk for chunk, _ in dynamic_chunks]
        if not chunks:
            return results
        if any(resolved is not voice_config for _, resolved in dynamic_chunks):
            # A dynamic narrator may use different configs per chunk; keep those
            # chunks on the established single-item path to avoid mixing voices.
            for chunk, resolved in dynamic_chunks:
                if is_audio_generation_cancelled():
                    return results
                if resolved is voice_config:
                    continue
                idx = chunk["index"]
                try:
                    success, receipt = self._generate_prepared_batch_parent(chunk, resolved, output_dir, batch_seed)
                    if success:
                        results["completed"].append(idx)
                        if receipt is not None:
                            results.setdefault("synthesis_receipts", {})[idx] = receipt
                    else:
                        results["failed"].append((idx, "Narrator voice generation failed"))
                except Exception as e:
                    results["failed"].append((idx, str(e)))
            chunks = [chunk for chunk, resolved in dynamic_chunks if resolved is voice_config]
            if not chunks:
                return results

        # Reset torch.compile state to prevent progressive slowdown
        # from dynamo guard accumulation across batches
        if self._compile_codec_enabled:
            self._reset_compile_cache()

        # Split parents keep their original index/config; unsplit rows retain
        # native list batching. Speech preparation has already run exactly once.
        if getattr(self, "_synthesis_windows", None) is not None:
            native_chunks = []
            for chunk in chunks:
                if is_audio_generation_cancelled():
                    return results
                idx, speaker = chunk["index"], chunk.get("speaker")
                try:
                    effective = get_batch_voice_config(voice_config, speaker, idx, batch_seed)
                    data = effective.get(speaker, {})
                    plan = self._get_synthesis_window_plan(chunk["text"], voice_category(data), data, effective)
                    if plan is None:
                        native_chunks.append(chunk)
                        continue
                    success, receipt = self._generate_prepared_batch_parent(chunk, voice_config, output_dir, batch_seed)
                    if success:
                        results["completed"].append(idx)
                        if receipt is not None:
                            results.setdefault("synthesis_receipts", {})[idx] = receipt
                    else:
                        results["failed"].append((idx, "Internal synthesis window generation failed"))
                except Exception as error:
                    results["failed"].append((idx, str(error)))
            chunks = native_chunks
            if not chunks:
                return results

        # Separate chunks by voice type
        custom_chunks = []
        clone_chunks = []
        lora_chunks = []
        design_chunks = []
        ensemble_chunks = []

        for chunk in chunks:
            speaker = chunk.get("speaker")
            voice_data = voice_config_for_chunk(voice_config, speaker, chunk["index"]).get(speaker, {})
            category = voice_category(voice_data)

            if category == "clone":
                clone_chunks.append(chunk)
            elif category == "lora":
                lora_chunks.append(chunk)
            elif category == "design":
                design_chunks.append(chunk)
            elif category == "ensemble":
                ensemble_chunks.append(chunk)
            else:
                custom_chunks.append(chunk)

        # Ensemble chunks need several different voices each, so they can't join
        # a same-voice batch — render them one at a time.
        if ensemble_chunks:
            if is_audio_generation_cancelled():
                return results
            batch_results = self._sequential_ensemble(ensemble_chunks, voice_config, output_dir, batch_seed)
            results["completed"].extend(batch_results["completed"])
            results["failed"].extend(batch_results["failed"])
            self._clear_gpu_cache()

        # Process custom voice chunks
        if custom_chunks:
            if is_audio_generation_cancelled():
                return results
            if self.get_voice_backend("custom") == "local":
                batch_results = self._local_batch_custom(custom_chunks, voice_config, output_dir, batch_seed)
            else:
                batch_results = self._external_batch(custom_chunks, voice_config, output_dir, "custom", batch_seed)
            results["completed"].extend(batch_results["completed"])
            results["failed"].extend(batch_results["failed"])
            self._clear_gpu_cache()

        # Process clone voice chunks (batched by speaker in local mode)
        if clone_chunks:
            if is_audio_generation_cancelled():
                return results
            if self.get_voice_backend("clone") == "local":
                batch_results = self._local_batch_clone(clone_chunks, voice_config, output_dir, batch_seed)
            else:
                batch_results = self._external_batch(clone_chunks, voice_config, output_dir, "clone", batch_seed)
            results["completed"].extend(batch_results["completed"])
            results["failed"].extend(batch_results["failed"])
            self._clear_gpu_cache()

        # Process LoRA voice chunks (batched by adapter in local mode)
        if lora_chunks:
            if is_audio_generation_cancelled():
                return results
            batch_results = self._local_batch_lora(lora_chunks, voice_config, output_dir, batch_seed)
            results["completed"].extend(batch_results["completed"])
            results["failed"].extend(batch_results["failed"])
            self._clear_gpu_cache()

        # Process design voice chunks (sequential — each line has unique description)
        if design_chunks:
            if is_audio_generation_cancelled():
                return results
            for chunk in design_chunks:
                if is_audio_generation_cancelled():
                    return results
                idx = chunk["index"]
                output_path = os.path.join(output_dir, f"temp_batch_{idx}.wav")
                speaker = chunk.get("speaker")
                voice_data = get_batch_voice_config(voice_config, speaker, idx, batch_seed).get(speaker, {})
                try:
                    render = self.generate_design_voice
                    if (isinstance(render, MethodType) and render.__self__ is self
                            and render.__func__ is _RAW_DESIGN_VOICE):
                        render = self._generate_design_voice_prepared
                    success = render(
                        text=chunk["text"], instruct_text=chunk.get("instruct", ""),
                        voice_data=voice_data, output_path=output_path,
                    )
                    if success:
                        results["completed"].append(idx)
                    else:
                        results["failed"].append((idx, "Design voice generation failed"))
                except Exception as e:
                    results["failed"].append((idx, str(e)))

        return results

    # ── Connection test ──────────────────────────────────────────

    def set_sub_batch_size(self, max_items: int):
        """Set the sub-batch size for benchmarking. Public wrapper for _sub_batch_max_items."""
        self._sub_batch_max_items = max_items

    @capture_tts_operation("run_benchmark_batch")
    def run_benchmark_batch(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Run a benchmark batch generation. Public wrapper for _local_batch_custom."""
        return self._local_batch_custom(chunks, voice_config, output_dir, batch_seed)

    @capture_tts_operation("run_clone_benchmark_batch")
    def run_clone_benchmark_batch(self, chunks, voice_config, output_dir,
                                  batch_seed=-1):
        """Run a benchmark batch through the production clone implementation."""
        return self._local_batch_clone(chunks, voice_config, output_dir, batch_seed)

    @ensure_local_tts_serialized
    def enable_codec_compilation(self):
        """Enable torch.compile for codec. Public wrapper for internal compilation."""
        if hasattr(self, '_compile_codec_enabled') and hasattr(self, '_compile_codec'):
            self._compile_codec_enabled = True
            if getattr(self, '_local_custom_model', None) is not None:
                self._compile_codec(self._local_custom_model)

    # ── Local backend methods ────────────────────────────────────

    @ensure_local_tts_serialized
    def _local_generate_custom(self, text, instruct_text, speaker, voice_config, output_path):
        """Generate custom voice audio using local Qwen3-TTS model."""
        try:
            import torch

            request = get_custom_voice_request(voice_config, speaker, instruct_text)
            if request is None:
                print(f"Warning: No voice configuration for '{speaker}'. Skipping.")
                apply_tts_failure(f"No voice configuration for '{speaker}'.", "missing_configuration",
                                  "Assign a voice to this speaker before retrying.")
                return False

            voice, seed, instruct = request.voice, request.seed, request.instruct

            import time

            print(f"TTS [local] generating with instruct='{instruct}' for text='{text[:50]}...'")

            model = self._init_local_custom()

            ensure_native_recovery_serving_current(self, request)
            if seed >= 0:
                torch.manual_seed(seed)

            t_start = time.time()
            with measure_tts_stage_time("inference_decode"):
                wavs, sr = model.generate_custom_voice(
                    text=text,
                    language=self._language,
                    speaker=voice,
                    instruct=instruct,
                    non_streaming_mode=True,
                    max_new_tokens=self._max_new_tokens,
                )
            gen_time = time.time() - t_start

            if wavs is None or len(wavs) == 0:
                print(f"Error: No audio generated for: '{text[:50]}...'")
                apply_tts_failure("TTS returned no audio.", "invalid_audio")
                return False

            # wavs is a list of numpy arrays; concatenate them
            audio = np.concatenate(wavs) if len(wavs) > 1 else wavs[0]
            duration = len(audio) / sr
            rtf = duration / gen_time if gen_time > 0 else 0
            print(f"TTS [local] done: {gen_time:.1f}s -> {duration:.1f}s audio ({rtf:.2f}x real-time)")
            self._save_wav(audio, sr, output_path)
            capture_speech_input(output_path, text, self._language, 'local_custom_api')
            return True

        except SynthesisRecoveryDependencyChanged:
            raise
        except Exception as e:
            import traceback
            print(f"Error generating custom voice for '{speaker}': {e}")
            traceback.print_exc()
            apply_tts_failure(e)
            return False

    @ensure_local_tts_serialized
    def _local_generate_clone(self, text, speaker, voice_config, output_path):
        """Generate voice-cloned audio using local Qwen3-TTS Base model."""
        try:
            import torch

            voice_data = voice_config.get(speaker)
            if not voice_data:
                print(f"Warning: No voice configuration for '{speaker}'. Skipping.")
                apply_tts_failure(f"No voice configuration for '{speaker}'.", "missing_configuration",
                                  "Assign a voice to this speaker before retrying.")
                return False

            seed = int(voice_data.get("seed", -1))

            import time

            print(f"TTS [local clone] generating for speaker='{speaker}', text='{text[:50]}...'")

            prompt = self._get_clone_prompt(speaker, voice_config)
            model = self._init_local_clone()

            if seed >= 0:
                torch.manual_seed(seed)

            t_start = time.time()
            with measure_tts_stage_time("inference_decode"):
                wavs, sr = model.generate_voice_clone(
                    text=text,
                    language=self._language,
                    voice_clone_prompt=prompt,
                    non_streaming_mode=True,
                    max_new_tokens=self._max_new_tokens,
                )
            gen_time = time.time() - t_start

            if wavs is None or len(wavs) == 0:
                print(f"Error: No audio generated for: '{text[:50]}...'")
                apply_tts_failure("TTS returned no audio.", "invalid_audio")
                return False

            audio = np.concatenate(wavs) if len(wavs) > 1 else wavs[0]
            duration = len(audio) / sr
            rtf = duration / gen_time if gen_time > 0 else 0
            print(f"TTS [local clone] done: {gen_time:.1f}s -> {duration:.1f}s audio ({rtf:.2f}x real-time)")
            self._save_wav(audio, sr, output_path)
            capture_speech_input(output_path, text, self._language, 'local_clone_api')
            return True

        except Exception as e:
            import traceback
            print(f"Error generating clone voice for '{speaker}': {e}")
            traceback.print_exc()
            apply_tts_failure(e)
            return False

    def save_batch_waveforms(self, waveforms, indices, sample_rate, output_dir, log_saved=False):
        """Publish available waveforms and return independent results and duration."""
        result = {"completed": [], "failed": []}
        for index in indices[len(waveforms):]:
            result["failed"].append((index, "Batch returned no waveform for this chunk"))
        duration = 0.0
        for waveform, index in zip(waveforms, indices):
            try:
                path = os.path.join(output_dir, f"temp_batch_{index}.wav")
                audio = self._concat_audio(waveform)
                self._save_wav(audio, sample_rate, path)
                result["completed"].append(index)
                clip_duration = len(audio) / sample_rate
                duration += clip_duration
                if log_saved:
                    print(f"    Chunk {index} saved: {os.path.getsize(path)} bytes ({clip_duration:.1f}s audio)")
            except Exception as error:
                print(f"    Error saving chunk {index}: {error}")
                result["failed"].append((index, str(error)))
        return result, duration

    def save_submitted_batch_waveforms(self, waveforms, indices, texts, sample_rate, output_dir,
                                      category, log_saved=False):
        result, duration = self.save_batch_waveforms(waveforms, indices, sample_rate, output_dir,
                                                    log_saved=log_saved)
        render = self.save_batch_waveforms
        if (is_speech_input_capture_active() and len(waveforms) <= len(indices) == len(texts)
                and isinstance(render, MethodType) and render.__func__ is _RAW_SAVE_BATCH_WAVEFORMS):
            submitted = dict(zip(indices, texts))
            for index in result['completed']:
                if index in submitted:
                    capture_speech_input(os.path.join(output_dir, f'temp_batch_{index}.wav'),
                        submitted[index], self._language, 'local_' + category + '_batch_api')
        return result, duration

    @ensure_local_tts_serialized
    def _local_batch_custom(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Batch generate custom voice using native list API with sub-batching.

        Autoregressive batch generation runs for as long as the longest sequence.
        Shorter sequences waste compute on padding. To minimize this, chunks are
        sorted by text length and split into sub-batches when the length ratio
        exceeds the configured threshold. Sub-batching can be disabled entirely
        via config, in which case everything runs as one batch.
        """
        import torch
        import time

        results = {"completed": [], "failed": []}
        batch_peak_vram_gb = 0.0

        texts = []
        speakers = []
        instructs = []
        indices = []

        for chunk in chunks:
            idx = chunk["index"]
            text = chunk.get("text", "")
            instruct_text = chunk.get("instruct", "")
            speaker_name = chunk.get("speaker", "")

            voice_data = voice_config_for_chunk(voice_config, speaker_name, idx).get(speaker_name, {})
            voice = voice_data.get("voice", "Ryan")
            instruct = anchored_instruct(voice_data, instruct_text)

            texts.append(text)
            speakers.append(voice)
            instructs.append(instruct)
            indices.append(idx)

        total_text_chars = sum(len(t) for t in texts)

        # Sort by text length to group similar-length chunks together.
        # This reduces wasted padding during autoregressive generation
        # (the LLM runs until ALL sequences finish, so short chunks
        # waste compute waiting for long ones).
        sort_order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        texts = [texts[i] for i in sort_order]
        speakers = [speakers[i] for i in sort_order]
        instructs = [instructs[i] for i in sort_order]
        indices = [indices[i] for i in sort_order]

        model = self._init_local_custom()
        measure_gpu_peak = torch.cuda.is_available() and self.get_memory_device(model) is not None

        # Warmup on first batch to pre-tune MIOpen/GPU solvers
        self.ensure_custom_warmup(model)

        # Clear stale GPU cache from any prior generation to avoid
        # fragmented VRAM blocking large batch allocations (ROCm especially).
        self._clear_gpu_cache()


        max_items = self._estimate_max_batch_size(
            model, max_text_chars=len(texts[-1]),
        )
        sub_batches = self._build_sub_batches(texts, max_items=max_items)

        print(f"Batch [local]: generating {len(texts)} chunks ({total_text_chars} chars) "
              f"in {len(sub_batches)} sub-batch(es)...")

        t_total_start = time.time()
        total_audio_duration = 0.0

        for sb_idx, (start, end) in enumerate(sub_batches):
            sb_texts = texts[start:end]
            sb_speakers = speakers[start:end]
            sb_instructs = instructs[start:end]
            sb_indices = indices[start:end]
            sb_chars = sum(len(t) for t in sb_texts)

            print(f"  Sub-batch {sb_idx+1}/{len(sub_batches)}: {len(sb_texts)} chunks "
                  f"({sb_chars} chars, {len(sb_texts[0])}-{len(sb_texts[-1])} chars/chunk)")

            try:
                if batch_seed >= 0:
                    torch.manual_seed(batch_seed)

                if measure_gpu_peak:
                    torch.cuda.reset_peak_memory_stats(self.get_memory_device(model))
                t_start = time.time()
                with measure_tts_stage_time("inference_decode"):
                    wavs_list, sr = model.generate_custom_voice(
                        text=sb_texts,
                        language=[self._language] * len(sb_texts),
                        speaker=sb_speakers,
                        instruct=sb_instructs,
                        non_streaming_mode=True,
                        max_new_tokens=self._max_new_tokens,
                    )
                gen_time = time.time() - t_start
                if measure_gpu_peak:
                    peak_gb = torch.cuda.max_memory_allocated(self.get_memory_device(model)) / 1e9
                    batch_peak_vram_gb = max(batch_peak_vram_gb, peak_gb)
                    print(f"  Peak VRAM sub-batch {sb_idx+1}: {peak_gb:.2f} GB")

                if wavs_list is None:
                    for idx in sb_indices:
                        results["failed"].append((idx, "Batch returned None"))
                    continue

                saved, sb_audio_duration = self.save_submitted_batch_waveforms(
                    wavs_list, sb_indices, sb_texts, sr, output_dir, 'custom', log_saved=True)
                results["completed"].extend(saved["completed"])
                results["failed"].extend(saved["failed"])

                total_audio_duration += sb_audio_duration
                sb_rtf = sb_audio_duration / gen_time if gen_time > 0 else 0
                print(f"  Sub-batch {sb_idx+1} done: {gen_time:.1f}s -> {sb_audio_duration:.1f}s audio ({sb_rtf:.2f}x RT)")

            except Exception as e:
                print(f"  Sub-batch {sb_idx+1} failed: {e}")
                for idx in sb_indices:
                    results["failed"].append((idx, f"Batch error: {e}"))

            # Free GPU memory between sub-batches to prevent VRAM exhaustion
            self._clear_gpu_cache()

        total_time = time.time() - t_total_start
        rtf = total_audio_duration / total_time if total_time > 0 else 0
        print(f"Batch total: {total_time:.1f}s -> {total_audio_duration:.1f}s audio ({rtf:.2f}x real-time)")



        results["peak_vram_gb"] = round(batch_peak_vram_gb, 2)
        return results

    @ensure_local_tts_serialized
    def _local_batch_clone(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Batch generate clone voices, grouped by speaker.

        Chunks sharing the same speaker (same reference audio) are batched
        together through generate_voice_clone(text=[list], ...).
        Sub-batching by text length is applied within each speaker group.
        """
        import torch
        import time

        results = {"completed": [], "failed": []}
        batch_peak_vram_gb = 0.0

        # Group only chunks that resolve to the same speaker and clone reference.
        speaker_groups = {}
        for chunk in chunks:
            speaker = chunk.get("speaker", "")
            try:
                selected = voice_config_for_chunk(voice_config, speaker, chunk["index"])
                data = selected.get(speaker, {})
                key = json.dumps([speaker, data.get("ref_audio"), data.get("ref_text")],
                                 sort_keys=True)
                speaker_groups.setdefault(key, (speaker, selected, []))[2].append(chunk)
            except (ValueError, TypeError, OverflowError) as error:
                results["failed"].append((chunk["index"], str(error)))

        model = self._init_local_clone()
        measure_gpu_peak = torch.cuda.is_available() and self.get_memory_device(model) is not None

        self._clear_gpu_cache()


        t_total_start = time.time()
        total_audio_duration = 0.0

        for speaker, selected, group in speaker_groups.values():
            try:
                prompt = self._get_clone_prompt(speaker, selected)
            except Exception as e:
                print(f"  Error building clone prompt for '{speaker}': {e}")
                for chunk in group:
                    results["failed"].append((chunk["index"], str(e)))
                continue

            texts = [c["text"] for c in group]
            indices = [c["index"] for c in group]

            # Sort by text length for sub-batching efficiency
            sort_order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
            texts = [texts[i] for i in sort_order]
            indices = [indices[i] for i in sort_order]

            # Estimate max batch size from VRAM + clone prompt overhead
            clone_tokens = prompt[0].ref_code.shape[0] if prompt[0].ref_code is not None else 0
            ref_text_chars = len(prompt[0].ref_text) if prompt[0].ref_text else 0
            max_items = self._estimate_max_batch_size(
                model, clone_tokens, ref_text_chars, len(texts[-1]),
            )
            sub_batches = self._build_sub_batches(texts, max_items=max_items)

            print(f"Batch [clone] speaker='{speaker}': {len(texts)} chunks "
                  f"in {len(sub_batches)} sub-batch(es)")

            for sb_idx, (start, end) in enumerate(sub_batches):
                sb_texts = texts[start:end]
                sb_indices = indices[start:end]

                print(f"  Sub-batch {sb_idx+1}/{len(sub_batches)}: {len(sb_texts)} chunks "
                      f"({len(sb_texts[0])}-{len(sb_texts[-1])} chars/chunk)")

                try:
                    # Seed for reproducible batch output when batch_seed is set
                    # (matches _local_batch_custom); otherwise random each run.
                    if batch_seed >= 0:
                        torch.manual_seed(batch_seed)
                    if measure_gpu_peak:
                        torch.cuda.reset_peak_memory_stats(self.get_memory_device(model))
                    t_start = time.time()
                    with measure_tts_stage_time("inference_decode"):
                        wavs_list, sr = model.generate_voice_clone(
                            text=sb_texts,
                            language=self._language,
                            voice_clone_prompt=prompt,
                            non_streaming_mode=True,
                            max_new_tokens=self._max_new_tokens,
                        )
                    gen_time = time.time() - t_start
                    if measure_gpu_peak:
                        peak_gb = torch.cuda.max_memory_allocated(self.get_memory_device(model)) / 1e9
                        batch_peak_vram_gb = max(batch_peak_vram_gb, peak_gb)
                        print(f"  Peak VRAM clone sub-batch {sb_idx+1}: {peak_gb:.2f} GB")

                    if wavs_list is None:
                        for idx in sb_indices:
                            results["failed"].append((idx, "Batch returned None"))
                        continue

                    saved, sb_audio_duration = self.save_submitted_batch_waveforms(
                        wavs_list, sb_indices, sb_texts, sr, output_dir, 'clone')
                    results["completed"].extend(saved["completed"])
                    results["failed"].extend(saved["failed"])

                    total_audio_duration += sb_audio_duration
                    sb_rtf = sb_audio_duration / gen_time if gen_time > 0 else 0
                    print(f"  Sub-batch {sb_idx+1} done: {gen_time:.1f}s -> {sb_audio_duration:.1f}s audio ({sb_rtf:.2f}x RT)")

                except Exception as e:
                    print(f"  Sub-batch {sb_idx+1} failed: {e}")
                    for idx in sb_indices:
                        results["failed"].append((idx, f"Batch error: {e}"))

                self._clear_gpu_cache()

        total_time = time.time() - t_total_start
        rtf = total_audio_duration / total_time if total_time > 0 else 0
        print(f"Batch [clone] total: {total_time:.1f}s -> {total_audio_duration:.1f}s audio ({rtf:.2f}x real-time)")



        results["peak_vram_gb"] = round(batch_peak_vram_gb, 2)
        return results

    @ensure_local_tts_serialized
    def _local_batch_lora(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Batch generate LoRA voices, grouped by adapter.

        Chunks sharing the same adapter are batched together through
        generate_voice_clone(text=[list], instruct_ids=[list], ...).
        Sub-batching by text length is applied within each adapter group.
        """
        import torch
        import time

        results = {"completed": [], "failed": []}

        # Group chunks by adapter_path (resolved to absolute). Two different
        # speakers can share the same adapter (e.g. aliases, or one trained
        # voice reused for two characters) while having different
        # character_style/default_style - so each chunk keeps its own
        # voice_data instead of the group inheriting just the first chunk's.
        adapter_groups = {}  # (adapter_path, generation pin) -> [(chunk, voice_data), ...]
        for chunk in chunks:
            speaker = chunk.get("speaker", "")
            voice_data = voice_config_for_chunk(voice_config, speaker, chunk["index"]).get(speaker, {})
            adapter_path = voice_data.get("adapter_path", "")

            if not adapter_path:
                results["failed"].append((chunk["index"], "No adapter_path"))
                continue

            if not os.path.isabs(adapter_path):
                adapter_path = _resolve_asset_path(adapter_path)
            adapter_path = get_resolved_adapter_path(adapter_path)

            adapter_groups.setdefault((adapter_path, voice_data.get("adapter_generation_sha256")), []).append((chunk, voice_data))

        self._clear_gpu_cache()

        t_total_start = time.time()
        total_audio_duration = 0.0

        for (adapter_path, expected_generation), group_entries in adapter_groups.items():
            group = [c for c, _ in group_entries]
            if (not os.path.isdir(adapter_path)
                    and os.path.basename(adapter_path).startswith("builtin_")):
                # Auto-download built-in adapters from HF, mirroring the
                # single-chunk generate_lora_voice path — otherwise a built-in
                # LoRA voice that hasn't been fetched yet fails every chunk of a
                # batch render while single-chunk regen of the same voice works.
                adapter_id = os.path.basename(adapter_path)
                downloaded = False
                if adapter_id.startswith("builtin_"):
                    print(f"  Adapter {adapter_id} not downloaded, attempting auto-download...")
                    try:
                        try:
                            from .hf_utils import download_builtin_adapter
                        except ImportError:
                            from hf_utils import download_builtin_adapter
                        download_builtin_adapter(adapter_id, os.path.dirname(adapter_path))
                        downloaded = os.path.isdir(adapter_path)
                    except Exception as e:
                        print(f"  Error: Auto-download failed for {adapter_id}: {e}")
                if not downloaded:
                    print(f"  Error: adapter path not found: {adapter_path}")
                    for chunk in group:
                        results["failed"].append((chunk["index"], f"Adapter not found: {adapter_path}"))
                    continue

            # Release the prior group's local references before the loader
            # evicts its cached generation and admits the next model.
            model = prompt = None
            try:
                model, prompt = self._ensure_local_lora_generation(adapter_path, expected_generation)
            except Exception as e:
                print(f"  Error loading LoRA adapter {os.path.basename(adapter_path)}: {e}")
                for chunk in group:
                    results["failed"].append((chunk["index"], str(e)))
                continue

            texts = [c["text"] for c in group]
            instructs_raw = [c.get("instruct", "") for c in group]
            character_styles = [(vd.get("character_style", "") or vd.get("default_style", ""))
                                for _, vd in group_entries]
            indices = [c["index"] for c in group]

            # Sort by text length
            sort_order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
            texts = [texts[i] for i in sort_order]
            instructs_raw = [instructs_raw[i] for i in sort_order]
            character_styles = [character_styles[i] for i in sort_order]
            indices = [indices[i] for i in sort_order]

            # Estimate max batch size from VRAM + clone prompt overhead
            clone_tokens = prompt[0].ref_code.shape[0] if prompt[0].ref_code is not None else 0
            ref_text_chars = len(prompt[0].ref_text) if prompt[0].ref_text else 0
            max_items = self._estimate_max_batch_size(
                model, clone_tokens, ref_text_chars, len(texts[-1]),
            )
            sub_batches = self._build_sub_batches(texts, max_items=max_items)

            print(f"Batch [lora] adapter='{os.path.basename(adapter_path)}': {len(texts)} chunks "
                  f"in {len(sub_batches)} sub-batch(es)")

            for sb_idx, (start, end) in enumerate(sub_batches):
                sb_texts = texts[start:end]
                sb_instructs = instructs_raw[start:end]
                sb_character_styles = character_styles[start:end]
                sb_indices = indices[start:end]

                print(f"  Sub-batch {sb_idx+1}/{len(sub_batches)}: {len(sb_texts)} chunks "
                      f"({len(sb_texts[0])}-{len(sb_texts[-1])} chars/chunk)")

                try:
                    # Seed for reproducible batch output when batch_seed is set
                    # (matches _local_batch_custom); otherwise random each run.
                    if batch_seed >= 0:
                        torch.manual_seed(batch_seed)
                    # Build instruct_ids list for this sub-batch
                    instruct_ids = []
                    for inst, character_style in zip(sb_instructs, sb_character_styles):
                        instruct = inst or ""
                        if character_style:
                            instruct = f"{instruct} {character_style}".strip()
                        if instruct:
                            instruct_formatted = f"<|im_start|>user\n{instruct}<|im_end|>\n"
                            instruct_ids.append(model._tokenize_texts([instruct_formatted])[0])
                        else:
                            instruct_ids.append(None)

                    gen_extra = {}
                    if any(iid is not None for iid in instruct_ids):
                        gen_extra["instruct_ids"] = instruct_ids

                    t_start = time.time()
                    with measure_tts_stage_time("inference_decode"):
                        wavs_list, sr = model.generate_voice_clone(
                            text=sb_texts,
                            language=self._language,
                            voice_clone_prompt=prompt,
                            non_streaming_mode=True,
                            max_new_tokens=self._max_new_tokens,
                            **gen_extra,
                        )
                    gen_time = time.time() - t_start

                    if wavs_list is None:
                        for idx in sb_indices:
                            results["failed"].append((idx, "Batch returned None"))
                        continue

                    saved, sb_audio_duration = self.save_submitted_batch_waveforms(
                        wavs_list, sb_indices, sb_texts, sr, output_dir, 'lora')
                    results["completed"].extend(saved["completed"])
                    results["failed"].extend(saved["failed"])

                    total_audio_duration += sb_audio_duration
                    sb_rtf = sb_audio_duration / gen_time if gen_time > 0 else 0
                    print(f"  Sub-batch {sb_idx+1} done: {gen_time:.1f}s -> {sb_audio_duration:.1f}s audio ({sb_rtf:.2f}x RT)")

                except Exception as e:
                    print(f"  Sub-batch {sb_idx+1} failed: {e}")
                    for idx in sb_indices:
                        results["failed"].append((idx, f"Batch error: {e}"))

                self._clear_gpu_cache()

        total_time = time.time() - t_total_start
        rtf = total_audio_duration / total_time if total_time > 0 else 0
        print(f"Batch [lora] total: {total_time:.1f}s -> {total_audio_duration:.1f}s audio ({rtf:.2f}x real-time)")



        return results

    # ── External backend methods ─────────────────────────────────

    @staticmethod
    @measure_tts_stage("audio_validation_save")
    def _save_external_audio(source_path, output_path, cancelled=None):
        """Validate the copied response before replacing a published render."""
        from audio_validation import validate_generated_audio
        staging_path = f"{output_path}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copy(source_path, staging_path)
            validate_generated_audio(staging_path, "external TTS")
            if cancelled and cancelled.is_set():
                return False
            published = publish_audio_output(staging_path, output_path, cancelled)
            if published:
                record_tts_audio(output_path)
            return published
        finally:
            if os.path.exists(staging_path):
                os.remove(staging_path)

    def _save_external_prediction(self, result, output_path, cancelled, text):
        """Consume a provider result while its client is still serialized."""
        try:
            generated_audio_filepath = result[0]
            if not generated_audio_filepath or not os.path.exists(generated_audio_filepath):
                print(f"Error: No audio file generated for: '{text[:50]}...'")
                apply_tts_failure("External TTS returned no audio file.", "invalid_audio",
                                  "Check the TTS server logs before retrying.")
                return False

            if os.path.getsize(generated_audio_filepath) == 0:
                print(f"Error: Generated audio file is empty for: '{text[:50]}...'")
                apply_tts_failure("External TTS returned an empty audio file.", "invalid_audio",
                                  "Check the TTS server logs before retrying.")
                return False

            if cancelled and cancelled.is_set():
                return False
            return self._save_external_audio(generated_audio_filepath, output_path, cancelled)
        finally:
            if isinstance(result, ExternalPrediction):
                result.close()

    def _external_generate_custom(self, text, instruct_text, speaker, voice_config, output_path,
                                  endpoint=None, cancelled=None):
        """Generate custom voice audio via an external Gradio server (a pool
        endpoint when given, else the next one round-robin)."""
        try:
            voice_data = voice_config.get(speaker)
            if not voice_data:
                print(f"Warning: No voice configuration for '{speaker}'. Skipping.")
                apply_tts_failure(f"No voice configuration for '{speaker}'.", "missing_configuration",
                                  "Assign a voice to this speaker before retrying.")
                return False

            voice = voice_data.get("voice", "Ryan")
            seed = int(voice_data.get("seed", -1))

            instruct = anchored_instruct(voice_data, instruct_text)

            print(f"TTS [external] generating with instruct='{instruct}' for text='{text[:50]}...'")

            client, lock = self._external_endpoint(endpoint)

            with lock:
                if cancelled and cancelled.is_set():
                    return False
                with measure_tts_stage_time("external_request"):
                    result = client.predict(
                        text=text,
                        language=self._language,
                        speaker=voice,
                        instruct=instruct,
                        model_size="1.7B",
                        seed=seed,
                        api_name="/generate_custom_voice"
                    )

                success = self._save_external_prediction(result, output_path, cancelled, text)
                if success:
                    capture_speech_input(output_path, text, self._language, 'external_custom_api')
                return success

        except Exception as e:
            import traceback
            print(f"Error generating custom voice for '{speaker}': {e}")
            traceback.print_exc()
            apply_tts_failure(e)
            return False

    def _external_generate_clone(self, text, speaker, voice_config, output_path, endpoint=None,
                                 cancelled=None):
        """Generate voice-cloned audio via an external Gradio server (a pool
        endpoint when given, else the next one round-robin)."""
        try:
            from gradio_client import handle_file

            voice_data = voice_config.get(speaker)
            if not voice_data:
                print(f"Warning: No voice configuration for '{speaker}'. Skipping.")
                apply_tts_failure(f"No voice configuration for '{speaker}'.", "missing_configuration",
                                  "Assign a voice to this speaker before retrying.")
                return False

            ref_audio = voice_data.get("ref_audio")
            ref_text = voice_data.get("ref_text")
            seed = int(voice_data.get("seed", -1))

            if not ref_audio or not ref_text:
                print(f"Warning: Clone voice for '{speaker}' missing ref_audio or ref_text. Skipping.")
                apply_tts_failure(f"Clone voice for '{speaker}' is missing reference audio or transcript.", "missing_configuration",
                                  "Set both clone reference audio and its transcript before retrying.")
                return False

            # Resolve relative paths against project root
            if not os.path.isabs(ref_audio):
                ref_audio = _resolve_asset_path(ref_audio)

            try:
                ref_audio, reference_bytes = get_adapter_asset_snapshot(ref_audio)
            except FileNotFoundError:
                print(f"Warning: Reference audio not found for '{speaker}': {ref_audio}")
                apply_tts_failure(f"Reference audio not found for '{speaker}': {ref_audio}", "missing_asset",
                                  "Restore the clone reference audio before retrying.")
                return False

            client, lock = self._external_endpoint(endpoint)

            # Gradio uploads lazily. Keep captured bytes private until predict
            # exits, so renaming or replacing the source cannot change input.
            with lock:
                if cancelled and cancelled.is_set():
                    return False
                with tempfile.TemporaryDirectory(prefix="external_clone_ref_") as directory:
                    reference_path = os.path.join(directory, os.path.basename(ref_audio))
                    with open(reference_path, "wb") as handle:
                        handle.write(reference_bytes)
                    if cancelled and cancelled.is_set():
                        return False
                    with measure_tts_stage_time("external_request"):
                        result = client.predict(
                            handle_file(reference_path),
                            ref_text,
                            text,
                            self._language,
                            False,       # use_xvector_only
                            "1.7B",
                            200,         # max_chunk_chars
                            0,           # chunk_gap
                            seed,
                            api_name="/generate_voice_clone"
                        )

                success = self._save_external_prediction(result, output_path, cancelled, text)
                if success:
                    capture_speech_input(output_path, text, self._language, 'external_clone_api')
                return success

        except Exception as e:
            import traceback
            print(f"Error generating clone voice for '{speaker}': {e}")
            traceback.print_exc()
            apply_tts_failure(e)
            return False

    def _sequential_ensemble(self, chunks, voice_config, output_dir, batch_seed=-1):
        """Render ensemble chunks one at a time (each needs several voices)."""
        results = {"completed": [], "failed": []}

        for chunk in chunks:
            idx = chunk["index"]
            output_path = os.path.join(output_dir, f"temp_batch_{idx}.wav")
            try:
                success = self._dispatch_prepared_voice(
                    chunk.get("text", ""),
                    chunk.get("instruct", ""),
                    chunk.get("speaker"),
                    get_batch_voice_config(voice_config, chunk.get("speaker"), idx, batch_seed),
                    output_path,
                )
                if success:
                    results["completed"].append(idx)
                else:
                    results["failed"].append((idx, "Ensemble generation failed"))
            except Exception as e:
                results["failed"].append((idx, str(e)))

        return results

    def _external_batch(self, chunks, voice_config, output_dir, kind, batch_seed=-1):
        """External-mode batch submits up to `parallel_workers` lines per
        pool endpoint. Each worker serializes requests through its client
        lock, with deadlines measured from submission. Same result shape as local batches.

        One engine pool serves all calls. SDK I/O runs in cached owned clients
        and is stopped at the request deadline; queued cancellation removes work
        without adding application threads or publishing late audio."""
        from concurrent.futures import TimeoutError as FutureTimeout
        import time
        results = {"completed": [], "failed": []}
        if not chunks:
            return results
        generate = {
            "custom": lambda c, out, ep, cancelled: self._external_generate_custom(
                c.get("text", ""), c.get("instruct", ""), c.get("speaker", ""), get_batch_voice_config(voice_config, c.get("speaker", ""), c["index"], batch_seed), out,
                endpoint=ep, cancelled=cancelled),
            "clone": lambda c, out, ep, cancelled: self._external_generate_clone(
                c.get("text", ""), c.get("speaker", ""), get_batch_voice_config(voice_config, c.get("speaker", ""), c["index"], batch_seed), out,
                endpoint=ep, cancelled=cancelled),
        }[kind]
        futures = {}
        capture_inputs = is_speech_input_capture_active()

        def remove_staging_audio(staging_path):
            try:
                os.remove(staging_path)
            except FileNotFoundError:
                return None
            except OSError as error:
                diagnostic = f"Pending audio cleanup failed at {staging_path}: {error}"
                logging.getLogger(__name__).warning(diagnostic)
                return diagnostic
            return None

        def render_to_staging(chunk, staging_path, endpoint, cancelled, deadline):
            try:
                if cancelled.is_set() or time.monotonic() >= deadline:
                    return False, time.monotonic(), None
                with (ensure_external_request_control(deadline, cancelled),
                      ensure_speech_input_capture(capture_inputs)):
                    success = generate(chunk, staging_path, endpoint, cancelled)
                    proof = get_captured_speech_input(staging_path) if success else None
                    return success, time.monotonic(), proof
            finally:
                if cancelled.is_set():
                    remove_staging_audio(staging_path)

        try:
            for chunk in chunks:
                idx = chunk["index"]
                output_path = os.path.join(output_dir, f"temp_batch_{idx}.wav")
                staging_path = f"{output_path}.pending.{uuid.uuid4().hex}"
                cancelled = threading.Event()
                deadline = time.monotonic() + self._external_timeout
                future = self._submit_external(render_to_staging, cancelled, chunk, staging_path,
                                               self._next_external_url(), cancelled, deadline)
                futures[idx] = (future, cancelled, staging_path, output_path, deadline)
            for idx, (future, cancelled, staging_path, output_path, deadline) in futures.items():
                try:
                    success, finished_at, speech_input = future.result(timeout=max(0, deadline - time.monotonic()))
                    if finished_at > deadline:
                        raise FutureTimeout()
                except FutureTimeout:
                    cancelled.set()
                    future.cancel()
                    cleanup_error = remove_staging_audio(staging_path)
                    message = f"external TTS timed out after {self._external_timeout}s"
                    if cleanup_error:
                        message += f"; {cleanup_error}"
                    results["failed"].append((idx, message))
                    continue
                except Exception as e:                              # noqa: BLE001
                    cancelled.set()
                    cleanup_error = remove_staging_audio(staging_path)
                    message = str(e) or type(e).__name__
                    if cleanup_error:
                        message += f"; {cleanup_error}"
                    results["failed"].append((idx, message))
                    continue
                if success and os.path.exists(staging_path):
                    if publish_audio_output(staging_path, output_path, cancelled):
                        promote_speech_input(speech_input, output_path)
                        results["completed"].append(idx)
                    else:
                        cleanup_error = remove_staging_audio(staging_path)
                        message = "external TTS cancelled before publication"
                        if cleanup_error:
                            message += f"; {cleanup_error}"
                        results["failed"].append((idx, message))
                else:
                    cleanup_error = remove_staging_audio(staging_path)
                    message = f"{kind} voice generation failed"
                    if cleanup_error:
                        message += f"; {cleanup_error}"
                    results["failed"].append((idx, message))
        except BaseException:
            for future, cancelled, staging_path, _, _ in futures.values():
                cancelled.set()
                future.cancel()
                remove_staging_audio(staging_path)
            raise
        return results

    # ── Utility ──────────────────────────────────────────────────

    @staticmethod
    @measure_tts_stage("audio_validation_save")
    def _save_wav(audio_array, sample_rate, output_path):
        """Save a numpy audio array as a WAV file, and verify it is one.

        EVERY generation path funnels through here - lora, clone, custom,
        design, and all three batch variants - which is why the check belongs
        at this single point rather than in seven callers that would drift.

        WHY VALIDATE AT ALL. `project.py` already validates when assembling
        chunks into an export, while this layer, which CREATES the audio, only
        called sf.write and returned. That asymmetry is worse than no checking:
        a guard in the assembly layer makes the pipeline look protected while
        the layer that can actually produce a bad file does not look at it.

        The failure this catches is not hypothetical. Truncating a real
        195,884-byte render to 5,000 bytes still DECODES - libsndfile returns
        whatever frames are present rather than raising - so a run killed
        mid-write, or a disk that fills, yields a short, valid, wrong file.
        Existence and size both pass on it.

        Raises GeneratedAudioError rather than returning False. The callers
        already treat exceptions as failure, and a returned flag is what six
        experiment harnesses ignored.
        """
        from audio_validation import save_generated_wav
        result = save_generated_wav(audio_array, sample_rate, output_path,
                                    f"TTS generation to {os.path.basename(output_path)}")
        record_tts_audio(output_path)
        return result


# Immutable references qualify the native recovery serving path. A public or
# internal override remains an ordinary temporary renderer.
_RAW_RECOVERY_METHODS = {name: getattr(TTSEngine, name) for name in (
    'generate_voice_result', 'generate_voice', '_dispatch_prepared_voice', '_generate_voice_prepared',
    'generate_custom_voice', '_generate_custom_voice_prepared', '_render_synthesis_windows',
    '_get_synthesis_window_plan', '_generate_prepared_batch_parent', '_local_generate_custom',
    'get_voice_backend', 'get_synthesis_recovery_policy', 'get_model_settings', 'generate_batch',
    '_init_local_custom', '_load_model', '_load_model_attempt', '_release_inactive_models_after_oom',
    '_release_inactive_local_models', '_release_local_model_families', '_release_local_model_family',
    '_clear_gpu_cache', '_resolve_local_model_path', '_resolve_device', '_save_wav')}
_RAW_RECOVERY_MODEL_IDS = dict(TTSEngine._LOCAL_MODEL_IDS)
_RAW_RECOVERY_ALLOCATION_FAILURE = device_utils.is_allocation_failure
from gpu_stats import OOM_MARKERS as _RAW_RECOVERY_ALLOCATION_MARKERS
_RAW_RECOVERY_FUNCTIONS = {name: globals()[name] for name in (
    'get_custom_voice_request', 'anchored_instruct', 'get_memory_bounded_codec_decode')}


# Capture native wrappers once so class/instance replacements take the fallback.
_RAW_CUSTOM_VOICE = TTSEngine.generate_custom_voice
_RAW_CLONE_VOICE = TTSEngine.generate_clone_voice
_RAW_LORA_VOICE = TTSEngine.generate_lora_voice
_RAW_DESIGN_VOICE = TTSEngine.generate_design_voice
_RAW_ENSEMBLE_VOICE = TTSEngine.generate_ensemble_voice
_RAW_GENERATE_VOICE = TTSEngine.generate_voice
_RAW_SAVE_BATCH_WAVEFORMS = TTSEngine.save_batch_waveforms
_RAW_MIX_TO_UNISON = mix_to_unison


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
