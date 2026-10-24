"""Owned CPU worker: native AAC pipe -> bounded, normal faststart M4B.

The caller holds task/book/quota admission and alone validates and publishes.
FFmpeg inherits the outer descendant owner (including the Windows Job); this
worker never creates a nested reaper or a breakaway process.
"""
from contextlib import ExitStack
import io
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from audio_encoder_identity import require_ffmpeg_binary_identity
from audio_take_history import _open_regular, _version
from audio_mastering_filter import get_mastering_filter
from bounded_media_io import BoundedMediaOutput, ForwardOnlyMediaInput
from m4b_cover import MAX_COVER_BYTES, get_validated_cover_bytes
from m4b_mux_profile import require_m4b_mux_runtime
from media_process import get_media_process_environment
from m4b_worker_request import (get_m4b_worker_request, MAX_REQUEST_BYTES,
                               MAX_METADATA_BYTES, MAX_PCM_BYTES)
from project_scope_files import capture_scope_file
from pcm_worker_contract import get_pcm_worker_error_context


def _capture(root, name, identity, limit, *, include_bytes=False):
    facts, raw = capture_scope_file(root, name, limit=limit, include_bytes=include_bytes)
    if (not facts['exists'] or facts['file_version'] != identity.version
            or facts['sha256'] != identity.sha256):
        raise ValueError('M4B worker source changed before encoding')
    return facts, raw


