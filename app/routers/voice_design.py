import asyncio
import filecmp
import logging
import math
import os
from pathlib import Path
import shutil
import time
import threading
import subprocess
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request
from fastapi.responses import Response
from pydantic import BaseModel

from core import (
    CLONE_VOICES_DIR,
    UPLOADS_DIR,
    check_disk_space,
    DESIGNED_VOICES_DIR,
    _load_manifest,
    _require_safe_filename,
    _safe_subpath,
    _save_manifest,
    _save_upload_limited,
    run_claimed_task_worker,
    process_state,
    project_manager,
)
from utils import file_lock, get_unique_id
from voice_reference_import import import_reference_audio
from audio_channel_staging import AudioChannelStaging
from audio_channel_preview import render_audio_channel_preview
from audio_channels import get_audio_channel_filter, get_audio_channel_count
from subprocess_ownership import run_owned_capture
from tts import UnsupportedVoiceBackendError


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


class VoiceDesignPreviewRequest(BaseModel):
    description: str
    sample_text: str
    language: Optional[str] = None

class VoiceDesignSaveRequest(BaseModel):
    name: str
    description: str
    sample_text: str
    preview_file: str
    # Set by the Designer's Edit flow: replace this manifest entry (and its
    # audio) instead of appending a duplicate.
    voice_id: Optional[str] = None


## ── Voice Designer ──────────────────────────────────────────────

DESIGNED_VOICES_MANIFEST = os.path.join(DESIGNED_VOICES_DIR, "manifest.json")


def _append_manifest_entry(path, entry):
    """Append one immutable voice entry without losing a concurrent upload."""
    with file_lock(path):
        manifest = _load_manifest(path, for_update=True)
        manifest.append(entry)
        _save_manifest(path, manifest)


@router.post("/api/voice_design/preview")
async def voice_design_preview(request: VoiceDesignPreviewRequest):
    """Generate a preview voice from a text description."""
    return await run_claimed_task_worker("voice_design", _generate_voice_design_preview, request)


def _generate_voice_design_preview(request):
    try:
        # Model initialization allocates VRAM too, so it belongs inside the same
        # reservation as inference rather than happening before the lock check.
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")
        wav_path, _ = engine.generate_voice_design(
            description=request.description,
            sample_text=request.sample_text,
            language=request.language,
        )
        # Return relative URL for the static mount
        filename = os.path.basename(wav_path)
        return {"status": "ok", "audio_url": f"/designed_voices/previews/{filename}"}
    except UnsupportedVoiceBackendError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as e:
        logger.exception("Voice design preview failed")
        raise HTTPException(status_code=500, detail="Voice design preview failed — see server logs for details.") from e

@router.post("/api/voice_design/save")
async def voice_design_save(request: VoiceDesignSaveRequest):
    """Save a preview voice as a permanent designed voice."""
    return await asyncio.to_thread(_save_voice_design, request)


def _save_voice_design(request):
    previews_dir = os.path.join(DESIGNED_VOICES_DIR, "previews")
    # Constrain to the previews dir so preview_file can't traverse out and copy
    # an arbitrary host file (e.g. ../../etc/passwd) into the web-served dir.
    preview_path = _safe_subpath(previews_dir, request.preview_file)

    if not os.path.exists(preview_path):
        raise HTTPException(status_code=404, detail="Preview file not found")

    safe_name = _require_safe_filename(request.name, "Invalid voice name")

    with file_lock(DESIGNED_VOICES_MANIFEST):
        manifest = _load_manifest(DESIGNED_VOICES_MANIFEST, for_update=True)
        existing = None
        if request.voice_id:
            existing = next((m for m in manifest if m.get("id") == request.voice_id), None)
            if existing is None:
                raise HTTPException(status_code=404, detail="Designed voice to update not found")
            voice_id = existing["id"]
            dest_filename = existing.get("filename") or f"{voice_id}.wav"
        else:
            voice_id = get_unique_id(safe_name)
            dest_filename = f"{voice_id}.wav"
        dest_path = os.path.join(DESIGNED_VOICES_DIR, dest_filename)
        staging_path = os.path.join(DESIGNED_VOICES_DIR,
                                    f".{dest_filename}.{get_unique_id('stage')}.tmp")
        entry = {"id": voice_id, "name": request.name,
                 "description": request.description, "sample_text": request.sample_text,
                 "filename": dest_filename}
        backup_path = staging_path + ".backup"
        retain_backup = False
        try:
            shutil.copy2(preview_path, staging_path)
            if existing is not None:
                had_audio = os.path.exists(dest_path)
                if had_audio:
                    shutil.copy2(dest_path, backup_path)
                # Listening provenance describes the reviewed waveform and prompt.
                # A redesign must not retain the old audit or the labels recorded with it.
                if existing.get("synthetic_design") and (
                        not had_audio or not filecmp.cmp(dest_path, staging_path, shallow=False)
                        or any(existing.get(key) != entry[key]
                               for key in ("description", "sample_text"))):
                    for key in ("synthetic_design", "gender", "age_group"):
                        existing.pop(key, None)
                os.replace(staging_path, dest_path)
                existing.update(entry)
                try:
                    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)
                except BaseException as error:
                    try:
                        if had_audio:
                            os.replace(backup_path, dest_path)
                        else:
                            os.remove(dest_path)
                    except OSError as rollback_error:
                        retain_backup = True
                        raise RuntimeError(
                            f"Designed voice rollback failed; recovery audio kept at {backup_path}: "
                            f"{rollback_error}") from error
                    raise
            else:
                os.replace(staging_path, dest_path)
                manifest.append(entry)
                try:
                    _save_manifest(DESIGNED_VOICES_MANIFEST, manifest)
                except Exception:
                    os.remove(dest_path)
                    raise
        finally:
            if os.path.exists(staging_path):
                os.remove(staging_path)
            if not retain_backup and os.path.exists(backup_path):
                os.remove(backup_path)

    logger.info(f"Designed voice {'updated' if existing else 'saved'}: '{request.name}' as {dest_filename}")
    return {"status": "updated" if existing else "saved", "voice_id": voice_id}

