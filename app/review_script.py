if __name__ == '__main__':
    import os as _startup_os
    import sys as _startup_sys
    from pathlib import Path as _StartupPath
    from utils import get_runtime_data_dir as _startup_data_dir
    from review_startup import ReviewStartupTrace, CURRENT as _STARTUP_CURRENT, ENV_ID, ENV_PATH
    _startup_path = _startup_os.environ.get(ENV_PATH) or str(_StartupPath(_startup_data_dir(str(_StartupPath(__file__).resolve().parent.parent))) / 'logs' / 'api' / 'review-startup.log')
    _startup_trace = ReviewStartupTrace(_startup_path, _startup_os.environ.get(ENV_ID))
    _STARTUP_CURRENT.set(_startup_trace)
    _startup_trace.event('cli_imports_started')
    def _startup_exception(kind, error, traceback):
        _startup_trace.event('cli_failed', error=error)
        _startup_sys.__excepthook__(kind, error, traceback)
    _startup_sys.excepthook = _startup_exception

from chunk_status_journal import remove_chunk_snapshot
import errno
import os
import stat
from core import (llm_timeout_seconds, is_llm_on_this_gpu, get_review_accounting_warning,
                  SPEAKER_MERGE_MARKER)
import sys
import json
import re
import time
import difflib
import copy
import subprocess
import argparse
import threading
import unicodedata
from dataclasses import fields, replace
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from config_settings import load_app_config, get_review_labels_only
from llm_provider import (make_llm_client, make_run_client, get_run_model_binding,
                          get_run_fingerprint_identity, resolve_api_key)
from completed_review_receipt import (REVIEW_ALGORITHM_VERSION, REVIEW_SOURCE_SPAN_ALGORITHM_VERSION,
    get_review_entries_fingerprint, get_review_sidecar_identity, get_completed_review_match,
    get_review_progress_paths, get_review_receipt_path, save_completed_review_receipt,
    remove_completed_review_receipt)
from llm_bench import get_cached_or_benchmarked_concurrency
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from generate_script import LLMGenParams, call_llm_for_entries, call_llm_for_object
from literal_policy import (get_literal_policy, ensure_literal_narration,
                            retain_literal_policy, get_word_identity, SCRIPT_POLICY_KEY)
from literal_fidelity import audit_literal_words
from lmstudio_settings import (ensure_ideal_settings, get_active_llm_config,
                               get_current_status, get_effective_max_tokens)
from lmstudio_settings import get_failover_llm_config
from generation_checkpoint_deltas import GenerationCheckpointDeltas, load_generation_checkpoint_document
from generation_checkpoint_shards import remove_generation_shard_checkpoint, get_generation_checkpoint_artifacts
from utils import file_lock, atomic_json_write, safe_load_json, run_rocm_smi_json, get_runtime_data_dir, get_app_config_path
from speaker_identity import (resolve_speaker_label, get_validated_alias_map,
                              is_group_speaker_label as _is_group_label,
                              is_speaker_merge_allowed, get_resolved_speaker_merge_map,
                              get_validated_alias_graph, get_safe_alias_proposals)


from review_startup import event as startup_event, bind_current as bind_startup_trace, record_failure
from dialogue_spans import get_source_span_fragments, SourceSpanError

# ── GPU VRAM watchdog ────────────────────────────────────────────────────────
# A long batch review run can grow VRAM usage (KV cache + other GPU apps) until
# the driver runs out of memory and crashes the whole display server. Before
# each batch we check headroom and pause/abort rather than let that happen.

VRAM_WARN_THRESHOLD = 0.90  # pause once VRAM usage crosses this fraction
VRAM_MAX_WAIT = 180         # seconds to wait for headroom before giving up
VRAM_POLL_INTERVAL = 15     # seconds between checks while waiting
MAX_REVIEW_SCRIPT_BYTES = 512 * 1024**2


def _read_review_script_bytes(path):
    """Read one exact regular file without following an alias or blocking FIFO."""
    before = os.lstat(path)
    version = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
               before.st_ctime_ns, before.st_mode, before.st_nlink)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or not 0 <= before.st_size <= MAX_REVIEW_SCRIPT_BYTES):
        raise ValueError('Review input must be a bounded regular file without aliases')
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                         | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(descriptor, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        opened_version = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                          opened.st_ctime_ns, opened.st_mode, opened.st_nlink)
        if opened_version != version:
            raise ValueError('Review input changed while opening')
        raw = handle.read(before.st_size + 1)
        after = os.fstat(handle.fileno())
        after_version = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                         after.st_ctime_ns, after.st_mode, after.st_nlink)
        if len(raw) != before.st_size or after_version != version:
            raise ValueError('Review input changed while reading')
    current = os.lstat(path)
    current_version = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns,
                       current.st_ctime_ns, current.st_mode, current.st_nlink)
    if current_version != version:
        raise ValueError('Review input changed while reading')
    return raw


def get_vram_usage():
    """Return (worst_used_bytes, worst_total_bytes) for the most constrained GPU.

    On multi-GPU systems, returns the card with the highest VRAM utilization ratio
    so the headroom check doesn't pass on GPU 0 while GPU 1 is saturated.
    Supports both AMD (rocm-smi) and NVIDIA (nvidia-smi) GPUs.
    """
    worst_ratio = 0.0
    worst_used = 0
    worst_total = 0
    # Inspect both backends: a machine can have AMD and NVIDIA cards.
    data = run_rocm_smi_json(["--showmeminfo", "vram"])
    if data is not None:
        for card_data in data.values():
            if not isinstance(card_data, dict):
                continue
            try:
                used = int(card_data.get("VRAM Total Used Memory (B)", 0))
                total = int(card_data.get("VRAM Total Memory (B)", 0))
                if total <= 0 or used < 0:
                    continue
                ratio = used / total
                # Track the GPU with the highest utilization ratio
                if worst_total == 0 or ratio > worst_ratio:
                    worst_ratio = ratio
                    worst_used = used
                    worst_total = total
            except (ValueError, TypeError):
                continue

    # Try NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(',')
                if len(parts) != 2:
                    continue
                try:
                    # nvidia-smi returns values in MiB
                    used_mib = float(parts[0].strip())
                    total_mib = float(parts[1].strip())
                    used = int(used_mib * 1024 * 1024)  # Convert to bytes
                    total = int(total_mib * 1024 * 1024)
                    if total <= 0 or used < 0:
                        continue
                    ratio = used / total
                    if worst_total == 0 or ratio > worst_ratio:
                        worst_ratio = ratio
                        worst_used = used
                        worst_total = total
                except (ValueError, TypeError, OverflowError):
                    continue
    except Exception:
        pass  # nvidia-smi not available either
    
    return (worst_used, worst_total) if worst_total > 0 else None


def wait_for_vram_headroom(fail_fast=False):
    """Pause if GPU VRAM usage is above VRAM_WARN_THRESHOLD, polling until it drops
    or VRAM_MAX_WAIT elapses. Returns True if safe to proceed, False if VRAM is
    still saturated after waiting (caller should save progress and stop).

    fail_fast is for the first check of a run, before any batch: a local LLM
    server allocates its KV cache at load, so VRAM already full at that point
    is the server review is about to call and waiting cannot free it (#1114).
    Return False at once with the cause instead of a wait that cannot succeed.
    The threshold is unchanged."""
    usage = get_vram_usage()
    if usage is None:
        return True  # rocm-smi unavailable (e.g. non-AMD GPU) - don't block on it

    used, total = usage
    if used / total < VRAM_WARN_THRESHOLD:
        return True

    if fail_fast:
        print(f"  VRAM is already {used/total:.0%} full ({used/1e9:.1f}/{total/1e9:.1f} GB) "
              f"before review started - probably held by your local LLM server. "
              f"Review needs headroom below {VRAM_WARN_THRESHOLD:.0%}: load the model "
              f"with a smaller context (16k measured working on a 16 GB card) or free "
              f"VRAM, then re-run. Nothing was reviewed; the script is unchanged.")
        return False

    print(f"  WARNING: GPU VRAM at {used/total:.0%} ({used/1e9:.1f}/{total/1e9:.1f} GB) "
          f"- pausing to avoid an OOM crash...")
    waited = 0
    last_reported = 0
    while waited < VRAM_MAX_WAIT:
        ensure_review_not_cancelled()
        time.sleep(VRAM_POLL_INTERVAL)
        waited += VRAM_POLL_INTERVAL
        
        # Report progress every 30 seconds so user knows we're still waiting
        if waited - last_reported >= 30:
            print(f"  Still waiting for VRAM to drop... ({waited}s elapsed, max {VRAM_MAX_WAIT}s)")
            last_reported = waited
        
        usage = get_vram_usage()
        if usage is None:
            return True
        used, total = usage
        if used / total < VRAM_WARN_THRESHOLD:
            print(f"  VRAM back to {used/total:.0%} after {waited}s - resuming.")
            return True

    print(f"  VRAM still at {used/total:.0%} after {VRAM_MAX_WAIT}s - "
          f"stopping early to avoid a crash. Progress so far will be saved.")
    return False


# ── Resume support ───────────────────────────────────────────────────────────
# A long batch review run that stops early (VRAM, crash, kill) checkpoints its
# progress so the next run can pick up where it left off instead of redoing
# every batch from scratch.

def _checkpoint_path(output_path):
    return get_review_progress_paths(output_path)[0]


_entries_fingerprint = get_review_entries_fingerprint


# Contextual review's extra context is measured either in whole entries or in
# characters of entry text. These are the one limit/definition shared by the
# HTTP routes and the CLI; see get_validated_context_setting and
# get_review_context_neighbors below.
CONTEXT_MODE_ENTRIES = 'entries'
CONTEXT_MODE_CHARS = 'chars'
CONTEXT_ENTRY_MAX = 100
CONTEXT_CHAR_MAX = 1000000
DEFAULT_CONTEXT_ENTRIES = 4
DEFAULT_CONTEXT_CHARS = 2000


def get_validated_context_setting(mode, value, default, minimum=1):
    """Return (mode, value) after validating a contextual-review context setting.

    One authority for both the single-book and batch-review routes so their
    limits cannot drift. Integers outside the mode's range are clamped, matching
    the old window_size behaviour; non-integers are rejected by Pydantic at the
    route boundary (422) before this is reached. Characters mode is used only
    when explicitly requested; every other value means entries mode.
    """
    if mode == CONTEXT_MODE_CHARS:
        limit = CONTEXT_CHAR_MAX
    else:
        mode = CONTEXT_MODE_ENTRIES
        limit = CONTEXT_ENTRY_MAX
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("context value must be an integer")
    return mode, max(minimum, min(value, limit))


def get_review_context_from_args(args):
    """Return (mode, value) for a review run, from its parsed CLI arguments.

    --context-chars takes precedence; --context-window keeps its old entries
    meaning. getattr keeps fake args in older tests working unchanged.
    """
    chars = getattr(args, 'context_chars', 0) or 0
    if chars > 0:
        return CONTEXT_MODE_CHARS, chars
    return CONTEXT_MODE_ENTRIES, args.context_window


def get_review_context_neighbors(entries, start, end, mode, value):
    """Return (before, after) whole-entry context neighbours for a batch.

    Entries mode is exactly the old +/- N slice. Characters mode accumulates
    whole neighbouring entries nearest-first until the next one would exceed
    the limit, counting only entry text (the same measure review_target_chars
    uses in iter_entry_windows). An entry larger than the limit on its own is
    still included as the single neighbour on that side, then accumulation
    stops - the same "an entry larger than the target goes alone" rule Step 2
    uses, rather than silently dropping the only available context. Sides are
    independent, so a big entry on one side cannot shrink the other.
    """
    if mode != CONTEXT_MODE_CHARS:
        window = max(1, int(value))
        return (entries[max(0, start - window):start],
                entries[end:min(len(entries), end + window)])
    limit = int(value)

    def pack(candidates):
        picked, used = [], 0
        for entry in candidates:
            length = len(str(entry.get("text") or "")) if isinstance(entry, dict) else 0
            if picked and used + length > limit:
                break
            picked.append(entry)
            used += length
            if used >= limit:
                break
        return picked

    before = pack(reversed(entries[:start]))
    before.reverse()
    return before, pack(entries[end:])


def get_review_batching(generation_config):
    """Return (batch_size, target_chars): the one authority for review batching.

    Every reader of review batching (the checkpoint identity, both request
    loops and the call-count estimate in the review route) goes through here,
    so they cannot disagree about how a config chunks entries. The packing
    itself is delegated to three_pass_generate.iter_entry_windows.
    """
    batch_size = generation_config.get("review_batch_size", 25)
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size < 1:
        raise ValueError("generation.review_batch_size must be a positive integer")
    target_chars = generation_config.get("review_target_chars", 0)
    if (isinstance(target_chars, bool) or not isinstance(target_chars, int)
            or not 0 <= target_chars <= 1000000):
        raise ValueError("generation.review_target_chars must be an integer between 0 and 1000000")
    return batch_size, target_chars


def get_review_entry_windows(entries, generation_config):
    """Return (start, end) pairs for each review request, in source-index space.

    Reuses the Step 2 windowing so review cannot develop a second accumulator:
    whole entries only, the next entry that would exceed the target starts a
    new window, and an entry larger than the target goes alone. The import is
    local because three_pass_generate imports normalize_text from this module.
    """
    from three_pass_generate import iter_entry_windows
    batch_size, target_chars = get_review_batching(generation_config)
    return [(window[0][0], window[-1][0] + 1)
            for window in iter_entry_windows(entries, batch_size, target_chars)]


def get_review_remaining_windows(review_windows, entries, resume_offset):
    """Return the windows a resumed review still owes, never losing entries.

    A checkpoint only records how many source entries were consumed, not which
    request boundaries they belonged to. If that offset is not a window start,
    the saved progress cannot be mapped onto this run's batching: filtering by
    ``start >= resume_offset`` would drop the window containing the offset and
    those entries would be neither reviewed nor reported. Refuse instead, with
    an instruction to start over.
    """
    if (0 < resume_offset < len(entries)
            and resume_offset not in {start for start, _ in review_windows}):
        raise ValueError(
            f"Review checkpoint resumes at entry {resume_offset + 1}, which is not a "
            f"batch boundary for the current review settings. Entries cannot be mapped "
            f"safely to requests; start the review over instead of resuming it.")
    return [bounds for bounds in review_windows if bounds[0] >= resume_offset]


def get_completed_review_fingerprint(config, llm_config, params, args, client, model_name, source_text=None,
                                     *, entries=()):
    generation_config = config.get('generation') or {}
    batch_size, target_chars = get_review_batching(generation_config)
    profiles = []
    for profile in (llm_config, get_failover_llm_config(config)):
        profiles.append({key: value for key, value in profile.items()
                         if key not in ('concurrency', 'concurrency_for', 'concurrency_environment',
                                        'concurrency_revision')})
        profiles[-1]['resolved_api_key'] = resolve_api_key(profile.get('api_key'))
    identity = {'algorithm': REVIEW_ALGORITHM_VERSION, 'profiles': profiles,
        'model_binding': get_run_model_binding(client, model_name),
        'params': {field.name: getattr(params, field.name) for field in fields(params) if field.compare
                   and (field.name != 'literal_source_preserving' or params.literal_source_preserving)
                   and (field.name != 'review_labels_only' or params.review_labels_only)},
        'context_window': args.context_window, 'dedupe_speakers': args.dedupe_speakers,
        'merge_narrators': generation_config.get('merge_narrators', False),
        'batch_size': batch_size,
        'aliases': get_review_sidecar_identity(args.alias_registry),
        'voice_config': get_review_sidecar_identity(args.remap_voice_config)}
    # Add the key only when opted in, so default (0) configs keep the exact
    # same receipt identity as before this setting existed.
    if target_chars:
        identity['review_target_chars'] = target_chars
    # The default (entries mode) already varies context_window in the identity
    # above. Characters mode is new, so its keys are added only when selected.
    context_mode, context_value = get_review_context_from_args(args)
    if context_mode == CONTEXT_MODE_CHARS:
        identity['context_mode'] = context_mode
        identity['context_chars'] = context_value
    if source_text is not None or is_review_source_span_script(entries):
        identity['algorithm'] = REVIEW_SOURCE_SPAN_ALGORITHM_VERSION
        identity['review_source_sha256'] = (get_review_entries_fingerprint(source_text)
                                            if source_text is not None else None)
    # Store only a digest, never profile credentials or private headers.
    return get_review_entries_fingerprint(get_run_fingerprint_identity(identity))


