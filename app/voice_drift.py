"""Speaker-drift check for generated chunks.

Every `done` chunk is scored against its speaker's reference with the same
ECAPA cosine the Voice Lab identity gate uses (`experiments/_ecapa_batch.py`,
run under the sibling interpreter that has speechbrain, on CPU). A chunk whose
voice does not match its reference is *flagged* - never regenerated here, never
blocking; the Editor shows the flag and the existing per-chunk Gen button is
the fix.

Two things this module refuses to do, both learned the hard way elsewhere in
this repo: it never substitutes a weaker metric when ECAPA is unavailable
(the run is reported as NOT MEASURED, like the identity gate's rc=2), and it
never treats "no opinion" as "pass".
"""
import math
import hashlib
import json
import os
import tempfile
import time

from experiments.library_voice_fidelity import ecapa_pairs
from utils import is_path_inside
from tts import get_chunk_voice_inputs, get_resolved_speaker

# Same rationale as verify_adapter_identity.py: working adapters score
# 0.65-0.74 against their human reference, failures 0.027-0.404, and the
# human-vs-human ceiling is ~0.83. Deliberately generous - a flag means "this
# is not the same voice", not "this could be a little closer".
DRIFT_MIN_SIMILARITY = 0.45
CONFIG_KEY = "voice_drift_min_similarity"
DRIFT_BATCH_SIZE = 32


def get_drift_threshold(app_config=None):
    """The one place the flag threshold is decided: config.json's
    `voice_drift_min_similarity` when present and sane, else the default."""
    value = (app_config or {}).get(CONFIG_KEY)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DRIFT_MIN_SIMILARITY
    return value if 0.0 < value < 1.0 else DRIFT_MIN_SIMILARITY


def get_speaker_model_python(voicelab_config=None):
    """Interpreter that has speechbrain (see voice_reference.get_speaker_model_python,
    the single resolver); None means the caller reports NOT MEASURED."""
    from voice_reference import get_speaker_model_python as resolve
    return resolve(voicelab_config)


def get_reference_for_speaker(speaker, voice_config, chunks, resolve_alias,
                              resolve_asset_path, effective_inputs=None):
    """(absolute reference path or None, label) for a speaker.

    clone -> its ref_audio; lora -> the adapter's ref_sample.wav; anything
    without reference audio (design, custom, ensemble) -> the speaker's
    earliest `done` chunk, so drift means "not like the rest of this voice".
    The label says which, so an artifact never hides what it compared to."""
    canonical = (resolve_alias(speaker, voice_config) if resolve_alias else
                 get_resolved_speaker(speaker, voice_config) if effective_inputs is not None else speaker)
    if effective_inputs is not None:
        # Shared generation inputs place the selected root before its members.
        # A timeline can change alias_of, so raw configuration is not its identity.
        canonical = next(iter(effective_inputs), canonical)
    cfg = (effective_inputs if effective_inputs is not None else voice_config).get(canonical) or {}
    kind = cfg.get("type")
    if kind == "clone" and cfg.get("ref_audio"):
        return resolve_asset_path(cfg["ref_audio"]), f"clone:{canonical}"
    if kind in ("lora", "builtin_lora") and cfg.get("adapter_path"):
        adapter_path = resolve_asset_path(cfg["adapter_path"])
        path = os.path.join(adapter_path, "ref_sample.wav") if adapter_path else None
        return path, f"lora:{canonical}"
    for chunk in chunks:
        if ((effective_inputs is not None or chunk.get("speaker") == speaker)
                and chunk.get("status") == "done"
                and chunk.get("audio_path")):
            return None, f"chunk:{chunk.get('uid')}"
    return None, "none"


def get_drift_voice_groups(chunks, voice_config, resolve_alias=None):
    """Index compatible rendered states without retaining decoded audio."""
    groups, keys = {}, {}
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    for index, chunk in enumerate(chunks):
        if chunk.get("status") != "done" or not chunk.get("audio_path"):
            continue
        inputs = get_chunk_voice_inputs(voice_config, chunk, index, resolve_alias)
        digest = hashlib.sha256()
        for part in encoder.iterencode(inputs):
            digest.update(part.encode("utf-8"))
        key = digest.hexdigest()
        keys[index] = key
        groups.setdefault(key, []).append(chunk)
    return groups, keys


def _decode_to_wav(src, dest_dir, stem):
    """MP3/anything -> WAV the ECAPA worker's soundfile loader can read."""
    from pydub import AudioSegment
    out = os.path.join(dest_dir, f"{stem}.wav")
    with open(src, "rb") as handle:
        with AudioSegment.from_file(handle).export(out, format="wav"):
            pass
    return out


