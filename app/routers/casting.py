"""Casting library routes (#1532). Casting itself goes through the voice candidate routes."""
import asyncio
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Literal, Optional

from casting_catalog import (CastingMarkConflict, CastingMarkNotFound, CastingMarksCorrupt,
                             get_casting_activity, get_casting_catalog, get_casting_marks, get_catalog_voice,
                             get_casting_marks_path, get_voice_clone_config, get_voice_lifecycle,
                             save_casting_lifecycle_batch, save_casting_mark, save_casting_mark_batch)
from core import DESIGNED_VOICES_DIR, VOICE_LIBRARY_PATH

router = APIRouter()
CASTING_MARKS_PATH = get_casting_marks_path(VOICE_LIBRARY_PATH)
VOICE_ID_PATTERN = r"^(persona/[^/\\]+/[^/\\]+|designer/[A-Za-z0-9_.-]+)$"
MAX_BATCH_VOICES = 1000
MAX_ACTION_LENGTH = 100
CastingLifecycle = Literal['active', 'trashed', 'deleted']


class CastingMarkRequest(BaseModel):
    voice_id: str = Field(min_length=1, max_length=400, pattern=VOICE_ID_PATTERN)
    expected_lifecycle: CastingLifecycle = "active"
    status: Optional[str] = None
    priority: Optional[str] = None
    notes: Optional[str] = None
    tags: Optional[List[str]] = None
    tag_colors: Optional[Dict[str, str]] = None
    stars: Optional[List[str]] = None
    circles: Optional[List[str]] = None
    collections: Optional[List[str]] = None


class CastingMarkChange(BaseModel):
    voice_id: str = Field(min_length=1, max_length=400, pattern=VOICE_ID_PATTERN)
    expected_lifecycle: CastingLifecycle = "active"
    patch: Dict[str, Any]
    expected: Dict[str, Any]


class CastingMarkBatchRequest(BaseModel):
    changes: List[CastingMarkChange] = Field(min_length=1, max_length=MAX_BATCH_VOICES)
    action: str = Field(min_length=1, max_length=MAX_ACTION_LENGTH)


class CastingLifecycleChange(BaseModel):
    voice_id: str = Field(min_length=1, max_length=400, pattern=VOICE_ID_PATTERN)
    to: CastingLifecycle
    expected_lifecycle: CastingLifecycle


class CastingLifecycleBatchRequest(BaseModel):
    changes: List[CastingLifecycleChange] = Field(min_length=1, max_length=MAX_BATCH_VOICES)


def get_casting_library():
    catalog = get_casting_catalog(DESIGNED_VOICES_DIR)
    marks = get_casting_marks(CASTING_MARKS_PATH)
    voices = [voice for voice in catalog["voices"] if get_voice_lifecycle(marks, voice["id"]) != "deleted"]
    return {**catalog, "voices": voices,
            "marks": {v["id"]: marks[v["id"]] for v in voices if v["id"] in marks},
            "activity": get_casting_activity(CASTING_MARKS_PATH)}


def apply_casting_mark(request):
    if get_catalog_voice(DESIGNED_VOICES_DIR, request.voice_id) is None:
        raise HTTPException(status_code=404, detail="Voice not found in the casting library; reload it")
    patch = request.model_dump(exclude_unset=True, exclude={"voice_id", "expected_lifecycle"})
    try:
        return save_casting_mark(CASTING_MARKS_PATH, request.voice_id, patch,
                                expected_lifecycle=request.expected_lifecycle)
    except CastingMarkConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CastingMarksCorrupt as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail="Casting marks are busy; try again.") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def apply_casting_mark_batch(request):
    known = {voice["id"] for voice in get_casting_catalog(DESIGNED_VOICES_DIR)["voices"]}
    changes = [{"voice_id": change.voice_id, "patch": change.patch, "expected": change.expected,
                "expected_lifecycle": change.expected_lifecycle}
               for change in request.changes]
    try:
        marks, activity = save_casting_mark_batch(CASTING_MARKS_PATH, changes, request.action, known)
    except CastingMarkConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CastingMarkNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CastingMarksCorrupt as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail="Casting marks are busy; try again.") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"marks": marks, "activity": activity}


def apply_casting_lifecycle_batch(request):
    known = {voice["id"] for voice in get_casting_catalog(DESIGNED_VOICES_DIR)["voices"]}
    changes = [{"voice_id": change.voice_id, "to": change.to, "expected_lifecycle": change.expected_lifecycle}
               for change in request.changes]
    action = {"trashed": "Trash voices", "active": "Restore voices",
              "deleted": "Delete permanently"}.get(changes[0]["to"], "Change lifecycle")
    try:
        marks, activity = save_casting_lifecycle_batch(CASTING_MARKS_PATH, changes, action, known)
    except CastingMarkConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CastingMarkNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CastingMarksCorrupt as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail="Casting marks are busy; try again.") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"marks": marks, "activity": activity}


def get_casting_clone_config(voice_id, speaker):
    if not re.match(VOICE_ID_PATTERN, voice_id):
        raise HTTPException(status_code=400, detail="Invalid voice id")
    voice = get_catalog_voice(DESIGNED_VOICES_DIR, voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice not found in the casting library; reload it")
    try:
        lifecycle = get_voice_lifecycle(get_casting_marks(CASTING_MARKS_PATH), voice_id)
    except CastingMarksCorrupt as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    if lifecycle == "deleted":
        raise HTTPException(status_code=404, detail="Voice not found in the casting library; reload it")
    if lifecycle == "trashed":
        raise HTTPException(status_code=409, detail="This voice is in Trash; restore it before casting")
    config = get_voice_clone_config(voice, speaker)
    if config is None:
        raise HTTPException(status_code=409, detail="This voice has no preview audio to clone from")
    return {"voice_id": voice_id, "speaker": speaker, "config": config}


@router.get("/api/casting/voices")
async def casting_voices():
    return await asyncio.to_thread(get_casting_library)


@router.post("/api/casting/marks")
async def casting_mark(request: CastingMarkRequest):
    mark = await asyncio.to_thread(apply_casting_mark, request)
    return {"voice_id": request.voice_id, "mark": mark}


@router.post("/api/casting/marks/batch")
async def casting_marks_batch(request: CastingMarkBatchRequest):
    return await asyncio.to_thread(apply_casting_mark_batch, request)


@router.post("/api/casting/lifecycle/batch")
async def casting_lifecycle_batch(request: CastingLifecycleBatchRequest):
    return await asyncio.to_thread(apply_casting_lifecycle_batch, request)


@router.get("/api/casting/clone_config")
async def casting_clone_config(voice_id: str = Query(min_length=1, max_length=400),
                               speaker: str = Query(min_length=1, max_length=200)):
    return await asyncio.to_thread(get_casting_clone_config, voice_id, speaker)
