"""Server-owned cue edits using existing scope guards and book transactions."""
import hashlib
import json
import re

from book_state_transaction import apply_book_state_locked
from audio_pcm_producer import get_promoted_pcm_shape
from json_metadata import get_unique_json_object
from project_scope_admission import ensure_scope_admission, ensure_audio_scope_admission
from sound_design_assets import SoundDesignAssetStore
from sound_design_context import (capture_sound_design_context, capture_sound_design_anchor,
                                  require_sound_design_context_files_current)
from sound_design_cues import (MAX_DIALOGUE, _require_digest, _require_int,
                               validate_sound_design_cue_parameters, get_sound_design_duration_error)
from sound_design_registry import (ACTIVE_CUES, get_sound_design_registry_bytes,
    get_sound_design_registry_edit, get_sound_design_registry_selection)
from source_evidence import _require_keys, _require_text
from sound_design_rendition_media import ensure_sound_rendition_assets
from sound_design_renditions import validate_sound_rendition_consumers

MAX_CUE_REQUEST_BYTES = 16384


class CueRequestError(ValueError):
    pass


def _nonfinite(_value):
    raise ValueError('Nonfinite cue requests are not supported')


def parse_sound_design_cue_request(operation, raw):
    """Reject untrusted structure and limits before any task/file admission."""
    try:
        return _parse_sound_design_cue_request(operation,raw)
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise CueRequestError('Invalid or oversized cue request') from None


def _parse_sound_design_cue_request(operation, raw):
    if not isinstance(raw,bytes) or not 1 <= len(raw) <= MAX_CUE_REQUEST_BYTES:
        raise ValueError('Cue request exceeds its byte limit or is empty')
    value = json.loads(raw.decode('utf-8'),object_pairs_hook=get_unique_json_object,parse_constant=_nonfinite)
    keys = ('context_revision','registry_revision','cue_id')
    if operation == 'edit':
        keys += ('anchor_revision','parent_version','chunk_uid','asset_id','asset_sha256','parameters')
    elif operation == 'select':
        keys += ('version',)
    else:
        raise ValueError('Unknown cue edit operation')
    _require_keys(value,keys,'Cue edit request')
    _require_digest(value['context_revision'],'Cue context revision')
    _require_digest(value['registry_revision'],'Cue registry revision',nullable=True)
    _require_text(value['cue_id'],128,'Cue identity')
    if operation == 'edit':
        _require_digest(value['anchor_revision'],'Cue anchor revision')
        _require_digest(value['parent_version'],'Parent cue revision',nullable=True)
        _require_text(value['chunk_uid'],128,'Stable dialogue identity')
        if not isinstance(value['asset_id'],str) or re.fullmatch(r'[0-9a-f]{32}',value['asset_id']) is None:
            raise ValueError('Select an admitted sound asset identity')
        _require_digest(value['asset_sha256'],'Expected asset fingerprint')
        validate_sound_design_cue_parameters(value['parameters'])
    else:
        _require_digest(value['version'],'Selected cue version',nullable=True)
    return value


def _require_context(snapshot, payload):
    if (snapshot['context_revision'] != payload['context_revision']
            or snapshot['registry_revision'] != payload['registry_revision']):
        raise ValueError('Cue context changed; refresh before editing')


def _require_asset_duration(recipe):
    asset = recipe['asset']
    layout = (asset['channels'], recipe['frame_rate'], asset['sample_width'])
    shape = ((asset['channels'], asset['frame_rate'], asset['sample_width']), asset['frames'])
    native = get_promoted_pcm_shape(shape,layout,reject_impossible_channels=True)[1]
    reason = get_sound_design_duration_error(recipe,native)
    if reason is not None:
        raise ValueError('Cue asset duration needs review: '+reason)


def get_sound_design_cue_view(snapshot, offset=0, limit=100):
    """Whitelist public metadata; never expose snapshot paths/configuration."""
    _require_int(offset,0,MAX_DIALOGUE,'Dialogue page offset')
    _require_int(limit,1,200,'Dialogue page length')
    rows = []
    for row in snapshot['chunks'][offset:offset+limit]:
        speaker, text = row.get('speaker','NARRATOR'), row.get('text','')
        if not isinstance(speaker,str) or not isinstance(text,str):
            raise ValueError('Saved dialogue text needs inspection in Editor')
        path = row.get('audio_path')
        rows.append(dict(chunk_uid=row['uid'],speaker=speaker[:128],text=text[:500],
                         has_recorded_take=row.get('status')=='done' and isinstance(path,str) and bool(path)))
    return dict(context_revision=snapshot['context_revision'],registry_revision=snapshot['registry_revision'],
                book_token=snapshot['book_token'],book=snapshot['book'],registry=snapshot['registry'],
                chunks=rows,total_chunks=len(snapshot['chunks']),
                next_offset=offset+len(rows) if offset+len(rows)<len(snapshot['chunks']) else None)


