"""Explicit consent to one full mixed candidate and normalized delivery options."""
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess

from adapter_publication import save_adapter_publication_bytes
from audio_generation_owner import ensure_audio_generation_publication
from audio_mastering import get_mastering_options
from book_state_transaction import get_snapshot_digest
from export_quality import get_export_quality_options
from json_metadata import get_metadata_json_bytes, get_unique_json_object
from project_scope_files import capture_scope_file
from sound_design_assets import MAX_TOTAL_BYTES
from sound_design_current import ensure_current_sound_candidate
from sound_design_cover import get_sound_delivery_cover
from sound_design_cues import _require_digest, _require_choice
from sound_design_staging import ensure_sound_render_quota, DIRECTORY
from source_evidence import _require_keys
from tts import ensure_audio_export_active

MAX_APPROVAL_BYTES = 512 * 1024


def get_sound_delivery_profiles(*,cancel_check=None):
    profiles = {}
    for fmt in ('mp3','wav','m4b'):
        ensure_audio_export_active(cancel_check)
        try:
            encoding = get_sound_delivery_encoding({'format':fmt,'export_quality':None,'mastering':None},cancel_check=cancel_check)
        except (ImportError,ValueError,OSError,subprocess.SubprocessError):
            # Recovery and other format settings remain usable. Diagnostics
            # can contain executable paths; expose only a fixed format status.
            profiles[fmt] = dict(available=False,revision=None,
                                 summary=f'{fmt.upper()} is currently unavailable. Refresh before approving it.')
            continue
        profiles[fmt] = dict(available=True,revision=get_snapshot_digest(encoding),
            summary={'mp3':'MP3 · 128 kb/s', 'wav':'WAV · original PCM',
                     'm4b':'M4B · AAC 128 kb/s · chapters and reviewed cover'}[fmt])
    ensure_audio_export_active(cancel_check)
    return profiles


def get_sound_delivery_options(value):
    if not isinstance(value,dict):
        raise ValueError('Invalid sound delivery options')
    _require_keys({key:item for key,item in value.items() if key != 'm4b'},
                  ('format','export_quality','mastering'), 'Sound delivery options')
    _require_choice(value['format'], ('wav','mp3','m4b'), 'sound delivery format')
    result = dict(format=value['format'], export_quality=get_export_quality_options(value['export_quality']),
                mastering=get_mastering_options(value['mastering']))
    if value['format'] == 'm4b':
        from m4b_metadata import M4BDeliveryOptions
        metadata = value.get('m4b')
        result['m4b'] = M4BDeliveryOptions.model_validate({} if metadata is None else metadata).model_dump()
    elif value.get('m4b') is not None:
        raise ValueError('M4B settings require M4B delivery')
    return result


def _get_sound_delivery_policy(delivery):
    """Versioned format policy included in the user's approval digest.

    PCM geometry and source chapter ranges are already pinned by the candidate.
    Changing this policy requires new approval, not reinterpretation of consent.
    """
    options = get_sound_delivery_options(delivery)
    if options['format'] in ('mp3','wav'):
        from pcm_mux_profile import get_pcm_mux_profile
        profile = get_pcm_mux_profile()
        if options['format'] == 'mp3':
            return dict(version=2,format='mp3',codec='libmp3lame',bitrate='128k',
                        id3v2_version=4,**profile)
        return dict(version=2,format='wav',codec='source-pcm',rf64='auto',**profile)
    from m4b_mux_profile import get_m4b_mux_profile
    return dict(version=3, format='m4b', codec='aac', bitrate='128k',
                movflags='+faststart', chapters='source-frame-ranges', **get_m4b_mux_profile())


def get_sound_delivery_encoding(delivery, *, cancel_check=None):
    policy = _get_sound_delivery_policy(delivery)
    import av
    if policy['format'] == 'm4b':
        from m4b_mux_profile import require_m4b_mux_runtime
        require_m4b_mux_runtime(av)
    else:
        from pcm_mux_profile import require_pcm_mux_runtime
        require_pcm_mux_runtime(av)
    from audio_encoder_identity import get_ffmpeg_runtime_identity, get_ffmpeg_runtime_digest
    identity = get_ffmpeg_runtime_identity(lambda: ensure_audio_export_active(cancel_check))
    policy = {**policy,'ffmpeg_runtime':get_ffmpeg_runtime_digest(identity)}
    return policy


