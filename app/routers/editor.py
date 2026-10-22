from audio_generation_owner import AUDIO_GENERATION_CANCELLED
from merge_integrity import get_source_integrity
from pathlib import Path
from review_report import get_review_report_info, apply_review_report_explanation
from book_state_transaction import ensure_book_state, get_book_operation_identity
from task_ownership import ensure_startup_recovery
import asyncio
import copy
import hashlib
import pronunciation
import logging
import json
import os
import soundfile
import tempfile
import time
import uuid
import warnings
import zipfile
from contextlib import contextmanager
from typing import Annotated, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile, Query, Path as PathParameter
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field, StrictInt, field_validator
from config_settings import load_app_config
from audio_mastering import MasteringOptions, get_audio_sha256
from export_quality import ExportQualityOptions
from export_quality_session import ExportQualityAttempt, get_session_evidence
from project_scope_exports import require_scope_export_current

from core import (
    AUDIOBOOK_PATH,
    CONFIG_PATH,
    DATA_DIR,
    M4B_PATH,
    REPORTS_DIR,
    ROOT_DIR,
    run_claimed_task_worker,
    _validate_voicelab_path,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    _load_voicelab_config, _llm_summarize_report, _is_evidence_bound_summary,
    _save_upload_limited,
    _warn_corrupted_json,
    _gpu_lock,
    check_global_gpu_lock,
    claim_gpu_task, schedule_claimed_background_task,
    reserve_background_task, register_claimed_background_task, release_gpu_task_claim,
    process_state,
    project_manager,
)
from generation_checkpoint_deltas import load_generation_checkpoint_document
from generation_checkpoint_shards import get_generation_checkpoint_artifacts
from editor_poll_snapshot import EditorPollSnapshots, get_snapshot_digest
from editor_review_facts import get_editor_review_facts
from speech_verification_receipt import get_speech_verification_chunk_view
from utils import safe_load_json, is_path_inside
from project import (CHAPTER_EXPORT_DIR, CHAPTER_TEMPLATE_FIELDS,
                     DEFAULT_CHAPTER_TEMPLATE, get_chapter_export_rows, get_index_selection,
                     get_chunk_generation_requests, get_speaker)
from runtime_estimate import get_book_runtime_estimate
from routers.voices import get_script_speaker
from tts import (voice_config_for_chunk, resolve_narrator_voice_config, voice_category,
                 ExportCancelled, ensure_audio_export_active)
import voice_drift


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


def build_zip_download(members, filename):
    """Build a ZIP on disk and remove it after FileResponse finishes."""
    fd, path = tempfile.mkstemp(prefix=".alexandria-export-", suffix=".zip", dir=DATA_DIR)
    os.close(fd)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for source, name in members:
                archive.write(source, name)
    except BaseException:
        os.unlink(path)
        raise
    return FileResponse(path, filename=filename, media_type="application/zip",
                        background=BackgroundTask(os.unlink, path))


class WordTimingRequest(BaseModel):
    expected_book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_uid: str = Field(min_length=1, max_length=128)
    expected_audio_revision: Optional[str] = Field(default=None, max_length=128)


@router.post("/api/chunks/{index}/word_timing")
async def create_chunk_word_timing(index: int, request: WordTimingRequest):
    from whisper_cpp_runtime import get_whisper_cpp_runtime
    from editor_word_timing import publish_word_timing
    if index < 0:
        raise HTTPException(status_code=400, detail="Choose a valid Editor line.")
    runtime = get_whisper_cpp_runtime(ROOT_DIR)
    if not runtime['available']:
        raise HTTPException(status_code=400, detail="Word timing needs the already installed whisper.cpp Small.en runtime. No model will be downloaded.")
    for key in ('binary', 'model'):
        _validate_voicelab_path(runtime[key], 'Word-timing ' + key)
    runtime = {**runtime, **{key: str(Path(runtime[key]).resolve()) for key in ('binary', 'model')}}
    def run():
        state = process_state['word_timing']
        state['logs'] = ['Word timing: one selected chunk, cached English provider, CPU only.']
        try:
            for key in ('binary', 'model'):
                _validate_voicelab_path(runtime[key], 'Word-timing ' + key)
            result = publish_word_timing(project_manager, index, request, runtime,
                check_chunk_identity, lambda: state.get('cancel', False))
            state['logs'].append('Timing ready. ASR spans do not certify pronunciation.')
            return result
        except (ValueError, OSError, KeyError, TypeError) as error:
            state['logs'].append('Timing failed: ' + str(error))
            raise HTTPException(status_code=409, detail=str(error)) from error
    # Both the bounded ffmpeg decode and whisper.cpp --no-gpu provider are CPU
    # work. Keep the per-task lease and cancellation ownership through cleanup.
    return await run_claimed_task_worker('word_timing', run, cpu_only=True)


class WordTimingAudioResponse(StreamingResponse):
    def __init__(self, stream, size, suffix):
        self.stream = stream
        def chunks():
            try:
                yield from iter(lambda: stream.read(65536), b'')
            finally:
                stream.close()
        import mimetypes
        media = mimetypes.guess_type('audio' + suffix)[0] or 'application/octet-stream'
        super().__init__(chunks(), media_type=media,
            headers={'Content-Length': str(size), 'Cache-Control': 'no-store'})

    async def __call__(self, scope, receive, send):
        try:
            return await super().__call__(scope, receive, send)
        finally:
            self.stream.close()


@router.get("/api/chunks/{index}/word_timing/audio")
def get_word_timing_audio(index: int,
    expected_book_token: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    expected_uid: Annotated[str, Query(min_length=1, max_length=128)],
    sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    expected_audio_revision: Annotated[Optional[str], Query(max_length=128)] = None):
    from editor_word_timing import open_verified_word_audio
    try:
        stream, size, suffix = open_verified_word_audio(project_manager, index, expected_book_token,
            expected_uid, expected_audio_revision, sha256, check_chunk_identity)
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return WordTimingAudioResponse(stream, size, suffix)


class TakeHistoryPolicyRequest(BaseModel):
    expected_book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    enabled: bool = Field(strict=True)


@router.get("/api/take_history_policy")
async def get_take_history_policy_endpoint():
    from editor_take_history import get_editor_take_policy
    try:
        return await asyncio.to_thread(get_editor_take_policy, project_manager)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/take_history_policy")
async def change_take_history_policy_endpoint(request: TakeHistoryPolicyRequest):
    from editor_take_history import change_editor_take_policy
    try:
        return await asyncio.to_thread(change_editor_take_policy, project_manager, request)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


class TakeSnapshotRequest(WordTimingRequest):
    pass


