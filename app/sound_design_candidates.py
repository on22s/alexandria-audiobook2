"""Bounded candidate inventory and explicit revision-bound private-stage discard."""
import json
from pathlib import Path

from adapter_publication import sync_adapter_directory
from audio_take_history import _version
from book_state_transaction import get_snapshot_digest
from json_metadata import get_unique_json_object
from project_scope_files import capture_scope_file
from sound_design_approval import get_sound_approval, get_sound_delivery_profiles, MAX_APPROVAL_BYTES
from sound_design_candidate import (get_sound_candidate_document, MAX_CANDIDATE_METADATA,
                                    validate_sound_candidate_stage)
from sound_design_cues import _require_digest
from sound_design_staging import ensure_sound_render_quota, get_sound_render_stage_files, MAX_TOTAL_BYTES
from tts import ensure_audio_export_active


def _capture_stage(stage, root, check):
    charged, files, directories = get_sound_render_stage_files(stage,root,cancel_check=check)
    records = []
    for path in sorted(files+directories):
        check()
        records.append([path.relative_to(stage).as_posix(),list(_version(path.lstat()))])
    return charged, get_snapshot_digest(records), files, directories


def list_sound_candidates(root, *, cancel_check=None):
    """Report metadata only; playback and delivery perform separate verification."""
    root = Path(root).absolute()
    check = lambda: ensure_audio_export_active(cancel_check)
    rows = []
    with ensure_sound_render_quota(root,cancel_check=check) as (directory,available,_):
        for stage in sorted(directory.iterdir()):
            if stage.name == 'quota.lock': continue
            validate_sound_candidate_stage(stage.name)
            charged, revision, _, _ = _capture_stage(stage,root,check)
            row = dict(stage=stage.name,bytes=charged,recovery_revision=revision,status='incomplete',
                       approval_status='absent',media_verified=False)
            relative = stage.relative_to(root).as_posix()
            try:
                _, raw = capture_scope_file(root,relative+'/candidate.json',include_bytes=True,
                                             limit=MAX_CANDIDATE_METADATA,cancel_check=check)
                if raw is not None:
                    value = json.loads(raw,object_pairs_hook=get_unique_json_object)
                    document = get_sound_candidate_document(raw,value.get('candidate_revision') if isinstance(value,dict) else None)
                    binding = document['binding']; plan = binding['pcm_plan']
                    row.update(status='metadata_ready',candidate_revision=document['candidate_revision'],
                        context_revision=binding['context_revision'],book_id=binding['book']['book_id'],
                        frames=plan['frames'],frame_rate=plan['frame_rate'])
                    try:
                        _, approval = capture_scope_file(root,relative+'/approval.json',include_bytes=True,
                                                         limit=MAX_APPROVAL_BYTES,cancel_check=check)
                        if approval is not None:
                            record = get_sound_approval(approval)
                            if record['candidate_revision'] != document['candidate_revision']:
                                raise ValueError('Approval belongs to another candidate')
                            row.update(approval_status='recorded',approval_revision=record['approval_revision'],
                                       delivery=record['delivery'])
                    except (ValueError,OSError,RecursionError,UnicodeError):
                        row['approval_status'] = 'invalid'
            except (ValueError,OSError,RecursionError,UnicodeError):
                row['status'] = 'invalid_metadata'
            check()
            if _capture_stage(stage,root,check)[1] != revision:
                raise ValueError('Candidate inventory changed while reading; refresh')
            rows.append(row)
    return dict(candidates=rows,charged_bytes=MAX_TOTAL_BYTES-available,limit_bytes=MAX_TOTAL_BYTES,
                delivery_profiles=get_sound_delivery_profiles(cancel_check=check))


def discard_sound_candidate(root, stage_name, expected_recovery, *, cancel_check=None):
    """Delete only an explicitly selected, unchanged private stage, including PCM.

    Cancellation or filesystem failure can leave a partial stage. Refresh the
    inventory for a new recovery revision; never silently continue old deletion.
    """
    validate_sound_candidate_stage(stage_name)
    _require_digest(expected_recovery,'Expected candidate recovery revision')
    root = Path(root).absolute()
    check = lambda: ensure_audio_export_active(cancel_check)
    with ensure_sound_render_quota(root,cancel_check=check) as (directory,_,_):
        stage = directory/stage_name
        _, revision, files, directories = _capture_stage(stage,root,check)
        if revision != expected_recovery:
            raise ValueError('Candidate changed since inspection; refresh before discard')
        for path in files:
            check(); path.unlink()
        for path in reversed(directories):
            check(); path.rmdir()
        sync_adapter_directory(directory)
    return dict(status='discarded',stage=stage_name)