def get_chunk_uid_index(chunks):
    """Index string UIDs, retaining the first match used by reference lookup."""
    chunks_by_uid = {}
    for chunk in chunks:
        uid = chunk.get("uid")
        if isinstance(uid, str):
            chunks_by_uid.setdefault(uid, chunk)
    return chunks_by_uid


def check_voice_drift(chunks, voice_config, root_dir, python_bin, threshold,
                      indices=None, resolve_alias=None, resolve_asset_path=None,
                      score_pairs=ecapa_pairs):
    """Score bounded batches, discarding all results if any batch is unmeasured.

    Each batch owns its decoded files and removes them before the next batch.
    References still come from the full chunk list, including unselected rows.
    """
    if not python_bin:
        return _check_voice_drift_batch(
            chunks, voice_config, root_dir, python_bin, threshold,
            indices=indices, resolve_alias=resolve_alias,
            resolve_asset_path=resolve_asset_path, score_pairs=score_pairs)
    if score_pairs is ecapa_pairs:
        deadline = None

        def score_with_deadline(pairs, python):
            nonlocal deadline
            if deadline is None:
                deadline = time.monotonic() + 3600
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "speaker scoring deadline exceeded"
            return ecapa_pairs(pairs, python, timeout=remaining)

        score_pairs = score_with_deadline
    wanted = set(indices) if indices is not None else None
    targets = [i for i, chunk in enumerate(chunks)
               if chunk.get("status") == "done" and chunk.get("audio_path")
               and (wanted is None or i in wanted)]
    chunks_by_uid = get_chunk_uid_index(chunks)
    voice_groups, voice_keys = get_drift_voice_groups(chunks, voice_config, resolve_alias)
    results = []
    for offset in range(0, len(targets), DRIFT_BATCH_SIZE):
        report = _check_voice_drift_batch(
            chunks, voice_config, root_dir, python_bin, threshold,
            indices=targets[offset:offset + DRIFT_BATCH_SIZE],
            resolve_alias=resolve_alias, resolve_asset_path=resolve_asset_path,
            score_pairs=score_pairs, chunks_by_uid=chunks_by_uid,
            voice_groups=voice_groups, voice_keys=voice_keys)
        if report["error"]:
            return {"results": [], "error": report["error"]}
        results.extend(report["results"])
    return {"results": results, "error": None}


