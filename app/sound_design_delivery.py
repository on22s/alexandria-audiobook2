"""Private, quota-reserved input for delivery of an explicitly approved mix."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path

from book_state_transaction import ensure_book_state
from merge_integrity import get_source_integrity, is_source_integrity_admitted
from sound_design_approval import (ensure_approved_sound_candidate, get_sound_delivery_options,
                                   get_sound_delivery_encoding)
from sound_design_assets import MAX_TOTAL_BYTES
from sound_design_cues import _require_int
from sound_design_publication import capture_sound_delivery_snapshot
from sound_design_cover import get_sound_delivery_cover
from sound_design_staging import (ensure_sound_render_stage, ensure_sound_render_quota,
                                  get_sound_render_stage_files)
from tts import ensure_audio_export_active
from utils import safe_load_json


class SoundDeliveryIntegrityRequired(ValueError):
    """Carry the same reviewable report as ordinary Editor merge admission."""
    def __init__(self, report):
        super().__init__('Review current source integrity before sound delivery')
        self.report = report


@contextmanager
def ensure_sound_delivery_stage(manager, stage_name, candidate_revision, approval_revision,
        delivery, *, minimum_output_bytes, integrity_confirmation=None,
        cancel_check=None, max_staged_bytes=MAX_TOTAL_BYTES):
    """Copy pinned approved PCM before allowing an encoder to consume its path.

    Keep the shared staging lease through encoding and publication, excluding
    discard and charging retained candidates plus the new raw copy. The caller
    must bound *all* encoder temporaries by output_allowance, validate the full
    output, then verify_sources and use source_snapshot.ensure_publication for
    its final rename. This context does not itself authorize publication.

    Failed calls remove only their new private stage; successful stages remain
    recoverable until explicitly discarded. No duration-sized RAM allocation.
    """
    _require_int(minimum_output_bytes, 1, MAX_TOTAL_BYTES, 'Delivery output reservation')
    root = Path(manager.root_dir).absolute()
    check = lambda: ensure_audio_export_active(cancel_check)
    with ensure_sound_render_stage(root, cancel_check=check,
                                   max_bytes=max_staged_bytes) as (stage, available):
        # Snapshot construction owns chunk locks: never nest it inside the
        # approved-candidate context, which takes those same non-reentrant locks.
        snapshot = capture_sound_delivery_snapshot(manager, stage_name, candidate_revision,
            approval_revision, delivery, cancel_check=check)
        # Compare the exact rows bound by the publication snapshot, using the
        # ordinary Editor comparison and confirmation policy. Mix approval is
        # never a substitute for reviewing differences from source prose.
        with ensure_book_state(root):
            state = safe_load_json(root / 'state.json', {})
            try:
                script_bytes = Path(manager.script_path).read_bytes()
            except FileNotFoundError:
                script_bytes = b''
            integrity = get_source_integrity(state, script_bytes, snapshot.chunks)
        snapshot.verify_sources()
        if not is_source_integrity_admitted(integrity, integrity_confirmation):
            raise SoundDeliveryIntegrityRequired(integrity)
        raw = stage / 'input.pcm'
        with ensure_approved_sound_candidate(root, stage_name, candidate_revision,
                approval_revision, delivery, cancel_check=check) as (document, stream):
            binding = document['binding']
            expected = binding['mix']['bytes']
            cover_bytes = get_sound_delivery_cover(root,delivery,cancel_check=check)
            output_allowance = available - expected - (len(cover_bytes) if cover_bytes is not None else 0)
            if output_allowance < minimum_output_bytes:
                raise ValueError('Insufficient quota for sound delivery input and output')
            digest, copied = hashlib.sha256(), 0
            with raw.open('xb') as output:
                while copied < expected:
                    check()
                    block = stream.read(min(1024**2, expected - copied))
                    if not block:
                        raise ValueError('Approved sound delivery input is truncated')
                    output.write(block)
                    digest.update(block)
                    copied += len(block)
                check()
                if stream.read(1) or digest.hexdigest() != binding['mix']['sha256']:
                    raise ValueError('Approved sound delivery input changed')
                output.flush()
                os.fsync(output.fileno())
            cover_path = None
            if cover_bytes is not None:
                cover_path = stage/'cover.image'
                with cover_path.open('xb') as cover:
                    cover.write(cover_bytes)
                    cover.flush()
                    os.fsync(cover.fileno())
        # Successful approval-context exit fences its source, manifest and PCM.
        snapshot.verify_sources()
        check()
        yield dict(directory=stage, raw=raw, pcm_plan=binding['pcm_plan'],
                   output_allowance=output_allowance, source_snapshot=snapshot,
                   check_active=check, integrity=integrity, dialogue=binding['dialogue'],cover=cover_path)


def save_sound_delivery(manager, stage_name, candidate_revision, approval_revision,
        delivery, *, integrity_confirmation=None, cancel_check=None,
        max_staged_bytes=MAX_TOTAL_BYTES):
    """Encode and publish a separate mixed audio file under the existing audio owner.

    The caller retains the existing audio task owner through all cleanup.
    This never invokes dialogue assembly or alters takes.
    """
    from audio_pcm_mux import export_bounded_pcm
    from export_quality import (get_pcm_export_encoding_plan, get_admitted_export_report,
                                get_export_file_version)
    options = get_sound_delivery_options(delivery)
    encoding = get_sound_delivery_encoding(options,cancel_check=cancel_check)
    fmt = options['format']
    with ensure_sound_delivery_stage(manager, stage_name, candidate_revision, approval_revision,
            options, minimum_output_bytes=1,
            integrity_confirmation=integrity_confirmation, cancel_check=cancel_check,
            max_staged_bytes=max_staged_bytes) as staged:
        output = staged['directory'] / ('output.' + fmt)
        expectation = {}
        if fmt == 'm4b':
            from m4b_metadata import get_m4b_tags, get_m4b_source_chapters
            rows = {row['uid']:row for row in staged['source_snapshot'].chunks}
            ordered = [rows[row['chunk_uid']] for row in staged['dialogue']]
            groups = manager._chapter_groups(ordered,options['m4b']['per_chunk_chapters'])
            expectation = dict(chapters=get_m4b_source_chapters(staged['pcm_plan'],groups,len(ordered)),
                chapter_time_base=1,expected_tags=get_m4b_tags(options['m4b']))
        quality = get_pcm_export_encoding_plan(staged['pcm_plan'], format_name=fmt,
                                              options=options['export_quality'], **expectation)
        reports = []
        from audio_m4b_pcm import export_pcm_m4b
        encoder = {'mp3':export_bounded_pcm,'wav':export_bounded_pcm,'m4b':export_pcm_m4b}[fmt]
        mastering = encoder(staged['raw'], output, staged['pcm_plan'],
            staged['check_active'], mastering=options['mastering'],
            **({'bitrate':encoding['bitrate']} if fmt == 'mp3' else {}),
            **({'cover_path':staged['cover'],'cover_sha256':options['m4b']['cover_sha256'],
                } if fmt == 'm4b' else {}),
            encoder_runtime=encoding['ffmpeg_runtime'],
            quality_plan=quality, quality_callback=reports.append,
            max_output_bytes=staged['output_allowance'])
        report = get_admitted_export_report(output, quality, reports[-1] if reports else None,
                                            cancel_check=staged['check_active'])
        staged['raw'].unlink()
        if staged['cover'] is not None:
            staged['cover'].unlink()
        snapshot = staged['source_snapshot']
    # Finish stage exit checks before an irreversible publication. Reacquire
    # the quota lease; a discarded/replaced private file fails the native
    # output-version fence rather than publishing different bytes.
    with ensure_sound_render_quota(manager.root_dir, cancel_check=cancel_check,
                                   max_bytes=max_staged_bytes):
        snapshot.verify_sources()
        if get_sound_delivery_encoding(options,cancel_check=cancel_check) != encoding:
            raise ValueError('Sound encoder profile changed before publication')
        destination = Path(manager.root_dir) / ('mixed_audiobook.' + fmt)
        # Construct the response before committing. Any later metadata problem
        # must say publication happened rather than inviting a blind retry.
        result = dict(status='published', file=destination.name,
            candidate_revision=candidate_revision, approval_revision=approval_revision,
            quality=None, mastering=mastering, encoding=encoding, warnings=[])
        with snapshot.ensure_publication(str(output), report):
            os.replace(output, destination)
            try:
                version = get_export_file_version(destination)
                if version[:4] != report['file_version'][:4]:
                    raise ValueError('Published output changed')
                result['quality'] = {**report,'file':destination.name,'file_version':version}
            except (OSError,ValueError):
                result['mastering'] = None
                result['warnings'].append('Audio was published, but its current file identity could not be confirmed.')
        # Only remove this now-empty stage. Unknown or retained contents remain
        # charged for recovery, and cleanup failure cannot undo publication.
        try:
            _, files, directories = get_sound_render_stage_files(staged['directory'],Path(manager.root_dir).absolute())
            if files or len(directories) != 1:
                raise ValueError('Delivery stage has retained contents')
            staged['directory'].rmdir()
        except (OSError,ValueError):
            result['warnings'].append('Audio was published; its delivery stage was retained for inspection.')
    return result


def save_sound_delivery_mp3(manager, stage_name, candidate_revision, approval_revision, delivery, **kwargs):
    """Compatibility entry point for the initial MP3 integration."""
    if get_sound_delivery_options(delivery)['format'] != 'mp3':
        raise ValueError('The MP3 delivery encoder requires MP3 approval')
    return save_sound_delivery(manager, stage_name, candidate_revision, approval_revision, delivery, **kwargs)


def save_approved_sound_delivery(manager, stage_name, candidate_revision, approval_revision, *,
        integrity_confirmation=None, cancel_check=None):
    """Deliver the exact saved approval; HTTP callers cannot substitute settings."""
    from project_scope_files import capture_scope_file
    from sound_design_approval import get_sound_approval, MAX_APPROVAL_BYTES
    from sound_design_candidate import validate_sound_candidate_stage
    from sound_design_staging import DIRECTORY
    from sound_design_cues import _require_digest
    validate_sound_candidate_stage(stage_name)
    _require_digest(candidate_revision,'Expected sound candidate')
    _require_digest(approval_revision,'Expected sound approval')
    check = lambda: ensure_audio_export_active(cancel_check)
    _, raw = capture_scope_file(Path(manager.root_dir).absolute(),
        f'{DIRECTORY}/{stage_name}/approval.json',limit=MAX_APPROVAL_BYTES,
        include_bytes=True,cancel_check=check)
    approval = get_sound_approval(raw)
    if (approval['candidate_revision'] != candidate_revision
            or approval['approval_revision'] != approval_revision):
        raise ValueError('Sound approval changed; refresh before delivery')
    return save_sound_delivery(manager,stage_name,candidate_revision,approval_revision,
        approval['delivery'],integrity_confirmation=integrity_confirmation,cancel_check=cancel_check)