def get_sound_approval(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_APPROVAL_BYTES:
        raise ValueError('Sound approval exceeds its metadata allowance')
    try:
        document = json.loads(raw, object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite sound approval')))
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid sound approval') from error
    _require_keys(document, ('schema','candidate_revision','delivery','encoding','full_mix_reviewed','approval_revision'),
                  'Sound approval')
    if type(document['schema']) is not int or document['schema'] != 2 or document['full_mix_reviewed'] is not True:
        raise ValueError('Sound approval requires explicit full-mix confirmation')
    for key in ('candidate_revision','approval_revision'): _require_digest(document[key], key)
    if get_sound_delivery_options(document['delivery']) != document['delivery']:
        raise ValueError('Sound approval options must be normalized')
    encoding = document['encoding']
    if document['delivery']['format'] == 'm4b':
        version = encoding.get('version') if isinstance(encoding,dict) else None
        if type(version) is not int or version not in (1,2,3):
            raise ValueError('Unsupported sound M4B encoder profile')
        # Historical receipts describe their original decision. Their schema
        # must not depend on fields added to the current encoder policy.
        fields = ('version','format','codec','bitrate','movflags','chapters')
        if version >= 2:
            fields += ('muxer','mux_avformat','mux_avcodec','mux_avutil',
                       'mux_transport','movie_clock')
        template = dict.fromkeys(fields,'')
        template['version'] = version
        if version == 3:
            template['ffmpeg_runtime'] = '0'*64
            _require_digest(encoding.get('ffmpeg_runtime'),'Native encoder runtime')
    else:
        version = encoding.get('version') if isinstance(encoding,dict) else None
        if type(version) is not int or version not in (1,2):
            raise ValueError('Unsupported sound PCM encoder profile')
        fields = (('version','format','codec','bitrate','id3v2_version')
                  if document['delivery']['format'] == 'mp3' else
                  ('version','format','codec','rf64'))
        if version == 2:
            fields += ('muxer','mux_avformat','mux_avcodec','mux_avutil',
                       'mux_transport','mp3_header','wav_header','ffmpeg_runtime')
            _require_digest(encoding.get('ffmpeg_runtime'),'Native encoder runtime')
        template = dict.fromkeys(fields,'')
        template['version'] = version
        if document['delivery']['format'] == 'mp3':
            template['id3v2_version'] = 4
    _require_keys(encoding, tuple(template), 'Sound encoder profile')
    if (encoding['format'] != document['delivery']['format']
            or any(type(encoding[key]) is not type(value) for key,value in template.items())
            or any(isinstance(value,str) and not 1 <= len(value) <= 64 for value in encoding.values())
            or any(type(value) is int and not 1 <= value <= 100 for value in encoding.values())):
        raise ValueError('Invalid sound encoder profile')
    seed = {key:value for key,value in document.items() if key != 'approval_revision'}
    if get_snapshot_digest(seed) != document['approval_revision']:
        raise ValueError('Sound approval revision changed')
    return document


def save_sound_candidate_approval(root, stage_name, expected_candidate, delivery, *,
        full_mix_reviewed=False, expected_approval=None, cancel_check=None,
        max_staged_bytes=MAX_TOTAL_BYTES, expected_encoding=None):
    """Record consent after verification; every delivery must revalidate again.

    A short audition or a general implementation approval is not this explicit
    user attestation. The receipt remains a historical decision if sources later
    change; it never authorizes a stale candidate to be delivered.
    """
    if full_mix_reviewed is not True:
        raise ValueError('Confirm review of the full mixed candidate before approval')
    _require_digest(expected_candidate, 'Expected candidate')
    _require_digest(expected_approval, 'Expected approval', nullable=True)
    options = get_sound_delivery_options(delivery)
    _require_digest(expected_encoding,'Expected encoder profile',nullable=True)
    check = lambda: ensure_audio_export_active(cancel_check)
    encoding = get_sound_delivery_encoding(options,cancel_check=check)
    if expected_encoding is not None and expected_encoding != get_snapshot_digest(encoding):
        raise ValueError('Sound encoder profile changed; refresh before approval')
    root = Path(root).absolute()
    with ensure_sound_render_quota(root, cancel_check=check, max_bytes=max_staged_bytes) as (_,available,_):
        with ensure_current_sound_candidate(root,stage_name,expected_candidate,cancel_check=check):
            get_sound_delivery_cover(root,options,cancel_check=check)
        relative = f'{DIRECTORY}/{stage_name}/approval.json'
        proof, previous = capture_scope_file(root,relative,include_bytes=True,
                                             limit=MAX_APPROVAL_BYTES,cancel_check=check)
        old = get_sound_approval(previous) if previous is not None else None
        if (old['approval_revision'] if old else None) != expected_approval:
            raise ValueError('Sound approval changed; refresh before confirming')
        seed = dict(schema=2,candidate_revision=expected_candidate,delivery=options,
                    encoding=encoding,full_mix_reviewed=True)
        document = {**seed,'approval_revision':get_snapshot_digest(seed)}
        raw = get_metadata_json_bytes(document,MAX_APPROVAL_BYTES,'Sound approval')
        get_sound_approval(raw)
        if previous == raw:
            check()
            return document
        # Count the complete new temporary alongside the existing receipt. The
        # shared quota lease excludes cooperating render/approval writers.
        if len(raw) > available:
            raise ValueError('Insufficient quota for sound approval metadata')
        check()
        if get_sound_delivery_encoding(options,cancel_check=check) != encoding:
            raise ValueError('Sound encoder profile changed before approval')
        with ensure_audio_generation_publication():
            check()
            current,_ = capture_scope_file(root,relative,limit=MAX_APPROVAL_BYTES,cancel_check=check)
            if current != proof:
                raise ValueError('Sound approval changed before publication')
            save_adapter_publication_bytes(root/relative,raw,mode=0o600)
    return document


@contextmanager
def ensure_approved_sound_candidate(root, stage_name, expected_candidate, expected_approval,
                                     delivery, *, cancel_check=None):
    """Admit exactly the approved settings and currently valid full candidate.

    Encoders consume this descriptor privately and publish only after successful
    exit plus their existing output-quality/publication checks.
    """
    _require_digest(expected_approval, 'Expected approval')
    options = get_sound_delivery_options(delivery)
    check = lambda: ensure_audio_export_active(cancel_check)
    with ensure_current_sound_candidate(root, stage_name, expected_candidate,
                                         cancel_check=check) as (candidate, stream):
        relative = f'{DIRECTORY}/{stage_name}/approval.json'
        proof, raw = capture_scope_file(root, relative, include_bytes=True,
                                        limit=MAX_APPROVAL_BYTES, cancel_check=check)
        approval = get_sound_approval(raw)
        if (approval['approval_revision'] != expected_approval
                or approval['candidate_revision'] != expected_candidate
                or approval['delivery'] != options):
            raise ValueError('Sound approval does not match this candidate and delivery')
        if get_snapshot_digest(approval['encoding']) != get_snapshot_digest(get_sound_delivery_encoding(options,cancel_check=check)):
            raise ValueError('Sound encoder profile changed; review and approve delivery again')
        get_sound_delivery_cover(root,options,cancel_check=check)
        yield candidate, stream
        if get_snapshot_digest(approval['encoding']) != get_snapshot_digest(get_sound_delivery_encoding(options,cancel_check=check)):
            raise ValueError('Sound encoder profile changed during delivery')
        get_sound_delivery_cover(root,options,cancel_check=check)
        current, _ = capture_scope_file(root, relative, limit=MAX_APPROVAL_BYTES,cancel_check=check)
        if current != proof:
            raise ValueError('Sound approval changed during delivery')