class TakeHistoryAction(BaseModel):
    expected_book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_uid: str = Field(min_length=1, max_length=128)
    version: str = Field(pattern=r"^[0-9a-f]{32}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: str = Field(pattern=r"^(protect|unprotect|delete|undo|purge)$")


class TakeRestoreRequest(WordTimingRequest):
    source_book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    version: str = Field(pattern=r"^[0-9a-f]{32}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_audio_path: Optional[str] = Field(..., max_length=4096)
    expected_take: Optional[StrictInt] = Field(..., ge=0)


@router.post("/api/chunks/{index}/take_restore")
async def restore_take_snapshot(index: int, request: TakeRestoreRequest, background_tasks: BackgroundTasks):
    from audio_generation_owner import AudioGenerationCancelled
    from take_restoration import capture_restore_target, restore_editor_take
    with ensure_audio_generation_claim() as claim_id:
        try:
            expected = await asyncio.to_thread(capture_restore_target, project_manager, index, request)
        except (ValueError, OSError, KeyError, TypeError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

        def task():
            state = process_state['audio']
            state.update(start_time=time.time(), logs=[f'Checking saved take for line {index}...'],
                         status='running', error=None)
            try:
                restore_editor_take(project_manager, index, request, expected)
                state['status'] = 'done'
                state['logs'].append('Restore complete. Original saved bytes selected; displaced take retained.')
            except AudioGenerationCancelled:
                state['status'] = 'cancelled'
                state['logs'].append('Restoration cancelled. Selected line audio was preserved.')
            except Exception as error:
                logger.exception('Saved take restoration failed')
                state['status'] = 'failed'
                state['error'] = str(error)
                state['logs'].append('Restoration refused: ' + str(error))
            finally:
                state['running'] = False

        register_claimed_background_task(background_tasks, 'audio', claim_id, task)
        return {'status': 'started'}


@router.post("/api/chunks/{index}/take_snapshots")
async def save_take_snapshot(index: int, request: TakeSnapshotRequest):
    from editor_take_history import capture_editor_take
    try:
        return await asyncio.to_thread(capture_editor_take, project_manager, index, request)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/take_snapshots")
async def list_take_snapshots(
    expected_book_token: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    uid: Annotated[Optional[str], Query(min_length=1, max_length=128)] = None,
    all_books: bool = False, offset: Annotated[int, Query(ge=0, le=4096)] = 0):
    from editor_take_history import get_editor_takes
    try:
        return await asyncio.to_thread(get_editor_takes, project_manager, expected_book_token,
            uid, all_books=all_books, offset=offset)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/api/take_snapshots/action")
async def change_take_snapshot(request: TakeHistoryAction):
    from editor_take_history import change_editor_take
    try:
        return await asyncio.to_thread(change_editor_take, project_manager, request, _get_undo_take_rows)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.get("/api/take_snapshots/{version}/audio")
def audition_take_snapshot(version: Annotated[str, PathParameter(pattern=r"^[0-9a-f]{32}$")],
    expected_book_token: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    source_book_token: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
    expected_uid: Annotated[str, Query(min_length=1, max_length=128)],
    sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]):
    from editor_take_history import open_editor_take
    try:
        stream, size, suffix = open_editor_take(project_manager, expected_book_token, version, sha256, source_book_token, expected_uid)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return WordTimingAudioResponse(stream, size, suffix)


@router.post("/api/word_timing/cancel")
async def cancel_word_timing():
    if not process_state['word_timing'].get('running'):
        raise HTTPException(status_code=400, detail='No word-timing task is running.')
    process_state['word_timing']['cancel'] = True
    return {'status': 'cancellation requested'}


class SpeechVerificationRequest(BaseModel):
    expected_book_token: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_uid: str = Field(min_length=1,max_length=128)
    expected_audio_revision: str = Field(min_length=1,max_length=128)


@router.post('/api/chunks/{index}/speech_verification')
async def recheck_chunk_speech(index: int, request: SpeechVerificationRequest):
    from editor_speech_verification import recheck_editor_speech
    from speech_verification_runtime import get_verification_runtime
    if index < 0:
        raise HTTPException(status_code=400,detail='Choose a valid Editor line.')
    def run():
        state=process_state['speech_verification']
        state['logs']=['Rechecking one saved take with its recorded synthesis input. CPU only.']
        try:
            result=recheck_editor_speech(project_manager,index,request,
                get_verification_runtime(ROOT_DIR),get_verification_runtime(ROOT_DIR,confirmation=True),
                check_chunk_identity)
            state['logs'].append('Speech check: '+result['receipt']['status']+'. Listening review is still needed.')
            return result
        except (ValueError,OSError,KeyError,TypeError,RuntimeError) as error:
            state['logs'].append('Speech check failed: '+str(error))
            raise HTTPException(status_code=409,detail=str(error)) from error
    return await run_claimed_task_worker('speech_verification',run,cpu_only=True)


@router.post('/api/speech_verification/cancel')
async def cancel_speech_verification():
    with _gpu_lock:
        state=process_state['speech_verification']
        if not state.get('running'):
            raise HTTPException(status_code=400,detail='No speech verification task is running.')
        state['cancel']=True
    return {'status':'cancellation requested'}


class ChunkUpdate(BaseModel):
    text: Optional[str] = None
    instruct: Optional[str] = None
    speaker: Optional[str] = None
    take: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    pause_after: Optional[int] = None

    @field_validator("text", "instruct", "speaker", mode="before")
    @classmethod
    def validate_render_field(cls, value):
        if value is None:
            raise ValueError("Render fields cannot be null; omit unchanged fields")
        return value

class BatchGenerateRequest(BaseModel):
    indices: List[StrictInt] = Field(min_length=1)
    expected_book_token: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_uids: Optional[List[Annotated[str, Field(min_length=1, max_length=128)]]] = None
    expected_repair_revision: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AudioRepairSelection(BaseModel):
    expected_options_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    chapter: Optional[Annotated[StrictInt, Field(ge=0)]] = None
    speakers: List[Annotated[str, Field(max_length=500)]] = Field(min_length=1, max_length=10000)


def get_audio_repair_revision(chunks):
    """Bind a preview to rows and the voice/settings inputs generation will read."""
    files = []
    for path in (project_manager.voice_config_path,
                 os.path.join(project_manager.root_dir, "character_aliases.json"), CONFIG_PATH,
                 pronunciation.DEFAULT_PATH):
        try:
            value = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except FileNotFoundError:
            value = None
        files.append(value)
    return get_snapshot_digest([chunks, files])


def get_audio_repair_options(chunks):
    groups = project_manager._chapter_groups(chunks, False) if chunks else []
    return {"chapters": [{"index": i, "title": title, "first": first, "last": last}
                         for i, (title, first, last) in enumerate(groups)],
            "speakers": sorted({get_speaker(chunk) for chunk in chunks
                                if chunk.get("text", "").strip()}),
            "grouping_note": "Uses export chapter detection; without headings, each line is a chapter."}


@router.get("/api/chunks/repair_options")
async def get_audio_repair_options_endpoint(expected_book_token: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]):
    return await asyncio.to_thread(_get_audio_repair_options_sync, expected_book_token)


def _get_audio_repair_options_sync(expected_book_token):
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        check_chunk_identity(0, expected_book_token)
        chunks = _ensure_editor_chunks()
        return {"book_token": expected_book_token, "options_revision": get_snapshot_digest(chunks),
                **get_audio_repair_options(chunks)}


@router.post("/api/chunks/repair_preview")
async def get_audio_repair_preview_endpoint(request: AudioRepairSelection):
    return await asyncio.to_thread(_get_audio_repair_preview_sync, request)


def _get_audio_repair_preview_sync(request):
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        check_chunk_identity(0, request.expected_book_token)
        chunks = _ensure_editor_chunks()
        if request.expected_options_revision != get_snapshot_digest(chunks):
            raise HTTPException(status_code=409, detail="Chapter options are stale. Load current chapters and speakers again.")
        options = get_audio_repair_options(chunks)
        first, last = 0, len(chunks) - 1
        if request.chapter is not None:
            if request.chapter >= len(options["chapters"]):
                raise HTTPException(status_code=409, detail="Chapter selection changed; refresh repair options.")
            chapter = options["chapters"][request.chapter]
            first, last = chapter["first"], chapter["last"]
        if not set(request.speakers).issubset(options["speakers"]):
            raise HTTPException(status_code=409, detail="Speaker selection changed; refresh repair options.")
        selected = [chunk for i, chunk in enumerate(chunks) if first <= i <= last
                    and chunk.get("text", "").strip() and get_speaker(chunk) in request.speakers]
        return {"book_token": request.expected_book_token,
                "repair_revision": get_audio_repair_revision(chunks), "rows": selected,
                "rendered_count": sum(bool(chunk.get("audio_path")) for chunk in selected)}


class DriftCheckRequest(BaseModel):
    indices: Optional[List[int]] = None       # default: every done chunk


@router.head("/api/audiobook")
@router.get("/api/audiobook")
async def get_audiobook():
    if not os.path.exists(AUDIOBOOK_PATH):
        raise HTTPException(status_code=404, detail="Audiobook not found")
    require_scope_export_current(DATA_DIR, AUDIOBOOK_PATH)
    return FileResponse(AUDIOBOOK_PATH, filename="audiobook.mp3", media_type="audio/mpeg")

# --- Chunk Management Endpoints ---

@router.get("/api/chunks")
async def get_chunks():
    return await asyncio.to_thread(_ensure_chunk_listing)


def _ensure_chunk_listing():
    root=os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        chunks = _get_editor_chunk_views(_ensure_editor_chunks(),get_snapshot_digest(get_editor_book_identity(root)))
    return chunks


def _get_editor_chunk_views(chunks, book_token):
    views = [get_speech_verification_chunk_view(chunk,book_token,project_manager.get_chunk_audio_path)
             for chunk in chunks]
    for view in views:
        view.pop('_retained_take', None)
    return views


def _ensure_editor_chunks():
    """Load rows without inventing IDs or discarding persisted progress."""
    try:
        chunks = project_manager.load_chunks()
    except (ValueError, OSError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    seen = set()
    for position, chunk in enumerate(chunks):
        chunk_id = chunk.get('id')
        if type(chunk_id) is not int or chunk_id < 0 or chunk_id in seen:
            raise HTTPException(status_code=409, detail=(
                f"Editor chunk at position {position} has a missing, invalid or duplicate id. "
                "Repair chunks.json before continuing; saved progress and audio are retained."))
        seen.add(chunk_id)
    return chunks

@router.get("/api/audio/estimate")
async def get_audio_estimate():
    return await asyncio.to_thread(_ensure_audio_estimate)


def _ensure_audio_estimate():
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        chunks = _ensure_editor_chunks()
    durations, unreadable = get_chunk_audio_durations(chunks)
    return {**get_book_runtime_estimate(chunks, durations), "unreadable": unreadable}


_audio_duration_cache = {}


def get_audio_duration(path):
    """Seconds of audio at path, cached by path, mtime and size so Editor polls stay cheap."""
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    if key not in _audio_duration_cache:
        if len(_audio_duration_cache) > 50000:
            _audio_duration_cache.clear()
        _audio_duration_cache[key] = soundfile.info(path).duration
    return _audio_duration_cache[key]


def get_chunk_audio_durations(chunks):
    """Measure rendered chunk audio inside the project; count unreadable files."""
    durations = {}
    unreadable = 0
    for chunk in chunks:
        if not isinstance(chunk, dict) or chunk.get('status') != 'done':
            continue
        path = chunk.get('audio_path')
        if not project_manager._is_project_audio_path(path):
            continue
        try:
            durations[chunk.get('id')] = get_audio_duration(
                os.path.join(project_manager.root_dir, path))
        except Exception:
            unreadable += 1
    return durations, unreadable


_editor_poll_snapshots = EditorPollSnapshots()


@router.get("/api/chunks/status")
async def get_chunk_status_snapshot(revision: Annotated[Optional[str], Query(max_length=64)] = None):
    return await asyncio.to_thread(_ensure_chunk_status_snapshot, revision)


def _ensure_chunk_status_snapshot(revision):
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        chunks = _ensure_editor_chunks()
        identity = get_editor_book_identity(root)
        chunks = _get_editor_chunk_views(chunks,get_snapshot_digest(identity))
        return _editor_poll_snapshots.ensure_snapshot(chunks, identity, revision)


def get_editor_book_identity(root):
    """Caller holds the book transaction lock across reading and using identity."""
    return get_book_operation_identity(root)


@router.get("/api/chunks/quality")
async def get_editor_quality(expected_book_token: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]):
    return await asyncio.to_thread(_get_editor_quality_sync, expected_book_token)


def _get_editor_quality_sync(expected_book_token):
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        identity = get_editor_book_identity(root)
        if get_snapshot_digest(identity) != expected_book_token:
            raise HTTPException(status_code=409, detail="The selected book changed. Refresh Editor before reviewing its lines.")
        chunks = _get_editor_chunk_views(_ensure_editor_chunks(),expected_book_token)
        snapshot = _editor_poll_snapshots.ensure_snapshot(chunks, identity, None)
        return {'book_token': snapshot['book_token'], 'revision': snapshot['revision'],
                'rows': get_editor_review_facts(chunks, project_manager.get_chunk_audio_path,expected_book_token)}


_deleted_chunk_receipts = {}


def _get_undo_take_rows():
    """Caller holds the book lock; unexpired deletion undo still owns its audio."""
    book = get_editor_book_identity(os.path.dirname(SCRIPT_PATH))
    now = time.monotonic()
    return [receipt['chunk'] for receipt in _deleted_chunk_receipts.values()
            if receipt['book'] == book and now - receipt['created'] <= 60]



def check_chunk_identity(index, expected_book_token=None, expected_uid=None):
    """Caller holds the book lock through the following mutation."""
    root = os.path.dirname(SCRIPT_PATH)
    if (expected_book_token is not None
            and expected_book_token != get_snapshot_digest(get_editor_book_identity(root))):
        raise HTTPException(status_code=409, detail="Active book changed; reload the Editor before saving")
    if expected_uid is not None:
        chunks = project_manager.load_chunks()
        if not (0 <= index < len(chunks)) or chunks[index].get("uid") != expected_uid:
            raise HTTPException(status_code=409, detail="Editor row changed; reload before saving")


@contextmanager
def ensure_audio_generation_claim():
    """Exclude book switching before any admission await, rolling back failures."""
    claim_id = reserve_background_task("audio")
    try:
        yield claim_id
    except BaseException:
        release_gpu_task_claim("audio", claim_id, pending_only=True)
        raise


def ensure_audio_generation_requests(indices, expected_book_token=None, expected_uids=None,
                                     expected_repair_revision=None):
    """Read and bind selection once while its caller owns the audio reservation."""
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        identity = get_snapshot_digest(get_editor_book_identity(root))
        if (expected_book_token is not None and expected_book_token != identity):
            raise HTTPException(status_code=409, detail="Active book changed; reload before rendering")
        chunks = project_manager.load_chunks()
        if get_snapshot_digest(get_editor_book_identity(root)) != identity:
            raise HTTPException(status_code=409, detail="Active book changed; reload before rendering")
        if expected_repair_revision is not None:
            if (expected_book_token is None or expected_uids is None
                    or expected_repair_revision != get_audio_repair_revision(chunks)):
                raise HTTPException(status_code=409, detail="Repair preview changed; preview the selection again.")
        selected = get_index_selection(indices, len(chunks))
        if expected_uids is not None:
            if len(expected_uids) != len(indices):
                raise HTTPException(status_code=422, detail="Provide one UID per selected index")
            if any(chunks[index].get("uid") != uid for index, uid in zip(indices, expected_uids)):
                raise HTTPException(status_code=409, detail="Editor row changed; reload before rendering")
        return selected, get_chunk_generation_requests(chunks, selected)


def apply_chunk_update(index, updates, expected_book_token, expected_uid):
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        check_chunk_identity(index, expected_book_token, expected_uid)
        return project_manager.update_chunk(index, updates)


def apply_chunk_insertion(index, expected_book_token=None, expected_uid=None):
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        check_chunk_identity(index, expected_book_token, expected_uid)
        return project_manager.insert_chunk(index)


def apply_chunk_deletion(index, expected_book_token=None, expected_uid=None):
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        check_chunk_identity(index, expected_book_token, expected_uid)
        result = project_manager.delete_chunk(index)
        if result is None:
            return None
        deleted, chunks = result
        now = time.monotonic()
        for token, receipt in list(_deleted_chunk_receipts.items()):
            if now - receipt['created'] > 60:
                del _deleted_chunk_receipts[token]
        while len(_deleted_chunk_receipts) >= 128:
            del _deleted_chunk_receipts[next(iter(_deleted_chunk_receipts))]
        token = uuid.uuid4().hex
        _deleted_chunk_receipts[token] = {'created': now, 'book': get_editor_book_identity(root),
            'chunk': copy.deepcopy(deleted), 'index': index}
        return deleted, chunks, token


def apply_chunk_restore(request):
    root = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(root):
        receipt = _deleted_chunk_receipts.get(request.undo_token)
        if (receipt is None or time.monotonic() - receipt['created'] > 60
                or receipt['book'] != get_editor_book_identity(root)
                or request.chunk != receipt['chunk'] or request.at_index != receipt['index']):
            raise HTTPException(status_code=409, detail='Undo expired or belongs to another book or deletion; refresh the Editor')
        chunks = project_manager.restore_chunk(receipt['index'], copy.deepcopy(receipt['chunk']))
        if chunks is not None:
            del _deleted_chunk_receipts[request.undo_token]
        return chunks


class ChunkRestoreRequest(BaseModel):
    chunk: dict
    at_index: int
    undo_token: Optional[str] = Field(default=None, max_length=64)

def _apply_chunk_edit(operation, *args):
    """Keep the conflict check and chunk mutation atomic with task claims."""
    with _gpu_lock:
        check_global_gpu_lock("audio")
        try:
            return operation(*args)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

@router.post("/api/chunks/restore")
async def restore_chunk(request: ChunkRestoreRequest):
    """Re-insert a previously deleted chunk at a specific index."""
    chunks = await asyncio.to_thread(_apply_chunk_edit, apply_chunk_restore, request)
    if chunks is None:
        raise HTTPException(status_code=400, detail="Failed to restore chunk")
    return {"status": "ok", "total": len(chunks)}

# Registered before /api/chunks/{index}: FastAPI matches routes in order, and
# the integer wildcard swallowed this literal path (422 since #525).
@router.post("/api/chunks/drift_check")
async def drift_check_endpoint(request: DriftCheckRequest, background_tasks: BackgroundTasks):
    """Score done chunks against their speaker's reference voice (ECAPA cosine,
    CPU, sibling interpreter) and flag the ones that drifted. Never
    regenerates anything; the per-chunk Gen button is the fix."""
    state = process_state["drift_check"]
    if state["running"]:
        raise HTTPException(status_code=400, detail="A voice-drift check is already running.")
    threshold = voice_drift.get_drift_threshold(await asyncio.to_thread(load_app_config, CONFIG_PATH))
    python_bin = voice_drift.get_speaker_model_python(await asyncio.to_thread(_load_voicelab_config))
    indices = request.indices

    def task():
        state["running"] = True
        state["logs"] = ["Checking generated chunks against their reference voices..."]
        try:
            from tts import _resolve_asset_path
            chunks = project_manager.load_chunks()
            voice_config = safe_load_json(project_manager.voice_config_path, default={}) or {}
            report = voice_drift.check_voice_drift(
                chunks, voice_config, project_manager.root_dir, python_bin, threshold,
                indices=indices, resolve_alias=project_manager._resolve_alias,
                resolve_asset_path=_resolve_asset_path)
            if report["error"]:
                state["logs"].append(f"NOT MEASURED: {report['error']}")
                return
            flagged = voice_drift.apply_drift_results(
                project_manager, report["results"], threshold, expected_chunks=chunks)
            scored = sum(1 for r in report["results"] if r["score"] is not None)
            state["logs"].append(
                f"Checked {scored} chunk(s) at threshold {threshold}: {flagged} flagged.")
            for r in report["results"]:
                if r["flagged"]:
                    state["logs"].append(f"  Chunk {r['index']} drifted: {r['score']} vs {r['reference']}")
        except Exception as e:
            logger.exception("Voice-drift check error")
            state["logs"].append(f"Voice-drift check error: {e}")
        finally:
            state["running"] = False

    # Reserve the task before handing it to FastAPI's background runner.  The
    # request-time running check above is only an early rejection; this atomic
    # claim closes the gap between two rapid requests both seeing it as idle.
    schedule_claimed_background_task(background_tasks, "drift_check", task)
    return {"status": "started", "threshold": threshold, "measured": python_bin is not None}

@router.post("/api/chunks/{index}")
async def update_chunk(index: int, update: ChunkUpdate,
                       expected_book_token: Annotated[str, Query(min_length=64, max_length=64)],
                       expected_uid: Annotated[str, Query(min_length=1, max_length=128)]):
    updates = update.model_dump(exclude_unset=True)
    logger.info(f"Updating chunk {index} with data: {updates}")
    chunk = await asyncio.to_thread(_apply_chunk_edit, apply_chunk_update, index, updates,
                                    expected_book_token, expected_uid)
    if not chunk:
        raise HTTPException(status_code=404, detail="Chunk not found")
    logger.info(f"Chunk {index} updated, instruct is now: '{chunk.get('instruct', '')}'")
    return chunk

@router.post("/api/chunks/{index}/insert")
async def insert_chunk(index: int,
                       expected_book_token: Annotated[Optional[str], Query(min_length=64, max_length=64)] = None,
                       expected_uid: Annotated[Optional[str], Query(max_length=128)] = None):
    """Insert an empty chunk after the given index."""
    chunks = await asyncio.to_thread(_apply_chunk_edit, apply_chunk_insertion, index,
                                    expected_book_token, expected_uid)
    if chunks is None:
        raise HTTPException(status_code=404, detail="Invalid chunk index")
    return {"status": "ok", "total": len(chunks)}

@router.delete("/api/chunks/{index}")
async def delete_chunk(index: int,
                       expected_book_token: Annotated[Optional[str], Query(min_length=64, max_length=64)] = None,
                       expected_uid: Annotated[Optional[str], Query(max_length=128)] = None):
    """Delete a chunk at the given index."""
    result = await asyncio.to_thread(_apply_chunk_edit, apply_chunk_deletion, index,
                                    expected_book_token, expected_uid)
    if result is None:
        raise HTTPException(status_code=400, detail="Cannot delete chunk (invalid index or last remaining chunk)")
    deleted, chunks, token = result
    return {"status": "ok", "deleted": deleted, "undo_token": token, "total": len(chunks)}

@router.post("/api/chunks/{index}/generate")
async def generate_chunk_endpoint(index: int, background_tasks: BackgroundTasks,
                                  expected_book_token: Annotated[Optional[str], Query(min_length=64, max_length=64)] = None,
                                  expected_uid: Annotated[Optional[str], Query(min_length=1, max_length=128)] = None):
    with ensure_audio_generation_claim() as claim_id:
        try:
            _indices, requested = await asyncio.to_thread(
                ensure_audio_generation_requests, [index], expected_book_token,
                [expected_uid] if expected_uid is not None else None)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Invalid chunk index") from exc
        if not requested[index].get("text", "").strip():
            raise HTTPException(status_code=400, detail="Cannot generate audio for an empty line")

        def task():
            state = process_state["audio"]
            state["start_time"] = time.time()
            state["logs"] = [f"Generating chunk {index}..."]
            state["status"] = "running"
            state["error"] = None
            try:
                success, message = project_manager.generate_chunk_audio(index, admitted_chunk=requested[index])
                cancelled = not success and message == AUDIO_GENERATION_CANCELLED
                state["status"] = "cancelled" if cancelled else ("done" if success else "failed")
                state["error"] = None if success or cancelled else message
                state["logs"].append(
                    f"Generation {'complete' if success else 'failed'}: {message}")
            except Exception as exc:
                logger.exception("Single-chunk generation error")
                state["status"] = "failed"
                state["error"] = str(exc)
                state["logs"].append(f"Generation failed: {exc}")
            finally:
                state["running"] = False

        # Same GPU resource as /api/generate_batch - must not race it. See F-032.
        register_claimed_background_task(background_tasks, "audio", claim_id, task)
        return {"status": "started"}

class MergeRequest(BaseModel):
    export_quality: Optional[ExportQualityOptions] = None
    mastering: Optional[MasteringOptions] = None
    integrity_confirmation: Optional[str] = Field(None, min_length=64, max_length=64,
                                                  pattern="^[0-9a-f]{64}$")


def _ensure_editor_integrity_locked():
    """Caller holds book state; native chunk loading may initialize missing rows."""
    chunks = project_manager.load_chunks()
    state = safe_load_json(os.path.join(DATA_DIR, "state.json"), {})
    try:
        script_bytes = Path(SCRIPT_PATH).read_bytes()
    except FileNotFoundError:
        script_bytes = b""
    return get_source_integrity(state, script_bytes, chunks), chunks


def _ensure_editor_integrity():
    with ensure_book_state(DATA_DIR):
        report, _chunks = _ensure_editor_integrity_locked()
        return report


@router.get("/api/editor/integrity")
async def get_editor_integrity():
    return await asyncio.to_thread(_ensure_editor_integrity)


def get_export_quality_attempt(request, state, *, audacity=False):
    # A cancelled queued worker must not start book I/O for evidence capture.
    ensure_audio_export_active(lambda: state.get('cancel', False))
    with ensure_book_state(DATA_DIR):
        token = get_snapshot_digest(get_editor_book_identity(DATA_DIR))
    options = getattr(request, 'export_quality', None)
    return ExportQualityAttempt(state, token,
        options.model_dump() if options is not None else None, audacity=audacity)


def record_export_quality_error(attempt, error):
    if attempt is None:
        return
    if isinstance(error, ExportCancelled):
        attempt.finish(False, 'Export cancelled')
    else:
        attempt.fail(error)


@router.get('/api/export_quality')
async def get_export_quality(expected_book_token: Annotated[str, Query(pattern=r'^[0-9a-f]{64}$')]):
    return await asyncio.to_thread(_get_export_quality_sync, expected_book_token)


def _get_export_quality_sync(token):
    with ensure_book_state(DATA_DIR):
        check_chunk_identity(0, token)
    result = get_session_evidence(DATA_DIR, process_state, token)
    with ensure_book_state(DATA_DIR):
        check_chunk_identity(0, token)
    return result


def ensure_export_mastering_context(request, state):
    """Initialize measurements only after this worker owns its task slot."""
    state['mastering_reports'] = []
    if request is None or request.mastering is None:
        return {}
    with ensure_book_state(DATA_DIR):
        token = get_snapshot_digest(get_editor_book_identity(DATA_DIR))
    def publish(report):
        state['mastering_reports'].append({**report, 'book_token': token})
    return {'mastering': request.mastering.model_dump(), 'mastering_callback': publish}


@router.get('/api/export_mastering')
async def get_export_mastering(expected_book_token: Annotated[str, Query(pattern=r'^[0-9a-f]{64}$')]):
    return await asyncio.to_thread(_get_export_mastering_sync, expected_book_token)


def _get_export_mastering_sync(token):
    with ensure_book_state(DATA_DIR):
        check_chunk_identity(0, token)
        captured = [(name, copy.deepcopy(state.get('mastering_reports', [])))
                    for name in ('audio', 'm4b_export', 'chapter_export')
                    if not (state := process_state[name]).get('running')]
    reports, unavailable = [], 0
    for name, candidates in captured:
        for report in candidates:
            if report.get('book_token') != token:
                continue
            path = os.path.join(DATA_DIR, report['file'])
            if not is_path_inside(path, DATA_DIR):
                unavailable += 1
                continue
            try:
                stat = os.stat(path)
                before = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                digest = get_audio_sha256(path)
                stat = os.stat(path)
                after = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                if digest != report['sha256'] or before != after:
                    unavailable += 1
                    continue
            except OSError:
                unavailable += 1
                continue
            # A newly admitted export invalidates an older measurement snapshot.
            state = process_state[name]
            if state.get('running') or report not in state.get('mastering_reports', []):
                unavailable += 1
                continue
            reports.append(report)
    with ensure_book_state(DATA_DIR):
        check_chunk_identity(0, token)
    return {'book_token': token, 'reports': reports, 'unavailable': unavailable}


@router.post("/api/merge")
async def merge_audio_endpoint(background_tasks: BackgroundTasks, request: Optional[MergeRequest] = None):
    return await asyncio.to_thread(_start_merge_audio, background_tasks, request)


def _start_merge_audio(background_tasks, request):
    with ensure_book_state(DATA_DIR):
        report, _chunks = _ensure_editor_integrity_locked()
        confirmation = request.integrity_confirmation if request is not None else None
        if ((confirmation is not None and confirmation != report["snapshot"])
                or (report["status"] != "verified" and confirmation is None)):
            raise HTTPException(status_code=409, detail=report)
        admitted_snapshot = report["snapshot"]

        def task():
            process_state["audio"]["start_time"] = time.time()
            process_state["audio"]["logs"] = ["Starting merge..."]
            quality_attempt = None
            try:
                with ensure_book_state(DATA_DIR):
                    current, chunks = _ensure_editor_integrity_locked()
                    if current["snapshot"] != admitted_snapshot:
                        raise ValueError("Source or editor chunks changed after merge admission; review again.")
                quality_attempt = get_export_quality_attempt(request, process_state["audio"])
                success, msg = project_manager.merge_audio(
                    cancel_check=lambda: process_state["audio"]["cancel"],
                    progress_callback=lambda m: process_state["audio"]["logs"].append(m),
                    chunks=chunks, **ensure_export_mastering_context(request, process_state["audio"]),
                    **quality_attempt.kwargs)
                quality_attempt.finish(success, msg)
                if success:
                    process_state["audio"]["logs"].append(f"Merge complete: {msg}")
                elif msg == "Merge cancelled":
                    process_state["audio"]["logs"].append("Merge cancelled")
                else:
                    process_state["audio"]["logs"].append(f"Merge failed: {msg}")
            except Exception as e:
                record_export_quality_error(quality_attempt, e)
                process_state["audio"]["logs"].append('Merge cancelled' if isinstance(e, ExportCancelled)
                                                    else f"Merge error: {e}")
            finally:
                process_state["audio"]["running"] = False
                process_state["audio"]["cancel"] = False

        # Keep the existing atomic owned GPU/TTS admission after integrity approval.
    schedule_claimed_background_task(background_tasks, "audio", task)
    return {"status": "started"}

def get_export_task_result(success, message):
    """Adapt the project's success flag and exact cancellation sentinel, not logs."""
    return {"status": "done" if success else ("cancelled" if message == "Export cancelled" else "failed"),
            "message": str(message)}


def get_export_task_error(error):
    if isinstance(error, ExportCancelled):
        return {'status': 'cancelled', 'message': 'Export cancelled'}
    return {'status': 'failed', 'message': str(error)}


@router.post("/api/export_audacity")
async def export_audacity_endpoint(background_tasks: BackgroundTasks):
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).
    # audacity_export is a NON_GPU_TASK, so this only guards against self-double-start.

    def task():
        process_state["audacity_export"]["logs"] = ["Starting Audacity export..."]
        quality_attempt = None
        try:
            quality_attempt = get_export_quality_attempt(None, process_state["audacity_export"], audacity=True)
            success, msg = project_manager.export_audacity(
                progress_callback=lambda m: process_state["audacity_export"]["logs"].append(m),
                **quality_attempt.kwargs)
            quality_attempt.finish(success, msg)
            process_state["audacity_export"]["result"] = get_export_task_result(success, msg)
            if success:
                process_state["audacity_export"]["logs"].append(f"Export complete: {msg}")
            else:
                process_state["audacity_export"]["logs"].append(f"Export failed: {msg}")
        except Exception as e:
            record_export_quality_error(quality_attempt, e)
            process_state["audacity_export"]["result"] = get_export_task_error(e)
            process_state["audacity_export"]["logs"].append(f"Export error: {e}")
        finally:
            process_state["audacity_export"]["running"] = False

    schedule_claimed_background_task(background_tasks, "audacity_export", task)
    process_state["audacity_export"]["result"] = None
    return {"status": "started"}

