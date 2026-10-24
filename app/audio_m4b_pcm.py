"""Native AAC/M4B encoding of captured PCM with independent quality admission."""
import os
import subprocess
import sys
from pathlib import Path
import tempfile

from audio_encoder_identity import get_ffmpeg_runtime_identity, get_ffmpeg_runtime_digest
from media_process import run_owned_media_check
from json_metadata import get_metadata_json_bytes
from project_scope_files import capture_scope_file
from m4b_worker_request import MAX_REQUEST_BYTES, MAX_PCM_BYTES, M4BInputShape, get_m4b_worker_request
from m4b_mux_profile import get_m4b_mux_profile
from pcm_worker_contract import get_pcm_encode_timeout
from audio_export import ensure_export_quality_output
from audio_mastering import get_loudness_measurements, get_mastering_filter, get_mastering_report
from export_quality import get_export_quality_plan_from_record
from m4b_metadata import get_m4b_metadata_text, escape_m4b_metadata
from m4b_cover import ensure_m4b_output_cover, MAX_COVER_BYTES
from tts import ensure_audio_export_active


def export_pcm_m4b(raw_path, output_path, settings, cancel_check, *, quality_plan,
                   max_output_bytes, mastering=None, quality_callback=None,cover_path=None,
                   cover_sha256=None,encoder_runtime=None):
    """Keep raw source clock, authored chapters/tags and native AAC mastering.

    Metadata bytes count toward the caller's remaining staging allowance.
    One guarded seekable output enforces the remaining byte allowance before
    every native write. Independent parent quality admission remains required.
    """
    plan = get_export_quality_plan_from_record(quality_plan)
    from audio_mastering_filter import get_mastering_options
    mastering = get_mastering_options(mastering)
    if (cover_path is None) != (cover_sha256 is None):
        raise ValueError('M4B cover input requires explicit approved digest')
    if plan['format'] != 'm4b' or plan['chapters'] is None or plan['expected_tags'] is None:
        raise ValueError('M4B delivery requires an explicit chapter and metadata plan')
    width,rate,channels,frames = (settings[key] for key in
                                ('sample_width','frame_rate','channels','frames'))
    if (any(type(value) is not int or value <= 0 for value in (width,rate,channels,frames))
            or width not in (1,2,4)):
        raise ValueError('Invalid staged M4B PCM geometry')
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= MAX_PCM_BYTES:
        raise ValueError('Invalid bounded M4B output allowance')
    check = lambda: ensure_audio_export_active(cancel_check)
    shape = M4BInputShape.model_validate(dict(raw_name=Path(raw_path).name,
        cover_name=Path(cover_path).name if cover_path is not None else None,
        frames=frames,frame_rate=rate,sample_width=width,channels=channels,
        output_rate=plan['sample_rate'],output_channels=plan['channels']))
    timeout = get_pcm_encode_timeout({key:getattr(shape,key) for key in (
        'raw_name','frames','frame_rate','sample_width','channels','output_rate','output_channels')})
    metadata = get_m4b_metadata_text(plan['expected_tags'],plan['chapters'],
        escape_m4b_metadata,max_bytes=min(8*1024**2,max_output_bytes)).encode('utf-8')
    input_args = ['-f',{1:'s8',2:'s16le',4:'s32le'}[width],'-ar',str(rate),
                  '-ac',str(channels),'-i',str(raw_path)]
    destination = Path(output_path)
    with tempfile.TemporaryDirectory(prefix='.pcm-encode-',dir=destination.parent) as directory:
        output = Path(directory)/'output.m4b'
        meta = Path(directory)/'chapters.txt'
        meta.write_bytes(metadata)
        source_root = destination.parent.absolute()
        raw_path = Path(raw_path).absolute()
        if raw_path.parent != source_root or (cover_path is not None
                and Path(cover_path).absolute().parent != source_root):
            raise ValueError('M4B inputs must belong to the destination private stage')
        def identity(root, name, limit):
            facts, _ = capture_scope_file(root, name, limit=limit,cancel_check=check)
            if not facts['exists']:
                raise ValueError('Staged M4B input is missing')
            return dict(version=facts['file_version'],sha256=facts['sha256'])
        cover_identity = (identity(source_root,Path(cover_path).name,MAX_COVER_BYTES)
                          if cover_path is not None else None)
        if cover_identity is not None and cover_identity['sha256'] != cover_sha256:
            raise ValueError('Approved M4B cover changed before encoding')
        raw_identity = identity(source_root,raw_path.name,MAX_PCM_BYTES)
        if raw_identity['version'][2] != frames*width*channels:
            raise ValueError('Invalid staged M4B PCM geometry')
        runtime = get_ffmpeg_runtime_identity(check)
        runtime_digest = get_ffmpeg_runtime_digest(runtime)
        if encoder_runtime is not None and encoder_runtime != runtime_digest:
            raise ValueError('Native audio encoder profile changed before encoding')
        request = dict(schema_version=1,encoder=runtime.model_dump(),raw_name=raw_path.name,
            raw=raw_identity,
            metadata=identity(Path(directory),'chapters.txt',8*1024**2),
            cover_name=Path(cover_path).name if cover_path is not None else None,cover=cover_identity,
            frames=frames,frame_rate=rate,sample_width=width,channels=channels,
            output_rate=plan['sample_rate'],output_channels=plan['channels'],
            output_limit=max_output_bytes-len(metadata),mux_profile=get_m4b_mux_profile(),
            mastering=mastering,measured=None)
        # Validate static geometry, confined filenames and captured regular-file
        # identities before any loudness process can read the PCM pathname.
        # Mastering options were parsed above; its final paired measurements
        # are admitted only after the real measurement pass below.
        get_m4b_worker_request(get_metadata_json_bytes(
            {**request,'mastering':None,'measured':None},MAX_REQUEST_BYTES,'M4B worker request'))
        before = get_loudness_measurements(input_args,mastering,cancel_check,
            executable=runtime.binary.path,timeout=timeout) if mastering is not None else None
        if before is not None:
            request['measured'] = {key:float(before[key]) for key in (
                'input_i','input_tp','input_lra','input_thresh','target_offset')}
        # Account for the request itself. Monotone reduction leaves at most a
        # digit of harmless slack at decimal-size boundaries; never over-allocate.
        for _ in range(4):
            payload = get_metadata_json_bytes(request,MAX_REQUEST_BYTES,'M4B worker request')
            available = max_output_bytes-len(metadata)-len(payload)
            if available < 1:
                raise ValueError('Insufficient M4B worker output allowance')
            if request['output_limit'] <= available:
                break
            request['output_limit'] = available
        else:
            raise ValueError('M4B request allowance did not stabilize')
        get_m4b_worker_request(payload)
        (Path(directory)/'request.json').write_bytes(payload)
        tail = bytearray()
        def consume(line):
            tail.extend((line+'\n').encode('utf-8'))
            if len(tail)>65536:
                del tail[:-65536]
        run_owned_media_check([sys.executable,str(Path(__file__).with_name('m4b_mux_worker.py')),str(directory)],
            lambda: ensure_audio_export_active(cancel_check),timeout,
            input_source=subprocess.DEVNULL,consume_line=consume)
        stderr = tail.decode('utf-8',errors='replace')
        if output.stat().st_size > request['output_limit']:
            raise ValueError('M4B exceeds its reserved output allowance')
        report = get_mastering_report(output,mastering,before,stderr,cancel_check,
            executable=runtime.binary.path,timeout=timeout) if before is not None else None
        cover_identity = ensure_m4b_output_cover(output,cover_sha256,cancel_check)
        def admit_report(receipt):
            if any(receipt[key] != cover_identity[key] for key in ('sha256','file_version','output_bytes')):
                raise ValueError('M4B changed after cover validation')
            if quality_callback is not None:
                quality_callback(receipt)
        ensure_export_quality_output(output,plan,admit_report,cancel_check)
        ensure_audio_export_active(cancel_check)
        if get_ffmpeg_runtime_digest(get_ffmpeg_runtime_identity(check)) != runtime_digest:
            raise ValueError('Native audio encoder profile changed during encoding')
        os.replace(output,destination)
        return report