REVIEW_EDIT_COUNTERS = ('text_changed', 'speaker_changed', 'instruct_changed', 'annotations_changed',
                       'entries_changed', 'entries_added', 'entries_removed')


def get_review_edit_counts(stats, *, required=True):
    """Read native edit counters without coercing unavailable values to zero."""
    for key in REVIEW_EDIT_COUNTERS:
        if key in stats and (type(stats[key]) is not int or stats[key] < 0):
            raise ValueError(f"Saved review counter {key} is unreadable; checkpoint preserved.")
    if any(key not in stats for key in REVIEW_EDIT_COUNTERS):
        if required:
            raise ValueError("Saved review edit counters are unavailable; checkpoint preserved.")
        return None
    return {key: stats[key] for key in REVIEW_EDIT_COUNTERS}


def is_review_accounting_coordinates(count, offsets, lengths, source_count, output_count):
    if (type(count) is not int or count < 0 or type(source_count) is not int or source_count < 0
            or not isinstance(offsets, list) or not isinstance(lengths, list)
            or len(offsets) != count or len(lengths) != count
            or any(type(value) is not int or value < 0 for value in offsets + lengths)):
        return False
    return (all(left < right for left, right in zip([0] + offsets, offsets))
            and (not offsets or offsets[-1] <= source_count)
            and sum(lengths) == output_count)


def get_review_contribution(source_end, output_length, counts):
    return {'source_end': source_end, 'output_length': output_length, **counts}


def get_review_checkpoint_accounting(data, entries, keep_count, *, migrated=False):
    """Bind counters before native rewind, independently of content acceptance."""
    counts = get_review_edit_counts(data['total_stats'], required=False)
    source_hash = _entries_fingerprint(entries)
    same_source = source_hash == data.get('source_sha256')
    count, lengths = data['completed_batches'], data['batch_lengths']
    offsets = data.get('source_offsets')
    old = data.get('review_accounting')
    rows = data.get('review_batch_contributions')
    valid = (counts is not None and isinstance(old, dict) and type(old.get('version')) is int
             and old.get('version') == 1 and isinstance(old.get('source_sha256'), str)
             and old.get('mode') == 'owned' and type(old.get('inherited_unverified')) is bool
             and old.get('source_sha256') == data.get('source_sha256')
             and type(keep_count) is int and 0 <= keep_count <= count
             and all(type(index) is int and 1 <= index <= count for index in data['failed_batches'])
             and is_review_accounting_coordinates(count, offsets, lengths,
                 old.get('source_entry_count'), len(data['all_corrected']))
             and isinstance(rows, list) and len(rows) == count)
    inherited = old.get('inherited') if isinstance(old, dict) else None
    if valid:
        try:
            valid = (isinstance(inherited, dict) and set(inherited) == set(REVIEW_EDIT_COUNTERS)
                     and get_review_edit_counts(inherited) is not None)
            for index, row in enumerate(rows):
                valid = (valid and isinstance(row, dict)
                         and set(row) == set(REVIEW_EDIT_COUNTERS) | {'source_end', 'output_length'}
                         and type(row['source_end']) is int and type(row['output_length']) is int
                         and row['source_end'] == offsets[index]
                         and row['output_length'] == lengths[index]
                         and get_review_edit_counts(row) is not None
                         and (index + 1 not in data['failed_batches']
                              or not any(row[key] for key in REVIEW_EDIT_COUNTERS)))
            valid = valid and all(inherited[key] + sum(row[key] for row in rows) == counts[key]
                                  for key in REVIEW_EDIT_COUNTERS)
        except (KeyError, TypeError, ValueError):
            valid = False
    if same_source and valid and old['source_entry_count'] != len(entries):
        valid = False
    if valid and same_source:
        contributions = copy.deepcopy(rows[:keep_count])
        inherited = dict(inherited)
        counts = {key: inherited[key] + sum(row[key] for row in contributions)
                  for key in REVIEW_EDIT_COUNTERS}
        owned = True
    else:
        # Incoming output already contains ALL prior edits, even past a failure.
        # Legacy counters have no removable ownership; retain them as inherited.
        inherited = dict(counts or {})
        incoming_offsets = offsets
        if not same_source:
            incoming_offsets, end = [], 0
            for length in lengths:
                if type(length) is not int or length < 0:
                    break
                end += length
                incoming_offsets.append(end)
        owned = (counts is not None and type(keep_count) is int and 0 <= keep_count <= count
                 and (keep_count == 0 or
            is_review_accounting_coordinates(count, incoming_offsets, lengths,
                                              len(entries), len(data['all_corrected']))))
        zero = {key: 0 for key in REVIEW_EDIT_COUNTERS}
        contributions = ([get_review_contribution(incoming_offsets[i], lengths[i], zero)
                          for i in range(keep_count)] if owned else [])
    metadata = {'version': 1, 'mode': 'owned' if owned else 'unowned',
                'source_sha256': source_hash, 'source_entry_count': len(entries),
                'inherited': inherited,
                'inherited_unverified': bool(migrated or not valid
                    or old.get('inherited_unverified', False))}
    return metadata, contributions, counts


def get_review_runtime_accounting(checkpoint, source_hash, source_count, total_stats,
                                  source_offsets, batch_lengths, output_count):
    counts = get_review_edit_counts(total_stats)
    metadata = (checkpoint['review_accounting'] if checkpoint else
        {'version': 1, 'mode': 'owned', 'source_sha256': source_hash,
         'source_entry_count': source_count,
         'inherited': {key: 0 for key in REVIEW_EDIT_COUNTERS}, 'inherited_unverified': False})
    return {'metadata': copy.deepcopy(metadata),
            'contributions': copy.deepcopy(checkpoint['review_batch_contributions']) if checkpoint else [],
            'totals': counts, 'source_offsets': list(source_offsets),
            'batch_lengths': list(batch_lengths), 'output_count': output_count}


def get_saved_review_accounting(accounting, data):
    """Project one native per-batch counter update; never split an unknown delta."""
    counts = get_review_edit_counts(data['total_stats'])
    metadata = copy.deepcopy(accounting['metadata'])
    rows = copy.deepcopy(accounting['contributions'])
    previous = len(accounting['batch_lengths'])
    current = data['completed_batches']
    owned = (metadata['mode'] == 'owned' and len(rows) == previous
             and metadata['source_sha256'] == data['source_sha256']
             and is_review_accounting_coordinates(current, data['source_offsets'], data['batch_lengths'],
                     metadata['source_entry_count'], len(data['all_corrected']))
             and data['source_offsets'][:previous] == accounting['source_offsets']
             and data['batch_lengths'][:previous] == accounting['batch_lengths']
             and sum(data['batch_lengths'][:previous]) == accounting['output_count'])
    if owned and current == previous:
        owned = counts == accounting['totals']
    elif owned and current == previous + 1:
        delta = {key: counts[key] - accounting['totals'][key] for key in REVIEW_EDIT_COUNTERS}
        owned = (all(value >= 0 for value in delta.values())
                 and (current not in data['failed_batches'] or not any(delta.values())))
        if owned:
            rows.append(get_review_contribution(data['source_offsets'][-1],
                                                data['batch_lengths'][-1], delta))
    else:
        owned = False
    if not owned:
        metadata.update(mode='unowned', inherited=counts, inherited_unverified=True)
        rows = []
    detached = {**data, 'review_accounting': metadata, 'review_batch_contributions': rows}
    return get_review_runtime_accounting(detached, data['source_sha256'],
        metadata['source_entry_count'], data['total_stats'], data['source_offsets'],
        data['batch_lengths'], len(data['all_corrected']))


def load_checkpoint(output_path, total_batches, batch_size, context_window, entries,
                    execution_sha256=None):
    """Return checkpoint dict if one exists and matches this run's parameters.

    If earlier batches failed (and were filled in with their original,
    unreviewed entries as a fallback), rewind the checkpoint to just before
    the earliest such failure so those batches get retried on this run
    instead of being permanently skipped. The rewind only runs when
    `batch_lengths` fully covers `completed_batches` - true for any
    checkpoint written entirely by this version; older checkpoints (missing
    these fields) just keep their previous resume-from-completed behavior.

    `total_batches` can legitimately drift between runs - earlier batches may
    have added/removed entries (splits/merges), changing the total entry
    count - so a mismatch here no longer discards the checkpoint. Only
    `batch_size`/`context_window` and execution identity must match. Main
    always supplies the settings fingerprint, so unbound legacy checkpoints
    restart rather than silently mixing settings. The caller re-derives this run's
    `total_batches` from `len(all_corrected)` and the current entry count.
    """
    path = _checkpoint_path(output_path)
    if not os.path.exists(path):
        return None
    try:
        data = load_generation_checkpoint_document(path)
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        print("Invalid review checkpoint shape - starting fresh.")
        return None
    if execution_sha256 is not None and data.get("execution_sha256") != execution_sha256:
        print("Review checkpoint execution settings changed or are unbound - starting fresh.")
        return None
    if data.get("literal_source_policy") != get_literal_policy(entries):
        print("Review checkpoint literal-source policy changed - starting fresh.")
        return None
    if (data.get("batch_size") != batch_size or
            data.get("context_window") != context_window):
        print("Found a review checkpoint but parameters changed - starting fresh.")
        return None
    entries_sha256 = _entries_fingerprint(entries)
    if entries_sha256 not in (data.get("source_sha256"),
                               data.get("output_sha256")):
        print("Review checkpoint source changed or has no fingerprint - starting fresh.")
        return None
    if (not isinstance(data.get("completed_batches"), int) or
            not isinstance(data.get("all_corrected"), list) or
            not isinstance(data.get("previous_tail"), (list, type(None))) or
            not isinstance(data.get("total_stats", {}), dict) or
            not isinstance(data.get("batch_lengths", []), list) or
            not isinstance(data.get("failed_batches", []), list)):
        print("Invalid review checkpoint fields - starting fresh.")
        return None
    offsets = data.get("source_offsets")
    if offsets is not None and (
            not isinstance(offsets, list) or len(offsets) != data["completed_batches"]
            or any(not isinstance(offset, int) or isinstance(offset, bool) or offset < 0 for offset in offsets)
            or offsets != sorted(offsets)
            or (entries_sha256 == data.get("source_sha256")
                and offsets and offsets[-1] > len(entries))):
        print("Invalid review checkpoint source offsets - starting fresh.")
        return None
    if get_literal_policy(entries) is not None and (
            data["completed_batches"] or data["all_corrected"]):
        end = ((offsets[-1] if offsets else data["completed_batches"] * batch_size)
               if entries_sha256 == data.get("source_sha256")
               else len(data["all_corrected"]))
        prefix = " ".join(entry.get("text", "") for entry in entries[:end])
        if not audit_literal_words(prefix, data["all_corrected"])["passed"]:
            print("Literal review checkpoint changed narration - starting fresh.")
            return None
    if data.get("total_batches") != total_batches:
        print("Note: entry count changed since the checkpoint was saved "
              "(earlier batches added/removed entries) - resuming from the "
              "saved progress anyway.")

    data.setdefault("batch_lengths", [])
    data.setdefault("failed_batches", [])
    data.setdefault("total_stats", {})
    data["total_stats"].setdefault("batches_skipped_vram", 0)
    migrated_counts = any(key not in data['total_stats'] for key in ('entries_changed', 'annotations_changed'))
    data['total_stats'].setdefault('annotations_changed', 0)
    data["total_stats"].setdefault("entries_changed", 0)

    failed_batches = sorted(data["failed_batches"])
    batch_lengths = data["batch_lengths"]
    completed_batches = data["completed_batches"]
    keep_count = (max(0, failed_batches[0] - 1)
                  if failed_batches and len(batch_lengths) == completed_batches else completed_batches)
    metadata, contributions, counts = get_review_checkpoint_accounting(
        data, entries, keep_count, migrated=migrated_counts)
    data['review_accounting'] = metadata
    data['review_batch_contributions'] = contributions
    if counts is not None:
        data['total_stats'].update(counts)
    if metadata['inherited_unverified']:
        print('WARNING: ' + get_review_accounting_warning({'accounting_unverified': True}))
    if failed_batches and len(batch_lengths) == completed_batches:
        retry_from = failed_batches[0]
        # Keep entries from all batches before the first failed one
        # retry_from is 1-indexed, so batch_lengths[:retry_from - 1] gives us
        # the lengths of batches 1 through (retry_from - 1)
        keep_count = max(0, retry_from - 1)
        keep_entries = sum(batch_lengths[:keep_count])
        all_corrected = data["all_corrected"][:keep_entries]
        data["all_corrected"] = all_corrected
        data["completed_batches"] = keep_count
        data["batch_lengths"] = batch_lengths[:keep_count]
        if "source_offsets" in data:
            data["source_offsets"] = data["source_offsets"][:keep_count]
        data["previous_tail"] = all_corrected[-2:] if all_corrected else None
        data["total_stats"]["batches_failed"] = max(
            0, data["total_stats"].get("batches_failed", 0) - len(failed_batches))
        data["failed_batches"] = []
        plural = "es" if len(failed_batches) != 1 else ""
        print(f"Found {len(failed_batches)} previously-failed batch{plural} - "
              f"rewinding checkpoint to retry starting at batch {retry_from}.")

    return data


def save_checkpoint(output_path, completed_batches, total_batches, batch_size,
                     context_window, all_corrected, total_stats, previous_tail,
                     batch_lengths, failed_batches, source_sha256,
                     output_sha256=None, writer=None, source_offsets=None,
                     literal_policy=None, execution_sha256=None, accounting=None):
    path = _checkpoint_path(output_path)
    data = {
        "execution_sha256": execution_sha256,
        "completed_batches": completed_batches,
        "total_batches": total_batches,
        "batch_size": batch_size,
        "context_window": context_window,
        "all_corrected": all_corrected,
        "total_stats": total_stats,
        "previous_tail": previous_tail,
        "batch_lengths": batch_lengths,
        "failed_batches": failed_batches,
        "source_sha256": source_sha256,
        "output_sha256": output_sha256,
        "source_offsets": list(source_offsets) if source_offsets is not None else
                          [(index + 1) * batch_size for index in range(completed_batches)],
    }
    if literal_policy is not None:
        data["literal_source_policy"] = literal_policy
    updated_accounting = None
    if accounting is not None:
        updated_accounting = get_saved_review_accounting(accounting, data)
        data['review_accounting'] = updated_accounting['metadata']
        data['review_batch_contributions'] = updated_accounting['contributions']
    # Use atomic_json_write for consistent cross-platform behavior with Windows retry logic
    try:
        if writer is not None:
            writer.save_checkpoint(data, compact=output_sha256 is not None)
        else:
            with file_lock(path):
                atomic_json_write(data, path, allow_nonatomic_fallback=False)
    except OSError as e:
        if e.errno == errno.EXDEV:
            # The strict writer refused a cross-device publish; copying instead
            # would make publication non-atomic. Stop here, before any output is
            # published, rather than continue with a checkpoint that may not
            # exist. Every individual atomic write here publishes or leaves its
            # target untouched, so the last good checkpoint stays resumable.
            record_failure('review_checkpoint_write_exdev', e, task='review',
                           resource=path, operation='save_checkpoint')
            raise OSError(errno.EXDEV,
                f"Review stopped: the checkpoint could not be written atomically at {path} "
                f"(errno {errno.EXDEV}, EXDEV cross-device link). Keep the data directory "
                "on one filesystem, then retry. No output was published; the last good "
                "checkpoint is retained for resume.") from e
        # If checkpoint save fails (e.g., disk full, permission denied),
        # log the error but don't crash the review process
        print(f"WARNING: Failed to save checkpoint: {e}. Review will continue but resume may not work.")
    # Advance the attempted boundary even on I/O failure, since review continues.
    return updated_accounting