@router.get("/api/export_zip")
async def export_zip():
    return await asyncio.to_thread(_build_export_zip_response)


def _build_export_zip_response():
    """One zip of whatever exported audio exists (MP3, M4B); 404 when nothing does."""
    members = [(path, name) for path, name in ((AUDIOBOOK_PATH, "audiobook.mp3"), (M4B_PATH, "audiobook.m4b"))
               if os.path.exists(path)]
    if not members:
        raise HTTPException(status_code=404, detail="No exported audio found. Merge or export first.")
    for path, _name in members:
        require_scope_export_current(DATA_DIR, path)
    return build_zip_download(members, "alexandria_export.zip")

@router.get("/api/export_audacity")
async def get_audacity_export():
    zip_path = os.path.join(DATA_DIR, "audacity_export.zip")
    if not os.path.exists(zip_path):
        raise HTTPException(status_code=404, detail="Audacity export not found. Generate it first.")
    require_scope_export_current(DATA_DIR, zip_path)
    return FileResponse(zip_path, filename="audacity_export.zip", media_type="application/zip")

def get_unready_export_speakers():
    """Read readiness without treating unusable state as an empty roster."""
    script = safe_load_json(SCRIPT_PATH)
    voices = safe_load_json(os.path.join(DATA_DIR, "voice_config.json"))
    if not isinstance(script, list):
        raise HTTPException(status_code=503, detail="Export readiness unavailable: active script must be a JSON array")
    if not isinstance(voices, dict):
        raise HTTPException(status_code=503, detail="Export readiness unavailable: voice config must be a JSON object")
    for speaker, config in voices.items():
        if not isinstance(config, dict):
            raise HTTPException(status_code=503, detail=f"Export readiness unavailable: voice config for {speaker} must be an object")
    chunks_path = os.path.join(DATA_DIR, "chunks.json")
    chunks = safe_load_json(chunks_path) if os.path.exists(chunks_path) else None
    if chunks is None and not any(config.get("version_timeline") for config in voices.values()):
        chunks = script
    if not isinstance(chunks, list) or any(not isinstance(chunk, dict) for chunk in chunks):
        raise HTTPException(status_code=503, detail="Export readiness unavailable: rebuild the active chunks")
    missing = set()
    try:
        for index, chunk in enumerate(chunks):
            speaker = get_script_speaker(chunk)
            if not speaker:
                continue
            canonical = project_manager._resolve_alias(speaker, voices)
            effective = voice_config_for_chunk(
                resolve_narrator_voice_config(canonical, voices, chunk), canonical, index)
            config = effective.get(canonical) or {}
            if not config.get("ready"):
                missing.add(speaker)
            if voice_category(config) == "ensemble":
                for member in config.get("members") or []:
                    if not (effective.get(member) or {}).get("ready"):
                        missing.add(speaker)
    except (TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=503, detail="Export readiness unavailable: invalid effective voice configuration") from exc
    missing = sorted(missing)
    return missing


