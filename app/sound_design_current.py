"""Current book, take and asset admission for candidate approval/delivery."""
from contextlib import contextmanager, ExitStack
import time

from project_scope_admission import ensure_audio_scope_admission
from sound_design_candidate import ensure_sound_candidate_open
from sound_design_context import (capture_sound_design_context, capture_sound_design_anchor,
                                  require_sound_design_context_files_current)
from sound_design_registry import get_selected_sound_design_versions
from sound_design_cues import get_sound_design_cue_plan
from sound_design_assets import SoundDesignAssetStore
from sound_design_rendition_media import ensure_sound_rendition_assets
from tts import ensure_audio_export_active


@contextmanager
def ensure_current_sound_candidate(root, stage_name, expected_revision, *, cancel_check=None):
    """Keep existing task/book admission and verified asset handles until exit.

    This establishes current identities, not user consent. Callers must obtain
    explicit approval separately and publish only after successful context exit.
    """
    deadline = time.monotonic() + 300
    def check():
        ensure_audio_export_active(cancel_check)
        if time.monotonic() >= deadline:
            raise TimeoutError('Current candidate verification exceeded its deadline')
    with ensure_audio_scope_admission(root), ensure_sound_candidate_open(
            root, stage_name, expected_revision, cancel_check=check) as (document, stream), ExitStack() as assets:
        binding = document['binding']
        context = capture_sound_design_context(root, check_active=check)
        if (context['context_revision'] != binding['context_revision']
                or context['book'] != binding['book']
                or context['registry_revision'] != binding['registry_revision']):
            raise ValueError('Sound candidate context is stale; render again')
        versions = get_selected_sound_design_versions(context['registry'])
        if [row['version'] for row in versions] != binding['cue_versions']:
            raise ValueError('Sound candidate cue selection changed')
        anchors = [capture_sound_design_anchor(root, context, row['uid'], check_active=check)
                   for row in context['chunks']]
        if [anchor['selection'] for anchor in anchors] != binding['dialogue']:
            raise ValueError('Sound candidate selected takes changed; render again')
        projection = get_sound_design_cue_plan(versions, context['book'], binding['dialogue'],
                                               binding['pcm_plan'], check_active=check)
        if (not projection['can_project']
                or projection['projection_revision'] != binding['mix']['projection_revision']):
            raise ValueError('Sound candidate projection changed; render again')
        store = SoundDesignAssetStore(root)
        rendition_ids = [row['recipe']['rendition_id'] for row in versions if row['schema'] == 2]
        known = assets.enter_context(ensure_sound_rendition_assets(root, context, rendition_ids,
                                                                  check_active=check))
        for version in versions:
            check()
            expected = version['recipe']['asset']; identity = expected['asset_id']
            if identity not in known:
                _, known[identity] = assets.enter_context(store.open_canonical(identity, cancel_check=check))
            if known[identity] != expected:
                raise ValueError('Sound candidate asset facts changed')
        def fence():
            check()
            require_sound_design_context_files_current(root, context)
            for anchor in anchors:
                check()
                require_sound_design_context_files_current(root, context, anchor)
        fence()
        stream.seek(0)
        yield document, stream
        fence()