def clear_checkpoint(output_path):
    """Retire the checkpoint and its owned epochs under the canonical lock."""
    path = _checkpoint_path(output_path)
    if os.path.exists(path) or len(get_generation_checkpoint_artifacts(path)) > 1:
        try:
            # Use file_lock to coordinate with concurrent reads/writes
            with file_lock(path):
                remove_generation_shard_checkpoint(path, locked=True)
        except (TimeoutError, OSError) as e:
            # If the file is still there, removal genuinely failed (lock
            # contention or e.g. permission error) - warn, since a stale
            # checkpoint can make the next run resume from the wrong place.
            if os.path.exists(path) or len(get_generation_checkpoint_artifacts(path)) > 1:
                print(f"WARNING: Failed to clear checkpoint {path}: {e}. "
                      "Stale checkpoint artifacts may remain.")


def get_review_resume_source_offsets(checkpoint, entries, batch_size):
    """Return consumed-input offsets in the coordinate space of this input."""
    if not checkpoint:
        return []
    count = checkpoint["completed_batches"]
    if _entries_fingerprint(entries) == checkpoint.get("source_sha256"):
        return list(checkpoint.get("source_offsets") or
                    [min(len(entries), (index + 1) * batch_size) for index in range(count)])
    lengths = checkpoint["batch_lengths"]
    if len(lengths) == count:
        offsets, consumed = [], 0
        for length in lengths:
            consumed += length
            offsets.append(consumed)
        return offsets
    # Legacy output checkpoints lack per-batch lengths; the whole corrected
    # prefix is still established, although its earlier split points are not.
    corrected_count = len(checkpoint["all_corrected"])
    offsets = [min(corrected_count, (index + 1) * batch_size) for index in range(count)]
    if offsets:
        offsets[-1] = corrected_count
    return offsets


def _load_resume_state(output_path, total_batches_estimate, batch_size, context_window,
                        entries, all_corrected, total_stats, execution_sha256=None):
    """Load a checkpoint (if any) and compute where this run should resume from.

    Shared by main()'s contextual and non-contextual review loops, which
    otherwise duplicate this checkpoint-load + resume_offset bookkeeping.

    Returns (all_corrected, total_stats, previous_tail, completed_batches,
    batch_lengths, failed_batches, resume_offset, checkpoint).
    """
    previous_tail = None
    completed_batches = 0
    batch_lengths = []
    failed_batches = []
    checkpoint = load_checkpoint(output_path, total_batches_estimate, batch_size,
                                 context_window, entries, execution_sha256=execution_sha256)
    if checkpoint:
        completed_batches = checkpoint["completed_batches"]
        all_corrected = checkpoint["all_corrected"]
        total_stats = checkpoint["total_stats"]
        previous_tail = checkpoint["previous_tail"]
        batch_lengths = checkpoint["batch_lengths"]
        failed_batches = checkpoint["failed_batches"]

    source_offsets = get_review_resume_source_offsets(checkpoint, entries, batch_size)
    resume_offset = source_offsets[-1] if source_offsets else 0
    return (all_corrected, total_stats, previous_tail, completed_batches,
            batch_lengths, failed_batches, resume_offset, checkpoint)


# ── Speaker de-duplication (merge aliases that are the same character) ──────────

SPEAKER_DEDUPE_SYSTEM_PROMPT = (
    "You are an expert at character identity resolution in narrated fiction. "
    "You are given the list of distinct SPEAKER labels found in an audiobook script, "
    "with a few sample lines for each. Some labels refer to the SAME character by "
    "different names (e.g. a first name vs full name vs an epithet like 'the old man' "
    "vs a title). Your job is to group labels that denote the same character and choose "
    "one canonical name for each group.\n\n"
    "Rules:\n"
    "- Only merge labels you are confident are the same individual. When in doubt, keep them separate.\n"
    "- NEVER merge distinct characters just because their names are similar.\n"
    "- NEVER merge a label that denotes MULTIPLE characters speaking together "
    "(e.g. 'RAM AND REM', 'THE TWINS', 'EMILIA/PUCK (DUAL)', 'CROWD', 'CHORUS') into a single "
    "character. Leave any such combined/group label exactly as-is.\n"
    "- Disguises, nicknames, internal monologue, and alternate forms of ONE character DO merge "
    "(e.g. 'SUBARU (INTERNAL)'->'SUBARU', a nickname->the real name).\n"
    "- Keep NARRATOR as its own label; never merge it into a character.\n"
    "- Prefer the clearest proper name as the canonical (usually the most common full name).\n"
    "- If an EXISTING CANONICAL NAMES list is provided, reuse those exact canonical names "
    "when a label refers to one of them, so names stay consistent across books.\n\n"
    "Respond with ONLY a JSON object mapping every label that should change to its canonical "
    'name, like {"Kenji Sato": "KENJI", "the boy": "KENJI"}. Labels that are already canonical '
    "or have no merge must be omitted. No prose, no markdown."
)


def _collect_speaker_samples(entries, max_per_speaker=4):
    samples = {}
    for e in entries:
        sp = (e.get("speaker") or e.get("type") or "").strip()
        txt = (e.get("text") or "").strip()
        if not sp or not txt:
            continue
        lines = samples.setdefault(sp, [])
        if len(lines) < max_per_speaker:
            lines.append(txt[:160])
    return samples