async def validate_export_readiness(require_ready):
    if require_ready:
        missing = await asyncio.to_thread(get_unready_export_speakers)
        if missing:
            raise HTTPException(status_code=409, detail={
                "message": "Mark every speaker ready before exporting.", "speakers": missing})


class M4bExportRequest(BaseModel):
    export_quality: Optional[ExportQualityOptions] = None
    mastering: Optional[MasteringOptions] = None
    per_chunk_chapters: bool = False
    title: str = ""
    author: str = ""
    narrator: str = ""
    year: str = ""
    description: str = ""
    require_ready: bool = False

@router.post("/api/merge_m4b")
async def merge_m4b_endpoint(request: M4bExportRequest, background_tasks: BackgroundTasks):
    await validate_export_readiness(request.require_ready)
    # Atomic check-and-set on the request thread (closes the double-start TOCTOU
    # where two rapid POSTs both pass a plain running check before either sets it).

    def task():
        process_state["m4b_export"]["logs"] = ["Starting M4B export..."]
        quality_attempt = None
        try:
            quality_attempt = get_export_quality_attempt(request, process_state["m4b_export"])
            meta = {
                "title": request.title,
                "author": request.author,
                "narrator": request.narrator,
                "year": request.year,
                "description": request.description,
                "cover_path": os.path.join(DATA_DIR, "m4b_cover.jpg") if os.path.exists(os.path.join(DATA_DIR, "m4b_cover.jpg")) else "",
            }
            success, msg = project_manager.merge_m4b(
                per_chunk_chapters=request.per_chunk_chapters, metadata=meta,
                cancel_check=lambda: process_state["m4b_export"].get("cancel", False),
                progress_callback=lambda message: process_state["m4b_export"]["logs"].append(message),
                **ensure_export_mastering_context(request, process_state["m4b_export"]),
                **quality_attempt.kwargs)
            quality_attempt.finish(success, msg)
            process_state["m4b_export"]["result"] = get_export_task_result(success, msg)
            if success:
                process_state["m4b_export"]["logs"].append(f"Export complete: {msg}")
            else:
                process_state["m4b_export"]["logs"].append(f"Export failed: {msg}")
        except Exception as e:
            record_export_quality_error(quality_attempt, e)
            process_state["m4b_export"]["result"] = get_export_task_error(e)
            process_state["m4b_export"]["logs"].append(f"Export error: {e}")
        finally:
            process_state["m4b_export"]["running"] = False

    schedule_claimed_background_task(background_tasks, "m4b_export", task)
    process_state["m4b_export"]["result"] = None
    return {"status": "started"}

