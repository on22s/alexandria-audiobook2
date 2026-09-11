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
import os
import tempfile
import time

from experiments.library_voice_fidelity import ecapa_pairs

# Same rationale as verify_adapter_identity.py: working adapters score
# 0.65-0.74 against their human reference, failures 0.027-0.404, and the
# human-vs-human ceiling is ~0.83. Deliberately generous - a flag means "this
# is not the same voice", not "this could be a little closer".
DRIFT_MIN_SIMILARITY = 0.45
CONFIG_KEY = "voice_drift_min_similarity"


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
    """Interpreter that has speechbrain: the Voice Lab's configured one, else
    the sibling default `voice_reference` already resolves. None if neither
    exists - the caller reports NOT MEASURED."""
    candidate = (voicelab_config or {}).get("rocm_python") or ""
    if candidate and os.path.exists(candidate):
        return candidate
    from voice_reference import SIBLING_PY
    return SIBLING_PY if os.path.exists(SIBLING_PY) else None


def get_reference_for_speaker(speaker, voice_config, chunks, resolve_alias,
                              resolve_asset_path):
    """(absolute reference path or None, label) for a speaker.

    clone -> its ref_audio; lora -> the adapter's ref_sample.wav; anything
    without reference audio (design, custom, ensemble) -> the speaker's
    earliest `done` chunk, so drift means "not like the rest of this voice".
    The label says which, so an artifact never hides what it compared to."""
    canonical = resolve_alias(speaker, voice_config) if resolve_alias else speaker
    cfg = voice_config.get(canonical) or {}
    kind = cfg.get("type")
    if kind == "clone" and cfg.get("ref_audio"):
        return resolve_asset_path(cfg["ref_audio"]), f"clone:{canonical}"
    if kind in ("lora", "builtin_lora") and cfg.get("adapter_path"):
        path = os.path.join(resolve_asset_path(cfg["adapter_path"]), "ref_sample.wav")
        return path, f"lora:{canonical}"
    for chunk in chunks:
        if (chunk.get("speaker") == speaker and chunk.get("status") == "done"
                and chunk.get("audio_path")):
            return None, f"chunk:{chunk.get('uid')}"
    return None, "none"


def _decode_to_wav(src, dest_dir, stem):
    """MP3/anything -> WAV the ECAPA worker's soundfile loader can read."""
    from pydub import AudioSegment
    out = os.path.join(dest_dir, f"{stem}.wav")
    AudioSegment.from_file(src).export(out, format="wav")
    return out


def check_voice_drift(chunks, voice_config, root_dir, python_bin, threshold,
                      indices=None, resolve_alias=None, resolve_asset_path=None,
                      score_pairs=ecapa_pairs):
    """Score chunks against their speaker's reference.

    Returns {"results": [{index, uid, score, flagged, reference}, ...],
             "error": None | "not measured: ..."}. On error `results` is empty:
    an unmeasured chunk is not an unflagged chunk.
    """
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

    references = {}
    for _, chunk in targets:
        speaker = chunk.get("speaker")
        if speaker not in references:
            references[speaker] = get_reference_for_speaker(
                speaker, voice_config, chunks, resolve_alias, resolve_asset_path)

    results, pairs, pair_owner = [], [], []
    with tempfile.TemporaryDirectory(prefix="voice_drift_") as tmp:
        decoded = {}

        def wav_for(path, stem):
            if path not in decoded:
                decoded[path] = _decode_to_wav(path, tmp, stem)
            return decoded[path]

        for index, chunk in targets:
            ref_path, label = references[chunk.get("speaker")]
            if label.startswith("chunk:"):
                ref_uid = label.split(":", 1)[1]
                if chunk.get("uid") == ref_uid:
                    results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                    "flagged": False, "reference": "self"})
                    continue
                ref_chunk = next((c for c in chunks if c.get("uid") == ref_uid), None)
                ref_path = os.path.join(root_dir, ref_chunk["audio_path"]) if ref_chunk else None
            if not ref_path or not os.path.exists(ref_path):
                results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                "flagged": False, "reference": label,
                                "error": "reference audio missing"})
                continue
            chunk_path = os.path.join(root_dir, chunk["audio_path"])
            if not os.path.exists(chunk_path):
                results.append({"index": index, "uid": chunk.get("uid"), "score": None,
                                "flagged": False, "reference": label,
                                "error": "chunk audio missing"})
                continue
            pairs.append([wav_for(chunk_path, f"chunk_{index}"),
                          wav_for(ref_path, f"ref_{len(decoded)}")])
            pair_owner.append((index, chunk.get("uid"), label))

        if pairs:
            scores, err = score_pairs(pairs, python_bin)
            if err:
                return {"results": [], "error": f"not measured: {err}"}
            for (index, uid, label), score in zip(pair_owner, scores):
                if score is None:
                    results.append({"index": index, "uid": uid, "score": None,
                                    "flagged": False, "reference": label,
                                    "error": "scoring failed"})
                    continue
                results.append({"index": index, "uid": uid, "score": round(float(score), 4),
                                "flagged": float(score) < threshold, "reference": label})
    results.sort(key=lambda r: r["index"])
    return {"results": results, "error": None}


def apply_drift_results(project_manager, results, threshold):
    """Write each result onto its chunk as `drift` (chunks.json). Returns the
    number of flagged chunks."""
    flagged = 0
    checked_at = time.time()
    for r in results:
        drift = {"score": r["score"], "flagged": bool(r["flagged"]),
                 "reference": r["reference"], "threshold": threshold,
                 "checked_at": checked_at}
        if r.get("error"):
            drift["error"] = r["error"]
        project_manager._update_chunk_fields(r["index"], drift=drift)
        flagged += int(bool(r["flagged"]))
    return flagged
