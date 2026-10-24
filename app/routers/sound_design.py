"""Asset-library operations independent of active-book selection."""
from typing import Annotated
import asyncio
from pathlib import Path as FilePath
import subprocess
import threading

from fastapi import APIRouter, HTTPException, Path, Query, File, UploadFile, Request
from fastapi.responses import Response

from core import DATA_DIR
from sound_design_assets import SoundDesignAssetStore, validate_sound_asset_display_name
from sound_design_audition import get_sound_asset_audition
from sound_design_cues import MAX_RATE, MAX_SECONDS
from sound_design_uploads import import_sound_asset_upload
from upload_operation import await_upload_operation
from audio_generation_owner import AudioGenerationCancelled
from audio_validation import GeneratedAudioError
from tts import ExportCancelled
from json_metadata import get_metadata_json_bytes
from sound_design_edit import (MAX_CUE_REQUEST_BYTES, CueRequestError,
                                run_sound_design_cue_operation, parse_sound_design_cue_request)

router = APIRouter()
AssetId = Annotated[str, Path(pattern=r'^[0-9a-f]{32}$')]


@router.post('/api/sound_design/assets/import')
async def import_sound_asset(request: Request, file: UploadFile = File(...)):
    if FilePath(file.filename or '').suffix.lower() not in {'.wav', '.flac', '.mp3', '.ogg'}:
        raise HTTPException(400, 'Choose WAV, FLAC, MP3 or OGG audio.')
    display_name = (file.filename or '').replace('\\', '/').rsplit('/', 1)[-1]
    try:
        validate_sound_asset_display_name(display_name)
    except ValueError as error:
        raise HTTPException(400, 'Use an audio filename of at most 128 characters without control characters.') from error
    cancelled = threading.Event()
    async def watch_disconnect():
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set()
                return
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        result, cancellation = await await_upload_operation(asyncio.to_thread(
            import_sound_asset_upload, DATA_DIR, file.file,
            display_name=display_name, cancel_check=cancelled.is_set),
            on_cancel=cancelled.set)
        if cancellation is not None:
            raise cancellation
        return result
    except (AudioGenerationCancelled, ExportCancelled) as error:
        raise HTTPException(409, 'Import cancelled. Refresh the library to check retained assets.') from error
    except (ValueError, OSError, GeneratedAudioError, subprocess.SubprocessError) as error:
        raise HTTPException(409, 'Sound asset import failed. Refresh the library to inspect retained incomplete assets.') from error
    finally:
        cancelled.set()
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


@router.get('/api/sound_design/assets')
def list_sound_assets():
    # Synchronous handlers run in FastAPI's worker pool, not the event loop.
    try:
        return SoundDesignAssetStore(DATA_DIR).list_assets()
    except (ValueError, OSError) as error:
        raise HTTPException(409, 'Sound asset library could not be read; retained files were preserved.') from error


@router.post('/api/sound_design/assets/{asset_id}/purge_incomplete')
def purge_incomplete_sound_asset(asset_id: AssetId):
    try:
        SoundDesignAssetStore(DATA_DIR).purge_incomplete(asset_id)
    except (ValueError, OSError) as error:
        raise HTTPException(409, 'Cleanup refused or incomplete; refresh the asset library before retrying.') from error
    return {'status': 'purged', 'asset_id': asset_id}


@router.get('/api/sound_design/assets/{asset_id}/audition')
def audition_sound_asset(asset_id: AssetId,
    sha256: Annotated[str, Query(pattern=r'^[0-9a-f]{64}$')],
    start_frame: Annotated[int, Query(ge=0, le=MAX_RATE * MAX_SECONDS)] = 0):
    try:
        audio = get_sound_asset_audition(DATA_DIR, asset_id, sha256, start_frame=start_frame)
    except (ValueError, OSError) as error:
        raise HTTPException(409, 'Sound asset audition could not be verified; refresh the asset library.') from error
    return Response(audio, media_type='audio/wav', headers={'Cache-Control': 'no-store'})


