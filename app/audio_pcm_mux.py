"""Bounded native MP3/WAV export of pinned PCM under existing audio ownership."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from audio_encoder_identity import get_ffmpeg_runtime_identity, get_ffmpeg_runtime_digest
from audio_mastering_filter import get_mastering_options
from json_metadata import get_metadata_json_bytes
from media_process import run_owned_media_check
from pcm_mux_profile import get_pcm_mux_profile
from pcm_mux_worker_request import MAX_REQUEST_BYTES, get_pcm_mux_worker_request
from pcm_worker_contract import PCMInputShape, MAX_PCM_BYTES, get_pcm_encode_timeout, get_pcm_output_shape
from project_scope_files import capture_scope_file


def export_bounded_pcm(raw_path, output_path, settings, cancel_check, *, quality_plan,
        max_output_bytes, mastering=None, quality_callback=None, bitrate='192k', encoder_runtime=None):
    from audio_export import ensure_export_quality_output
    from audio_mastering import get_loudness_measurements, get_mastering_report
    from export_quality import get_export_quality_plan_from_record
    from tts import ensure_audio_export_active
    plan = get_export_quality_plan_from_record(quality_plan)
    fmt = plan['format']
    if fmt not in ('mp3','wav'):
        raise ValueError('Bounded PCM export requires MP3 or WAV')
    mastering = get_mastering_options(mastering)
    shape = PCMInputShape.model_validate(dict(raw_name=Path(raw_path).name,
        **{key:settings[key] for key in ('frames','frame_rate','sample_width','channels')},
        output_rate=plan['sample_rate'],output_channels=plan['channels']))
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_PCM_BYTES:
        raise ValueError('Invalid bounded PCM output allowance')
    get_pcm_output_shape(fmt,shape,max_output_bytes)
    # Validate caller-controlled codec choices before hashing or native probing.
    if bitrate not in ('128k','192k'):
        raise ValueError('Unsupported bounded PCM bitrate')
    destination = Path(output_path).absolute()
    raw_path = Path(raw_path).absolute()
    if raw_path.parent != destination.parent:
        raise ValueError('PCM input must belong to the destination private stage')
    check = lambda: ensure_audio_export_active(cancel_check)
    check()
    facts, _ = capture_scope_file(raw_path.parent,raw_path.name,limit=MAX_PCM_BYTES,cancel_check=check)
    if not facts['exists'] or facts['file_version'][2] != shape.frames*shape.sample_width*shape.channels:
        raise ValueError('Staged PCM size changed')
    runtime = get_ffmpeg_runtime_identity(check)
    runtime_digest = get_ffmpeg_runtime_digest(runtime)
    if encoder_runtime is not None and runtime_digest != encoder_runtime:
        raise ValueError('Native audio encoder profile changed before encoding')
    timeout = get_pcm_encode_timeout(shape)
    inputs = ['-f',{1:'s8',2:'s16le',4:'s32le'}[shape.sample_width],
        '-ar',str(shape.frame_rate),'-ac',str(shape.channels),'-i',str(raw_path)]
    mix_channels = shape.output_channels if shape.output_channels != shape.channels else None
    before = (get_loudness_measurements(inputs,mastering,cancel_check,output_channels=mix_channels,
        executable=runtime.binary.path,timeout=timeout) if mastering is not None else None)
    with tempfile.TemporaryDirectory(prefix='.pcm-encode-',dir=destination.parent) as directory:
        request = dict(**shape.model_dump(),schema_version=1,format=fmt,
            raw=dict(version=facts['file_version'],sha256=facts['sha256']),
            encoder=runtime.model_dump(),output_limit=max_output_bytes,mux_profile=get_pcm_mux_profile(),
            bitrate=bitrate,mastering=mastering,measured=None if before is None else {
                key:float(before[key]) for key in ('input_i','input_tp','input_lra','input_thresh','target_offset')})
        for _ in range(4):
            payload = get_metadata_json_bytes(request,MAX_REQUEST_BYTES,'PCM worker request')
            available = max_output_bytes-len(payload)
            if available < 1:
                raise ValueError('Insufficient PCM worker output allowance')
            if request['output_limit'] <= available:
                break
            request['output_limit'] = available
        else:
            raise ValueError('PCM request allowance did not stabilize')
        get_pcm_mux_worker_request(payload)
        (Path(directory)/'request.json').write_bytes(payload)
        tail = bytearray()
        def consume(line):
            tail.extend((line+'\n').encode('utf-8'))
            if len(tail)>65536:
                del tail[:-65536]
        run_owned_media_check([sys.executable,str(Path(__file__).with_name('pcm_mux_worker.py')),str(directory)],
            check,timeout,input_source=subprocess.DEVNULL,consume_line=consume)
        output = Path(directory)/('output.'+fmt)
        if output.stat().st_size > request['output_limit']:
            raise ValueError('PCM output exceeds its reserved allowance')
        report = (get_mastering_report(output,mastering,before,tail.decode('utf-8',errors='replace'),
            cancel_check,executable=runtime.binary.path,timeout=timeout) if before is not None else None)
        ensure_export_quality_output(output,plan,quality_callback,cancel_check)
        check()
        current, _ = capture_scope_file(raw_path.parent,raw_path.name,limit=MAX_PCM_BYTES,cancel_check=check)
        if current != facts:
            raise ValueError('Staged PCM changed during encoding')
        if get_ffmpeg_runtime_digest(get_ffmpeg_runtime_identity(check)) != runtime_digest:
            raise ValueError('Native audio encoder profile changed during encoding')
        os.replace(output,destination)
        return report