def _cancel_cpu_export(task_name, missing_message):
    with _gpu_lock:
        state = process_state[task_name]
        if not state["running"]:
            raise HTTPException(status_code=400, detail=missing_message)
        state["cancel"] = True
    return {"status": "cancelling"}


@router.post("/api/merge_m4b/cancel")
async def cancel_m4b_export():
    return await asyncio.to_thread(_cancel_cpu_export, 'm4b_export', 'No M4B export is running.')


class ChapterExportRequest(BaseModel):
    export_quality: Optional[ExportQualityOptions] = None
    mastering: Optional[MasteringOptions] = None
    format: str = "mp3"
    per_chunk_chapters: bool = False
    template: str = DEFAULT_CHAPTER_TEMPLATE
    padding: int = Field(2, ge=0, le=6)
    book_name: str = ""
    series_name: str = ""
    volume_number: str = ""
    chapters: Optional[List[StrictInt]] = None     # indices to export; None = all
    changed_only: bool = False     
    require_ready: bool = False


def _chapter_export_dir():
    return os.path.join(DATA_DIR, CHAPTER_EXPORT_DIR)


@router.post("/api/export_chapters")
async def export_chapters(request: ChapterExportRequest, background_tasks: BackgroundTasks):
    """Write chapters as separate MP3/WAV files (CPU only, no GPU lock)."""
    if request.format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp3 or wav")
    await validate_export_readiness(request.require_ready)
    chapters = request.chapters
    if chapters is not None:
        try:
            preview = await asyncio.to_thread(
                project_manager.preview_chapter_filenames,
                fmt=request.format, per_chunk_chapters=request.per_chunk_chapters,
                template=request.template, padding=request.padding,
                book_name=request.book_name, series_name=request.series_name,
                volume_number=request.volume_number)
            chapters = get_index_selection(chapters, len(preview))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    state = process_state["chapter_export"]

    def task():
        state["logs"] = ["Starting chapter export..."]
        quality_attempt = None
        try:
            quality_attempt = get_export_quality_attempt(request, state)
            success, msg = project_manager.export_chapters(
                fmt=request.format, per_chunk_chapters=request.per_chunk_chapters,
                template=request.template, padding=request.padding,
                book_name=request.book_name, series_name=request.series_name,
                volume_number=request.volume_number, chapters=chapters,
                changed_only=request.changed_only,
                progress_callback=lambda m: state["logs"].append(m),
                cancel_check=lambda: state["cancel"], **ensure_export_mastering_context(request, state),
                **quality_attempt.kwargs)
            quality_attempt.finish(success, msg)
            state["result"] = get_export_task_result(success, msg)
            state["logs"].append(f"Export complete: {msg}" if success else f"Export failed: {msg}")
        except Exception as e:
            record_export_quality_error(quality_attempt, e)
            state["result"] = get_export_task_error(e)
            state["logs"].append(f"Export error: {e}")
        finally:
            state["running"] = False
            state["cancel"] = False

    schedule_claimed_background_task(background_tasks, "chapter_export", task)
    state["result"] = None
    return {"status": "started"}


