"""Explicit source-backed scope preview/apply/undo; work runs off the event loop."""
import asyncio
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from core import DATA_DIR
from project_scope_admission import ensure_scope_admission
from project_scope_apply import (apply_source_scope, apply_source_scope_undo,
    get_source_scope_undo_status, discard_source_scope_undo)
from project_scope_plan import get_scope_preview
from project_scope_files import capture_scope_file

router = APIRouter()


class ScopeSelection(BaseModel):
    start_entry: int = Field(strict=True, ge=1)


class ScopeRevision(BaseModel):
    expected_revision: str = Field(strict=True, pattern=r'^[0-9a-f]{64}$')


class ScopeApply(ScopeSelection, ScopeRevision):
    pass


def _get_public_preview(plan):
    verified = {item['uid']:item['audio'] is not None for item in plan['audio']}
    return {'revision':plan['revision'], 'book_token':plan['book_token'],
        'start_entry':plan['partition']['script_start'],
        'source_name':Path(plan['source_file']['absolute_path']).name,
        'source_range':{'removed_start_word':plan['removed_source_start_word'],
            'retained_start_word':plan['source_scope']['retained_start_word'],
            'end_word':plan['source_scope']['retained_end_word']},
        'removed_uids':plan['removed_uids'],
        'removed_script':[{'speaker':row.get('speaker'), 'text':row.get('text','')}
                          for row in plan['removed_script']],
        'retained':[{'uid':row['uid'],'text':row['text'],'speaker':row.get('speaker'),
                     'audio_verified':verified[row['uid']]} for row in plan['selected_chunks']],
        'voices':plan['voices']['retained'],
        'history_versions':len(plan['history']['retained_versions']),
        'preserved_files':[{'path':row['path'],'exists':row['exists']} for row in plan['preserved_files']],
        'notice':'Exact wording and retained UIDs stay. Cast, audio, history and older exports stay on disk; timelines shift with retained rows. Older exports require rebuilding. One exact undo is retained until later edits invalidate it or you discard it.'}


def _run_scope_operation(operation, request=None):
    try:
        with ensure_scope_admission(DATA_DIR):
            if operation=='preview':return _get_public_preview(get_scope_preview(DATA_DIR,request.start_entry))
            if operation=='apply':return apply_source_scope(DATA_DIR,request.start_entry,request.expected_revision)
            if operation=='undo_status':return get_source_scope_undo_status(DATA_DIR)
            if operation=='undo':return apply_source_scope_undo(DATA_DIR,request.expected_revision)
            if operation=='discard':return discard_source_scope_undo(DATA_DIR,request.expected_revision)
            if operation=='entries':
                _proof, raw = capture_scope_file(DATA_DIR,'annotated_script.json',include_bytes=True)
                rows=json.loads(raw) if raw is not None else []
                if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
                    raise ValueError('Saved annotations need inspection.')
                search, offset=request; result=[]; next_offset=None
                for index in range(offset,len(rows)):
                    row=rows[index]; text=row.get('text','')
                    if not isinstance(text,str):raise ValueError('Saved annotation text needs inspection.')
                    if search.casefold() not in text.casefold():continue
                    if len(result)==50:
                        next_offset=index;break
                    result.append({'start_entry':index,'label':f"{index+1}. {row.get('speaker','')}: {text[:180]}"})
                return {'entries':result,'next_offset':next_offset,'total_entries':len(rows)}
            raise ValueError('Unknown source-scope operation.')
    except (ValueError,OSError,RecursionError) as error:
        raise HTTPException(status_code=409,detail=str(error)[:500]) from error


@router.post('/api/project/scope/preview')
async def preview_source_scope(request: ScopeSelection):
    return await asyncio.to_thread(_run_scope_operation,'preview',request)


@router.get('/api/project/scope/entries')
async def get_scope_entries(search: str=Query(default='',max_length=200), offset: int=Query(default=1,ge=1)):
    return await asyncio.to_thread(_run_scope_operation,'entries',(search,offset))


@router.post('/api/project/scope/apply')
async def apply_scope(request: ScopeApply):
    return await asyncio.to_thread(_run_scope_operation,'apply',request)


@router.get('/api/project/scope/undo')
async def get_scope_undo():
    return await asyncio.to_thread(_run_scope_operation,'undo_status')


@router.post('/api/project/scope/undo')
async def undo_scope(request: ScopeRevision):
    return await asyncio.to_thread(_run_scope_operation,'undo',request)


@router.post('/api/project/scope/discard_undo')
async def discard_scope_undo(request: ScopeRevision):
    return await asyncio.to_thread(_run_scope_operation,'discard',request)