def dedupe_speakers(client, model_name, entries, registry_path=None,
                    max_tokens=2000, temperature=0.2, context_length=None, params=None,
                    registry_staging=None):
    """Ask the LLM to merge speaker labels that are the same character.
    Does not mutate `entries` - the caller applies the returned changes. If
    `registry_path` is given, the shared canonical names there are fed to the
    model and updated, so the same character keeps one canonical name across
    multiple books (a series).
    Returns (mapping, renamed_count, changes), where changes is a list of
    (index, key, new_value) tuples to apply to the caller's own entries list.

    This stage runs AFTER the review's last batch, so it reports its own
    progress under SPEAKER_MERGE_MARKER instead of leaving the finished
    "Reviewing batch N/N" line as the newest marker (#2037). Every whole label
    list goes to the model in ONE call, so its count is that call's number:
    "call 1/1". The lines are plain stdout like the review's own, so they land
    in the task log the UI reads and in the downloadable review log."""
    started = time.monotonic()
    samples = _collect_speaker_samples(entries)
    speakers = sorted(samples.keys())
    if not speakers:
        return {}, 0, []

    planned_calls = 1 if len(speakers) >= 2 else 0
    plan = "1 model call" if planned_calls else "no model call needed"
    print(f"{SPEAKER_MERGE_MARKER} {len(speakers)} speaker label(s), {plan}", flush=True)

    # Load existing alias map / cross-book canonical registry. Entries here are
    # treated as KNOWN aliases and applied deterministically (this is how a
    # nickname file from find_nicknames.py gets honored on a re-run).
    def get_current_registry():
        if not registry_path or not os.path.exists(registry_path):
            return {}
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                current = json.load(f)
            get_validated_alias_graph(current)
            return current
        except (ValueError, OSError) as e:
            raise ValueError(f"Invalid alias registry at {registry_path}: {e}") from e

    initial_registry = get_current_registry()
    registry = get_resolved_speaker_merge_map(initial_registry)
    existing_canonicals = sorted(set(registry.values())) if registry else []

    def _resolve_label(name):
        """Return the actual speaker label that `name` refers to (using the
        same normalization generation uses), or None."""
        return resolve_speaker_label(name, samples.keys())

    # Pre-seed forced merges from the alias file for labels present in this script
    forced_map = {}
    for actual in speakers:
        variant = resolve_speaker_label(actual, registry)
        canonical = registry.get(variant)
        if canonical and actual != canonical and is_speaker_merge_allowed(actual, canonical):
            forced_map[actual] = canonical

    catalog_lines = []
    for sp in speakers:
        sample_str = " | ".join(samples[sp])
        catalog_lines.append(f'- "{sp}": {sample_str}')
    user_parts = []
    if existing_canonicals:
        user_parts.append("EXISTING CANONICAL NAMES (reuse these exact spellings when applicable):")
        user_parts.append(", ".join(existing_canonicals))
        user_parts.append("")
    if forced_map:
        user_parts.append("KNOWN ALIASES (already confirmed — apply these and reuse the spelling):")
        user_parts.append(json.dumps(forced_map, ensure_ascii=False))
        user_parts.append("")
    user_parts.append("SPEAKER LABELS IN THIS SCRIPT:")
    user_parts.extend(catalog_lines)
    user_parts.append("\nReturn the JSON merge map now.")
    user_prompt = "\n".join(user_parts)

    mapping = {}
    if planned_calls:
        print(f"{SPEAKER_MERGE_MARKER} call 1/1 - asking the model", flush=True)
    call_started = time.monotonic()
    try:
        call_params = replace(params or LLMGenParams(), max_tokens=max_tokens,
                              temperature=temperature,
                              context_length=(context_length if context_length is not None
                                              else params.context_length if params else None),
                              hard_max_tokens=12000,
                              task_max_tokens=max_tokens, task_hard_max_tokens=12000,
                              request_output_ceiling=12000)
        result = (call_llm_for_object(
            client, model_name, SPEAKER_DEDUPE_SYSTEM_PROMPT, user_prompt, call_params,
            label="SPEAKER DEDUPE") if len(speakers) >= 2 else {})
        # call_llm_for_object returns {} for an accepted (possibly empty) object
        # and [] when every attempt failed, so this is the one place the two are
        # distinguishable - say which happened instead of reporting the failure
        # as a 0-label success.
        mapping = result if isinstance(result, dict) else {}
        if planned_calls:
            outcome = (f"{len(mapping)} label(s) proposed" if isinstance(result, dict)
                       else "no usable response - applying known aliases only")
            print(f"{SPEAKER_MERGE_MARKER} call 1/1 finished in "
                  f"{time.monotonic() - call_started:.1f}s - {outcome}", flush=True)
    except Exception as e:
        # LLM unavailable — still apply the known aliases from the file
        if planned_calls:
            print(f"{SPEAKER_MERGE_MARKER} call 1/1 failed after "
                  f"{time.monotonic() - call_started:.1f}s ({type(e).__name__}: {e}) - "
                  f"applying known aliases only", flush=True)
        print(f"  Speaker dedupe LLM step failed ({e}); applying known aliases only.")

    # Sanitize: drop no-ops, self-maps, and any NARRATOR merges
    proposals = []
    for variant, canonical in mapping.items():
        if not isinstance(variant, str) or not isinstance(canonical, str):
            continue
        variant, canonical = variant.strip(), canonical.strip()
        if not variant or not canonical:
            continue
        actual = _resolve_label(variant)
        if not actual or actual == canonical:
            continue
        # A removed human alias must not be restored by the in-flight model.
        if resolve_speaker_label(actual, initial_registry) is not None:
            continue
        proposals.append({actual: canonical})

    def get_updated_registry(current):
        accepted = get_safe_alias_proposals(proposals, speakers, current)
        updated = dict(current)
        updated.update(accepted)
        resolved = get_resolved_speaker_merge_map(updated)
        clean = {}
        for actual in speakers:
            variant = resolve_speaker_label(actual, resolved)
            canonical = resolved.get(variant)
            if canonical and actual != canonical and is_speaker_merge_allowed(actual, canonical):
                clean[actual] = canonical
        return updated, clean

    if registry_path:
        # Revalidate while locked before deriving either saved aliases or the
        # changes returned to the caller. Keep human registry entries verbatim.
        with file_lock(registry_path):
            current_registry = get_current_registry()
            updated_registry, clean_map = get_updated_registry(current_registry)
            if registry_staging is not None:
                from project_scope_files import capture_scope_file
                parent = os.path.dirname(os.path.abspath(registry_path))
                _, raw = capture_scope_file(parent, os.path.basename(registry_path),
                                             limit=4 * 1024**2, include_bytes=True)
                # Tie the decisions above to the exact bytes that will be
                # compared again under the publication lock.
                captured = json.loads(raw) if raw is not None else {}
                if captured != current_registry:
                    raise ValueError('Alias registry changed during review')
                registry_staging.update(path=os.path.abspath(registry_path), before=raw,
                    after=(json.dumps(updated_registry, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
                           if updated_registry != current_registry else None))
            elif updated_registry != current_registry:
                atomic_json_write(updated_registry, registry_path, allow_nonatomic_fallback=False)
    else:
        _, clean_map = get_updated_registry({})

    if not clean_map:
        print(f"{SPEAKER_MERGE_MARKER} no duplicate names found in "
              f"{time.monotonic() - started:.1f}s", flush=True)
        return {}, 0, []

    renamed = 0
    changes = []
    for i, e in enumerate(entries):
        sp = (e.get("speaker") or e.get("type") or "").strip()
        if sp in clean_map:
            key = "speaker" if "speaker" in e else "type"
            changes.append((i, key, clean_map[sp]))
            renamed += 1

    for variant, canonical in clean_map.items():
        print(f"  [MERGE] '{variant}' -> '{canonical}'")

    print(f"{SPEAKER_MERGE_MARKER} {len(clean_map)} label(s) merged, {renamed} "
          f"entries updated in {time.monotonic() - started:.1f}s", flush=True)
    return clean_map, renamed, changes


def _remapped_voice_config(cfg, mapping):
    """Remap a parsed voice config in memory, returning the number moved."""
    moved = 0
    for variant, canonical in mapping.items():
        if variant not in cfg:
            continue
        if canonical not in cfg:
            cfg[canonical] = cfg[variant]
        else:
            existing = cfg[canonical]
            variant_cfg = cfg[variant]
            if isinstance(existing, dict) and isinstance(variant_cfg, dict):
                for key, value in variant_cfg.items():
                    if key not in existing:
                        existing[key] = value
            elif isinstance(variant_cfg, dict):
                cfg[canonical] = variant_cfg
        del cfg[variant]
        moved += 1
    return moved


def _remap_voice_config(voice_config_path, mapping):
    """Rename keys in a voice_config.json so configured voices follow renamed speakers."""
    if not voice_config_path or not os.path.exists(voice_config_path) or not mapping:
        return 0
    # Hold the lock across the whole read-modify-write so this can't lose an
    # update to (or clobber) a concurrent write from the UI, e.g. "apply cast
    # to multiple books" writing the same companion voice_config.json.
    try:
        with file_lock(voice_config_path):
            cfg = safe_load_json(voice_config_path)
            if cfg is None:
                return 0
            moved = _remapped_voice_config(cfg, mapping)
            if moved:
                try:
                    atomic_json_write(cfg, voice_config_path, allow_nonatomic_fallback=False)
                except OSError as e:
                    if e.errno == errno.EXDEV:
                        # The strict writer refused a cross-device publish. Stop
                        # before the review publishes its output: the in-memory
                        # remap has not reached disk, so the voice config is still
                        # the last good version.
                        record_failure('voice_config_remap_write_exdev', e, task='review',
                                       resource=voice_config_path, operation='remap_voice_config')
                        raise OSError(errno.EXDEV,
                            f"Review stopped: the voice config could not be written atomically "
                            f"at {voice_config_path} (errno {errno.EXDEV}, EXDEV cross-device "
                            "link). Keep the data directory on one filesystem, then retry. "
                            "The voice config on disk is unchanged.") from e
                    print(f"  Warning: failed to write {voice_config_path}: {e}")
                    moved = 0
    except TimeoutError as e:
        print(f"  Warning: could not lock {voice_config_path} for speaker remap ({e}); skipping.")
        return 0
    return moved


def _is_section_break(text):
    """Check if text looks like a chapter heading or section title."""
    stripped = text.strip()
    # "CHAPTER ONE", "CHAPTER II", "Chapter Three", etc.
    if re.match(r'(?i)^chapter\b', stripped):
        return True
    # All-caps short text = likely a title ("A SCANDAL IN BOHEMIA", "THE RED-HEADED LEAGUE")
    if stripped == stripped.upper() and len(stripped) < 80 and stripped.isascii():
        return True
    return False


def get_typed_metadata(entry):
    """Return a recursive JSON-type-preserving identity of non-text fields."""
    return _entries_fingerprint({key: value for key, value in entry.items() if key != 'text'})


def merge_consecutive_narrators(entries, max_merged_length=800):
    """Merge consecutive NARRATOR entries that share the same instruct value.

    Skips merging across section/chapter breaks. Caps merged text at
    max_merged_length characters to avoid creating overly long TTS entries.
    """
    if not entries:
        return entries, 0

    merged = []
    merges = 0
    i = 0
    while i < len(entries):
        entry = entries[i]

        if entry.get("speaker") != "NARRATOR" or _is_section_break(entry.get("text", "")):
            merged.append(entry)
            i += 1
            continue

        # Start a narrator run — accumulate consecutive NARRATORs with same instruct
        combined_text = entry["text"]
        instruct = entry.get("instruct", "")
        metadata = get_typed_metadata(entry)
        run_count = 1
        j = i + 1

        while j < len(entries):
            next_entry = entries[j]
            if next_entry.get("speaker") != "NARRATOR":
                break
            if next_entry.get("instruct", "") != instruct:
                break
            if entry.get("pause_after") or metadata != get_typed_metadata(next_entry):
                break
            if _is_section_break(next_entry.get("text", "")):
                break
            candidate = combined_text + " " + next_entry["text"]
            if len(candidate) > max_merged_length:
                break
            combined_text = candidate
            run_count += 1
            j += 1

        merged.append({**entry, "text": combined_text})
        if run_count > 1:
            merges += run_count - 1
        i = j

    return merged, merges


REVIEW_ATTRIBUTION_RULE = (
    'Speaker labels, including NARRATOR, may be corrected using the text and context. '
    'Input speaker/type labels and Boolean spoken flags are fallible hints, not '
    'attribution locks. Use NARRATOR for actual narration; do not relabel it without '
    'contextual evidence. Return the required labels and any documented optional annotation corrections.')


from speaker_traits import GENDERS, AGE_GROUP_NAMES

REVIEW_ANNOTATION_SCHEMA = {
    'type': {'type': ['string', 'null'], 'enum': ['NARRATOR', 'SPOKEN', None]},
    'spoken': {'type': ['boolean', 'null']},
    'speaker_gender': {'type': ['string', 'null'], 'enum': [*GENDERS, None]},
    'speaker_age_group': {'type': ['string', 'null'], 'enum': [*AGE_GROUP_NAMES, None]},
    'speaker_ageless': {'type': ['boolean', 'null']},
}
REVIEW_ANNOTATION_RULE = (
    'You may also return corrected annotations: type (NARRATOR or SPOKEN), spoken '
    '(Boolean), speaker_gender (' + ', '.join(GENDERS) + '), speaker_age_group ('
    + ', '.join(AGE_GROUP_NAMES) + '), speaker_ageless (Boolean). '
    'Use the passage context and evidence for the corrected speaker at this point '
    'in the story; do not invent traits. Omit type to retain the source role hint. '
    'Omit other annotations to retain their existing value or known-speaker fallback. Use null only to explicitly clear an annotation. '
    'Source text, source_span, provenance, pause_after and other source metadata '
    'are protected: do not edit them; they are restored from the original entries. ')
REVIEW_CLASSIC_RESPONSE_RULE = (
    'REVIEW RESPONSE CONTRACT (takes precedence over conflicting format instructions): '
    'Return a JSON array with speaker, text and instruct string fields in every entry. '
    + REVIEW_ANNOTATION_RULE +
    'When a speaker changes and no explicit annotations are supplied, known '
    'attributes follow that corrected speaker; unknown or conflicting attributes '
    'are cleared and reported. Preserve all text in its original order. Split or '
    'merge only when text and source metadata can be preserved. Never omit '
    'speaker, text or instruct.')
REVIEW_LABEL_SCHEMA = {"name": "review_labels", "schema": {
    "type": "array", "items": {"type": "object", "properties": {
        "n": {"type": "integer"}, "speaker": {"type": "string"},
        "instruct": {"type": "string"}, **REVIEW_ANNOTATION_SCHEMA},
        "required": ["n", "speaker", "instruct"], "additionalProperties": False}}}
REVIEW_LABEL_SYSTEM = (
    'Review speaker identities and TTS delivery instructions for an audiobook. '
    'Use speech tags and surrounding context to correct the speaker of each entry. '
    'Return ONLY a JSON array with n, speaker and instruct in each object, one '
    'object per input index including unchanged entries. Speaker names are '
    'UPPERCASE; narration uses NARRATOR. Never return text, change entry boundaries, '
    'or label the surrounding context. ' + REVIEW_ANNOTATION_RULE + REVIEW_ATTRIBUTION_RULE)


def get_review_annotations(entry):
    return {key: copy.deepcopy(entry[key]) for key in REVIEW_ANNOTATION_SCHEMA if key in entry}


def get_review_annotation_findings(entry, number):
    findings = []
    for key, value in get_review_annotations(entry).items():
        schema = REVIEW_ANNOTATION_SCHEMA[key]
        valid = (value is None or (type(value) is bool if 'boolean' in schema['type']
                 else isinstance(value, str) and value in schema['enum']))
        if not valid:
            findings.append({'code': 'review_annotation_value', 'entry_number': number,
                             'field': key, 'message': ('Review annotation ' + key + ' must be ' +
                                 ('a Boolean or null.' if 'boolean' in schema['type'] else
                                  ', '.join(str(value) for value in schema['enum']) + '.'))})
    return findings



def get_review_prompt_entry(entry):
    """Show only contract-valid optional annotations; bind unchanged facts to source."""
    invalid = {finding['field'] for finding in get_review_annotation_findings(entry, 0)}
    return {key: copy.deepcopy(value) for key, value in entry.items()
            if key != SCRIPT_POLICY_KEY and key not in invalid}


def apply_review_annotations(entry, reply):
    for key, value in get_review_annotations(reply).items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return entry


def get_review_source_narration(entry):
    """Return the quote-derived role hint for lossless metadata boundary checks."""
    from dialogue_spans import get_source_narration
    return get_source_narration(entry)


def get_review_label_binding(batch_entries, entries):
    """Validate complete indexes and label fields, without trusting model text."""
    from pass_quality import index_head_check
    ok, reason, ordered = index_head_check(batch_entries, entries)
    findings = []
    if not ok:
        findings.append({"code": "review_label_alignment", "message": reason})
    else:
        for i, row in enumerate(ordered):
            findings.extend(get_review_annotation_findings(row, i + 1))
            if not {"n", "speaker", "instruct"} <= set(row) or set(row) - {"n", "speaker", "instruct"} - set(REVIEW_ANNOTATION_SCHEMA):
                findings.append({"code": "review_label_fields", "entry_number": i + 1,
                                 "message": "Return n, speaker, instruct and only the documented editable annotations."})
            for key in ("speaker", "instruct"):
                if (not isinstance(row.get(key), str)
                        or (key == "speaker" and not row[key].strip())):
                    findings.append({"code": "review_label_value", "entry_number": i + 1,
                                     "message": "Labels must be strings; speaker must be nonempty."})
    return ordered, {"passed": not findings, "findings": findings}


def get_review_field_report(entries, *, validate_annotations=True):
    """Validate the common classic-review fields before source binding."""
    findings = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            findings.append({"code": "invalid_entry", "entry_number": number,
                             "message": "Review entries must be JSON objects."})
            continue
        if validate_annotations:
            findings.extend(get_review_annotation_findings(entry, number))
        missing = [key for key in ("speaker", "text", "instruct") if key not in entry]
        if missing:
            findings.append({"code": "missing_fields", "entry_number": number,
                             "fields": missing,
                             "message": "Review entry is missing required fields."})
        for key in ('speaker', 'text', 'instruct'):
            if key in entry and (not isinstance(entry[key], str)
                                 or (key != 'instruct' and not entry[key].strip())):
                findings.append({'code': 'invalid_field', 'entry_number': number,
                                 'field': key, 'message': 'Review labels and text must be strings; speaker and text must be nonempty.'})
    return {"passed": not findings, "findings": findings}


def get_review_text_identity(text):
    """Keep word case/contractions and NFC nonpunctuation symbols."""
    symbols = ''.join(char for char in unicodedata.normalize('NFC', text)
                      if not char.isspace() and not unicodedata.category(char).startswith('P'))
    return get_word_identity([text])['sha256'], symbols


REVIEW_SPEAKER_FIELDS = ('speaker_gender', 'speaker_age_group', 'speaker_ageless', 'spoken')


def get_review_explicit_speaker_annotations(annotations):
    """Return only the speaker annotations a reviewer explicitly supplied."""
    # An explicit null is a request to clear, not evidence about the speaker.
    return {key: value for key, value in (annotations or {}).items()
            if key in REVIEW_SPEAKER_FIELDS and value is not None}


def get_review_speaker_bound_entry(entry, original_entries, reference_entries, batch_entries,
                                   explicit_annotations=None):
    """Rebind changed speaker annotations without moving source provenance.

    A corrected speaker with a known source row follows that row. A corrected
    speaker with no known row keeps the annotations the reviewer supplied for it
    (#2036): they are the model's evidence, so the unresolved warning is only
    for a changed speaker that carries stale attributes and no explicit ones.
    """
    target = entry['speaker'].strip().casefold()
    if all(str(original.get('speaker', '')).strip().casefold() == target
           for original in original_entries):
        return entry, None
    def matching(rows):
        return [row for row in rows
                if str(row.get('speaker', '')).strip().casefold() == target]
    donors = matching(reference_entries) or matching(batch_entries)
    states = [{key: copy.deepcopy(row[key]) for key in REVIEW_SPEAKER_FIELDS if key in row}
              for row in donors]
    identities = {get_typed_metadata(state) for state in states}
    explicit = get_review_explicit_speaker_annotations(explicit_annotations)
    # A nearby overlapping target entry is stronger evidence than a whole-batch
    # roster. Never choose between conflicting temporal states or invent traits.
    result = {key: value for key, value in entry.items() if key not in REVIEW_SPEAKER_FIELDS}
    if len(identities) == 1 and states[0]:
        result.update(states[0])
        return result, None
    if explicit:
        result.update(copy.deepcopy(explicit))
        return result, None
    if not any(key in entry for key in REVIEW_SPEAKER_FIELDS):
        return result, None
    return result, {'code': 'review_speaker_metadata_unresolved', 'message': (
        'Corrected speaker attributes are unknown or conflicting; stale speaker '
        'attributes were cleared. Source text, provenance and pauses were retained.')}


def get_review_labelled_entries(batch_entries, ordered):
    """Apply index labels and the same speaker-annotation policy as full review."""
    bound, warnings = [], []
    for number, (original, labels) in enumerate(zip(batch_entries, ordered), 1):
        entry = {**copy.deepcopy(original), 'speaker': labels['speaker'],
                 'instruct': labels['instruct']}
        entry, warning = get_review_speaker_bound_entry(entry, [original], [], batch_entries,
                                                       get_review_annotations(labels))
        bound.append(apply_review_annotations(entry, labels))
        if warning:
            warnings.append({**warning, 'entry_number': number})
    return bound, warnings


def get_review_raw_text_slice(text, start, end):
    """Map a normalized token interval back to the reviewer's exact whitespace."""
    position, left = 0, None
    for match in re.finditer(r'\S+', text):
        stop = position + len(match.group())
        if left is None and position <= start < stop:
            left = match.start() + start - position
        if position < end <= stop:
            if left is None:
                break
            return text[left:match.start() + end - position]
        position = stop + 1
    raise ValueError('Review fragment has no matching raw text interval')


def verify_review_source_spans(entries, source_text):
    """Require exact normalized slices and ordered, non-overlapping intervals."""
    previous_end = 0
    for number, entry in enumerate(entries, 1):
        if 'source_span' not in entry:
            continue
        try:
            get_source_span_fragments(source_text, entry['text'], entry['source_span'], [entry['text']])
        except SourceSpanError as error:
            raise SourceSpanError(error.reason_code, entry_number=number,
                                  fragment_number=error.fragment_number) from error
        start, end = entry['source_span']
        if start < previous_end:
            raise SourceSpanError('source_span_order', entry_number=number)
        previous_end = end


def get_review_source_entries(entries, source_text):
    """Resolve inherited parent intervals only through a complete exact partition."""
    resolved = []
    position = 0
    while position < len(entries):
        entry = entries[position]
        stop = position + 1
        if 'source_span' in entry:
            while stop < len(entries) and entries[stop].get('source_span') == entry['source_span']:
                stop += 1
        group = entries[position:stop]
        if len(group) > 1:
            texts = [row['text'] for row in group]
            try:
                spans = get_source_span_fragments(source_text, ' '.join(texts), entry['source_span'], texts)
            except SourceSpanError as error:
                raise SourceSpanError(error.reason_code, entry_number=position + 1,
                                      fragment_number=error.fragment_number) from error
            resolved.extend({**row, 'source_span': span} for row, span in zip(group, spans))
        else:
            resolved.append(entry)
        position = stop
    verify_review_source_spans(resolved, source_text)
    return resolved


def get_review_source_span_finding(error, *, entry_number=None):
    return {'code': 'review_source_span_unverified',
            'entry_number': error.entry_number or entry_number,
            'fragment_number': error.fragment_number,
            'reason_code': error.reason_code,
            'message': ('Review fragments require verified, ordered source spans; retain original entries. '
                        f'entry={error.entry_number or entry_number}; fragment={error.fragment_number}; '
                        f'reason={error.reason_code}.')}


def get_review_source_text(data_dir, script_path, script_bytes, entries, *, _source_depth=0):
    snapshot = get_review_source_snapshot(data_dir, script_path, script_bytes, entries,
                                         _source_depth=_source_depth)
    return snapshot['text'] if snapshot is not None else None


def is_review_source_span_script(entries):
    """True when any entry carries a source_span. The one test for both the
    receipt contract and source capture, so the two cannot disagree."""
    return any('source_span' in entry for entry in entries)


def get_review_source_snapshot(data_dir, script_path, script_bytes, entries, *, _source_depth=0):
    """Capture only a bound book source whose prepared coordinates verify."""
    if not is_review_source_span_script(entries):
        return None
    import hashlib
    from pathlib import Path
    from book_state_transaction import reuse_book_state, get_book_operation_token
    from project_scope_files import capture_scope_file
    from script_selection_binding import (_read_selection_state, _read_source_snapshot,
        _path_digest, parse_script_binding, saved_binding_matches, check_binding_source,
        is_script_selection_pending, BINDING_NAME, MAX_BINDING_BYTES)
    from source_encoding import get_decoded_source_text, get_normalized_source_newlines
    from generate_script import get_preprocessed_source
    from three_pass_generate import prepare_source_text
    try:
        active = os.path.abspath(script_path) == os.path.abspath(os.path.join(data_dir, 'annotated_script.json'))
        directory = data_dir if active else os.path.dirname(os.path.abspath(script_path))
        name = BINDING_NAME if active else Path(script_path).stem + '.selection_binding.json'
        with reuse_book_state(data_dir), file_lock(os.path.join(data_dir, 'state.json')):
            state = _read_selection_state(data_dir, required=active)
            _, raw_binding = capture_scope_file(directory, name, limit=MAX_BINDING_BYTES, include_bytes=True)
            binding = parse_script_binding(raw_binding)
            if not saved_binding_matches(binding, script_sha256=hashlib.sha256(script_bytes).hexdigest(),
                                         entries=entries):
                raise ValueError('Review script has no matching source binding')
            selected = state.get('input_file_path')
            if active:
                if is_script_selection_pending(state) or not isinstance(selected, str):
                    raise ValueError('Review source selection is unavailable')
                candidates = [selected]
            else:
                candidates = [selected] if isinstance(selected, str) else []
                uploads = Path(data_dir, 'uploads')
                if uploads.is_dir():
                    for path in uploads.iterdir():
                        candidates.extend((str(path), path.as_posix(),
                                           os.path.join(data_dir, 'uploads', path.name)))
            paths = {path for path in candidates if _path_digest(path) == binding['source_path_sha256']}
            if len(paths) != 1:
                raise ValueError('Review source path cannot be verified')
            source_path = paths.pop()
            digest, raw = _read_source_snapshot(source_path, include_bytes=True, owning_root=data_dir)
            if digest != binding['source_sha256']:
                raise ValueError('Review source bytes changed')
            if active:
                check_binding_source(raw_binding, get_book_operation_token(data_dir, state=state),
                                     digest, source_path=source_path)
            identity = {'book_token': get_book_operation_token(data_dir, state=state) if active else None,
                        'source_path': source_path if active else None,
                        'sources': [{'source_path': source_path, 'source_sha256': digest,
                                     'binding_path': os.path.join(directory, name),
                                     'binding_sha256': hashlib.sha256(raw_binding).hexdigest()}]}
        decoded, _encoding = get_decoded_source_text(raw)
        decoded = get_normalized_source_newlines(decoded)
        policy = get_literal_policy(entries)
        expected = policy.get('source_sha256') if policy else None
        if Path(source_path).suffix.lower() == '.json':
            if _source_depth:
                raise ValueError('Review source binding chain is not an original book source')
            source_entries = json.loads(raw)
            if not isinstance(source_entries, list) or any(not isinstance(row, dict) for row in source_entries):
                raise ValueError('Selected saved source is not a script')
            original = get_review_source_snapshot(data_dir, source_path, raw, source_entries, _source_depth=1)
            text = original['text'] if original is not None else None
            if text is None or expected is not None and hashlib.sha256(text.encode('utf-8')).hexdigest() != expected:
                raise ValueError('Selected saved source has no verified original coordinates')
            get_review_source_entries(entries, text)
            identity['sources'].extend(original['identity']['sources'])
            return {'text': text, 'identity': identity}
        if policy and policy.get('input_sha256', digest) != digest:
            raise ValueError('Literal source identity differs from the captured source')
        sources = set()
        for strip_front_matter in (False, True):
            prepared, _report = get_preprocessed_source(decoded, strip_front_matter=strip_front_matter)
            repaired, _report = prepare_source_text(prepared)
            for text in (prepared, repaired):
                if expected is not None and hashlib.sha256(text.encode('utf-8')).hexdigest() != expected:
                    continue
                try:
                    get_review_source_entries(entries, text)
                except ValueError:
                    continue
                sources.add(text)
        if len(sources) != 1:
            raise ValueError('Prepared review source coordinates are unavailable or ambiguous')
        return {'text': sources.pop(), 'identity': identity}
    except (OSError, ValueError, TypeError, UnicodeError) as error:
        record_failure('review_source_unavailable', error, task='review')
        return None


def get_review_source_binding(batch_entries, entries, literal_source_preserving=False, source_text=None):
    """Bind classic labels and immutable metadata to ordered source spans.

    Lossless boundary changes inherit only compatible source metadata. A model
    entry crossing conflicting metadata or a pause is partitioned at the source
    boundaries, retaining its labels without moving or discarding source facts.
    Punctuation drift can recover labels only for unique, unchanged per-entry
    word sequences, using the same label contract as index-only review.
    """
    report = get_review_field_report(entries)
    if not report['passed']:
        return None, report
    findings = []

    if source_text is not None:
        try:
            batch_entries = get_review_source_entries(batch_entries, source_text)
        except SourceSpanError as error:
            return None, {'passed': False, 'findings': [get_review_source_span_finding(error)]}

    original_texts = [" ".join(entry.get("text", "").split()) for entry in batch_entries]
    reviewed_texts = [" ".join(entry["text"].split()) for entry in entries]
    original_text = " ".join(original_texts)
    reviewed_text = " ".join(reviewed_texts)
    if literal_source_preserving and reviewed_text == original_text:
        audit = audit_literal_words(original_text, entries)
        if not audit['passed']:
            return None, {"passed": False, "findings": [{
                "code": "literal_source_word_mismatch",
                "message": "Review narration words could not be verified.", "details": audit}]}
    if reviewed_text != original_text:
        try:
            source_ids = [get_review_text_identity(entry['text']) for entry in batch_entries]
            reply_ids = [get_review_text_identity(entry['text']) for entry in entries]
        except ValueError:
            source_ids, reply_ids = None, None
        if (source_ids is not None and source_ids == reply_ids
                and len(set(source_ids)) == len(source_ids)):
            labels = [{"n": i, "speaker": entry["speaker"], "instruct": entry["instruct"],
                       **get_review_annotations(entry)} for i, entry in enumerate(entries)]
            ordered, quality = get_review_label_binding(batch_entries, labels)
            if quality['passed']:
                bound, warnings = get_review_labelled_entries(batch_entries, ordered)
                return bound, {**quality, "source_bound_label_recovery": True, 'warnings': warnings}
            return None, quality
        if literal_source_preserving:
            audit = audit_literal_words(original_text, entries)
            if not audit["passed"]:
                findings.append({"code": "literal_source_word_mismatch",
                                 "message": "Review changed ordered narration words.",
                                 "details": audit})
        if not findings:
            findings.append({"code": "review_text_mismatch", "message": (
                "Reviewed entries must preserve the complete text in its original order; "
                "punctuation-only label recovery requires unique, unchanged entry correspondence.")})
        return None, {"passed": False, "findings": findings}

    source_spans = []
    position = 0
    for original, text in zip(batch_entries, original_texts):
        source_spans.append((position, position + len(text), original))
        position += len(text) + 1
    source_numbers = {start: number for number, (start, _, _) in enumerate(source_spans, 1)}
    bound, warnings, span_fragments = [], [], {}
    position = 0
    for number, (entry, text) in enumerate(zip(entries, reviewed_texts), 1):
        end = position + len(text)
        sources = [(start, stop, original) for start, stop, original in source_spans
                   if start < end and stop > position]
        metadata = [{key: value for key, value in original.items()
                     if key not in ('speaker', 'text', 'instruct', 'pause_after')}
                    for _, _, original in sources]
        source_roles = [get_review_source_narration(original) for _, _, original in sources]
        if not sources:
            findings.append({"code": "review_metadata_conflict", "entry_number": number,
                             "message": "A reviewed entry has no matching source span."})
        else:
            # JSON identity also distinguishes nested bool/int values which
            # compare equal in Python. Never collapse distinct source facts.
            identities = [get_typed_metadata(value) for value in metadata]
            groups = []
            for i, source in enumerate(sources):
                if (i == 0 or identities[i] != identities[i - 1]
                        or source_roles[i] is not source_roles[i - 1]
                        or 'pause_after' in sources[i - 1][2]):
                    groups.append(([source], metadata[i]))
                else:
                    groups[-1][0].append(source)
            for group, facts in groups:
                start, stop, original = group[-1]
                left, right = max(position, group[0][0]), min(end, stop)
                row = {**copy.deepcopy(facts), "speaker": entry["speaker"],
                       "text": (entry["text"] if len(groups) == 1
                                else get_review_raw_text_slice(entry["text"], left - position, right - position)),
                       "instruct": entry["instruct"]}
                if right == stop and 'pause_after' in original:
                    row['pause_after'] = copy.deepcopy(original['pause_after'])
                if len(group) == 1 and start == left and stop == right:
                    row['text'] = original['text']
                if 'source_span' in facts:
                    if len(group) != 1:
                        findings.append(get_review_source_span_finding(
                            SourceSpanError('incompatible_source_intervals', fragment_number=1),
                            entry_number=source_numbers[start]))
                    else:
                        fragment = span_fragments.setdefault(start, {'original': original, 'rows': [],
                            'entry_number': source_numbers[start], 'changed': False})
                        fragment['rows'].append(row)
                        fragment['changed'] |= left != start or right != stop
                row, warning = get_review_speaker_bound_entry(row,
                    [original for _, _, original in group],
                    [original for _, _, original in sources], batch_entries,
                    get_review_annotations(entry))
                row = apply_review_annotations(row, entry)
                bound.append(row)
                if 'source_span' in facts and len(group) == 1:
                    span_fragments[start]['rows'][-1] = row
                if warning:
                    warnings.append({**warning, 'entry_number': number})
        position = end + 1
    if any(fragment['changed'] for fragment in span_fragments.values()):
        entry_number = None
        try:
            for fragment in span_fragments.values():
                if not fragment['changed']:
                    continue
                original, rows = fragment['original'], fragment['rows']
                entry_number = fragment['entry_number']
                spans = get_source_span_fragments(source_text, original['text'], original['source_span'],
                                                  [row['text'] for row in rows])
                for row, span in zip(rows, spans):
                    row['source_span'] = span
            verify_review_source_spans(bound, source_text)
        except SourceSpanError as error:
            findings.append(get_review_source_span_finding(error, entry_number=entry_number))
    return (None if findings else bound), {"passed": not findings, "findings": findings,
                                         'warnings': warnings}


REVIEW_PARTIAL_EVENT = 'review_partial_label_retention'


def get_review_batch_status(entries, records):
    """Separate terminal partial status from actual model-attempt records."""
    partial = [record for record in records if record.get('event') == REVIEW_PARTIAL_EVENT]
    return {'status': ('failed' if not entries else 'partial' if partial else 'complete'),
            'attempts': [copy.deepcopy(record) for record in records
                         if record.get('event') != REVIEW_PARTIAL_EVENT],
            'partial_retention': copy.deepcopy(partial[-1]) if partial else None}


def get_review_partial_label_retention(batch_entries, entries, literal):
    """Retain labels only on exact, unique, same-position source text."""
    if (not entries
            or not get_review_field_report(batch_entries, validate_annotations=False)['passed']
            or not get_review_field_report(entries)['passed']):
        return None, None
    try:
        source_ids = [get_review_text_identity(row['text']) for row in batch_entries]
        reply_ids = [get_review_text_identity(row['text']) for row in entries]
    except ValueError:
        return None, None
    source_counts, reply_counts = Counter(source_ids), Counter(reply_ids)
    labels, retained, skipped = [], [], []
    for i, original in enumerate(batch_entries):
        reply = entries[i] if i < len(entries) else None
        same_position = (reply is not None and source_ids[i] == reply_ids[i]
                         and original['text'] == reply['text'])
        exact = (same_position and source_counts[source_ids[i]] == 1
                 and reply_counts[source_ids[i]] == 1)
        changed = exact and (any(original[key] != reply[key] for key in ('speaker', 'instruct'))
                             or get_typed_metadata(apply_review_annotations(copy.deepcopy(original), reply))
                             != get_typed_metadata(original))
        chosen = reply if exact else original
        labels.append({'n': i, 'speaker': chosen['speaker'], 'instruct': chosen['instruct'],
                       **(get_review_annotations(chosen) if exact else {})})
        if exact and changed:
            retained.append(i)
        else:
            skipped.append({'n': i, 'reason': ('labels_unchanged' if exact else
                'ambiguous_text_identity' if same_position else 'text_not_exact_at_source_position')})
    ordered, quality = get_review_label_binding(batch_entries, labels)
    if not retained or not quality['passed']:
        return None, None
    proposed = [{'text': original['text'], 'speaker': row['speaker'], 'instruct': row['instruct'],
                 **get_review_annotations(row)} for original, row in zip(batch_entries, ordered)]
    bound, quality = get_review_source_binding(batch_entries, proposed, literal)
    if not quality['passed']:
        return None, None
    return bound, {'event': REVIEW_PARTIAL_EVENT, 'status': 'partial',
                   'retained_positions': retained, 'skipped_positions': skipped,
                   'reason': 'strict_stop_quality_exhausted', 'warnings': quality.get('warnings', [])}


def review_batch(client, model_name, batch_entries, batch_num, total_batches, params,
                 previous_tail=None, source_context=None, max_retries=2,
                 attempt_observer=None, retain_partial_labels=False, source_text=None):
    """Send a batch of script entries through the LLM for review and correction.

    Returns the corrected entries, or None if every attempt failed (so the caller
    can keep the originals and retry on the next resume).
    Completion-aware callers can opt into source-bound partial labels, reported
    by a distinct terminal observer event; those batches remain incomplete.
    """
    def ensure_secondary_admission(profile):
        ensure_review_not_cancelled()
        if params.request_admission is not None and not params.request_admission(profile):
            return False
        config = {"llm": profile["config"],
                  "llm_mode": "remote" if profile["is_remote"] else "local"}
        return not is_llm_on_this_gpu(config) or wait_for_vram_headroom()

    request_params = replace(params, request_admission=ensure_secondary_admission)
    sys_prompt = params.system_prompt or REVIEW_SYSTEM_PROMPT
    usr_template = params.user_prompt_template or REVIEW_USER_PROMPT
    literal = params.literal_source_preserving or get_literal_policy(batch_entries) is not None
    if literal:
        from literal_prompts import REVIEW_SYSTEM, REVIEW_USER
        sys_prompt, usr_template = REVIEW_SYSTEM, REVIEW_USER
    if not params.review_labels_only:
        sys_prompt += '\n\n' + REVIEW_CLASSIC_RESPONSE_RULE

    # Build context
    context_parts = []
    context_parts.append(f"Batch {batch_num} of {total_batches}.")
    if not params.review_labels_only:
        context_parts.append("\n" + REVIEW_ATTRIBUTION_RULE)

    if previous_tail:
        context_parts.append("\nPrevious batch ended with:")
        for entry in previous_tail:
            context_parts.append(json.dumps(get_review_prompt_entry(entry), ensure_ascii=False))

    # Optional extra context (e.g. source snippet or surrounding entries)
    if source_context:
        context_parts.append(f"\nADDITIONAL REVIEW CONTEXT:\n{source_context}")

    context = "\n".join(context_parts)
    batch_json = json.dumps([get_review_prompt_entry(e) for e in batch_entries], indent=2, ensure_ascii=False)
    if params.review_labels_only:
        sys_prompt = REVIEW_LABEL_SYSTEM
        indexed = [{**get_review_prompt_entry(entry), "n": i}
                   for i, entry in enumerate(batch_entries)]
        user_prompt = context + "\n\nENTRIES TO REVIEW:\n" + json.dumps(indexed, ensure_ascii=False)
        from three_pass_generate import get_exact_index_schema
        request_params = replace(request_params, response_schema=get_exact_index_schema(
            REVIEW_LABEL_SCHEMA, range(len(batch_entries))))
    else:
        user_prompt = usr_template.format(context=context, batch=batch_json)

    candidate = None
    attempts = []

    def validate_review_entries(entries):
        nonlocal candidate
        if params.review_labels_only:
            return get_review_label_binding(batch_entries, entries)[1]
        if retain_partial_labels and attempt_observer is not None and attempts:
            candidate = (copy.deepcopy(entries), attempts[-1])
        quality = get_review_source_binding(batch_entries, entries, literal, source_text)[1]
        if any(finding['code'] == 'review_source_span_unverified' for finding in quality['findings']):
            record_failure('review_source_span_rejected', task='review', batch=batch_num,
                           entries=len(batch_entries))
        return quality

    def observe(attempt):
        attempts.append(attempt)
        if attempt_observer:
            attempt_observer(attempt)

    startup_event('model_request_started', batch=batch_num)
    entries = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, request_params,
        log_name="review_responses.log",
        label=f"BATCH {batch_num}/{total_batches}",
        max_retries=max_retries,
        attempt_observer=observe, validate_entries=validate_review_entries,
    )
    if entries:
        if params.review_labels_only:
            ordered, _ = get_review_label_binding(batch_entries, entries)
            bound, warnings = get_review_labelled_entries(batch_entries, ordered)
            quality = {'warnings': warnings}
        else:
            bound, quality = get_review_source_binding(batch_entries, entries, literal, source_text)
        for warning in quality.get('warnings', []):
            print(f"Warning: {warning['code']} (entry {warning['entry_number']}): {warning['message']}")
        if quality.get('source_bound_label_recovery'):
            print("  Recovered source-bound review labels after punctuation drift; original text retained.")
        return bound
    # Completion-aware consumers opt in; ordinary list/None callers retain
    # their original semantics. Earlier candidates are never combined.
    if (retain_partial_labels and attempt_observer is not None and candidate is not None
            and not params.review_labels_only and not getattr(client, 'switched', False)
            and type(max_retries) is int and max_retries >= 0
            and len(attempts) == max_retries + 1 and candidate[1] is attempts[-1]
            and all(attempt.get('attempt') == i + 1
                    and attempt.get('finish_reason') == 'stop'
                    and attempt.get('outcome') == 'quality_rejected'
                    and attempt.get('error') is None
                    and 'request_log_error' not in attempt and 'response_log_error' not in attempt
                    and attempt.get('parse_provenance') == {
                        'method': 'strict_json', 'filtered_entries': 0, 'codec': 'json',
                        'extraction_unchanged': True, 'raw_fallback_used': False, 'salvage_used': False}
                    for i, attempt in enumerate(attempts))):
        retained, status = get_review_partial_label_retention(batch_entries, candidate[0], literal)
        if retained is not None:
            for warning in status['warnings']:
                print(f"Warning: {warning['code']} (entry {warning['entry_number']}): {warning['message']}")
            attempt_observer({**status, 'source_attempt': attempts[-1]['attempt']})
            return retained
    # Keep the caller's checkpoint batch intact: adapt only the requests,
    # then validate the complete recombination against that original batch.
    if (not getattr(client, "switched", False)
            or not any(attempt.get("error_category") == "context_budget"
                       or attempt.get("finish_reason") == "length" for attempt in attempts)):
        return None
    if len(batch_entries) > 1:
        middle = len(batch_entries) // 2
        parts = [batch_entries[:middle], batch_entries[middle:]]
    elif batch_entries and not params.review_labels_only:
        from generate_script import split_failed_chunk
        texts = split_failed_chunk(batch_entries[0].get("text", ""))
        if not texts:
            return None
        parts = [[{**batch_entries[0], "text": text}] for text in texts]
    else:
        return None
    combined = []
    failed = False
    for part in parts:
        reviewed = review_batch(
            client, model_name, part, batch_num, total_batches, params,
            previous_tail=(list(previous_tail or []) + combined)[-2:],
            source_context=source_context, max_retries=max_retries,
            attempt_observer=attempt_observer, source_text=source_text)
        if reviewed is None:
            failed = True
        else:
            combined.extend(reviewed)
    if failed:
        return None
    if params.review_labels_only:
        bound, warnings = get_review_labelled_entries(batch_entries, combined)
        for warning in warnings:
            print(f"Warning: {warning['code']} (entry {warning['entry_number']}): {warning['message']}")
        return bound
    bound, quality = get_review_source_binding(batch_entries, combined, literal, source_text)
    if quality['passed']:
        for warning in quality.get('warnings', []):
            print(f"Warning: {warning['code']} (entry {warning['entry_number']}): {warning['message']}")
        return bound
    return None