@router.get("/api/voice_design/list")
async def voice_design_list():
    """List all saved designed voices."""
    return _load_manifest(DESIGNED_VOICES_MANIFEST)

def get_voice_asset_path(base_dir, entry):
    """Resolve a stored voice filename within its asset directory."""
    name = entry.get("filename")
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=400, detail="Invalid voice asset filename")
    return _safe_subpath(base_dir, name)


@router.delete("/api/voice_design/{voice_id}")
async def voice_design_delete(voice_id: str):
    """Delete a saved designed voice."""
    return await asyncio.to_thread(_delete_voice_design, voice_id)


def _delete_voice_design(voice_id):
    with file_lock(DESIGNED_VOICES_MANIFEST):
        manifest = _load_manifest(DESIGNED_VOICES_MANIFEST, for_update=True)
        entry = next((v for v in manifest if v["id"] == voice_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Voice not found")
        wav_path = get_voice_asset_path(DESIGNED_VOICES_DIR, entry)
        _save_manifest(DESIGNED_VOICES_MANIFEST,
                       [v for v in manifest if v["id"] != voice_id])
        if os.path.exists(wav_path):
            os.remove(wav_path)

    logger.info(f"Designed voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}

## ── Clone Voice Uploads ───────────────────────────────────────

CLONE_VOICES_MANIFEST = os.path.join(CLONE_VOICES_DIR, "manifest.json")
ALLOWED_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg"}
import hashlib
import json
import uuid

import reference_cleanup
from reference_cleanup import acquire_cleanup_slot, apply_affine_cleanup, plan_digest, sha256_file, verify_affine_candidate, validate_cleanup_plan
from voice_reference_import import check_reference_audio, measure_reference_audio

CLEANUP_SECONDS = 1800
CLEANUP_MAX = 8
MAX_REF_TEXT = 4000
MAX_SOURCE_TITLE = 512
MAX_SOURCE_URL = 2048
MAX_RIGHTS_BASIS = 1024


def validate_reference_provenance(ref_text, source_title, source_url, rights_basis, rights_confirmed):
    def bound(value, limit, label):
        text = value.strip() if isinstance(value, str) else ""
        if len(text) > limit:
            raise ValueError(f"{label} is too long (at most {limit} characters).")
        return text
    ref_text = bound(ref_text, MAX_REF_TEXT, "Transcript")
    source_title = bound(source_title, MAX_SOURCE_TITLE, "Source title")
    source_url = bound(source_url, MAX_SOURCE_URL, "Source URL")
    rights_basis = bound(rights_basis, MAX_RIGHTS_BASIS, "Rights basis")
    if not ref_text:
        raise ValueError("The exact transcript of the clip is required (ref_text).")
    if rights_confirmed is not True:
        raise ValueError("Confirm you have the right to use this voice (rights_confirmed).")
    return ref_text, source_title, source_url, rights_basis


def provenance_digest(provenance):
    return hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()


def _probe_source_channels(path):
    try:
        import soundfile as sf
        return int(sf.info(path).channels)
    except Exception:
        try:
            return get_audio_channel_count(path, run_owned_capture)
        except Exception as error:
            raise ValueError("Could not determine the source audio channels.") from error


def _require_disk_space():
    has_space, free_gb = check_disk_space(UPLOADS_DIR, 2.0)
    if has_space:
        return
    if free_gb == 0.0:
        raise ValueError("Disk space check unavailable.")
    raise ValueError("At least 2 GB free space is required.")


def _is_busy_error(error):
    return "already running" in str(error)


def _raise_cleanup_error(error):
    if isinstance(error, TimeoutError) or _is_busy_error(error):
        raise HTTPException(status_code=503, detail="Cleanup is temporarily unavailable; try again shortly.") from error
    raise HTTPException(status_code=400, detail=str(error)) from error


def _cleanup_root():
    return Path(UPLOADS_DIR) / ".reference-cleanup"


def _validate_cleanup_token(token):
    if not isinstance(token, str) or len(token) != 32 or any(ch not in "0123456789abcdef" for ch in token):
        raise ValueError("Invalid cleanup handle.")


def _cleanup_lock(token, timeout=10):
    _validate_cleanup_token(token)
    if not _cleanup_root().is_dir():
        raise ValueError("Cleanup session expired or was removed. Prepare the candidate again.")
    return file_lock(str(_cleanup_root() / (".lock-" + token[0])), timeout=timeout)


def _load_cleanup_session(token):
    _validate_cleanup_token(token)
    directory = _cleanup_root() / token
    if not directory.is_dir():
        raise ValueError("Cleanup session expired or was removed. Prepare the candidate again.")
    try:
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Cleanup session is corrupt. Prepare the candidate again.") from error
    try:
        if not isinstance(session, dict) or session["token"] != token:
            raise ValueError("session identity changed")
        if type(session["created"]) not in (int, float) or not math.isfinite(session["created"]):
            raise ValueError("invalid session time")
        if type(session["consumed"]) is not bool:
            raise ValueError("invalid consumed state")
        _validate_cleanup_token(session["publication_id"])
        for key in ("raw_source_sha256", "normalized_original_sha256", "candidate_sha256", "plan_digest", "provenance_digest"):
            digest = session[key]
            if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise ValueError("invalid digest")
        plan = session["plan"]
        reference_cleanup.require_cleanup_plan(plan)
        if plan_digest(plan) != session["plan_digest"]:
            raise ValueError("invalid cleanup plan")
        provenance = session["provenance"]
        validate_reference_provenance(**provenance)
        if provenance_digest(provenance) != session["provenance_digest"]:
            raise ValueError("invalid provenance")
        get_audio_channel_filter(session["audio_channel"])
        if not isinstance(session["source_filename"], str) or type(session["source_channels"]) is not int or session["source_channels"] < 1:
            raise ValueError("invalid source metadata")
        for key in ("original_measures", "candidate_measures", "verification"):
            if not isinstance(session[key], dict):
                raise ValueError("invalid verification metadata")
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValueError("Cleanup session is corrupt. Prepare the candidate again.") from error
    if time.time() - session["created"] > CLEANUP_SECONDS:
        shutil.rmtree(directory, ignore_errors=True)
        raise ValueError("Cleanup session expired. Prepare the candidate again.")
    if session.get("consumed"):
        raise ValueError("Cleanup session was already accepted or rejected.")
    return directory, session


def _save_cleanup_session(directory, session):
    from utils import atomic_json_write
    atomic_json_write(session, str(Path(directory) / "session.json"))


def _remove_cleanup_session(token):
    _validate_cleanup_token(token)
    directory = _cleanup_root() / token
    if directory.is_dir():
        with _cleanup_lock(token):
            if directory.is_dir():
                shutil.rmtree(directory)


def _cleanup_session_expired(directory):
    try:
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        created = session.get("created") if isinstance(session, dict) else None
        if isinstance(created, (int, float)) and not isinstance(created, bool) and math.isfinite(created):
            return time.time() - created > CLEANUP_SECONDS
    except (OSError, ValueError):
        pass
    try:
        return time.time() - directory.stat().st_mtime > CLEANUP_SECONDS
    except OSError:
        return False


def _sweep_expired_cleanup_sessions():
    root = _cleanup_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(str(root / ".create")):
        for directory in list(root.iterdir()):
            if not directory.is_dir() or len(directory.name) != 32 or any(ch not in "0123456789abcdef" for ch in directory.name):
                continue
            if not _cleanup_session_expired(directory):
                continue
            try:
                with _cleanup_lock(directory.name, timeout=0):
                    if directory.is_dir() and _cleanup_session_expired(directory):
                        shutil.rmtree(directory, ignore_errors=True)
            except TimeoutError:
                continue


def _find_cleanup_publication(publication_id, manifest=None):
    if not publication_id:
        return None
    if manifest is None:
        manifest = _load_manifest(CLONE_VOICES_MANIFEST, for_update=True)
    for entry in manifest:
        receipt = entry.get("cleanup_receipt") if isinstance(entry, dict) else None
        if isinstance(receipt, dict) and receipt.get("publication_id") == publication_id:
            return entry
    return None


@router.post("/api/clone_voices/cleanup/prepare")
async def cleanup_prepare(request: Request, preview_token: str = Form(""), audio_channel: str = Form("mix"),
                           gain: float = Form(1.0), remove_dc: bool = Form(False),
                           ref_text: str = Form(""), source_title: str = Form(""),
                           source_url: str = Form(""), rights_basis: str = Form(""),
                           rights_confirmed: bool = Form(False)):
    try:
        plan = validate_cleanup_plan(gain, remove_dc)
        get_audio_channel_filter(audio_channel)
        provenance_values = validate_reference_provenance(ref_text, source_title, source_url, rights_basis, rights_confirmed)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    provenance = {"ref_text": provenance_values[0], "source_title": provenance_values[1],
                  "source_url": provenance_values[2], "rights_basis": provenance_values[3], "rights_confirmed": True}
    cancelled = threading.Event()
    async def watch_disconnect():
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set()
                return
            await asyncio.sleep(0.1)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        session, token = await asyncio.to_thread(
            _prepare_cleanup_sync, preview_token, audio_channel, plan, provenance, cancelled)
    except (ValueError, TimeoutError) as error:
        _raise_cleanup_error(error)
    finally:
        cancelled.set()
        watcher.cancel()
    return {"status": "prepared", "cleanup_token": token, "plan": plan, "audio_channel": audio_channel,
            "plan_digest": session["plan_digest"], "raw_source_sha256": session["raw_source_sha256"],
            "normalized_original_sha256": session["normalized_original_sha256"],
            "candidate_sha256": session["candidate_sha256"], "measures": session["original_measures"],
            "candidate_measures": session["candidate_measures"], "candidate_problems": [],
            "verification": session["verification"],
            "provenance": session["provenance"], "provenance_digest": session["provenance_digest"],
            "source_filename": session["source_filename"], "source_channels": session["source_channels"],
            "integrity": "verified",
            "asr": "UNMEASURED", "speaker_identity": "UNMEASURED", "naturalness": "UNMEASURED"}


def _prepare_cleanup_sync(preview_token, audio_channel, plan, provenance, cancelled):
    with acquire_cleanup_slot():
        if cancelled.is_set():
            raise ValueError("Cleanup preparation was cancelled.")
        _require_disk_space()
        _sweep_expired_cleanup_sessions()
        root = _cleanup_root()
        token = uuid.uuid4().hex
        directory = root / token
        with file_lock(str(root / ".create")):
            if sum(p.is_dir() for p in root.iterdir()) >= CLEANUP_MAX:
                raise ValueError("Too many pending cleanup sessions. Close one or wait for expiry.")
            directory.mkdir(mode=0o700)
        with _cleanup_lock(token):
            source_path = str(directory / "source.wav")
            try:
                metadata = get_audio_channel_staging().copy_stage(preview_token, source_path, "clone")
                raw_sha = sha256_file(source_path, cancelled)
                original_path = str(directory / "original.wav")
                candidate_path = str(directory / "candidate.wav")
                measures, problems = import_reference_audio(source_path, original_path, channel=audio_channel)
                if problems:
                    raise ValueError("Original refused: " + "; ".join(problems))
                source_channels = _probe_source_channels(source_path)
                apply_affine_cleanup(original_path, candidate_path, plan)
                valid, verify_problems, records = verify_affine_candidate(original_path, candidate_path, plan)
                candidate_measures = measure_reference_audio(candidate_path)
                candidate_problems = check_reference_audio(candidate_measures)
                if not valid or candidate_problems:
                    raise ValueError("Candidate refused: " + "; ".join(verify_problems + candidate_problems))
                if cancelled.is_set():
                    raise ValueError("Cleanup preparation was cancelled.")
                session = {"token": token, "created": time.time(), "consumed": False, "plan": plan,
                           "plan_digest": plan_digest(plan), "audio_channel": audio_channel,
                           "publication_id": uuid.uuid4().hex,
                           "provenance": provenance, "provenance_digest": provenance_digest(provenance),
                           "source_filename": metadata.get("filename", ""), "source_channels": source_channels,
                           "raw_source_sha256": raw_sha, "normalized_original_sha256": measures["sha256"],
                           "candidate_sha256": candidate_measures["sha256"], "original_measures": measures,
                           "candidate_measures": candidate_measures, "verification": records,
                           "asr": "UNMEASURED", "speaker_identity": "UNMEASURED", "naturalness": "UNMEASURED"}
                if cancelled.is_set():
                    raise ValueError("Cleanup preparation was cancelled.")
                _save_cleanup_session(directory, session)
                if cancelled.is_set():
                    shutil.rmtree(directory, ignore_errors=True)
                    raise ValueError("Cleanup preparation was cancelled.")
                return session, token
            except BaseException:
                if directory.is_dir():
                    shutil.rmtree(directory, ignore_errors=True)
                raise


@router.get("/api/clone_voices/cleanup/{token}/audio")
async def cleanup_audio(token: str, which: str = "original"):
    if which not in ("original", "candidate"):
        raise HTTPException(status_code=400, detail="Choose original or candidate player.")
    def _read():
        with _cleanup_lock(token):
            directory, session = _load_cleanup_session(token)
            path = directory / (which + ".wav")
            if not path.is_file():
                raise ValueError("Cleanup audio is unavailable.")
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != session[("normalized_original_sha256" if which == "original" else "candidate_sha256")]:
                raise ValueError("Cleanup audio changed since preparation.")
            return data
    try:
        data = await asyncio.to_thread(_read)
    except (ValueError, TimeoutError) as error:
        _raise_cleanup_error(error)
    return Response(data, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post("/api/clone_voices/cleanup/accept")
async def cleanup_accept(request: Request, cleanup_token: str = Form(""), ref_text: str = Form(""),
                          source_title: str = Form(""), source_url: str = Form(""),
                          rights_basis: str = Form(""), rights_confirmed: bool = Form(False),
                          listened_original: bool = Form(False), listened_candidate: bool = Form(False),
                          raw_source_sha256: str = Form(""), normalized_original_sha256: str = Form(""),
                          candidate_sha256: str = Form(""), plan_digest_value: str = Form(""),
                          provenance_digest_value: str = Form(""),
                          audio_channel: str = Form("mix")):
    try:
        provenance_values = validate_reference_provenance(ref_text, source_title, source_url, rights_basis, rights_confirmed)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if listened_original is not True or listened_candidate is not True:
        raise HTTPException(status_code=400, detail="Listen to both Original and Candidate before accepting.")
    _validate_cleanup_token_or_400(cleanup_token)
    provenance = {"ref_text": provenance_values[0], "source_title": provenance_values[1],
                  "source_url": provenance_values[2], "rights_basis": provenance_values[3], "rights_confirmed": True}
    cancelled = threading.Event()
    async def watch_disconnect():
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set()
                return
            await asyncio.sleep(0.1)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        result = await asyncio.to_thread(
            _accept_cleanup_sync, cleanup_token, provenance, provenance_digest(provenance),
            (raw_source_sha256, normalized_original_sha256, candidate_sha256, plan_digest_value, provenance_digest_value, audio_channel), cancelled)
    except (ValueError, TimeoutError) as error:
        _raise_cleanup_error(error)
    finally:
        cancelled.set()
        watcher.cancel()
    return result


def _validate_cleanup_token_or_400(token):
    try:
        _validate_cleanup_token(token)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _accept_cleanup_sync(cleanup_token, provenance, digest, echoes, cancelled):
    raw_echo, normalized_echo, candidate_echo, plan_echo, provenance_echo, audio_channel = echoes
    with acquire_cleanup_slot(), _cleanup_lock(cleanup_token):
        directory, session = _load_cleanup_session(cleanup_token)
        existing = _find_cleanup_publication(session.get("publication_id", ""))
        if existing is not None:
            shutil.rmtree(directory, ignore_errors=True)
            return {"status": "accepted", "voice_id": existing["id"], "filename": existing["filename"],
                    "measures": {k: existing[k] for k in ("duration_s", "sample_rate", "peak", "silence_ratio", "clipping_ratio", "sha256") if k in existing},
                    "cleanup_receipt": existing["cleanup_receipt"], "already_accepted": True}
        if (raw_echo, normalized_echo, candidate_echo, plan_echo, provenance_echo, audio_channel) != (
                session["raw_source_sha256"], session["normalized_original_sha256"], session["candidate_sha256"],
                session["plan_digest"], session["provenance_digest"], session["audio_channel"]):
            raise ValueError("Cleanup details changed since preparation. Prepare the candidate again.")
        if provenance != session.get("provenance") or digest != session.get("provenance_digest"):
            raise ValueError("Transcript or rights details changed since preparation. Prepare the candidate again.")
        if plan_digest(session["plan"]) != session["plan_digest"]:
            raise ValueError("Cleanup plan changed since preparation. Prepare the candidate again.")
        if cancelled.is_set():
            raise ValueError("Cleanup acceptance was cancelled.")
        _require_disk_space()
        original_path = str(directory / "original.wav")
        candidate_path = str(directory / "candidate.wav")
        original_measures = measure_reference_audio(original_path)
        measures = measure_reference_audio(candidate_path)
        if original_measures["sha256"] != session["normalized_original_sha256"] or measures["sha256"] != session["candidate_sha256"] or sha256_file(str(directory / "source.wav"), cancelled) != session["raw_source_sha256"]:
            raise ValueError("Cleanup audio changed since preparation.")
        valid, problems, records = verify_affine_candidate(original_path, candidate_path, session["plan"])
        problems = problems + check_reference_audio(measures)
        if not valid or problems:
            raise ValueError("Candidate refused: " + "; ".join(problems))
        if cancelled.is_set():
            raise ValueError("Cleanup acceptance was cancelled.")
        stem = Path(session.get("source_filename") or "reference").stem or "reference"
        base_name = stem[:72] + "-cleaned"
        try:
            safe_name = _require_safe_filename(base_name, "Invalid filename")
        except Exception:
            safe_name = "reference-cleaned"
        voice_id = get_unique_id(safe_name)
        dest_filename = f"{voice_id}.wav"
        dest_path = os.path.join(CLONE_VOICES_DIR, dest_filename)
        if os.path.exists(dest_path):
            raise ValueError("Cleanup destination already exists. Prepare the candidate again.")
        staged_path = os.path.join(CLONE_VOICES_DIR, f".{dest_filename}.tmp-{session['publication_id']}")
        lineage_root = Path(UPLOADS_DIR) / ".reference-cleanup-lineage" / voice_id
        lineage_created = False
        try:
            shutil.copyfile(candidate_path, staged_path)
            with open(staged_path, "r+b") as handle:
                os.fsync(handle.fileno())
            if sha256_file(staged_path, cancelled) != session["candidate_sha256"]:
                raise ValueError("Cleanup candidate changed during publication.")
            lineage_base = Path(UPLOADS_DIR) / ".reference-cleanup-lineage"
            lineage_base.mkdir(parents=True, exist_ok=True, mode=0o700)
            lineage_root.mkdir(parents=True, mode=0o700)
            lineage_created = True
            for name, source, expected in (("source.wav", directory / "source.wav", session["raw_source_sha256"]), ("original.wav", directory / "original.wav", session["normalized_original_sha256"])):
                staged_lineage = lineage_root / ("." + name + ".tmp")
                shutil.copyfile(source, staged_lineage)
                with open(staged_lineage, "r+b") as handle:
                    os.fsync(handle.fileno())
                if sha256_file(staged_lineage, cancelled) != expected:
                    raise ValueError("Cleanup lineage changed during publication.")
                os.replace(staged_lineage, lineage_root / name)
            if cancelled.is_set():
                raise ValueError("Cleanup acceptance was cancelled.")
            receipt = {"origin": "affine-cleanup", "plan": session["plan"], "plan_digest": plan_digest(session["plan"]),
                       "selected_bounds": None, "bounds_mode": "full-length", "publication_id": session["publication_id"],
                       "raw_source_sha256": session["raw_source_sha256"],
                       "normalized_original_sha256": session["normalized_original_sha256"],
                       "candidate_sha256": measures["sha256"],
                       "provenance": provenance, "provenance_digest": digest,
                       "source_filename": session.get("source_filename", ""), "source_channels": session.get("source_channels"),
                       "lineage": {"raw_source": f".reference-cleanup-lineage/{voice_id}/source.wav",
                                   "normalized_original": f".reference-cleanup-lineage/{voice_id}/original.wav"},
                       "implementation": reference_cleanup.IMPLEMENTATION, "encoder": reference_cleanup.ENCODER,
                       "verification": records,
                       "asr": "UNMEASURED", "speaker_identity": "UNMEASURED", "naturalness": "UNMEASURED"}
            entry = {"id": voice_id, "name": safe_name, "filename": dest_filename, "ref_text": provenance["ref_text"],
                     "audio_channel": audio_channel, "source_channels": session.get("source_channels"),
                     "source_title": provenance["source_title"], "source_url": provenance["source_url"],
                     "rights_basis": provenance["rights_basis"], "rights_confirmed": True,
                     "imported_at": time.time(), "cleanup_receipt": receipt, **measures}
            with file_lock(CLONE_VOICES_MANIFEST):
                manifest = _load_manifest(CLONE_VOICES_MANIFEST, for_update=True)
                if _find_cleanup_publication(session["publication_id"], manifest=manifest) is not None:
                    raise ValueError("Cleanup candidate was already accepted.")
                if os.path.exists(dest_path):
                    raise ValueError("Cleanup destination already exists. Prepare the candidate again.")
                if cancelled.is_set():
                    raise ValueError("Cleanup acceptance was cancelled.")
                os.replace(staged_path, dest_path)
                try:
                    manifest.append(entry)
                    if cancelled.is_set():
                        raise ValueError("Cleanup acceptance was cancelled.")
                    _save_manifest(CLONE_VOICES_MANIFEST, manifest)
                except BaseException:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    raise
        except BaseException:
            if os.path.exists(staged_path):
                os.remove(staged_path)
            if lineage_created and lineage_root.is_dir() and _find_cleanup_publication(session.get("publication_id", "")) is None:
                shutil.rmtree(lineage_root, ignore_errors=True)
            raise
        session["consumed"] = True
        try:
            _save_cleanup_session(directory, session)
        except BaseException:
            return {"status": "accepted", "voice_id": voice_id, "filename": dest_filename, "measures": measures,
                    "cleanup_receipt": receipt, "session_cleanup_pending": True}
        shutil.rmtree(directory, ignore_errors=True)
        return {"status": "accepted", "voice_id": voice_id, "filename": dest_filename, "measures": measures, "cleanup_receipt": receipt}


@router.delete("/api/clone_voices/cleanup/{token}")
async def cleanup_reject(token: str):
    try:
        await asyncio.to_thread(_remove_cleanup_session, token)
    except (ValueError, TimeoutError) as error:
        _raise_cleanup_error(error)
    return {"status": "discarded", "detail": "Preview removed if present; already accepted voices remain in the library."}

def get_audio_channel_staging():
    return AudioChannelStaging(UPLOADS_DIR)


@router.post("/api/audio_channels/stage")
async def stage_audio_channels(file: UploadFile = File(...), purpose: str = Form("clone")):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail="Choose WAV, MP3, FLAC or OGG audio.")
    if not check_disk_space(UPLOADS_DIR, 2.0)[0]:
        raise HTTPException(status_code=400, detail="At least 2 GB free space is required.")
    staging = get_audio_channel_staging()
    try:
        token, path = await asyncio.to_thread(staging.create_stage, file.filename, purpose)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    try:
        await _save_upload_limited(file, str(path), (512 * 1024**2 if purpose == "clone" else 20 * 1024**3))
    except BaseException:
        await asyncio.to_thread(staging.remove_stage, token)
        raise
    return {"token": token, "expires_in": 1800, "preview_seconds": 10}


@router.get("/api/audio_channels/preview")
async def preview_audio_channel(request: Request, token: str, channel: str = "mix"):
    cancelled = threading.Event()
    def render():
        with get_audio_channel_staging().open_stage(token) as (path, metadata):
            return render_audio_channel_preview(path, channel, cancelled.is_set)
    async def watch_disconnect():
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set()
                return
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        audio, channels = await asyncio.to_thread(render)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        raise HTTPException(status_code=400, detail="Audio preview failed: " + (
            str(error) if isinstance(error, ValueError) else "could not decode the selected channel.")) from error
    finally:
        cancelled.set()
        watcher.cancel()
    return Response(audio, media_type="audio/wav", headers={
        "Cache-Control": "no-store", "X-Audio-Channels": str(channels)})


@router.delete("/api/audio_channels/stage/{token}")
async def delete_audio_channel_stage(token: str):
    try:
        await asyncio.to_thread(get_audio_channel_staging().remove_stage, token)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "removed"}


@router.get("/api/clone_voices/list")
async def clone_voices_list():
    """List all uploaded clone voices."""
    return _load_manifest(CLONE_VOICES_MANIFEST)

@router.post("/api/clone_voices/upload")
async def clone_voices_upload(file: Optional[UploadFile] = File(None),
                              ref_text: str = Form(""),
                              source_title: str = Form(""),
                              source_url: str = Form(""),
                              rights_basis: str = Form(""),
                              rights_confirmed: bool = Form(False),
                              preview_token: str = Form(""),
                              audio_channel: str = Form("mix")):
    """Import a reference clip for voice cloning.

    The clip is decoded, normalised to what the engine reads (24 kHz mono
    PCM16 WAV), measured, and refused if it is not one clean 3-30 s sentence
    (see voice_reference_import). The exact transcript and a rights
    confirmation are required; source title/URL/basis are recorded when given.
    Every import is a new immutable id - a changed reference is a new voice."""
    preview_token = preview_token if isinstance(preview_token, str) else ""
    audio_channel = audio_channel if isinstance(audio_channel, str) else "mix"
    try:
        get_audio_channel_filter(audio_channel)
        if preview_token:
            metadata = await asyncio.to_thread(get_audio_channel_staging().ensure_stage_metadata, preview_token, "clone")
            filename = metadata["filename"]
        elif file is not None:
            filename = file.filename
        else:
            raise ValueError("Select an audio file.")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"Unsupported format. Use: {', '.join(ALLOWED_AUDIO_EXTS)}")
    try:
        ref_text, source_title, source_url, rights_basis = validate_reference_provenance(
            ref_text, source_title, source_url, rights_basis, rights_confirmed)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    base_name = os.path.splitext(filename)[0]
    safe_name = _require_safe_filename(base_name, "Invalid filename")

    voice_id = get_unique_id(safe_name)
    dest_filename = f"{voice_id}.wav"
    dest_path = os.path.join(CLONE_VOICES_DIR, dest_filename)
    upload_path = os.path.join(CLONE_VOICES_DIR, f".upload_{voice_id}{ext}")

    if preview_token:
        try:
            await asyncio.to_thread(get_audio_channel_staging().copy_stage, preview_token, upload_path, "clone")
        except BaseException:
            if os.path.exists(upload_path):
                os.remove(upload_path)
            raise
    else:
        await _save_upload_limited(file, upload_path, 512 * 1024**2)
    def _import():
        try:
            measures, problems = import_reference_audio(upload_path, dest_path, channel=audio_channel)
            source_channels = None
            if not problems:
                try:
                    source_channels = _probe_source_channels(upload_path)
                except ValueError as error:
                    if os.path.exists(dest_path):
                        os.remove(dest_path)
                    raise HTTPException(status_code=400, detail=str(error)) from error
        except HTTPException:
            raise
        except BaseException:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise
        finally:
            if os.path.exists(upload_path):
                os.remove(upload_path)
        if problems:
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise HTTPException(status_code=400, detail="Reference clip refused: " + "; ".join(problems))

        try:
            _append_manifest_entry(CLONE_VOICES_MANIFEST, {
                "id": voice_id,
                "name": base_name,
                "filename": dest_filename,
                "ref_text": ref_text,
                "audio_channel": audio_channel,
                "source_channels": source_channels,
                "source_title": source_title,
                "source_url": source_url,
                "rights_basis": rights_basis,
                "rights_confirmed": True,
                "imported_at": time.time(),
                **measures,
            })
        except BaseException:
            # This immutable import owns its new file until the manifest accepts it.
            if os.path.exists(dest_path):
                os.remove(dest_path)
            raise


        logger.info(f"Clone voice imported: '{base_name}' as {dest_filename} "
                    f"({measures['duration_s']}s, sha256 {measures['sha256'][:12]})")
        return {"status": "uploaded", "voice_id": voice_id, "filename": dest_filename,
                "measures": measures}

    return await asyncio.to_thread(_import)

@router.delete("/api/clone_voices/{voice_id}")
async def clone_voices_delete(voice_id: str):
    """Delete an uploaded clone voice."""
    with file_lock(CLONE_VOICES_MANIFEST):
        manifest = _load_manifest(CLONE_VOICES_MANIFEST, for_update=True)
        entry = next((v for v in manifest if v["id"] == voice_id), None)
        if not entry:
            raise HTTPException(status_code=404, detail="Clone voice not found")
        wav_path = get_voice_asset_path(CLONE_VOICES_DIR, entry)
        receipt = entry.get("cleanup_receipt") if isinstance(entry, dict) else None
        if isinstance(receipt, dict):
            entry_id = entry.get("id")
            if isinstance(entry_id, str) and entry_id and entry_id not in (".", "..") and "/" not in entry_id and "\\" not in entry_id:
                lineage_base = Path(UPLOADS_DIR) / ".reference-cleanup-lineage"
                lineage_root = lineage_base / entry_id
                if lineage_root.parent == lineage_base and lineage_root.exists():
                    shutil.rmtree(lineage_root)
        _save_manifest(CLONE_VOICES_MANIFEST,
                       [v for v in manifest if v["id"] != voice_id])
        if os.path.exists(wav_path):
            os.remove(wav_path)

    logger.info(f"Clone voice deleted: {voice_id}")
    return {"status": "deleted", "voice_id": voice_id}
