"""Metadata-only rendition cleanup guarded by retained history and stage proofs."""
import json
from pathlib import Path

from book_state_transaction import apply_book_state_locked, get_snapshot_digest
from json_metadata import get_unique_json_object
from project_scope_files import capture_scope_file
from sound_design_approval import get_sound_approval, MAX_APPROVAL_BYTES
from sound_design_candidate import (get_sound_candidate_document, validate_sound_candidate_stage,
                                    MAX_CANDIDATE_METADATA)
from sound_design_candidates import _capture_stage
from sound_design_context import capture_sound_design_context, require_sound_design_context_files_current
from sound_design_renditions import (ACTIVE_RENDITIONS, get_sound_rendition_ledger_bytes,
                                      validate_sound_rendition_consumers)
from sound_design_staging import ensure_sound_render_quota, MAX_STAGES


def _stage_names(directory):
    names = []
    for path in directory.iterdir():
        if path.name == 'quota.lock':
            continue
        if len(names) >= MAX_STAGES:
            raise ValueError('Too many retained sound candidates for cleanup')
        validate_sound_candidate_stage(path.name)
        names.append(path.name)
    return sorted(names)


def _capture_candidate_protection(root, directory, snapshot, check_active):
    """Read at most one bounded metadata document at a time; never hash PCM."""
    versions = {row['version']: row for row in snapshot['registry']['versions']}
    names = _stage_names(directory)
    protections, proofs = {}, []
    for name in names:
        check_active()
        stage = directory/name
        _, inventory, files, directories = _capture_stage(stage, root, check_active)
        found = {path.name for path in files}
        if (directories != [stage] or found - {'candidate.json', 'mixed.pcm', 'approval.json'}
                or not {'candidate.json', 'mixed.pcm'} <= found):
            raise ValueError('Incomplete or unknown candidate proof requires explicit stage inspection')
        relative = stage.relative_to(root).as_posix()
        candidate_proof, raw = capture_scope_file(root, relative+'/candidate.json',
            limit=MAX_CANDIDATE_METADATA, include_bytes=True, cancel_check=check_active)
        if raw is None:
            raise ValueError('Candidate metadata disappeared during cleanup capture')
        value = json.loads(raw, object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite candidate metadata')))
        candidate = get_sound_candidate_document(raw,
            value.get('candidate_revision') if isinstance(value, dict) else None)
        binding = candidate['binding']
        if binding['book']['book_id'] != snapshot['book']['book_id']:
            raise ValueError('Foreign candidate proof prevents automatic rendition cleanup')
        if (stage/'mixed.pcm').lstat().st_size != binding['mix']['bytes']:
            raise ValueError('Incomplete candidate PCM prevents automatic rendition cleanup')
        required = set()
        for identity in binding['cue_versions']:
            cue = versions.get(identity)
            if cue is None:
                raise ValueError('Unknown retained candidate cue prevents automatic rendition cleanup')
            if cue['schema'] == 2:
                required.add(cue['recipe']['rendition_id'])
        for identity in required:
            protections.setdefault(identity, set()).add('retained_candidate')
        approval_proof, approval_raw = capture_scope_file(root, relative+'/approval.json',
            limit=MAX_APPROVAL_BYTES, include_bytes=True, cancel_check=check_active)
        if approval_raw is not None:
            approval = get_sound_approval(approval_raw)
            if approval['candidate_revision'] != candidate['candidate_revision']:
                raise ValueError('Contradictory approval prevents automatic rendition cleanup')
            for identity in required:
                protections.setdefault(identity, set()).add('retained_approval')
        if _capture_stage(stage, root, check_active)[1] != inventory:
            raise ValueError('Candidate proof changed while capturing cleanup impact')
        proofs.append(dict(stage=name, inventory_revision=inventory,
                           candidate_sha256=candidate_proof['sha256'],
                           approval_sha256=approval_proof['sha256']))
    _require_candidate_proofs_current(root, directory, names, proofs, check_active)
    return protections, proofs, names


def _require_candidate_proofs_current(root, directory, names, proofs, check_active):
    if _stage_names(directory) != names:
        raise ValueError('Candidate inventory changed before rendition cleanup')
    for proof in proofs:
        check_active()
        stage = directory/proof['stage']
        if _capture_stage(stage, root, check_active)[1] != proof['inventory_revision']:
            raise ValueError('Candidate proof changed before rendition cleanup')
        # Metadata hashes fence content independently of platform stat precision.
        # PCM remains stat-only here; cleanup neither decodes nor hashes audio.
        for name, limit, expected in (
                ('candidate.json', MAX_CANDIDATE_METADATA, proof['candidate_sha256']),
                ('approval.json', MAX_APPROVAL_BYTES, proof['approval_sha256'])):
            current, _ = capture_scope_file(root, (stage/name).relative_to(root).as_posix(),
                                            limit=limit, cancel_check=check_active)
            if current['sha256'] != expected:
                raise ValueError('Candidate metadata proof changed before rendition cleanup')
        if _capture_stage(stage, root, check_active)[1] != proof['inventory_revision']:
            raise ValueError('Candidate proof changed while verifying cleanup metadata')


def run_sound_rendition_cleanup(root, snapshot, payload, *, execute, check_active=lambda: None):
    """Keep shared quota and task/book admission through the atomic ledger edit."""
    root = Path(root).absolute()
    requested = payload['rendition_ids']
    ledger, registry = snapshot['renditions'], snapshot['registry']
    known = {record['rendition_id'] for record in ledger['records']}
    if any(identity not in known for identity in requested):
        raise ValueError('Cleanup selection is absent from retained rendition history')
    protections = {identity: set() for identity in known}
    selected = set(registry['selected'].values())
    for cue in registry['versions']:
        if cue['schema'] == 2:
            identity = cue['recipe']['rendition_id']
            protections[identity].add('retained_cue_history')
            if cue['version'] in selected:
                protections[identity].add('draft_selected')
    for identity in ledger['keep']:
        protections[identity].add('kept')
    for record in ledger['records']:
        parent = record['derivation']['parent']
        if parent is not None:
            protections[parent].add('retained_child')
    with ensure_sound_render_quota(root, cancel_check=check_active) as (directory, _available, _count):
        staged, proofs, names = _capture_candidate_protection(root, directory, snapshot, check_active)
        for identity, reasons in staged.items():
            protections[identity].update(reasons)
        rows = [dict(rendition_id=identity, protected=bool(protections[identity]),
                     reasons=sorted(protections[identity])) for identity in requested]
        removable = [row['rendition_id'] for row in rows if not row['protected']]
        impact = get_snapshot_digest(dict(requested=requested,
            context_revision=snapshot['context_revision'], registry_revision=snapshot['registry_revision'],
            ledger_revision=snapshot['ledger_revision'], rows=rows, candidate_proofs=proofs))
        require_sound_design_context_files_current(root, snapshot)
        _require_candidate_proofs_current(root, directory, names, proofs, check_active)
        if not execute:
            return dict(context_revision=snapshot['context_revision'], registry_revision=snapshot['registry_revision'],
                ledger_revision=snapshot['ledger_revision'], impact_revision=impact, rows=rows,
                removable_ids=removable, book_token=snapshot['book_token'])
        if impact != payload['impact_revision'] or len(removable) != len(requested):
            raise ValueError('Rendition cleanup impact changed or a requested record is protected')
        removed = set(requested)
        proposed = {**ledger, 'records': [row for row in ledger['records'] if row['rendition_id'] not in removed],
                    'keep': [identity for identity in ledger['keep'] if identity not in removed]}
        validate_sound_rendition_consumers(proposed, registry)
        raw = get_sound_rendition_ledger_bytes(proposed)
        check_active()
        require_sound_design_context_files_current(root, snapshot)
        _require_candidate_proofs_current(root, directory, names, proofs, check_active)
        check_active()
        apply_book_state_locked(root, {ACTIVE_RENDITIONS: raw}, ())
        current = capture_sound_design_context(root, check_active=check_active)
        return dict(status='removed', removed_ids=list(requested), ledger_revision=current['ledger_revision'],
                    context_revision=current['context_revision'], book_token=current['book_token'],
                    refresh_required=True)