def _cue_response(operation, raw, selection, check_active, *, audio_owned=False):
    try:
        result = run_sound_design_cue_operation(DATA_DIR,operation,raw=raw,
                                               check_active=check_active,audio_owned=audio_owned,**selection)
        encoded = get_metadata_json_bytes(result,4*1024**2,'Cue response')
        return Response(encoded,media_type='application/json',headers={'Cache-Control':'no-store'})
    except CueRequestError:
        raise HTTPException(400,'Invalid cue request. Refresh the cue editor and check its fields.') from None
    except (AudioGenerationCancelled,ExportCancelled):
        raise HTTPException(409,'Cue operation cancelled. Refresh the cue editor to inspect saved state.') from None
    except (ValueError,OSError,TimeoutError,RecursionError,UnicodeError):
        # Snapshot proofs/configuration and source paths stay private.
        raise HTTPException(409,'Cue context is busy, changed or unavailable. Refresh the cue editor before retrying.') from None


async def _dispatch_cues(operation, request, **selection):
    raw = None
    if operation in ('edit','select'):
        parts = []; size = 0
        async for part in request.stream():
            size += len(part)
            if size > MAX_CUE_REQUEST_BYTES:
                raise HTTPException(413,'Cue request exceeds its 16 KiB limit.')
            parts.append(part)
        raw = b''.join(parts)
        try:
            parse_sound_design_cue_request(operation, raw)
        except CueRequestError:
            raise HTTPException(400, 'Invalid cue request. Refresh the cue editor and check its fields.') from None
        from tts import ensure_audio_export_active
        def owned(cancel_check):
            return _cue_response(operation, raw, selection,
                lambda: ensure_audio_export_active(cancel_check), audio_owned=True)
        return await _dispatch_sound_candidate(request, owned, busy_conflicts=True)
    return await _dispatch_sound_metadata(request, lambda check: _cue_response(
        operation, raw, selection, check))


async def _dispatch_sound_metadata(request, operation):
    cancelled = threading.Event()
    def check_active():
        if cancelled.is_set():
            raise AudioGenerationCancelled('Sound metadata operation cancelled')
    async def watch_disconnect():
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set(); return
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        response, cancellation = await await_upload_operation(asyncio.to_thread(
            operation,check_active),on_cancel=cancelled.set)
        if cancellation is not None:
            raise cancellation
        return response
    finally:
        cancelled.set(); watcher.cancel()
        await asyncio.gather(watcher,return_exceptions=True)


@router.get('/api/sound_design/cues')
async def get_sound_design_cues(request:Request,
    offset:Annotated[int,Query(ge=0,le=100000)]=0,
    limit:Annotated[int,Query(ge=1,le=200)]=100):
    return await _dispatch_cues('metadata',request,offset=offset,limit=limit)


@router.get('/api/sound_design/cues/anchor')
async def get_sound_design_cue_anchor(request:Request,
    context_revision:Annotated[str,Query(pattern=r'^[0-9a-f]{64}$')],
    chunk_uid:Annotated[str,Query(min_length=1,max_length=128)]):
    return await _dispatch_cues('anchor',request,expected_context=context_revision,chunk_uid=chunk_uid)


@router.post('/api/sound_design/cues/revise')
async def revise_sound_design_cue(request:Request):
    return await _dispatch_cues('edit',request)


@router.post('/api/sound_design/cues/select')
async def select_sound_design_cue(request:Request):
    return await _dispatch_cues('select',request)