def run_sound_design_cue_operation(root, operation, *, raw=None, chunk_uid=None,
                                   expected_context=None, offset=0, limit=100,
                                   check_active=lambda: None, audio_owned=False):
    """Enter/retain thread-local task/book ownership in the worker itself."""
    payload = parse_sound_design_cue_request(operation,raw) if operation in ('edit','select') else None
    if operation == 'anchor':
        _require_text(chunk_uid,128,'Stable dialogue identity')
        _require_digest(expected_context,'Cue context revision')
    elif operation == 'metadata':
        _require_int(offset,0,MAX_DIALOGUE,'Dialogue page offset')
        _require_int(limit,1,200,'Dialogue page length')
    elif payload is None:
        raise ValueError('Unknown cue operation')
    check_active()
    admission = ensure_audio_scope_admission if audio_owned else ensure_scope_admission
    with admission(root):
        snapshot = capture_sound_design_context(root,check_active=check_active)
        if operation == 'metadata':
            return get_sound_design_cue_view(snapshot,offset,limit)
        if operation == 'anchor':
            if snapshot['context_revision'] != expected_context:
                raise ValueError('Cue context changed; refresh before selecting dialogue')
            anchor = capture_sound_design_anchor(root,snapshot,chunk_uid,check_active=check_active)
            require_sound_design_context_files_current(root,snapshot,anchor)
            check_active()
            return dict(context_revision=expected_context,anchor_revision=anchor['anchor_revision'],
                        selection=anchor['selection'])
        _require_context(snapshot,payload)
        asset = None
        rendition_ids = []
        if operation == 'edit':
            anchor = capture_sound_design_anchor(root,snapshot,payload['chunk_uid'],check_active=check_active)
            if anchor['anchor_revision'] != payload['anchor_revision']:
                raise ValueError('Selected dialogue audio changed; review the anchor again')
            with SoundDesignAssetStore(root).open_canonical(payload['asset_id'],cancel_check=check_active) as (_,facts):
                if facts['sha256'] != payload['asset_sha256']:
                    raise ValueError('Selected sound asset changed; refresh the library')
            recipe = dict(book=snapshot['book'],selection=anchor['selection'],asset=facts,**payload['parameters'])
            _require_asset_duration(recipe)
            registry = {**snapshot['registry'],'book':snapshot['book']}
            parent = next((row for row in registry['versions'] if row['version'] == payload['parent_version']), None)
            schema = 1
            if parent is not None and parent['schema'] == 2 and parent['recipe']['asset'] == facts:
                recipe['rendition_id'] = parent['recipe']['rendition_id']
                rendition_ids.append(recipe['rendition_id']); schema = 2
            updated = get_sound_design_registry_edit(registry,payload['cue_id'],recipe,payload['parent_version'],schema=schema)
            selection = anchor['selection']
            asset = facts
        else:
            updated = get_sound_design_registry_selection(snapshot['registry'],payload['cue_id'],payload['version'])
            selection = None
            if payload['version'] is not None:
                selected_version = next(row for row in updated['versions'] if row['version']==payload['version'])
                selected = selected_version['recipe']
                if selected_version['schema'] == 2:
                    rendition_ids.append(selected['rendition_id'])
                if selected['book'] != snapshot['book']:
                    raise ValueError('Saved cue source is stale; create an explicit revision')
                _require_asset_duration(selected)
                anchor = capture_sound_design_anchor(root,snapshot,selected['selection']['chunk_uid'],check_active=check_active)
                if anchor['selection'] != selected['selection']:
                    raise ValueError('Saved dialogue selection is stale; create an explicit revision')
                with SoundDesignAssetStore(root).open_canonical(selected['asset']['asset_id'],cancel_check=check_active) as (_,facts):
                    if facts != selected['asset']:
                        raise ValueError('Saved sound asset is stale; inspect the library')
                selection = selected['selection']
                asset = facts
        validate_sound_rendition_consumers(snapshot['renditions'], updated)
        with ensure_sound_rendition_assets(root, snapshot, rendition_ids, check_active=check_active):
            # Asset verification and row hashing may take time. Re-capture the exact
            # physical registry/context and selected bytes before atomic mutation.
            fresh = capture_sound_design_context(root,check_active=check_active)
            _require_context(fresh,payload)
            latest = None
            if selection is not None:
                latest = capture_sound_design_anchor(root,fresh,selection['chunk_uid'],check_active=check_active)
                if latest['selection'] != selection:
                    raise ValueError('Selected audio changed before cue publication')
            encoded = get_sound_design_registry_bytes(updated)
            # Context/audio recapture may be lengthy. Recheck the admitted asset
            # after it, and let the canonical file exit fences finish before commit.
            if asset is not None:
                with SoundDesignAssetStore(root).open_canonical(asset['asset_id'],cancel_check=check_active) as (_,current):
                    if current != asset:
                        raise ValueError('Selected sound asset changed before cue publication')
        check_active()
        require_sound_design_context_files_current(root,fresh,latest)
        check_active()
        apply_book_state_locked(root,{ACTIVE_CUES:encoded},())
        return dict(status='saved',cue_id=payload['cue_id'],version=updated['selected'].get(payload['cue_id']),
                    registry_revision=hashlib.sha256(encoded).hexdigest(),book_token=snapshot['book_token'],
                    refresh_required=True)