def normalize_text(text):
    """Normalize text for comparison: lowercase, collapse whitespace, strip punctuation."""
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def check_text_loss(original_entries, corrected_entries, threshold=0.95, upper_bound=None):
    """Check if corrected entries lost or gained significant text.

    Returns (passed, original_text, corrected_text, ratio).
    passed is True if the corrected word count ratio falls within
    [threshold, upper_bound]. If upper_bound is None, it defaults to
    1.0 + (1.0 - threshold), i.e. symmetric around 1.0.
    """
    orig_words = []
    for e in original_entries:
        # Use regex split to handle all Unicode whitespace properly
        text = normalize_text(e.get("text", ""))
        words = re.split(r'\s+', text.strip())
        orig_words.extend([w for w in words if w])  # Filter empty strings

    corr_words = []
    for e in corrected_entries:
        text = normalize_text(e.get("text", ""))
        words = re.split(r'\s+', text.strip())
        corr_words.extend([w for w in words if w])

    if not orig_words:
        return True, "", "", 1.0

    orig_joined = " ".join(orig_words)
    corr_joined = " ".join(corr_words)

    ratio = len(corr_words) / len(orig_words) if orig_words else 1.0

    if upper_bound is None:
        upper_bound = 1.0 + (1.0 - threshold)
    passed = threshold <= ratio <= upper_bound
    return passed, orig_joined, corr_joined, ratio


