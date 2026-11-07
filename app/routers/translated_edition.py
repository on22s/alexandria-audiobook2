"""Translation preview endpoints: list pairs, build a proposal, read one back."""
import asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from core import DATA_DIR
import translated_edition

router = APIRouter()


class ProposalRequest(BaseModel):
    source_language: str = Field(strict=True, min_length=1, max_length=20)
    target_language: str = Field(strict=True, min_length=1, max_length=20)


def _get_pairs():
    return translated_edition.get_installed_pairs()


def _create_proposal(request):
    try:
        installed = translated_edition.get_installed_pairs()
        if not installed["available"]:
            raise ValueError("Translation package is unavailable")
        translate_fn = translated_edition.build_argos_translator(
            request.source_language, request.target_language)
        proposal = translated_edition.build_proposal(
            DATA_DIR, source_language=request.source_language,
            target_language=request.target_language, translate_fn=translate_fn,
            installed_pairs=installed["pairs"])
        return {**proposal, "stale": False, "runtime_version": installed["runtime_version"]}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)[:500]) from error


def _read_proposal(proposal_id):
    try:
        proposal = translated_edition.get_proposal(DATA_DIR, proposal_id)
        return {**proposal, "stale": translated_edition.is_proposal_stale(DATA_DIR, proposal)}
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)[:500]) from error


@router.get('/api/translated_edition/pairs')
async def get_translated_edition_pairs():
    return await asyncio.to_thread(_get_pairs)


@router.post('/api/translated_edition/proposals')
async def create_translated_edition_proposal(request: ProposalRequest):
    return await asyncio.to_thread(_create_proposal, request)


@router.get('/api/translated_edition/proposals/{proposal_id}')
async def get_translated_edition_proposal(proposal_id: str):
    return await asyncio.to_thread(_read_proposal, proposal_id)
