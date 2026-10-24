"""Render one full private mixed candidate under the existing audio task owner."""
import os
from pathlib import Path

from adapter_publication import save_adapter_publication_bytes
from book_state_transaction import get_snapshot_digest
from json_metadata import get_metadata_json_bytes
from sound_design_assets import SoundDesignAssetStore, MAX_TOTAL_BYTES
from sound_design_capture import ensure_sound_design_audio_capture
from sound_design_candidate import MAX_CANDIDATE_METADATA, get_sound_candidate_document
from sound_design_mix import write_sound_design_pcm
from sound_design_registry import get_selected_sound_design_versions
from sound_design_rendition_media import ensure_sound_rendition_assets
from sound_design_staging import ensure_sound_render_stage
from tts import ensure_audio_export_active


def save_sound_design_candidate(root, expected_context, *, cancel_check=None,
                                max_staged_bytes=MAX_TOTAL_BYTES, timeout=300):
    """Return a private candidate only after complete capture and quota fences.

    The full PCM and manifest are retained for subsequent audition/approval.
    This does not approve media, publish an export, or claim listener validation.
    The caller must already own the existing audio task; no GPU is required.
    """
    root = Path(root).absolute()
    with ensure_sound_render_stage(root, cancel_check=cancel_check,
                                   max_bytes=max_staged_bytes) as (stage, available):
        # Reserve metadata before PCM work. The atomic writer has one temporary
        # copy and replaces an absent manifest, so no second retained copy exists.
        allowance = available - MAX_CANDIDATE_METADATA
        if allowance <= 0:
            raise ValueError('Insufficient sound candidate staging allowance')
        with ensure_sound_design_audio_capture(root, stage, expected_context,
                cancel_check=cancel_check, max_staged_bytes=allowance, timeout=timeout) as captured:
            context = captured['context']
            versions = get_selected_sound_design_versions(context['registry'])
            rendition_ids = [row['recipe']['rendition_id'] for row in versions if row['schema'] == 2]
            with ensure_sound_rendition_assets(root, context, rendition_ids,
                    check_active=captured['check_active']):
                with (stage / 'mixed.pcm').open('x+b') as output:
                    report = write_sound_design_pcm(captured['dry'], output, versions,
                        context['book'], captured['dialogue'], captured['pcm_plan'],
                        SoundDesignAssetStore(root), stage, check_active=captured['check_active'],
                        max_staged_bytes=allowance, timeout=timeout)
                    output.flush(); os.fsync(output.fileno())
                binding = dict(context_revision=context['context_revision'], book=context['book'],
                               registry_revision=context['registry_revision'],
                               cue_versions=[row['version'] for row in versions],
                               dialogue=captured['dialogue'], pcm_plan=captured['pcm_plan'], mix=report)
                document = dict(schema=1, candidate_revision=get_snapshot_digest(binding),
                                binding=binding, pcm_file='mixed.pcm')
                raw = get_metadata_json_bytes(document, MAX_CANDIDATE_METADATA, 'Sound candidate')
                get_sound_candidate_document(raw, document['candidate_revision'])
        # Successful capture exit fences source/take/context again. Only now
        # create the durable candidate descriptor for later review.
        ensure_audio_export_active(cancel_check)
        save_adapter_publication_bytes(stage / 'candidate.json', raw, mode=0o600)
        ensure_audio_export_active(cancel_check)
    return dict(candidate_revision=document['candidate_revision'],
                directory=stage.relative_to(root).as_posix(), mix=report)