def run_m4b_mux_worker(directory):
    directory = Path(directory).absolute()
    if (directory.resolve() != directory or not directory.is_dir()
            or re.fullmatch(r'\.pcm-encode-[A-Za-z0-9_-]+', directory.name) is None):
        raise ValueError('M4B worker requires its private canonical directory')
    request_facts, raw = capture_scope_file(directory, 'request.json',
        limit=MAX_REQUEST_BYTES, include_bytes=True)
    if not request_facts['exists']:
        raise ValueError('M4B worker request is missing')
    request = get_m4b_worker_request(raw)
    executable = require_ffmpeg_binary_identity(request.encoder.binary)
    source_root = directory.parent
    raw_facts, _ = _capture(source_root, request.raw_name, request.raw, MAX_PCM_BYTES)
    metadata_facts, metadata = _capture(directory, 'chapters.txt', request.metadata,
        MAX_METADATA_BYTES, include_bytes=True)
    cover_facts = cover_raw = None
    if request.cover is not None:
        cover_facts, cover_raw = _capture(source_root, request.cover_name, request.cover,
            MAX_COVER_BYTES, include_bytes=True)
        media_type = 'image/png' if cover_raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg'
        get_validated_cover_bytes(cover_raw, media_type)
    import av
    require_m4b_mux_runtime(av)
    with av.open(io.BytesIO(metadata), format='ffmetadata') as information:
        tags, chapters = information.metadata, information.chapters()
    # Only the native audiobook tag/chapter domain may reach the muxer.
    if (not set(tags) <= {'title','artist','album_artist','date','comment','genre'}
            or any(not isinstance(v,str) or len(v)>20000 for v in tags.values())
            or len(chapters)>100000):
        raise ValueError('Invalid M4B worker metadata')
    previous = 0
    for row in chapters:
        start, end = row['start']*row['time_base'], row['end']*row['time_base']
        if (not 0 <= previous <= start < end <= request.frames/request.frame_rate+.002
                or set(row['metadata']) != {'title'} or len(row['metadata']['title'])>20000):
            raise ValueError('Invalid M4B worker chapter clock')
        previous = end
    process = reader = None
    tail, reader_errors = bytearray(), []
    def drain():
        try:
            while block := process.stderr.read(4096):
                tail.extend(block)
                if len(tail)>65536:
                    del tail[:-65536]
        except BaseException as error:
            reader_errors.append(error)
            try:
                process.stderr.close()
            finally:
                if process.poll() is None:
                    process.terminate()
    try:
        with ExitStack() as stack:
            source, before = _open_regular(source_root/request.raw_name, source_root, MAX_PCM_BYTES)
            stack.enter_context(source)
            if list(_version(before)) != raw_facts['file_version']:
                raise ValueError('M4B worker PCM changed before open')
            command = [executable,'-nostdin','-hide_banner','-nostats','-xerror',
                '-threads','1','-filter_threads','1','-f',{1:'s8',2:'s16le',4:'s32le'}[request.sample_width],
                '-ar',str(request.frame_rate),'-ac',str(request.channels),'-i','pipe:0','-map','0:a:0']
            if request.mastering is not None:
                command += ['-af',get_mastering_filter(request.mastering.model_dump(),request.measured)]
            command += ['-ar',str(request.output_rate),'-ac',str(request.output_channels),
                '-c:a','aac','-b:a','128k','-movflags','frag_keyframe+delay_moov+default_base_moof',
                '-frag_duration','250000','-f','mp4','pipe:1']
            environment = get_media_process_environment()
            # Inherited FFREPORT writes an uncharged file outside the bounded sink.
            environment.update(TMPDIR=str(directory), TEMP=str(directory), TMP=str(directory))
            output = stack.enter_context(BoundedMediaOutput(directory,'output.m4b',request.output_limit))
            require_ffmpeg_binary_identity(request.encoder.binary)
            process = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, start_new_session=False, creationflags=0, env=environment)
            reader = threading.Thread(target=drain, daemon=True); reader.start()
            with av.open(ForwardOnlyMediaInput(process.stdout),format='mp4') as input_container, av.open(output,'w',format='mp4',
                    options={'movflags':'+faststart','movie_timescale':str(math.lcm(request.output_rate,1000))},
                    io_open=output.reopen) as mux:
                if (len(input_container.streams)!=1 or len(input_container.streams.audio)!=1
                        or input_container.streams.audio[0].codec_context.name != 'aac'
                        or input_container.streams.audio[0].codec_context.sample_rate != request.output_rate
                        or len(input_container.streams.audio[0].codec_context.layout.channels) != request.output_channels):
                    raise ValueError('Unexpected native M4B encoder streams')
                audio_stream = mux.add_stream_from_template(input_container.streams.audio[0])
                mux.metadata.update(tags); mux.set_chapters(chapters)
                if cover_raw is not None:
                    with av.open(io.BytesIO(cover_raw)) as cover:
                        picture_stream = mux.add_stream_from_template(cover.streams.video[0])
                        picture_stream.disposition = 1024
                        for packet in cover.demux(video=0):
                            if packet.size:
                                packet.pts=packet.dts=0; packet.stream=picture_stream; mux.mux(packet)
                for packet in input_container.demux(audio=0):
                    if packet.size:
                        if packet.pts is None or packet.dts is None:
                            raise ValueError('Native AAC packet is missing its source clock')
                        packet.stream=audio_stream; mux.mux(packet)
            output.check(); output.flush()
            if process.wait(timeout=5):
                raise ValueError('Native M4B encoder failed')
            reader.join(timeout=5)
            if reader.is_alive() or reader_errors:
                raise ValueError('M4B encoder diagnostics did not finish')
            require_ffmpeg_binary_identity(request.encoder.binary)
            for root, name, facts in [(source_root, request.raw_name, raw_facts),
                    (directory,'chapters.txt',metadata_facts), (directory,'request.json',request_facts)]:
                if list(_version((root/name).lstat())) != facts['file_version']:
                    raise ValueError('M4B worker source changed during encoding')
            if cover_facts is not None and list(_version((source_root/request.cover_name).lstat())) != cover_facts['file_version']:
                raise ValueError('M4B worker cover changed during encoding')
            if list(_version(os.fstat(source.fileno()))) != raw_facts['file_version']:
                raise ValueError('M4B worker PCM descriptor changed')
    finally:
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            if reader is not None:
                reader.join(timeout=5)
            process.stdout.close(); process.stderr.close()
        if tail:
            sys.stderr.buffer.write(tail); sys.stderr.buffer.flush()


if __name__ == '__main__':
    try:
        if len(sys.argv)!=2:
            raise ValueError('Expected one private worker directory')
        run_m4b_mux_worker(sys.argv[1])
    except Exception as error:
        print('Bounded M4B worker failed: '+str(error)[:500]+' ['+get_pcm_worker_error_context(error)+']', file=sys.stderr)
        raise SystemExit(1)
