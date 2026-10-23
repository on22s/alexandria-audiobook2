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