_MAX_HIGHLIGHT_POOL = 500
_HIGHLIGHT_SNIPPET_LEN = 300
_HIGHLIGHT_CONTEXT_LEN = 160


def get_failed_section(batch, zero_based_start, length, category, word_ratio=None):
    """Return structured metadata for one unresolved source-preserving batch."""
    section = {
        "batch": batch,
        "entry_start": zero_based_start + 1,
        "entry_end": zero_based_start + length,
        "category": category,
    }
    if word_ratio is not None:
        section["word_ratio"] = round(word_ratio, 4)
    return section


def diff_entries(original, corrected, highlight_pool=None, entry_offset=0):
    """Compare original and corrected entries, return a summary dict.

    If highlight_pool is given (a dict with "text" and "speaker" lists), append
    notable per-entry before/after snippets to it for the "diff preview" report,
    capped at _MAX_HIGHLIGHT_POOL each so a huge book can't blow up memory.

    Entries are aligned by their text before paired fields are compared. This
    keeps a split or merge from making every later entry look changed merely
    because its list index moved.
    """
    stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "annotations_changed": 0,
        "entries_changed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "entries_original": len(original),
        "entries_corrected": len(corrected),
    }
    changed_entry_indices = set()

    def compare_pair(orig, corr, orig_index):
        if get_typed_metadata(get_review_annotations(orig)) != get_typed_metadata(get_review_annotations(corr)):
            changed_entry_indices.add(orig_index)
            stats["annotations_changed"] += 1
        orig_text = orig.get("text", "")
        corr_text = corr.get("text", "")
        orig_speaker = orig.get("speaker") or ""
        corr_speaker = corr.get("speaker") or ""

        if orig_text != corr_text:
            changed_entry_indices.add(orig_index)
            stats["text_changed"] += 1
            if highlight_pool is not None and len(highlight_pool["text"]) < _MAX_HIGHLIGHT_POOL:
                ratio = difflib.SequenceMatcher(None, orig_text, corr_text).ratio()
                highlight_pool["text"].append({
                    "speaker": corr_speaker or orig_speaker or "Narrator",
                    "before": orig_text[:_HIGHLIGHT_SNIPPET_LEN],
                    "after": corr_text[:_HIGHLIGHT_SNIPPET_LEN],
                    "magnitude": round(1.0 - ratio, 4),
                })
        if orig_speaker != corr_speaker:
            changed_entry_indices.add(orig_index)
            stats["speaker_changed"] += 1
            if highlight_pool is not None and len(highlight_pool["speaker"]) < _MAX_HIGHLIGHT_POOL:
                speaker_change = {
                    "text": (corr_text or orig_text)[:_HIGHLIGHT_SNIPPET_LEN],
                    "before": orig_speaker or "(unknown)",
                    "after": corr_speaker or "(unknown)",
                    "entry_number": entry_offset + orig_index + 1,
                    "context_before": (
                        original[orig_index - 1].get("text", "")[:_HIGHLIGHT_CONTEXT_LEN]
                        if orig_index > 0 else ""
                    ),
                    "context_after": (
                        original[orig_index + 1].get("text", "")[:_HIGHLIGHT_CONTEXT_LEN]
                        if orig_index + 1 < len(original) else ""
                    ),
                }
                if (orig_speaker.casefold() == "narrator" and
                        corr_speaker.casefold() != "narrator"):
                    speaker_change["manual_review_reason"] = (
                        "Narrator-to-character changes alter which voice reads the line."
                    )
                highlight_pool["speaker"].append(speaker_change)
        if orig.get("instruct") != corr.get("instruct"):
            changed_entry_indices.add(orig_index)
            stats["instruct_changed"] += 1

    original_texts = [entry.get("text", "") for entry in original]
    corrected_texts = [entry.get("text", "") for entry in corrected]
    matcher = difflib.SequenceMatcher(None, original_texts, corrected_texts, autojunk=False)
    for tag, orig_start, orig_end, corr_start, corr_end in matcher.get_opcodes():
        if tag == "equal":
            for offset, (orig, corr) in enumerate(zip(
                    original[orig_start:orig_end], corrected[corr_start:corr_end])):
                compare_pair(orig, corr, orig_start + offset)
            continue
        if tag == "delete":
            stats["entries_removed"] += orig_end - orig_start
            continue
        if tag == "insert":
            stats["entries_added"] += corr_end - corr_start
            continue

        original_block = original[orig_start:orig_end]
        corrected_block = corrected[corr_start:corr_end]
        paired = min(len(original_block), len(corrected_block))
        for offset, (orig, corr) in enumerate(zip(
                original_block[:paired], corrected_block[:paired])):
            compare_pair(orig, corr, orig_start + offset)
        stats["entries_removed"] += len(original_block) - paired
        stats["entries_added"] += len(corrected_block) - paired

    stats["entries_changed"] = len(changed_entry_indices)
    return stats


def ensure_review_not_cancelled():
    """Stop between review transactions, retaining the last durable checkpoint."""
    path = os.environ.get('ALEXANDRIA_REVIEW_CANCEL_FILE')
    if path:
        try:
            os.stat(path)
        except FileNotFoundError:
            return
        print('Review cancelled; saved checkpoint retained.', flush=True)
        raise SystemExit(0)


def _emit_json_marker(marker, payload):
    """Print one machine-read '<MARKER>: {json}' line for the parent to parse.

    ensure_ascii stays at its default so the line is pure ASCII: core reads it
    back with json.loads, which restores every character exactly, while an
    ASCII line cannot fail to print on a stdout whose encoding is not UTF-8 —
    a Windows child's pipe defaults to the ANSI code page (cp1252), where
    U+2770 or any CJK text raised UnicodeEncodeError after a finished review
    (#2062).
    """
    print(f"{marker}: {json.dumps(payload)}")