def _check_voice_drift_batch(chunks, voice_config, root_dir, python_bin, threshold,
                      indices=None, resolve_alias=None, resolve_asset_path=None,
                      score_pairs=ecapa_pairs, chunks_by_uid=None,
                      voice_groups=None, voice_keys=None):
    """Score chunks against their speaker's reference.

    Returns {"results": [{index, uid, score, flagged, reference}, ...],
             "error": None | "not measured: ..."}. On error `results` is empty:
    an unmeasured chunk is not an unflagged chunk.
    """
    from pydub.exceptions import CouldntDecodeError

    resolve_asset_path = resolve_asset_path or (lambda p: p)
    if not python_bin:
        return {"results": [], "error": "not measured: no speechbrain interpreter "
                                        "(set Voice Lab's rocm_python or ALEXANDRIA_SIBLING_PYTHON)"}
    wanted = set(indices) if indices is not None else None
    targets = [(i, c) for i, c in enumerate(chunks)
               if c.get("status") == "done" and c.get("audio_path")
               and (wanted is None or i in wanted)]
    if not targets:
        return {"results": [], "error": None}

    if voice_groups is None or voice_keys is None:
        voice_groups, voice_keys = get_drift_voice_groups(chunks, voice_config, resolve_alias)
    references = {}
    for index, chunk in targets:
        speaker = chunk.get("speaker")
        key = voice_keys[index]
        if key not in references:
            references[key] = get_reference_for_speaker(
                speaker, voice_config, voice_groups[key], resolve_alias, resolve_asset_path,
                get_chunk_voice_inputs(voice_config, chunk, index, resolve_alias))

    if chunks_by_uid is None:
        chunks_by_uid = get_chunk_uid_index(chunks)
    results, pairs, pair_owner = [], [], []
    with tempfile.TemporaryDirectory(prefix="voice_drift_") as tmp:
        decoded = {}

        def wav_for(path, stem):
            if path not in decoded:
                decoded[path] = _decode_to_wav(path, tmp, stem)
            return decoded[path]

        for index, chunk in targets:
            ref_path, label = references[voice_keys[index]]
            if label.startswith("chunk:"):
                ref_uid = label.split(":", 1)[1]
                if chunk.get("uid") == ref_uid:
                    results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                    "flagged": False, "reference": "self",
                                    "error": "not measured: chunk is its own reference"})
                    continue
                ref_chunk = chunks_by_uid.get(ref_uid)
                ref_path = os.path.join(root_dir, ref_chunk["audio_path"]) if ref_chunk else None
            if ref_path:
                ref_path = os.path.join(root_dir, ref_path)
                if not is_path_inside(ref_path, root_dir):
                    results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                    "flagged": False, "reference": label,
                                    "error": "reference audio path is outside project"})
                    continue
            if not ref_path or not os.path.exists(ref_path):
                results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                "flagged": False, "reference": label,
                                "error": "reference audio missing"})
                continue
            chunk_path = os.path.join(root_dir, chunk["audio_path"])
            if not is_path_inside(chunk_path, root_dir):
                results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                "flagged": False, "reference": label,
                                "error": "chunk audio path is outside project"})
                continue
            if not os.path.exists(chunk_path):
                results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                "flagged": False, "reference": label,
                                "error": "chunk audio missing"})
                continue
            try:
                pairs.append([wav_for(chunk_path, f"chunk_{index}"),
                              wav_for(ref_path, f"ref_{len(decoded)}")])
                pair_owner.append((index, chunk.get("uid"), label))
            except (OSError, ValueError, RuntimeError, CouldntDecodeError) as exc:
                results.append({"index": index, "uid": chunk.get("uid"),
                                "score": None, "flagged": False,
                                "reference": label,
                                "error": f"audio decode failed: {str(exc)[:160]}"})

        if pairs:
            scores, err = score_pairs(pairs, python_bin)
            if err:
                return {"results": [], "error": f"not measured: {err}"}
            if not isinstance(scores, list) or len(scores) != len(pairs):
                return {"results": [], "error": f"not measured: invalid scoring response; "
                                                f"expected {len(pairs)} scores"}
            for (index, uid, label), score in zip(pair_owner, scores):
                if score is None:
                    results.append({"index": index, "uid": uid, "score": None,
                                    "flagged": False, "reference": label,
                                    "error": "scoring failed"})
                    continue
                try:
                    value = float(score)
                    valid = not isinstance(score, bool) and math.isfinite(value)
                except (TypeError, ValueError, OverflowError):
                    valid = False
                if not valid:
                    results.append({"index": index, "uid": uid, "score": None,
                                    "flagged": False, "reference": label,
                                    "error": "invalid similarity score: not measured"})
                    continue
                results.append({"index": index, "uid": uid, "score": round(value, 4),
                                "flagged": value < threshold, "reference": label})
    results.sort(key=lambda r: r["index"])
    return {"results": results, "error": None}


def apply_drift_results(project_manager, results, threshold, *, expected_chunks=None):
    """Write each result onto its chunk as `drift` (chunks.json). Returns the
    number of flagged chunks. Missing targets raise after the remaining
    results are applied, so the caller cannot report a complete saved check."""
    from project import ChunkTargetMissingError
    flagged = 0
    applied = 0
    missing = []
    checked_at = time.time()
    expected_by_uid = get_chunk_uid_index(expected_chunks) if expected_chunks is not None else None
    for r in results:
        drift = {"score": r["score"], "flagged": bool(r["flagged"]),
                 "reference": r["reference"], "threshold": threshold,
                 "checked_at": checked_at}
        if r.get("error"):
            drift["error"] = r["error"]
        uid = r.get("uid")
        if not uid:
            missing.append(f"index {r.get('index', '?')} (no UID)")
            continue
        if expected_by_uid is not None:
            expected = expected_by_uid.get(uid)
            if expected is None:
                continue
            related = []
            if str(r.get("reference", "")).startswith("chunk:"):
                reference = expected_by_uid.get(r["reference"][6:])
                if reference is None:
                    continue
                related.append(reference)
            try:
                updated = project_manager._update_chunk_fields_by_uid(
                    uid, expected_chunk=expected, required_status="done", require_target=True,
                    expected_related_chunks=related,
                    expected_fields={key: expected.get(key) for key in ("audio_path", "audio_revision")}, drift=drift)
            except ChunkTargetMissingError:
                missing.append(str(uid))
                continue
            if updated is None:
                continue
        else:
            updated = project_manager._update_chunk_fields_by_uid(uid, drift=drift)
        if updated is not None:
            applied += 1
            flagged += int(bool(r["flagged"]))
        else:
            missing.append(str(uid))
    if missing:
        raise ValueError(f"Voice-drift targets disappeared or have no UID: {', '.join(missing)}; "
                         f"persisted {applied}/{len(results)} results ({flagged} flagged)")
    return flagged
