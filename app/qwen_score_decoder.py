# coding=utf-8
"""Maximum-score monotonic timestamp decoder for Qwen3-ForcedAligner (#1844).

Worker-only module: imported solely by ``qwen_align_worker.py`` inside the
isolated qwen interpreter. The main app never imports it (no torch/numpy
leak into the Torch-free app).

Algorithm provenance: the constrained score-path search is borrowed from
hangry-labs/Qwen3-ASR-STT (``qwen_asr/inference/qwen3_forced_aligner.py``,
commit 97ad227a7ec2fb1ae6f601dbe0206b308158252e, Apache License 2.0,
Copyright 2026 The Alibaba Qwen team). Only the decoder is borrowed —
not the fork server, not its Transformers-5 migration.

Deliberate, documented divergences from the donor:

* The donor's ``_repair_timestamps`` (LIS anchor repair with interpolated
  bins for anomalous blocks) is NOT ported, and its valid-LIS fast path
  is not used either: if the raw per-token argmax bins already form a
  strictly-positive, monotonic path they are kept verbatim (that path
  is jointly optimal: its score is the sum of the per-token maxima).
  Otherwise the maximum-score constrained path is computed from the
  real model scores. A non-monotonic argmax therefore falls through
  to the score-based search instead of interpolated times, so no time
  is ever invented, clamped or stretched here. The parent diagnostics
  used the donor's exact function; this implementation is a
  transparent variant of it, and the prototypes are not proof of this
  code — parent real-model qualification must run on this exact code.
* Impossible / non-finite / malformed inputs raise ``ValueError``
  instead of the donor's return-the-repaired-path fallback. The parent
  strict validators (``get_validated_aligner_tokens``) would reject an
  invalid returned path anyway; raising makes the failure explicit at
  the decoder boundary.

Resource bounds are checked from shape metadata BEFORE any
copy/list/dtype conversion (see :func:`_shape_of`): the caller may pass
a NumPy array, a torch-like tensor, or nested lists, and oversize
input is rejected without materializing it. Real pinned-model captures
are [timestamp_tokens, 5000] (``classify_num=5000``), so the bounds
cover genuine 180-second slices while capping DP memory (float64
scores plus int32 backpointers over at most MAX_SCORE_CELLS cells).

Path constraints (donor semantics): bins are a non-decreasing sequence
(a word may start where the previous word ends) and each word's end bin
is strictly after its start bin. All returned bins lie in
``[0, bin_count)``; conversion to seconds and bound checks against the
actual audio slice happen in the worker/parent, never by clipping here.
"""
import numpy as np

MAX_TIMESTAMP_TOKENS = 8192
MAX_TIMESTAMP_BINS = 8192
MAX_SCORE_CELLS = 16_000_000


def _is_valid_path(path):
    if any(path[i] >= path[i + 1] for i in range(0, len(path), 2)):
        return False
    return all(path[i] <= path[i + 1] for i in range(len(path) - 1))


def _shape_of(scores):
    """Return (token_count, bin_count) from metadata, without converting.

    Accepts NumPy arrays, torch-like tensors (``.shape`` attribute), and
    nested lists/tuples. Raises ValueError for anything else. No element
    data is materialized here, so an oversize sentinel is rejected
    before any large allocation.
    """
    if isinstance(scores, np.ndarray):
        if scores.ndim != 2:
            raise ValueError("timestamp scores must have shape [timestamp_tokens, timestamp_bins].")
        return int(scores.shape[0]), int(scores.shape[1])
    shape = getattr(scores, "shape", None)
    if isinstance(shape, tuple) and len(shape) == 2 \
            and all(type(v) is int for v in shape):
        return int(shape[0]), int(shape[1])
    if isinstance(scores, (list, tuple)):
        if not scores:
            raise ValueError("timestamp scores are empty.")
        bin_count = None
        for row in scores:
            if not isinstance(row, (list, tuple)):
                raise ValueError("timestamp scores must have shape [timestamp_tokens, timestamp_bins].")
            if bin_count is None:
                bin_count = len(row)
            elif len(row) != bin_count:
                raise ValueError("timestamp scores rows have inconsistent lengths.")
        return len(scores), int(bin_count or 0)
    raise ValueError("timestamp scores must have shape [timestamp_tokens, timestamp_bins].")


def decode_timestamp_bins(scores):
    """Decode ``scores`` ([timestamp_tokens, timestamp_bins]) to bins.

    Returns a list of ints with one bin per timestamp token (start/end
    pairs). Raises ValueError on odd counts, wrong shape, non-finite
    scores, oversize inputs, or when no strictly-positive monotonic
    path exists within the bin bounds. Bounds are enforced from shape
    metadata before any conversion to a NumPy array.
    """
    token_count, bin_count = _shape_of(scores)
    if token_count == 0:
        raise ValueError("timestamp scores are empty.")
    if token_count % 2:
        raise ValueError("timestamp scores must contain paired start and end tokens.")
    if bin_count < 2:
        raise ValueError("timestamp scores have too few bins for a positive word.")
    if token_count > MAX_TIMESTAMP_TOKENS or bin_count > MAX_TIMESTAMP_BINS \
            or token_count * bin_count > MAX_SCORE_CELLS:
        raise ValueError("timestamp scores exceed decoder bounds.")
    try:
        if isinstance(scores, np.ndarray):
            array = scores.astype(np.float64, copy=False) \
                if scores.dtype != np.float64 else scores
        else:
            array = np.asarray(scores, dtype=np.float64)
    except (ValueError, TypeError) as error:
        raise ValueError("timestamp scores are malformed.") from error
    if array.shape != (token_count, bin_count):
        raise ValueError("timestamp scores shape changed during conversion.")
    if not np.isfinite(array).all():
        raise ValueError("timestamp scores contain NaN or infinite values.")

    raw = [int(v) for v in array.argmax(axis=1)]
    if _is_valid_path(raw):
        return raw

    # Maximum-score monotonic path (donor DP): cummax over previous
    # scores; end tokens must strictly follow their paired start.
    previous = array[0].copy()
    backpointers = []
    for position in range(1, token_count):
        prefix_scores = np.maximum.accumulate(previous)
        # prefix_indices[b] = first argmax over j <= b of previous[j].
        # Vectorized first-tie form: the prefix argmax changes only
        # at strict new maxima, so segment the bins at those indices
        # and repeat each winning index over its segment. Ties keep
        # the earliest index (first-argmax), identical to the
        # `previous[b] > previous[best]` scan.
        _cum = np.maximum.accumulate(previous)
        _new_max = np.flatnonzero(
            np.concatenate(([True], previous[1:] > _cum[:-1])))
        _bounds = np.append(_new_max, bin_count)
        prefix_indices = np.repeat(
            _new_max.astype(np.int32), np.diff(_bounds))
        if position % 2:
            best_scores = np.concatenate(([-np.inf], prefix_scores[:-1]))
            best_indices = np.concatenate(([0], prefix_indices[:-1]))
        else:
            best_scores = prefix_scores
            best_indices = prefix_indices
        previous = array[position] + best_scores
        backpointers.append(best_indices)
    if not np.isfinite(previous).any():
        raise ValueError("no finite monotonic timestamp path exists.")
    cursor = int(np.argmax(previous))
    constrained = [cursor]
    for pointers in reversed(backpointers):
        cursor = int(pointers[cursor])
        constrained.append(cursor)
    constrained.reverse()
    if not _is_valid_path(constrained):
        raise ValueError("no strictly-positive monotonic timestamp path exists.")
    return constrained