def main():
    ensure_review_not_cancelled()
    parser = argparse.ArgumentParser(description="Review and fix annotated audiobook script")
    parser.add_argument("--context-window", type=int, default=0,
                        help="If > 0, review each entry with +/- N neighboring entries for better segmentation and speaker fixes")
    parser.add_argument("--context-chars", type=int, default=0,
                        help="If > 0, use whole neighbouring entries up to this many characters of entry text on each side (never splits an entry); overrides --context-window")
    parser.add_argument("--labels-only", action="store_true",
                        help="Experimental index-keyed review; preserve source text and boundaries")
    parser.add_argument("--force-review", action="store_true", help="Rerun an unchanged completed review")
    parser.add_argument("--input", help="Path to the script JSON to review (default: ../annotated_script.json)")
    parser.add_argument("--output", help="Where to write the reviewed script (default: same as --input)")
    parser.add_argument("--dedupe-speakers", action="store_true",
                        help="Merge speaker labels that refer to the same character (alias resolution)")
    parser.add_argument("--alias-registry",
                        help="Path to a shared canonical-name JSON file so merges stay consistent across books")
    parser.add_argument("--remap-voice-config",
                        help="Path to a voice_config.json whose keys should follow renamed speakers")
    args = parser.parse_args()
    context_mode, context_value = get_review_context_from_args(args)
    startup_event('arguments_parsed', context_window=args.context_window, context_mode=context_mode,
                  context_chars=context_value, dedupe=args.dedupe_speakers, force=args.force_review)

    # Locate the script to review (default: working annotated_script.json)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    default_script = os.path.join(data_dir, "annotated_script.json")
    script_path = args.input or default_script
    output_path = args.output or script_path
    saved_dir = os.path.join(data_dir, 'scripts')
    canonical_output = os.path.normcase(os.path.realpath(output_path))
    canonical_active = os.path.normcase(os.path.realpath(default_script))
    canonical_saved = os.path.normcase(os.path.realpath(saved_dir))
    canonical_data = os.path.normcase(os.path.realpath(data_dir))
    canonical_parent = os.path.normcase(os.path.dirname(os.path.realpath(output_path)))
    if (canonical_output == canonical_active and os.path.abspath(output_path) != os.path.abspath(default_script)
            or canonical_parent == canonical_saved
            and os.path.abspath(os.path.dirname(output_path)) != os.path.abspath(saved_dir)):
        raise ValueError('Review book destinations must use their direct path, not an alias')
    active_review = (os.path.abspath(script_path) == os.path.abspath(default_script)
                     and os.path.abspath(output_path) == os.path.abspath(default_script))
    saved_review = (os.path.abspath(os.path.dirname(output_path)) == os.path.abspath(saved_dir)
                    and os.path.abspath(script_path) == os.path.abspath(output_path))
    from three_pass_generate import three_pass_checkpoint_path
    from script_recovery_facts import three_pass_manifest_path
    reserved_outputs = {
        os.path.normcase(os.path.realpath(os.path.join(data_dir, name)))
        for name in ('state.json', 'voice_config.json', 'chunks.json',
                     'chunks.json.status.jsonl', 'annotated_script.selection_binding.json',
                     '.active_book_transaction.json')
    }
    reserved_outputs.add(canonical_active)
    reserved_outputs.add(os.path.normcase(os.path.realpath(
        get_review_receipt_path(default_script))))
    reserved_outputs.update(os.path.normcase(os.path.realpath(path)) for path in (
        three_pass_checkpoint_path(default_script), three_pass_manifest_path(default_script)))
    # Lock paths are authority too: replacing an inode while another process
    # holds the old lock defeats serialization even though the protected JSON
    # itself remains untouched.
    reserved_outputs.update(os.path.normcase(os.path.realpath(path + '.lock'))
                            for path in tuple(reserved_outputs))
    try:
        relative_output = os.path.relpath(canonical_output, canonical_data)
    except ValueError:
        relative_output = None
    if (not active_review and (canonical_output in reserved_outputs
            or relative_output is not None and relative_output.split(os.sep, 1)[0].casefold().startswith(
                '.book-switch-'))):
        raise ValueError('Review output cannot replace reserved active-book metadata')
    sidecar_paths = [path for path in (args.alias_registry, args.remap_voice_config) if path]
    sidecar_destinations = [os.path.normcase(os.path.realpath(os.path.abspath(path)))
                            for path in sidecar_paths]
    allowed_reserved_sidecars = set()
    if active_review and args.remap_voice_config:
        allowed_reserved_sidecars.add(os.path.normcase(os.path.realpath(
            os.path.join(data_dir, 'voice_config.json'))))
    if (len(set(sidecar_destinations)) != len(sidecar_destinations)
            or any(path in {canonical_output, os.path.normcase(os.path.realpath(script_path))}
                   for path in sidecar_destinations)):
        raise ValueError('Review companion paths must be distinct from the script and each other')
    for path in sidecar_destinations:
        try:
            relative = os.path.relpath(path, canonical_data)
        except ValueError:
            relative = None
        if ((path in reserved_outputs and path not in allowed_reserved_sidecars) or relative is not None
                and relative.split(os.sep, 1)[0].casefold().startswith('.book-switch-')):
            raise ValueError('Review companion cannot replace reserved active-book metadata')
    if (os.path.abspath(output_path) == os.path.abspath(default_script) and not active_review
            or os.path.abspath(os.path.dirname(output_path)) == os.path.abspath(saved_dir) and not saved_review):
        raise ValueError('Reviewing into an existing book requires that book as the input')
    if (active_review or saved_review) and args.dedupe_speakers:
        if args.alias_registry:
            from script_selection_binding import _staged_registry_path
            _staged_registry_path(data_dir, {'path':os.path.abspath(args.alias_registry),
                                             'before':None, 'after':None})
        if args.remap_voice_config:
            expected_voice = (os.path.join(data_dir, 'voice_config.json') if active_review else
                              os.path.splitext(output_path)[0] + '.voice_config.json')
            if os.path.abspath(args.remap_voice_config) != os.path.abspath(expected_voice):
                raise ValueError('Review voice config must belong to the selected book')
    if saved_review:
        from book_state_transaction import ensure_book_state
        from script_selection_binding import saved_script_is_active_source
        with ensure_book_state(data_dir):
            if saved_script_is_active_source(data_dir, output_path):
                raise ValueError('This saved book is currently loaded; switch books before reviewing it')
    if not os.path.exists(script_path):
        print(f"Error: script not found: {script_path}. Generate a script first.")
        sys.exit(1)

    original_script_bytes = _read_review_script_bytes(script_path)
    entries = json.loads(original_script_bytes.decode('utf-8'))
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        print("Error: script JSON must contain a list of entry objects.")
        sys.exit(1)
    if not entries:
        print("Error: script JSON must contain at least one entry.")
        sys.exit(1)
    for index, entry in enumerate(entries, 1):
        if (not isinstance(entry.get("text"), str) or
                any(key in entry and not isinstance(entry[key], str)
                    for key in ("speaker", "type", "instruct"))):
            print(f"Error: script entry {index} must have string text and string label/instruct fields.")
            sys.exit(1)

    print(f"Loaded {len(entries)} script entries for review")
    literal_policy = ensure_literal_narration(entries)

    # Load config
    config_path = get_app_config_path(data_dir, root, app_dir)
    if not os.path.exists(config_path):
        print("Warning: config.json not found. Using defaults.")
    startup_event('config_load_started')
    config = load_app_config(config_path)
    startup_event('config_loaded', entries=len(entries))

    llm_config = get_active_llm_config(config)
    base_url = llm_config.get("base_url", "http://localhost:11434/v1")
    api_key = llm_config.get("api_key", "local")
    model_name = llm_config.get("model_name", "local-model")

    # Load custom review prompts or use defaults from review_prompts.txt
    prompts_config = config.get("prompts") or {}
    review_sys = prompts_config.get("review_system_prompt") or REVIEW_SYSTEM_PROMPT
    review_usr = prompts_config.get("review_user_prompt") or REVIEW_USER_PROMPT

    if literal_policy is not None:
        from literal_prompts import REVIEW_SYSTEM, REVIEW_USER
        review_sys, review_usr = REVIEW_SYSTEM, REVIEW_USER

    generation_config = config.get("generation") or {}
    batch_size, target_chars = get_review_batching(generation_config)
    max_tokens = generation_config.get("max_tokens", 8000)
    temperature = generation_config.get("temperature", 0.4)
    top_p = generation_config.get("top_p", 0.8)
    top_k = generation_config.get("top_k", 20)
    min_p = generation_config.get("min_p", 0)
    presence_penalty = generation_config.get("presence_penalty", 0.0)
    banned_tokens = generation_config.get("banned_tokens", [])

    # Constant across every batch in both review modes - build once.
    gen_params = LLMGenParams(
        review_labels_only=args.labels_only or get_review_labels_only(config),
        literal_source_preserving=literal_policy is not None,
        system_prompt=review_sys,
        user_prompt_template=review_usr,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        min_p=min_p,
        presence_penalty=presence_penalty,
        banned_tokens=banned_tokens,
        provider_extra_body=llm_config.get("provider_extra_body"),
        structured_output=llm_config.get("structured_output", "auto"),
        api_retry_limit=llm_config.get("api_retry_limit"),
        retry_initial_delay_seconds=llm_config.get("retry_initial_delay_seconds", 1),
        retry_multiplier=llm_config.get("retry_multiplier", 2),
        retry_max_delay_seconds=llm_config.get("retry_max_delay_seconds", 30),
        retry_jitter=llm_config.get("retry_jitter", 0.2),
        on_api_exhaustion=llm_config.get("on_api_exhaustion", "fail"),
    )

    print(f"Connecting to: {base_url}")
    print(f"Using model: {model_name}")
    print(f"Batch size: {batch_size} entries, Max tokens: {max_tokens}")
    if target_chars:
        print(f"Review text per request: {target_chars} characters of entry text")
    if banned_tokens:
        print(f"Banned tokens: {banned_tokens}")

    # Runtime management follows the endpoint; the VRAM watchdog follows the
    # profile's declared ownership through the same policy as task admission.
    llm_mode = config.get("llm_mode", "local")
    startup_event('model_setup_started')
    is_remote, lm_status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"), api_key=api_key)
    print(heal_msg)
    from llm_request_budget import get_request_limits
    limits = get_request_limits(llm_config, lm_status.get("context_length"))
    gen_params.context_length = limits["context_length"]
    gen_params.max_input_tokens = limits["max_input_tokens"]
    gen_params.task_max_tokens = gen_params.max_tokens
    gen_params.task_hard_max_tokens = 32768
    gen_params.hard_max_tokens = limits["max_output_tokens"] or 32768
    gen_params.max_tokens = min(gen_params.max_tokens, gen_params.hard_max_tokens)

    startup_event('client_create_started')
    client = make_run_client(config, llm_config, llm_timeout_seconds())
    startup_event('client_created')

    def is_active_llm_on_this_gpu():
        active_config = config
        if get_run_model_binding(client, model_name)["failover_used"]:
            active_config = {**config, "llm_mode": "remote" if llm_mode == "local" else "local"}
        return is_llm_on_this_gpu(active_config)

    source_sha256 = _entries_fingerprint(entries)
    review_source_snapshot = (None if gen_params.review_labels_only else
        get_review_source_snapshot(data_dir, script_path, original_script_bytes, entries))
    review_source_text = review_source_snapshot['text'] if review_source_snapshot is not None else None
    review_source_identity = review_source_snapshot['identity'] if review_source_snapshot is not None else None
    receipt_fingerprint = get_completed_review_fingerprint(config, llm_config, gen_params, args, client,
                                                         model_name, review_source_text, entries=entries)
    matched, receipt_reason = get_completed_review_match(script_path, output_path, source_sha256, receipt_fingerprint)
    if matched and not args.force_review:
        print(receipt_reason)
        print(f"Review complete: {len(entries)} -> {len(entries)} entries")
        for label in ('Text changed', 'Speaker changed', 'Instruct changed', 'Annotations changed', 'Entries changed',
                      'Entries added', 'Entries removed', 'Narrators merged', 'Speakers merged', 'Total changes'):
            print(f"  {label}: 0")
        print("Task review completed successfully.")
        close = getattr(client, 'close', None)
        if close:
            close()
        return
    print('Completed-review reuse: ' + ('forced rerun' if args.force_review else receipt_reason))
    if review_source_text is not None:
        entries = get_review_source_entries(entries, review_source_text)
    all_corrected = []
    total_stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "annotations_changed": 0,
        "entries_changed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "batches_failed": 0,
        "batches_skipped_vram": 0,
    }
    review_windows = get_review_entry_windows(entries, generation_config)
    total_batches_estimate = len(review_windows)
    try:
        (all_corrected, total_stats, previous_tail, completed_batches, batch_lengths,
         failed_batches, resume_offset, checkpoint) = _load_resume_state(
            output_path, total_batches_estimate, batch_size, args.context_window,
            entries, all_corrected, total_stats, execution_sha256=receipt_fingerprint)
        source_offsets = get_review_resume_source_offsets(checkpoint, entries, batch_size)
        accounting = get_review_runtime_accounting(checkpoint, source_sha256, len(entries),
            total_stats, source_offsets, batch_lengths, len(all_corrected))
        remaining_windows = get_review_remaining_windows(review_windows, entries, resume_offset)
    except ValueError:
        close = getattr(client, 'close', None)
        if close:
            try:
                close()
            except Exception as close_error:
                print(f"WARNING: client close failed: {type(close_error).__name__}")
        raise
    checkpoint_writer = GenerationCheckpointDeltas(
        _checkpoint_path(output_path), reset=checkpoint is None)
    remove_completed_review_receipt(output_path)

    startup_event('concurrency_setup_started')
    wave_size = get_cached_or_benchmarked_concurrency(
        config_path, llm_mode, base_url, model_name, client,
        ssh_alias=config.get("llm_remote_ssh"), status=lm_status, llm_config=llm_config)
    startup_event('concurrency_setup_complete')
    if wave_size > 1:
        print(f"Using concurrency: {wave_size}")

    # Re-verify settings are still optimized right before review starts, to
    # catch drift between the initial heal and now (e.g. a slow concurrency
    # benchmark, or - for remote - TTL/idle expiry, since
    # apply_remote_lmstudio_settings intentionally doesn't pin a TTL).
    pre_review_status = get_current_status(llm_mode, base_url, model_name,
                                            ssh_alias=config.get("llm_remote_ssh"), api_key=api_key)
    if pre_review_status["loaded"] and not pre_review_status["optimized"]:
        label = "Remote LM Studio" if is_remote else "LM Studio"
        print(f"WARNING: {label} model '{model_name}' is loaded but NOT optimized.")
        if not is_remote:
            print("This may cause OOM crashes during batch review. Consider restarting LM Studio")
            print("with VRAM-safe settings or reducing batch size.")

    highlight_pool = {"text": [], "speaker": []}
    failed_sections = []
    vram_aborted = False

    contextual = (context_mode == CONTEXT_MODE_CHARS or
                  (args.context_window and args.context_window > 0))
    if contextual:
        if context_mode == CONTEXT_MODE_CHARS:
            print(f"Contextual review mode enabled: batching ~{batch_size} entries per LLM call with up to "
                  f"{context_value} characters of neighbouring entry text on each side")
        else:
            print(f"Contextual review mode enabled: batching ~{batch_size} entries per LLM call with +/-{args.context_window} neighbors")

        num_remaining_batches = len(remaining_windows)
        total_batches = max(1, completed_batches + num_remaining_batches)
        if checkpoint:
            print(f"Resuming from checkpoint: {completed_batches}/{total_batches} batches already reviewed.")

        unreviewed_remainder = []
        for offset_idx, (start, end) in enumerate(remaining_windows):
            ensure_review_not_cancelled()
            batch_index = completed_batches + 1 + offset_idx

            batch = entries[start:end]
            before, after = get_review_context_neighbors(
                entries, start, end, context_mode, context_value)

            # Only this run's first check fails fast (#1114); later ones wait.
            if is_active_llm_on_this_gpu() and not wait_for_vram_headroom(fail_fast=offset_idx == 0):
                unreviewed_remainder = entries[start:]
                total_stats["batches_skipped_vram"] = total_batches - batch_index + 1
                vram_aborted = True
                break

            print(f"\nReviewing batch {batch_index}/{total_batches} ({len(batch)} entries)...")

            contextual_lines = [
                "Contextual batch review mode.",
                "The 'SCRIPT ENTRIES TO REVIEW' below is your TARGET BATCH.",
                "Use the following PREVIOUS and NEXT entries for context, but DO NOT include them in your output. Only return the corrected TARGET BATCH.",
            ]
            if before:
                contextual_lines.append("\n--- PREVIOUS ENTRIES (Context Only) ---")
                contextual_lines.extend(json.dumps(get_review_prompt_entry(e), ensure_ascii=False) for e in before)
            if after:
                contextual_lines.append("\n--- NEXT ENTRIES (Context Only) ---")
                contextual_lines.extend(json.dumps(get_review_prompt_entry(e), ensure_ascii=False) for e in after)

            batch_attempts = []
            corrected = review_batch(
                client, model_name, batch, batch_index, total_batches, gen_params,
                previous_tail=None,  # contextual mode uses explicit before/after window instead
                source_context="\n".join(contextual_lines),
                source_text=review_source_text,
                attempt_observer=batch_attempts.append, retain_partial_labels=True,
            )
            source_offsets.append(end)

            if corrected is None:
                print(f"  FAILED — keeping original entries for batch {batch_index} (will retry on next resume)")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(batch_index)
                failed_sections.append(get_failed_section(
                    batch_index, start, len(batch), "review_failed"))
                accounting = save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                continue

            passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected, threshold=0.95, upper_bound=1.05)
            if not passed:
                print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.05)")
                print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                print(f"  Keeping original entries for batch {batch_index} to prevent data corruption (will retry on next resume).")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(batch_index)
                failed_sections.append(get_failed_section(
                    batch_index, start, len(batch), "text_length_mismatch", ratio))
                accounting = save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                continue

            if get_review_batch_status(corrected, batch_attempts)['status'] == 'partial':
                print(f"  PARTIAL — source-bound labels retained for batch {batch_index}; unresolved entries will retry.")
                all_corrected.extend(corrected)
                total_stats["batches_failed"] += 1
                previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
                batch_lengths.append(len(corrected))
                failed_batches.append(batch_index)
                failed_sections.append(get_failed_section(
                    batch_index, start, len(batch), REVIEW_PARTIAL_EVENT))
                accounting = save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                continue

            stats = diff_entries(batch, corrected, highlight_pool, entry_offset=start)

            total_stats["entries_added"] += stats["entries_added"]
            total_stats["entries_removed"] += stats["entries_removed"]

            total_stats["text_changed"] += stats["text_changed"]
            total_stats["speaker_changed"] += stats["speaker_changed"]
            total_stats["instruct_changed"] += stats["instruct_changed"]
            total_stats["entries_changed"] += stats["entries_changed"]
            total_stats["annotations_changed"] += stats["annotations_changed"]

            changes = stats["text_changed"] + stats["speaker_changed"] + stats["instruct_changed"] + stats["annotations_changed"]
            if changes > 0 or stats["entries_added"] or stats["entries_removed"]:
                print(f"  Changes: {stats['text_changed']} text, {stats['speaker_changed']} speaker, {stats['instruct_changed']} instruct, {stats['annotations_changed']} annotations", end="")
                if stats["entries_added"] or stats["entries_removed"]:
                    print(f", +{stats['entries_added']}/-{stats['entries_removed']} entries")
                else:
                    print()
            else:
                print("  No changes")

            all_corrected.extend(corrected)
            previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
            batch_lengths.append(len(corrected))
            accounting = save_checkpoint(output_path, batch_index, total_batches, batch_size,
                            args.context_window, all_corrected, total_stats, previous_tail,
                            batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
    else:
        print(f"Split into {total_batches_estimate} batches of ~{batch_size} entries")

        remaining_batches = [entries[start:end] for start, end in remaining_windows]
        total_batches = completed_batches + len(remaining_batches)
        if checkpoint:
            print(f"Resuming from checkpoint: {completed_batches}/{total_batches} batches already reviewed.")

        unreviewed_remainder = []
        # Guards the per-batch VRAM check below: wave_size>1 batches run
        # concurrently, so a single pre-wave check (the old approach) can't
        # see VRAM consumed by sibling batches still starting up within the
        # same wave. vram_lock serializes each batch's own live check (so it
        # reflects whatever siblings dispatched moments earlier already
        # claimed); vram_abort lets one batch's sustained-saturation timeout
        # tell not-yet-started siblings/waves to stop too.
        vram_abort = threading.Event()
        vram_lock = threading.Lock()
        # Set by the run's first admission check, the only one that fails
        # fast when VRAM is already full before any batch (#1114).
        first_check_done = threading.Event()
        for wave_start in range(0, len(remaining_batches), wave_size):
            ensure_review_not_cancelled()
            wave_batches = remaining_batches[wave_start:wave_start + wave_size]
            wave_windows = remaining_windows[wave_start:wave_start + wave_size]
            wave_indices = [completed_batches + 1 + wave_start + j for j in range(len(wave_batches))]
            # Every batch in this wave gets the SAME previous_tail (from the end of
            # the last wave) - they can't see each other's corrections yet, only
            # results from earlier, already-finished waves. Mirrors find_nicknames.py.
            wave_tail = previous_tail

            if len(wave_batches) > 1:
                print(f"\nReviewing batches {wave_indices[0]}-{wave_indices[-1]}/{total_batches} "
                      f"({sum(len(b) for b in wave_batches)} entries, {len(wave_batches)} concurrent)...")
            else:
                print(f"\nReviewing batch {wave_indices[0]}/{total_batches} ({len(wave_batches[0])} entries)...")

            def _run_one(item):
                ensure_review_not_cancelled()
                i, batch = item
                if is_active_llm_on_this_gpu():
                    with vram_lock:
                        if vram_abort.is_set():
                            return "VRAM_SKIP"
                        is_first_check = not first_check_done.is_set()
                        first_check_done.set()
                        if not wait_for_vram_headroom(fail_fast=is_first_check):
                            vram_abort.set()
                            return "VRAM_SKIP"
                batch_attempts = []
                corrected = review_batch(
                    client, model_name, batch, i, total_batches, gen_params,
                    previous_tail=wave_tail,
                    source_context=None,  # Mode 2: would pass source text chunk here
                    source_text=review_source_text,
                    attempt_observer=batch_attempts.append, retain_partial_labels=True,
                )
                return corrected, get_review_batch_status(corrected, batch_attempts)['status']

            with ThreadPoolExecutor(max_workers=len(wave_batches)) as executor:
                wave_results = list(executor.map(bind_startup_trace(_run_one), zip(wave_indices, wave_batches)))

            for i, (start, end), batch, result in zip(wave_indices, wave_windows, wave_batches, wave_results):
                source_offsets.append(end)
                if result == "VRAM_SKIP":
                    # Keep the skipped batch at its original position even if
                    # concurrent workers acquired the VRAM lock out of order.
                    all_corrected.extend(batch)
                    batch_lengths.append(len(batch))
                    failed_batches.append(i)
                    previous_tail = batch[-2:] if len(batch) >= 2 else batch
                    accounting = save_checkpoint(output_path, i, total_batches, batch_size,
                                    args.context_window, all_corrected, total_stats, previous_tail,
                                    batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                    continue

                corrected, review_status = result
                if corrected is None:
                    print(f"  FAILED — keeping original entries for batch {i} (will retry on next resume)")
                    all_corrected.extend(batch)
                    total_stats["batches_failed"] += 1
                    previous_tail = batch[-2:] if len(batch) >= 2 else batch
                    batch_lengths.append(len(batch))
                    failed_batches.append(i)
                    entry_start = start + 1
                    failed_sections.append(get_failed_section(
                        i, entry_start - 1, len(batch), "review_failed"))
                    accounting = save_checkpoint(output_path, i, total_batches, batch_size,
                                    args.context_window, all_corrected, total_stats, previous_tail,
                                    batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                    continue

                # Text-loss safety check (same bounds as contextual mode for consistency)
                passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected, threshold=0.95, upper_bound=1.05)
                if not passed:
                    print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.05)")
                    print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                    print(f"  Keeping original entries for batch {i} to prevent data corruption (will retry on next resume).")
                    all_corrected.extend(batch)
                    total_stats["batches_failed"] += 1
                    previous_tail = batch[-2:] if len(batch) >= 2 else batch
                    batch_lengths.append(len(batch))
                    failed_batches.append(i)
                    entry_start = start + 1
                    failed_sections.append(get_failed_section(
                        i, entry_start - 1, len(batch), "text_length_mismatch", ratio))
                    accounting = save_checkpoint(output_path, i, total_batches, batch_size,
                                    args.context_window, all_corrected, total_stats, previous_tail,
                                    batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                    continue

                if review_status == 'partial':
                    print(f"  PARTIAL — source-bound labels retained for batch {i}; unresolved entries will retry.")
                    all_corrected.extend(corrected)
                    total_stats["batches_failed"] += 1
                    previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
                    batch_lengths.append(len(corrected))
                    failed_batches.append(i)
                    entry_start = start
                    failed_sections.append(get_failed_section(
                        i, entry_start, len(batch), REVIEW_PARTIAL_EVENT))
                    accounting = save_checkpoint(output_path, i, total_batches, batch_size,
                                    args.context_window, all_corrected, total_stats, previous_tail,
                                    batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)
                    continue

                # Diff stats
                batch_start = start
                stats = diff_entries(batch, corrected, highlight_pool, entry_offset=batch_start)

                total_stats["entries_added"] += stats["entries_added"]
                total_stats["entries_removed"] += stats["entries_removed"]

                total_stats["text_changed"] += stats["text_changed"]
                total_stats["speaker_changed"] += stats["speaker_changed"]
                total_stats["instruct_changed"] += stats["instruct_changed"]
                total_stats["entries_changed"] += stats["entries_changed"]
                total_stats["annotations_changed"] += stats["annotations_changed"]

                changes = stats["text_changed"] + stats["speaker_changed"] + stats["instruct_changed"] + stats["annotations_changed"]
                if changes > 0 or stats["entries_added"] or stats["entries_removed"]:
                    print(f"  Changes: {stats['text_changed']} text, {stats['speaker_changed']} speaker, {stats['instruct_changed']} instruct, {stats['annotations_changed']} annotations", end="")
                    if stats["entries_added"] or stats["entries_removed"]:
                        print(f", +{stats['entries_added']}/-{stats['entries_removed']} entries")
                    else:
                        print()
                else:
                    print(f"  No changes")

                all_corrected.extend(corrected)
                previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
                batch_lengths.append(len(corrected))
                accounting = save_checkpoint(output_path, i, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches, source_sha256, writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)

            if vram_abort.is_set():
                # Some batches in this wave may have already succeeded before
                # the abort was detected (kept above, in all_corrected) -
                # only the genuinely-skipped ones and every later, untried
                # wave count as unreviewed.
                skipped_count = sum(1 for result in wave_results if result == "VRAM_SKIP")
                for remaining in remaining_batches[wave_start + len(wave_batches):]:
                    unreviewed_remainder.extend(remaining)
                    skipped_count += 1
                total_stats["batches_skipped_vram"] = skipped_count
                vram_aborted = True
                break

    if args.dedupe_speakers and not vram_aborted and is_active_llm_on_this_gpu():
        if not wait_for_vram_headroom():
            vram_aborted = True

    if literal_policy is not None:
        ensure_literal_narration(all_corrected + unreviewed_remainder, literal_policy)
    # Keep incomplete output in the exact layout described by its checkpoint.
    # Post-processing runs only after all batches have completed successfully.
    review_complete = not vram_aborted and not failed_batches
    merge_narrators_enabled = generation_config.get("merge_narrators", False)
    narrator_merges = 0
    speakers_merged = 0
    if merge_narrators_enabled and review_complete:
        pre_merge_count = len(all_corrected)
        all_corrected, narrator_merges = merge_consecutive_narrators(all_corrected, max_merged_length=800)
        if narrator_merges > 0:
            print(f"\nPost-processing: merged {narrator_merges} consecutive narrator entries "
                  f"({pre_merge_count} -> {len(all_corrected)} entries)")
    elif not merge_narrators_enabled:
        print("\nNarrator merging: disabled (enable in Setup > Advanced)")

    # Speaker de-duplication: merge aliases that are the same character.
    # Skipped on a VRAM abort since this step makes additional LLM calls,
    # which is exactly what we're trying to avoid when VRAM is already low.
    ensure_review_not_cancelled()
    dedupe_map = {}
    registry_staging = ({} if ((active_review or saved_review) and args.alias_registry
                               and args.dedupe_speakers) else None)
    if args.dedupe_speakers and not review_complete:
        print("\nSkipping speaker alias resolution until review is complete.")
    elif args.dedupe_speakers:
        print("\nResolving character aliases (merging duplicate names)...")
        dedupe_map, speakers_merged, dedupe_changes = dedupe_speakers(
            client, model_name, all_corrected, registry_path=args.alias_registry,
            context_length=lm_status.get("context_length"), params=gen_params,
            **({'registry_staging': registry_staging} if registry_staging is not None else {})
        )
        for idx, key, new_value in dedupe_changes:
            all_corrected[idx][key] = new_value
        if speakers_merged > 0:
            print(f"Merged {len(dedupe_map)} alias label(s), updating {speakers_merged} entries.")
            if not active_review and not saved_review:
                moved = _remap_voice_config(args.remap_voice_config, dedupe_map)
                if moved:
                    print(f"Remapped {moved} voice config entr(y/ies) to canonical names.")
        else:
            print("No duplicate character names found.")

    output_entries = all_corrected + unreviewed_remainder
    output_entries = retain_literal_policy(output_entries, literal_policy)

    if not review_complete:
        accounting = save_checkpoint(output_path, len(batch_lengths), total_batches, batch_size,
                        args.context_window, all_corrected, total_stats, previous_tail,
                        batch_lengths, failed_batches, source_sha256,
                        _entries_fingerprint(output_entries), writer=checkpoint_writer, source_offsets=source_offsets, literal_policy=literal_policy, execution_sha256=receipt_fingerprint, accounting=accounting)

    # Keep active script identity and old chunks in the same recoverable commit.
    if active_review:
        from script_selection_binding import publish_controlled_script_edit
        ensure_review_not_cancelled()
        moved = publish_controlled_script_edit(data_dir, original_script_bytes,
            json.dumps(output_entries, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'),
            voice_config_path=args.remap_voice_config, voice_mapping=dedupe_map,
            remap_voice_config=_remapped_voice_config, registry_staging=registry_staging,
            expected_source_identity=review_source_identity)
        if moved:
            print(f"Remapped {moved} voice config entr(y/ies) to canonical names.")
    elif saved_review:
        from script_selection_binding import publish_saved_script_edit
        ensure_review_not_cancelled()
        moved = publish_saved_script_edit(data_dir, saved_dir, output_path, original_script_bytes,
            json.dumps(output_entries, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'),
            voice_config_path=args.remap_voice_config, voice_mapping=dedupe_map,
            remap_voice_config=_remapped_voice_config, registry_staging=registry_staging,
            expected_source_identity=review_source_identity)
        if moved:
            print(f"Remapped {moved} voice config entr(y/ies) to canonical names.")
    else:
        ensure_review_not_cancelled()
        atomic_json_write(output_entries, output_path, allow_nonatomic_fallback=False)

    # Checkpoint is only needed while a run is incomplete; clear it once the
    # full review (and any post-processing) has finished successfully.
    if review_complete:
        save_completed_review_receipt(script_path, output_path, source_sha256,
            _entries_fingerprint(output_entries), get_completed_review_fingerprint(
                config, llm_config, gen_params, args, client, model_name, review_source_text, entries=entries))
        clear_checkpoint(output_path)

    # Delete chunks.json so editor regenerates — only when we reviewed the
    # working script (batch review of saved scripts must not touch it).
    if not active_review and os.path.abspath(output_path) == os.path.abspath(default_script):
        chunks_path = os.path.join(data_dir, "chunks.json")
        if remove_chunk_snapshot(chunks_path):
            print("Cleared old chunks.json")

    accounting_unverified = accounting['metadata']['inherited_unverified']
    _emit_json_marker('REVIEW_ACCOUNTING_JSON', {'inherited_unverified': accounting_unverified})
    warning = get_review_accounting_warning({'accounting_unverified': accounting_unverified})
    if warning:
        print('WARNING: ' + warning)

    # Final summary
    total_changes = (total_stats["text_changed"] + total_stats["speaker_changed"] +
                     total_stats["instruct_changed"] + total_stats["annotations_changed"] + total_stats["entries_added"] +
                     total_stats["entries_removed"] + narrator_merges + speakers_merged)

    print(f"\n{'='*60}")
    print(f"Review {'complete' if review_complete else 'incomplete'}: {len(entries)} -> {len(output_entries)} entries")
    partial_batches = sum(section['category'] == REVIEW_PARTIAL_EVENT for section in failed_sections)
    if partial_batches:
        print(f"  Partial label retention: {partial_batches} batch(es); unresolved review remains pending.")
        print("  Completed-batch changes below exclude those partial label edits.")
    print(f"  Text changed:    {total_stats['text_changed']}")
    print(f"  Speaker changed: {total_stats['speaker_changed']}")
    print(f"  Instruct changed:{total_stats['instruct_changed']}")
    print(f"  Annotations changed: {total_stats['annotations_changed']}")
    print(f"  Entries changed: {total_stats['entries_changed']}")
    print(f"  Entries added:   {total_stats['entries_added']}")
    print(f"  Entries removed: {total_stats['entries_removed']}")
    print(f"  Narrators merged:{narrator_merges}")
    print(f"  Speakers merged: {speakers_merged}")
    if total_stats["batches_failed"] > 0:
        print(f"  Batches failed:  {total_stats['batches_failed']}")
    if total_stats["batches_skipped_vram"] > 0:
        print(f"  Batches skipped (low GPU VRAM): {total_stats['batches_skipped_vram']}")
    print(f"  Total changes:   {total_changes}")
    print(f"{'='*60}")

    if failed_sections:
        failure_details = {
            "sections": failed_sections,
            "original_entries_preserved": not any(
                section['category'] == REVIEW_PARTIAL_EVENT for section in failed_sections),
            "checkpoint_retained": os.path.exists(_checkpoint_path(output_path)),
            "retry_from_batch": min(section["batch"] for section in failed_sections),
        }
        _emit_json_marker('FAILED_SECTIONS_JSON', failure_details)

    # Emit a compact JSON line with the most notable before/after examples so the
    # web UI can show a "diff preview" without re-diffing the whole script.
    diff_highlights = {
        "text_rewrites": sorted(highlight_pool["text"], key=lambda h: h["magnitude"], reverse=True)[:5],
        "speaker_changes": highlight_pool["speaker"][:5],
    }
    if diff_highlights["text_rewrites"] or diff_highlights["speaker_changes"]:
        _emit_json_marker('DIFF_PREVIEW_JSON', diff_highlights)

    if vram_aborted:
        print("Stopped early: GPU VRAM stayed too high to safely continue.")
        print("The remaining entries were saved unreviewed. Free up VRAM "
              "(e.g. close other GPU apps) and re-run to review the rest.")
    elif not review_complete:
        print("Review remains incomplete; unresolved batches will retry on resume.")
    elif accounting_unverified:
        print(f"Review recorded {total_changes} changes; historical counts are unverified.")
    elif total_changes == 0:
        print("No issues found -- script looks clean.")
    else:
        print(f"Fixed {total_changes} issues across {total_batches} batches.")

    print(f"Output saved to: {output_path}")
    if vram_aborted:
        print("Task review completed with an early VRAM-abort - some entries were saved unreviewed.")
    elif total_stats["batches_failed"] > 0:
        print(f"Task review completed with {total_stats['batches_failed']} unresolved batch(es) - complete review remains pending.")
    else:
        print("Task review completed successfully.")


if __name__ == "__main__":
    startup_event('cli_imports_complete')
    try:
        main()
    except SystemExit as error:
        startup_event('cli_finished' if error.code in (0, None) else 'cli_failed', exit_code=error.code)
        raise
    startup_event('cli_finished')