# Full mixed candidates share the native audio task owner, but use CPU-only
# admission. All file hashing, decoding and mixing happen in its worker thread.
from typing import Literal
import json
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from core import run_claimed_task_worker
from json_metadata import get_unique_json_object
from export_quality import ExportQualityOptions
from audio_mastering import MasteringOptions
from sound_design_render import save_sound_design_candidate
from sound_design_candidate_audition import read_sound_candidate_audition
from sound_design_approval import save_sound_candidate_approval, MAX_APPROVAL_BYTES
from m4b_metadata import M4BDeliveryOptions
from sound_design_delivery import (SoundDeliveryIntegrityRequired, save_approved_sound_delivery,
                                   get_sound_source_review)

# Five native 20k-character metadata fields may use twelve JSON bytes per
# Unicode scalar when a client emits escaped surrogate pairs. Receipt storage
# remains separately bounded in compact UTF-8 by MAX_APPROVAL_BYTES.
MAX_APPROVAL_REQUEST_BYTES = 2 * 1024**2


class SoundRenderRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    context_revision: str = Field(pattern=r'^[0-9a-f]{64}$')


class SoundDeliveryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['wav','mp3','m4b']
    export_quality: ExportQualityOptions | None = None
    mastering: MasteringOptions | None = None
    m4b: M4BDeliveryOptions | None = None


class SoundApprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_approval: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    expected_encoding: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')
    full_mix_reviewed: StrictBool
    delivery: SoundDeliveryRequest


