"""Casting library routes (#1532). Casting itself goes through the voice candidate routes."""
import asyncio
import os
import re

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from casting_catalog import (CastingMarkConflict, CastingMarkNotFound, CastingMarksCorrupt,
                             get_casting_activity, get_casting_catalog, get_casting_marks, get_catalog_voice,
                             get_voice_clone_config, save_casting_mark, save_casting_mark_batch)
from core import DATA_DIR, DESIGNED_VOICES_DIR

router = APIRouter()
CASTING_MARKS_PATH = os.path.join(DATA_DIR, "casting_marks.json")
VOICE_ID_PATTERN = r"^(persona/[^/\\]+/[^/\\]+|designer/[A-Za-z0-9_.-]+)$"
MAX_BATCH_VOICES = 1000
MAX_ACTION_LENGTH = 100


class CastingMarkRequest(BaseModel):
    voice_id: str = Field(min_length=1, max_length=400, pattern=VOICE_ID_PATTERN)
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
    patch: Dict[str, Any]
    expected: Dict[str, Any]


class CastingMarkBatchRequest(BaseModel):
    changes: List[CastingMarkChange] = Field(min_length=1, max_length=MAX_BATCH_VOICES)
    action: str = Field(min_length=1, max_length=MAX_ACTION_LENGTH)


def get_casting_library():
    catalog = get_casting_catalog(DESIGNED_VOICES_DIR)
    marks = get_casting_marks(CASTING_MARKS_PATH)
    return {**catalog, "marks": {v["id"]: marks[v["id"]] for v in catalog["voices"] if v["id"] in marks},
            "activity": get_casting_activity(CASTING_MARKS_PATH)}


def apply_casting_mark(request):
    if get_catalog_voice(DESIGNED_VOICES_DIR, request.voice_id) is None:
        raise HTTPException(status_code=404, detail="Voice not found in the casting library; reload it")
    patch = request.model_dump(exclude_unset=True, exclude={"voice_id"})
    try:
        return save_casting_mark(CASTING_MARKS_PATH, request.voice_id, patch)
    except CastingMarksCorrupt as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def apply_casting_mark_batch(request):
    known = {voice["id"] for voice in get_casting_catalog(DESIGNED_VOICES_DIR)["voices"]}
    changes = [{"voice_id": change.voice_id, "patch": change.patch, "expected": change.expected}
               for change in request.changes]
    try:
        marks, activity = save_casting_mark_batch(CASTING_MARKS_PATH, changes, request.action, known)
    except CastingMarkConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except CastingMarkNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except CastingMarksCorrupt as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"marks": marks, "activity": activity}


def get_casting_clone_config(voice_id, speaker):
    if not re.match(VOICE_ID_PATTERN, voice_id):
        raise HTTPException(status_code=400, detail="Invalid voice id")
    voice = get_catalog_voice(DESIGNED_VOICES_DIR, voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice not found in the casting library; reload it")
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


@router.get("/api/casting/clone_config")
async def casting_clone_config(voice_id: str = Query(min_length=1, max_length=400),
                               speaker: str = Query(min_length=1, max_length=200)):
    return await asyncio.to_thread(get_casting_clone_config, voice_id, speaker)
