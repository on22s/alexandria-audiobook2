"""Extend the native export snapshot with every approved sound-design source."""
from pathlib import Path

from export_publication import ExportSourceSnapshot
from sound_design_approval import ensure_approved_sound_candidate
from sound_design_assets import SoundDesignAssetStore, MAX_TOTAL_BYTES
from project_scope_files import capture_scope_file
from sound_design_context import capture_sound_design_context
from sound_design_registry import ACTIVE_CUES, get_selected_sound_design_versions
from sound_design_staging import DIRECTORY
from sound_design_renditions import ACTIVE_RENDITIONS, get_required_sound_rendition_artifacts
from sound_design_cover import capture_sound_delivery_cover
from tts import ensure_audio_export_active


def capture_sound_delivery_snapshot(manager, stage_name, candidate_revision, approval_revision,
                                     delivery, *, cancel_check=None):
    """Use native final publication admission without nesting chunk file locks.

    Native ExportSourceSnapshot takes its own chunk locks; construct it only
    after approved-candidate admission has exited, then verify approval again.
    The returned native snapshot must still verify_sources() after encoding and
    guard the final rename with ensure_publication(output, quality_report).
    """
    root = Path(manager.root_dir).absolute()
    check = lambda: ensure_audio_export_active(cancel_check)
    with ensure_approved_sound_candidate(root,stage_name,candidate_revision,approval_revision,
                                         delivery,cancel_check=check):
        context = capture_sound_design_context(root,check_active=check)
        versions = get_selected_sound_design_versions(context['registry'])
        directory = root/DIRECTORY/stage_name
        paths = {root/ACTIVE_CUES,root/ACTIVE_RENDITIONS,root/'voice_config.json',root/'chunks.json.status.jsonl',
                 directory/'candidate.json',directory/'mixed.pcm',directory/'approval.json'}
        cover_sources = capture_sound_delivery_cover(root,delivery,cancel_check=check)['expected_sources']
        paths.update(Path(path) for path in cover_sources)
        store = SoundDesignAssetStore(root)
        paths.add(store.manifest)
        for version in versions:
            check()
            identity = version['recipe']['asset']['asset_id']
            paths.add(store._path(identity,'.source'))
            paths.add(store._path(identity,'.pcm'))
        rendition_ids = [row['recipe']['rendition_id'] for row in versions if row['schema'] == 2]
        for artifact in get_required_sound_rendition_artifacts(context['renditions'], context['registry'], rendition_ids):
            paths.add(store._path(artifact['asset_id'], '.source'))
            paths.add(store._path(artifact['asset_id'], '.pcm'))
        approved_sources = {}
        for path in sorted(paths):
            check()
            base = root if path.is_relative_to(root) else path.parent
            facts, _ = capture_scope_file(base, str(path.relative_to(base)),
                limit=MAX_TOTAL_BYTES, cancel_check=check)
            approved_sources[str(path)] = ({'version':facts['file_version'], 'sha256':facts['sha256']}
                if facts['exists'] else None)
        if any(path not in approved_sources or approved_sources[path] != identity
                for path, identity in cover_sources.items()):
            raise ValueError('Cover publication sources changed before capture')
    snapshot = ExportSourceSnapshot(manager,cancel_check=check,
                                    additional_sources=tuple(str(path) for path in sorted(paths)))
    if any(path not in snapshot.identities or snapshot.identities[path] != identity
            for path, identity in approved_sources.items()):
        raise ValueError('Sound publication sources changed before capture')
    with ensure_approved_sound_candidate(root,stage_name,candidate_revision,approval_revision,
                                         delivery,cancel_check=check):
        pass
    return snapshot