async def _read_sound_candidate_request(request, model, *, max_bytes=MAX_CUE_REQUEST_BYTES):
    parts = []; size = 0
    async for part in request.stream():
        size += len(part)
        if size > max_bytes:
            raise HTTPException(413,'Sound request exceeds its byte limit.')
        parts.append(part)
    try:
        value = json.loads(b''.join(parts),object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite sound request')))
        return model.model_validate(value)
    except (ValueError,UnicodeError,RecursionError,ValidationError):
        raise HTTPException(400,'Invalid sound candidate request.') from None


async def _dispatch_sound_candidate(request, operation, *, busy_conflicts=False):
    cancelled = threading.Event()
    started = threading.Event()
    def observed(cancel_check):
        started.set()
        return operation(cancel_check)
    async def watch_disconnect():
        while not cancelled.is_set():
            if await request.is_disconnected():
                cancelled.set(); return
            await asyncio.sleep(.1)
    watcher = asyncio.create_task(watch_disconnect())
    try:
        result, cancellation = await await_upload_operation(
            run_claimed_task_worker('audio',observed,cancelled.is_set,cpu_only=True),
            on_cancel=cancelled.set)
        if cancellation is not None:
            raise cancellation
        return result
    except (AudioGenerationCancelled,ExportCancelled):
        raise HTTPException(409,'Sound operation cancelled. Refresh saved candidates before retrying.') from None
    except HTTPException as error:
        # New metadata/media operations promise conflict status for admission
        # failures; request validation has already completed before this point.
        if busy_conflicts and not started.is_set() and error.status_code == 400:
            raise HTTPException(409, 'Sound operation is busy; refresh before retrying.') from None
        raise
    except SoundDeliveryIntegrityRequired as error:
        raise HTTPException(409,detail=error.report) from None
    except (ValueError,OSError,TimeoutError,subprocess.SubprocessError,GeneratedAudioError):
        raise HTTPException(409,'Sound candidate is stale, busy or unavailable. Refresh before retrying.') from None
    finally:
        cancelled.set(); watcher.cancel()
        await asyncio.gather(watcher,return_exceptions=True)


@router.post('/api/sound_design/candidates/render')
async def render_sound_candidate(request:Request):
    body = await _read_sound_candidate_request(request,SoundRenderRequest)
    def render(cancel_check):
        result = save_sound_design_candidate(DATA_DIR,body.context_revision,cancel_check=cancel_check)
        return {'stage':FilePath(result['directory']).name,
                'candidate_revision':result['candidate_revision'],'mix':result['mix']}
    return await _dispatch_sound_candidate(request,render)


CandidateStage = Annotated[str,Path(pattern=r'^render-[A-Za-z0-9_-]{1,128}$')]


@router.get('/api/sound_design/candidates/{stage}/audition')
async def audition_sound_candidate(request:Request,stage:CandidateStage,
    candidate_revision:Annotated[str,Query(pattern=r'^[0-9a-f]{64}$')],
    start_frame:Annotated[int,Query(ge=0,le=MAX_RATE*MAX_SECONDS)],
    end_frame:Annotated[int,Query(ge=1,le=MAX_RATE*MAX_SECONDS)]):
    def audition(cancel_check):
        result = read_sound_candidate_audition(DATA_DIR,stage,candidate_revision,
                                               start_frame,end_frame,cancel_check=cancel_check)
        return Response(result['wav'],media_type='audio/wav',headers={'Cache-Control':'no-store'})
    return await _dispatch_sound_candidate(request,audition)


@router.post('/api/sound_design/candidates/{stage}/approve')
async def approve_sound_candidate(request:Request,stage:CandidateStage):
    body = await _read_sound_candidate_request(request,SoundApprovalRequest,max_bytes=MAX_APPROVAL_REQUEST_BYTES)
    if not body.full_mix_reviewed:
        raise HTTPException(400,'Confirm review of the full mixed candidate before approval.')
    def approve(cancel_check):
        return save_sound_candidate_approval(DATA_DIR,stage,body.candidate_revision,
            body.delivery.model_dump(),full_mix_reviewed=body.full_mix_reviewed,
            expected_approval=body.expected_approval,expected_encoding=body.expected_encoding,cancel_check=cancel_check)
    return await _dispatch_sound_candidate(request,approve)


from sound_design_candidates import list_sound_candidates, discard_sound_candidate


@router.get('/api/sound_design/candidates')
async def get_sound_candidates(request:Request):
    return await _dispatch_sound_candidate(request,
        lambda cancel: list_sound_candidates(DATA_DIR,cancel_check=cancel))


@router.post('/api/sound_design/candidates/{stage}/discard')
async def discard_sound_candidate_endpoint(request:Request,stage:CandidateStage,
    recovery_revision:Annotated[str,Query(pattern=r'^[0-9a-f]{64}$')]):
    return await _dispatch_sound_candidate(request,
        lambda cancel: discard_sound_candidate(DATA_DIR,stage,recovery_revision,cancel_check=cancel))


class SoundPublishRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    approval_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    integrity_confirmation: str | None = Field(default=None,pattern=r'^[0-9a-f]{64}$')


class SoundSourceReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    approval_revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    expected_snapshot: str = Field(pattern=r'^[0-9a-f]{64}$')
    offset: int = Field(default=0, ge=0, le=2**53-1, strict=True)


@router.post('/api/sound_design/candidates/{stage}/source_review')
async def get_sound_source_review_endpoint(request:Request,stage:CandidateStage):
    body = await _read_sound_candidate_request(request,SoundSourceReviewRequest)
    def review(cancel_check):
        from project import ProjectManager
        return get_sound_source_review(ProjectManager(str(DATA_DIR)),stage,
            body.candidate_revision,body.approval_revision,body.expected_snapshot,
            body.offset,cancel_check=cancel_check)
    return await _dispatch_sound_candidate(request,review)


@router.post('/api/sound_design/candidates/{stage}/deliver')
async def deliver_sound_candidate(request:Request,stage:CandidateStage):
    body = await _read_sound_candidate_request(request,SoundPublishRequest)
    def deliver(cancel_check):
        from project import ProjectManager
        return save_approved_sound_delivery(ProjectManager(str(DATA_DIR)),stage,
            body.candidate_revision,body.approval_revision,
            integrity_confirmation=body.integrity_confirmation,cancel_check=cancel_check)
    return await _dispatch_sound_candidate(request,deliver)


@router.get('/api/sound_design/delivery/{format_name}/download')
def download_sound_delivery(format_name:Literal['mp3','wav','m4b'],
        sha256:Annotated[str,Query(pattern=r'^[0-9a-f]{64}$')]):
    # FastAPI runs synchronous file hashing/opening outside the event loop.
    from audio_stream_response import AudioStreamResponse
    from sound_design_download import open_sound_delivery
    try:
        stream,size,suffix = open_sound_delivery(DATA_DIR,format_name,sha256)
    except (ValueError,OSError):
        raise HTTPException(409,'Mixed delivery is missing or changed; export it again.') from None
    try:
        return AudioStreamResponse(stream,size,suffix,filename='mixed_audiobook'+suffix)
    except BaseException:
        stream.close()
        raise


# Score lineage metadata and owned media-use actions share the established
# response bounds, cancellation watchers and CPU-only audio worker dispatcher.
from sound_design_rendition_ops import (MAX_RENDITION_REQUEST_BYTES, RenditionRequestError,
    parse_sound_rendition_request, run_sound_rendition_operation)


def _rendition_response(operation, payload, selection, check_active):
    try:
        result = run_sound_rendition_operation(DATA_DIR, operation, payload=payload,
                                               check_active=check_active, **selection)
        encoded = get_metadata_json_bytes(result, 4*1024**2, 'Rendition response')
        return Response(encoded, media_type='application/json', headers={'Cache-Control': 'no-store'})
    except RenditionRequestError:
        raise HTTPException(400, 'Invalid rendition request. Refresh history and check the fields.') from None
    except (AudioGenerationCancelled, ExportCancelled):
        raise HTTPException(409, 'Rendition operation cancelled. Refresh history to inspect saved state.') from None
    except (ValueError, OSError, TimeoutError, RecursionError, UnicodeError):
        raise HTTPException(409, 'Rendition context is busy, changed or unavailable. Refresh history before retrying.') from None


async def _dispatch_renditions(request, operation, **selection):
    payload = None
    if operation != 'metadata':
        parts = []; size = 0
        async for part in request.stream():
            size += len(part)
            if size > MAX_RENDITION_REQUEST_BYTES:
                raise HTTPException(413, 'Rendition request exceeds its 16 KiB limit.')
            parts.append(part)
        try:
            payload = parse_sound_rendition_request(operation, b''.join(parts))
        except RenditionRequestError:
            raise HTTPException(400, 'Invalid rendition request. Refresh history and check the fields.') from None
    if operation in ('record', 'consume'):
        from tts import ensure_audio_export_active
        def owned(cancel_check):
            return _rendition_response(operation, payload, selection,
                                      lambda: ensure_audio_export_active(cancel_check))
        return await _dispatch_sound_candidate(request, owned, busy_conflicts=True)
    return await _dispatch_sound_metadata(request,
        lambda check: _rendition_response(operation, payload, selection, check))


@router.get('/api/sound_design/renditions')
async def list_sound_renditions(request: Request,
    offset: Annotated[int, Query(ge=0, le=1024)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    cue_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None):
    return await _dispatch_renditions(request, 'metadata', offset=offset, limit=limit, cue_id=cue_id)


@router.post('/api/sound_design/renditions/record')
async def record_sound_rendition(request: Request):
    return await _dispatch_renditions(request, 'record')


@router.post('/api/sound_design/renditions/keep')
async def keep_sound_rendition(request: Request):
    return await _dispatch_renditions(request, 'keep')


@router.post('/api/sound_design/renditions/consume')
async def consume_sound_rendition(request: Request):
    return await _dispatch_renditions(request, 'consume')


@router.post('/api/sound_design/renditions/cleanup_preview')
async def preview_sound_rendition_cleanup(request: Request):
    return await _dispatch_renditions(request, 'cleanup_preview')


@router.post('/api/sound_design/renditions/cleanup')
async def cleanup_sound_renditions(request: Request):
    return await _dispatch_renditions(request, 'cleanup')