@router.post("/api/export_chapters/cancel")
async def cancel_chapter_export():
    return await asyncio.to_thread(_cancel_cpu_export, 'chapter_export', 'No chapter export is running.')


@router.get("/api/export_chapters/preview")
async def preview_chapter_filenames(format: str = "mp3", per_chunk_chapters: bool = False,
                                    template: str = DEFAULT_CHAPTER_TEMPLATE, padding: int = 2,
                                    book_name: str = "", series_name: str = "",
                                    volume_number: str = "", chapters: Annotated[Optional[List[int]], Query()] = None,
                                    changed_only: bool = False, require_ready: bool = False,
                                    mastering: Annotated[Optional[str], Query(max_length=512)] = None):
    """The selected filenames an export would write after existing-file reuse."""
    if format not in ("mp3", "wav"):
        raise HTTPException(status_code=400, detail="format must be mp3 or wav")
    if not 0 <= padding <= 6:
        raise HTTPException(status_code=400, detail="padding must be 0-6 digits")
    await validate_export_readiness(require_ready)
    try:
        mastering_options = MasteringOptions.model_validate_json(mastering).model_dump() if mastering is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail='Invalid mastering options.') from exc
    try:
        rows = await asyncio.to_thread(project_manager.preview_chapter_filenames,
            fmt=format, per_chunk_chapters=per_chunk_chapters, template=template,
            padding=padding, book_name=book_name,
            series_name=series_name, volume_number=volume_number,
            chapters=chapters, changed_only=changed_only,
            **({"mastering": mastering_options} if mastering_options is not None else {}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"fields": list(CHAPTER_TEMPLATE_FIELDS), "chapters": rows}


@router.get("/api/chapter_exports")
async def list_chapter_exports():
    return await asyncio.to_thread(_get_chapter_export_listing)


def _get_chapter_export_listing():
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    rows = []
    for row in get_chapter_export_rows(manifest):
        try:
            path, _ = _exported_chapter_path(row["file"])
        except HTTPException:
            rows.append({**row, "exists": False, "bytes": 0})
        else:
            rows.append({**row, "exists": True, "bytes": os.path.getsize(path)})
    return {**manifest, "chapters": rows}


def _exported_chapter_path(name):
    safe = os.path.basename(name)
    path = os.path.join(_chapter_export_dir(), safe)
    if (safe != name or "\\" in name
            or os.path.splitext(safe)[1].lower() not in {".mp3", ".wav"}
            or not is_path_inside(path, _chapter_export_dir())
            or not os.path.isfile(path)):
        raise HTTPException(status_code=404, detail="No such exported chapter")
    require_scope_export_current(DATA_DIR, path)
    return path, safe


@router.get("/api/chapter_exports/file/{name}")
async def download_chapter(name: str):
    path, safe = await asyncio.to_thread(_get_listed_chapter_path, name)
    media = "audio/mpeg" if safe.lower().endswith(".mp3") else "audio/wav"
    return FileResponse(path, filename=safe, media_type=media)


def _get_listed_chapter_path(name):
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    if not any(row["file"] == name for row in get_chapter_export_rows(manifest)):
        raise HTTPException(status_code=404, detail="No such exported chapter")
    return _exported_chapter_path(name)


@router.get("/api/chapter_exports/zip")
async def download_chapters_zip(names: Optional[str] = None):
    return await asyncio.to_thread(_build_chapter_zip_response, names)


def _build_chapter_zip_response(names: Optional[str] = None):
    """All exported chapters, or the comma-separated `names`, in one zip."""
    manifest = safe_load_json(os.path.join(_chapter_export_dir(), "manifest.json"), {}) or {}
    wanted = set(n for n in (names or "").split(",") if n) or None
    members = []
    for row in get_chapter_export_rows(manifest):
        if wanted is not None and row["file"] not in wanted:
            continue
        try:
            members.append(_exported_chapter_path(row["file"]))
        except HTTPException:
            continue
    if not members:
        raise HTTPException(status_code=404, detail="No exported chapters found. Export first.")
    return build_zip_download(
        members,
        "chapters.zip")


@router.get("/api/audiobook_m4b")
async def get_audiobook_m4b():
    if not os.path.exists(M4B_PATH):
        raise HTTPException(status_code=404, detail="M4B audiobook not found. Export it first.")
    require_scope_export_current(DATA_DIR, M4B_PATH)
    return FileResponse(M4B_PATH, filename="audiobook.m4b", media_type="audio/mp4")

def _apply_m4b_cover(cover_tmp, cover_path):
    """Validate uploaded image data before publishing an MP4-compatible cover."""
    from PIL import Image, ImageOps, UnidentifiedImageError
    normalized = cover_tmp + ".jpg"
    try:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(cover_tmp) as image:
                    image.verify()
                with Image.open(cover_tmp) as image:
                    image.load()
                    cover_format = "PNG" if image.format == "PNG" else "JPEG"
                    converted = ImageOps.exif_transpose(image)
                    if cover_format == "JPEG" and converted.mode != "RGB":
                        try:
                            rgb = converted.convert("RGB")
                        finally:
                            converted.close()
                        converted = rgb
        except (UnidentifiedImageError, OSError, ValueError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise HTTPException(status_code=400, detail="Cover must contain a complete, decodable image") from exc
        try:
            converted.save(normalized, format=cover_format)
        finally:
            converted.close()
        os.replace(normalized, cover_path)
    finally:
        for path in (cover_tmp, normalized):
            if os.path.exists(path):
                os.remove(path)


@router.post("/api/m4b_cover")
async def upload_m4b_cover(file: UploadFile = File(...)):
    """Upload a cover image for M4B export."""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
    cover_tmp = cover_path + f".upload.{uuid.uuid4().hex}"
    try:
        await _save_upload_limited(file, cover_tmp, 25 * 1024**2)
        await asyncio.to_thread(_apply_m4b_cover, cover_tmp, cover_path)
    finally:
        if os.path.exists(cover_tmp):
            os.remove(cover_tmp)
    return {"status": "uploaded", "path": "m4b_cover.jpg"}

@router.delete("/api/m4b_cover")
async def delete_m4b_cover():
    """Remove the uploaded cover image."""
    cover_path = os.path.join(DATA_DIR, "m4b_cover.jpg")
    if os.path.exists(cover_path):
        os.remove(cover_path)
    return {"status": "removed"}

@router.post("/api/generate_batch")
async def generate_batch_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks in parallel using configured worker count."""
    check_global_gpu_lock("audio")
    with ensure_audio_generation_claim() as claim_id:

        # Load worker count from config
        workers = 2
        cfg = await asyncio.to_thread(load_app_config, CONFIG_PATH)
        workers = max(1, cfg.get("tts", {}).get("parallel_workers", 2))

        try:
            indices, requested = await asyncio.to_thread(
                ensure_audio_generation_requests, request.indices,
                request.expected_book_token, request.expected_uids, request.expected_repair_revision)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        total = len(indices)

        def progress_callback(completed, failed, total):
            """Update logs with progress."""
            process_state["audio"]["logs"].append(
                f"Progress: {completed + failed}/{total} ({completed} done, {failed} failed)"
            )

        def cancel_check():
            return process_state["audio"]["cancel"]

        def task():
            process_state["audio"]["running"] = True
            process_state["audio"]["start_time"] = time.time()
            process_state["audio"]["logs"] = [
                f"Starting parallel generation of {total} chunks with {workers} workers..."
            ]
            try:
                results = project_manager.generate_chunks_parallel(
                    indices, workers, progress_callback, cancel_check=cancel_check,
                    admitted_requests=requested
                )
                completed = len(results["completed"])
                failed = len(results["failed"])
                cancelled = results.get("cancelled", 0)
                msg = f"Batch generation complete: {completed} succeeded, {failed} failed"
                if cancelled:
                    msg += f", {cancelled} cancelled"
                process_state["audio"]["logs"].append(msg)
                if results["failed"]:
                    for idx, err in results["failed"]:
                        process_state["audio"]["logs"].append(f"  Chunk {idx} failed: {err}")
            except Exception as e:
                logger.exception("Batch generation error")
                process_state["audio"]["logs"].append(f"Batch generation error: {e}")
            finally:
                process_state["audio"]["running"] = False
                process_state["audio"]["cancel"] = False

        register_claimed_background_task(background_tasks, "audio", claim_id, task)
        return {"status": "started", "workers": workers, "total_chunks": total}


@router.post("/api/generate_batch_fast")
async def generate_batch_fast_endpoint(request: BatchGenerateRequest, background_tasks: BackgroundTasks):
    """Generate multiple chunks using batch TTS API with single seed. Faster but less flexible.
    Requires custom Qwen3-TTS with /generate_batch endpoint."""
    check_global_gpu_lock("audio")
    with ensure_audio_generation_claim() as claim_id:

        # Load batch_seed and batch_size from config
        batch_seed = -1
        batch_size = 4
        batch_group_by_type = False
        cfg = await asyncio.to_thread(load_app_config, CONFIG_PATH)
        try:
            tts_cfg = cfg.get("tts", {})
            seed_val = tts_cfg.get("batch_seed")
            if seed_val is not None and seed_val != "":
                batch_seed = int(seed_val)
            batch_size = max(1, tts_cfg.get("parallel_workers", 4))
            batch_group_by_type = tts_cfg.get("batch_group_by_type", False)
        except (TypeError, ValueError) as e:
            _warn_corrupted_json("config", CONFIG_PATH, "using default batch settings", e)

        try:
            indices, requested = await asyncio.to_thread(
                ensure_audio_generation_requests, request.indices,
                request.expected_book_token, request.expected_uids, request.expected_repair_revision)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        total = len(indices)

        def progress_callback(completed, failed, total):
            process_state["audio"]["logs"].append(
                f"Progress: {completed + failed}/{total} ({completed} done, {failed} failed)"
            )

        def cancel_check():
            return process_state["audio"]["cancel"]

        def task():
            process_state["audio"]["running"] = True
            process_state["audio"]["start_time"] = time.time()
            process_state["audio"]["logs"] = [
                f"Starting batch generation of {total} chunks (batch_size={batch_size}, seed={batch_seed})..."
            ]
            try:
                results = project_manager.generate_chunks_batch(
                    indices, batch_seed, batch_size, progress_callback,
                    batch_group_by_type=batch_group_by_type,
                    cancel_check=cancel_check, admitted_requests=requested,
                )
                completed = len(results["completed"])
                failed = len(results["failed"])
                cancelled = results.get("cancelled", 0)
                msg = f"Batch generation complete: {completed} succeeded, {failed} failed"
                if cancelled:
                    msg += f", {cancelled} cancelled"
                process_state["audio"]["logs"].append(msg)
                if results["failed"]:
                    for idx, err in results["failed"]:
                        process_state["audio"]["logs"].append(f"  Chunk {idx} failed: {err}")
            except Exception as e:
                logger.exception("Batch generation error")
                process_state["audio"]["logs"].append(f"Batch generation error: {e}")
            finally:
                process_state["audio"]["running"] = False
                process_state["audio"]["cancel"] = False

        register_claimed_background_task(background_tasks, "audio", claim_id, task)
        return {"status": "started", "batch_seed": batch_seed, "batch_size": batch_size, "total_chunks": total}

@router.post("/api/cancel_audio")
async def cancel_audio():
    return await asyncio.to_thread(_apply_audio_cancel)


def _apply_audio_cancel():
    """Cancel ongoing audio generation and reset in-progress chunks."""
    # Keep the idle decision and disk reset atomic with new audio claims.
    with _gpu_lock:
        if process_state["audio"]["running"]:
            process_state["audio"]["cancel"] = True
            process_state["audio"]["logs"].append("[CANCEL] Cancellation requested")
            return {"status": "cancelling"}

        # A foreign server may own the render even when our local flag is idle.
        # Hold admission closed through recovery and update only still-generating
        # UIDs, never replace a full snapshot over newer journal completions.
        reset_count = 0
        try:
            with ensure_startup_recovery(DATA_DIR, timeout=0) as idle:
                if not idle:
                    raise HTTPException(status_code=409, detail="Another worker owns an active task; cancel it on its owning server")
                with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
                    for chunk in project_manager.load_chunks():
                        if chunk.get("status") == "generating":
                            updated = project_manager._update_chunk_fields_by_uid(
                                chunk.get("uid"), expected_chunk=chunk,
                                required_status="generating", status="pending")
                            if updated is not None:
                                reset_count += 1
        except TimeoutError as exc:
            raise HTTPException(status_code=409, detail="Task admission is busy; retry cancellation") from exc
        return {"status": "not_running", "reset_chunks": reset_count}

## ── Saved Scripts ──────────────────────────────────────────────

@router.get("/api/reports")
async def list_reports():
    return await asyncio.to_thread(_get_reports)


def _get_report_path(filename: str):
    """Resolve one regular Markdown report within the configured report root."""
    if (os.path.basename(filename) != filename or "\\" in filename
            or not filename.endswith(".md")):
        raise HTTPException(status_code=400, detail="Invalid report filename.")
    filepath = os.path.realpath(os.path.join(REPORTS_DIR, filename))
    if not is_path_inside(filepath, REPORTS_DIR):
        raise HTTPException(status_code=400, detail="Invalid report path.")
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Report not found.")
    return filepath


def _get_reports():
    """List all generated review reports in the reports/ directory, newest first."""
    if not os.path.isdir(REPORTS_DIR):
        return []
    reports = []
    for f in os.listdir(REPORTS_DIR):
        if not f.endswith(".md"):
            continue
        try:
            filepath = _get_report_path(f)
            entry = {
                "filename": f,
                "type": "batch" if f.startswith("batch_review_") else "review",
                "mtime": os.path.getmtime(filepath),
                "size": os.path.getsize(filepath),
            }
        except (OSError, HTTPException):
            # File vanished between listdir and stat (concurrent delete) - skip it.
            continue
        entry["can_explain"] = False
        try:
            entry["can_explain"] = not get_review_report_info(filepath)["incomplete"]
        except (OSError, ValueError):
            # Legacy, edited and incomplete records remain readable in the list.
            pass
        reports.append(entry)
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    return reports


@router.get("/api/reports/{filename}")
async def get_report(filename: str):
    return await asyncio.to_thread(_get_report_response, filename)


@router.post("/api/reports/{filename}/explain")
async def explain_report(filename: str, background_tasks: BackgroundTasks):
    filepath = _get_report_path(filename)
    try:
        info = await asyncio.to_thread(get_review_report_info, filepath)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if info["incomplete"]:
        raise HTTPException(status_code=409, detail="Incomplete reviews retain their deterministic summary")

    def run():
        from manual_llm_context import ManualRequestCancelled, apply_manual_cancellation
        state = process_state["report_explanation"]
        state.update(status="running", filename=filename, error=None, logs=[])
        try:
            if state.get("cancel"):
                state["status"] = "cancelled"
                return
            with apply_manual_cancellation(lambda: state.get("cancel")):
                candidate = _llm_summarize_report(info["content"])
            if state.get("cancel"):
                state["status"] = "cancelled"
                return
            if not candidate or not _is_evidence_bound_summary(candidate):
                raise ValueError("Explanation unavailable or unsupported; the original report was retained")
            apply_review_report_explanation(filepath, info["sha256"], candidate)
            state["status"] = "done"
            state["logs"].append("Report explanation saved")
        except ManualRequestCancelled:
            state["status"] = "cancelled"
        except Exception as exc:
            state["status"] = "failed"
            state["error"] = str(exc)
            state["logs"].append(str(exc))

    run_id = schedule_claimed_background_task(background_tasks, "report_explanation", run)
    process_state["report_explanation"].update(run_id=run_id, filename=filename,
                                              status="pending", error=None, logs=[])
    return {"status":"started", "filename":filename, "run_id":run_id}


class ReportExplanationCancelRequest(BaseModel):
    run_id: str


@router.post("/api/reports/explanation/cancel")
async def cancel_report_explanation(request: ReportExplanationCancelRequest):
    state = process_state["report_explanation"]
    if not state["running"] or state.get("run_id") != request.run_id:
        raise HTTPException(status_code=409, detail="This explanation run is no longer active")
    state["cancel"] = True
    return {"status":"cancel_requested"}


def _get_report_response(filename: str):
    """Return the raw Markdown contents of a generated report."""
    filepath = _get_report_path(filename)
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    return PlainTextResponse(content, media_type="text/markdown")


def _summarize_review_checkpoint(path: str) -> Optional[dict]:
    """Summarize a *.review_checkpoint.json for the UI: how far it got and where
    a resumed review would pick up. Returns None if the file isn't a usable
    checkpoint."""
    try:
        data = load_generation_checkpoint_document(path)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "completed_batches" not in data:
        return None
    completed = data.get("completed_batches")
    total = data.get("total_batches", 0)
    failed = data.get("failed_batches")
    batch_lengths = data.get("batch_lengths")
    stats = data.get("total_stats")
    corrected = data.get("all_corrected")
    # Legacy optional null fields still mean no progress; other shapes cannot
    # safely support arithmetic, sorting, or the UI's entry counts.
    completed = 0 if completed is None else completed
    total = 0 if total is None else total
    failed = [] if failed is None else failed
    batch_lengths = [] if batch_lengths is None else batch_lengths
    stats = {} if stats is None else stats
    corrected = [] if corrected is None else corrected
    if (not isinstance(completed, int) or not isinstance(total, int)
            or not isinstance(failed, list)
            or any(not isinstance(batch, int) for batch in failed)
            or not isinstance(batch_lengths, list)
            or not isinstance(stats, dict) or not isinstance(corrected, list)):
        return None
    failed = sorted(failed)
    # Mirror load_checkpoint's rewind: a failed batch (with full batch_lengths
    # coverage) rewinds the resume point back to the first failed batch.
    resume_from_batch = completed + 1
    if failed and len(batch_lengths) == completed:
        resume_from_batch = failed[0]
    try:
        mtime = os.path.getmtime(path)
        for directory in get_generation_checkpoint_artifacts(path)[1:]:
            mtime = max(mtime, os.path.getmtime(directory))
    except FileNotFoundError:
        return None
    return {
        "completed_batches": completed,
        "total_batches": total,
        "resume_from_batch": resume_from_batch,
        "entries_done": len(corrected),
        "batch_size": data.get("batch_size"),
        "context_window": data.get("context_window"),
        "failed_batches": failed,
        "batches_skipped_vram": stats.get("batches_skipped_vram", 0),
        "text_changed": stats.get("text_changed", 0),
        "speaker_changed": stats.get("speaker_changed", 0),
        "mtime": mtime,
    }


@router.get("/api/review/checkpoints")
async def list_review_checkpoints():
    """List saved review checkpoints (what's done + where a re-run resumes), plus
    the live pass/order if a batch review is currently running."""
    out = await asyncio.to_thread(_get_review_checkpoint_rows)

    # Live pass/order while a bidirectional batch is mid-flight.
    bstate = process_state.get("batch_review", {})
    live = None
    if bstate.get("running"):
        live = {
            "bidirectional": bstate.get("bidirectional", False),
            "current_pass": bstate.get("current_pass"),  # "fwd" / "bwd" / None
            "current_task_idx": bstate.get("current_task_idx"),
            "tasks": [
                {"name": t.get("name"), "status": t.get("status")}
                for t in bstate.get("tasks", []) if isinstance(t, dict)
            ],
        }
    return {"checkpoints": out, "live": live}


def _get_review_checkpoint_rows():
    out = []
    suffix = ".review_checkpoint.json"

    active_cp = SCRIPT_PATH + suffix
    if os.path.exists(active_cp):
        s = _summarize_review_checkpoint(active_cp)
        if s:
            out.append({"book": "(active script)", **s})

    if os.path.isdir(SCRIPTS_DIR):
        for f in sorted(os.listdir(SCRIPTS_DIR)):
            if not f.endswith(suffix):
                continue
            book = f[:-len(suffix)]
            if book.endswith(".json"):
                book = book[:-5]  # "{name}.json.review_checkpoint.json" -> "{name}"
            s = _summarize_review_checkpoint(os.path.join(SCRIPTS_DIR, f))
            if s:
                out.append({"book": book, **s})

    out.sort(key=lambda c: c.get("mtime") or 0, reverse=True)

    return out
