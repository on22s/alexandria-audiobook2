"""Translation preview endpoints: list pairs, build a proposal, read one back.

A running preview can be cancelled from the panel: the client sends a random
cancel token with the create request, and ``POST .../{token}/cancel`` sets the
token's event, which the builder checks between rows and again before it
writes. The token registry is in-process (this app runs one worker) and is
cleaned up when a build finishes.

One preview runs at a time (``_PREVIEW_LOCK``, non-blocking): a second create
is refused with 409 rather than loading a second translator/model. The lock is
released on every exit path, including the duplicate-token 409 and an
unexpected ``BaseException``. It is a module-level lock with no watchdog, so a
translator call that never returns holds it for the life of the process: the
row loop only checks the cancel event between rows, so cancelling cannot
interrupt a call already inside the translator, and every later preview gets
409 until the app is restarted. That is a documented limit, not a silent one.
"""
import asyncio
import re
import threading
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core import DATA_DIR
import translated_edition

router = APIRouter()

_CANCEL_TOKEN_PATTERN = r"^[0-9a-f]{32}$"
_CANCEL_TOKENS = {}
_CANCEL_LOCK = threading.Lock()
# One preview at a time: a second concurrent create is refused with 409
# instead of loading a second translator (and its model) in parallel.
_PREVIEW_LOCK = threading.Lock()


class ProposalRequest(BaseModel):
    source_language: str = Field(strict=True, min_length=1, max_length=20)
    target_language: str = Field(strict=True, min_length=1, max_length=20)
    # Client-generated so a cancel request can name a build before its
    # response exists. Optional: a caller that does not want cancellation
    # simply omits it.
    cancel_token: str | None = Field(default=None, strict=True, min_length=32,
                                     max_length=32, pattern=_CANCEL_TOKEN_PATTERN)


def _get_pairs():
    return translated_edition.get_installed_pairs()


def _begin_preview(cancel_token):
    if cancel_token is None:
        return None
    event = threading.Event()
    with _CANCEL_LOCK:
        if cancel_token in _CANCEL_TOKENS:
            raise ValueError("A preview with this cancel token is already running")
        _CANCEL_TOKENS[cancel_token] = event
    return event


def _end_preview(cancel_token, event):
    if cancel_token is None:
        return
    with _CANCEL_LOCK:
        if _CANCEL_TOKENS.get(cancel_token) is event:
            _CANCEL_TOKENS.pop(cancel_token, None)


def _cancel_preview(cancel_token):
    if not isinstance(cancel_token, str) or not re.fullmatch(_CANCEL_TOKEN_PATTERN,
                                                            cancel_token):
        raise ValueError("Invalid cancel token")
    with _CANCEL_LOCK:
        event = _CANCEL_TOKENS.get(cancel_token)
        if event is None:
            return {"cancelled": False, "reason": "no_preview_running"}
        event.set()
        return {"cancelled": True, "reason": "cancel_requested"}


def _create_proposal(request):
    if not _PREVIEW_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409,
                            detail="A translation preview is already running")
    event = None
    try:
        try:
            event = _begin_preview(request.cancel_token)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)[:500]) from error
        try:
            installed = translated_edition.get_installed_pairs()
            if not installed["available"]:
                raise ValueError("Translation package is unavailable")
            translate_fn = translated_edition.build_argos_translator(
                request.source_language, request.target_language)
            proposal = translated_edition.build_proposal(
                DATA_DIR, source_language=request.source_language,
                target_language=request.target_language, translate_fn=translate_fn,
                installed_pairs=installed["pairs"],
                cancel_check=event.is_set if event is not None else None,
                runtime_version=installed["runtime_version"])
            return {**proposal, "stale": False}
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)[:500]) from error
    finally:
        _end_preview(request.cancel_token, event)
        _PREVIEW_LOCK.release()


def _read_proposal(proposal_id):
    try:
        proposal = translated_edition.get_proposal(DATA_DIR, proposal_id)
        return {**proposal, "stale": translated_edition.is_proposal_stale(DATA_DIR, proposal)}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)[:500]) from error


def _read_latest_proposal():
    try:
        result = translated_edition.get_latest_proposal(DATA_DIR)
        proposal = result["proposal"]
        if proposal is not None:
            proposal = {**proposal, "stale": translated_edition.is_proposal_stale(
                DATA_DIR, proposal)}
        return {"proposal": proposal,
                "malformed_proposals": result["malformed_proposals"]}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)[:500]) from error


@router.get('/api/translated_edition/pairs')
async def get_translated_edition_pairs():
    return await asyncio.to_thread(_get_pairs)


@router.post('/api/translated_edition/proposals')
async def create_translated_edition_proposal(request: ProposalRequest):
    return await asyncio.to_thread(_create_proposal, request)


@router.get('/api/translated_edition/latest-proposal')
async def get_translated_edition_latest_proposal():
    return await asyncio.to_thread(_read_latest_proposal)


@router.get('/api/translated_edition/proposals/{proposal_id}')
async def get_translated_edition_proposal(proposal_id: str):
    return await asyncio.to_thread(_read_proposal, proposal_id)


@router.post('/api/translated_edition/proposals/{cancel_token}/cancel')
async def cancel_translated_edition_proposal(cancel_token: str):
    """Request a stop after the current row for one in-flight preview.

    Cancelling never approves anything. A cancel that arrives before the
    proposal is written is saved with status ``cancelled``; one that arrives
    after the write leaves the finished status, so the caller must not claim
    the saved status will be ``cancelled``.
    """
    try:
        return await asyncio.to_thread(_cancel_preview, cancel_token)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)[:500]) from error
