"""Owned native MP3/WAV encoder piped into a guarded seekable final container.

NUT4 carries native packet side data, including MP3 delay/padding. FFmpeg
inherits the parent's descendant owner and never writes an encoded pathname.
The parent removes this private directory on failure and publishes only after
independent full-output admission; worker success alone is not publication.
"""
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
from media_process import get_media_process_environment
from pcm_mux_profile import require_pcm_mux_runtime
from pcm_mux_worker_request import get_pcm_mux_worker_request, MAX_REQUEST_BYTES
from pcm_worker_contract import MAX_PCM_BYTES, get_pcm_worker_error_context
from project_scope_files import capture_scope_file


def run_pcm_mux_worker(directory):
    directory = Path(directory).absolute()
    if (directory.resolve() != directory or not directory.is_dir()
            or re.fullmatch(r'\.pcm-encode-[A-Za-z0-9_-]+', directory.name) is None):
        raise ValueError('PCM worker requires its private canonical directory')
    request_facts, raw = capture_scope_file(directory, 'request.json',
        limit=MAX_REQUEST_BYTES, include_bytes=True)
    if not request_facts['exists']:
        raise ValueError('PCM worker request is missing')
    request = get_pcm_mux_worker_request(raw)
    executable = require_ffmpeg_binary_identity(request.encoder.binary)
    source_root = directory.parent
    raw_facts, _ = capture_scope_file(source_root, request.raw_name, limit=MAX_PCM_BYTES)
    if (not raw_facts['exists'] or raw_facts['file_version'] != request.raw.version
            or raw_facts['sha256'] != request.raw.sha256):
        raise ValueError('PCM worker source changed before encoding')
    import av
    require_pcm_mux_runtime(av)
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
            # A dead diagnostic reader must not leave the native producer
            # blocked on a full pipe while demux waits for more audio.
            try:
                process.stderr.close()
            finally:
                if process.poll() is None:
                    process.terminate()
    try:
        source, before = _open_regular(source_root/request.raw_name, source_root, MAX_PCM_BYTES)
        with source, BoundedMediaOutput(directory,'output.'+request.format,request.output_limit) as output:
            if list(_version(before)) != raw_facts['file_version']:
                raise ValueError('PCM worker source changed before open')
            command = [executable,'-nostdin','-hide_banner','-nostats','-xerror',
                '-threads','1','-filter_threads','1','-f',{1:'s8',2:'s16le',4:'s32le'}[request.sample_width],
                '-ar',str(request.frame_rate),'-ac',str(request.channels),'-i','pipe:0','-map','0:a:0']
            if request.mastering is not None:
                mix_channels = request.output_channels if request.output_channels != request.channels else None
                command += ['-af',get_mastering_filter(request.mastering.model_dump(),request.measured,
                    output_channels=mix_channels)]
            codec = ('libmp3lame' if request.format == 'mp3' else
                     {1:'pcm_u8',2:'pcm_s16le',4:'pcm_s32le'}[request.sample_width])
            command += ['-ar',str(request.output_rate),'-ac',str(request.output_channels),'-c:a',codec]
            if request.format == 'mp3':
                command += ['-b:a',request.bitrate]
            command += ['-syncpoints','none','-strict','experimental','-f','nut','pipe:1']
            environment = get_media_process_environment()
            environment.update(TMPDIR=str(directory), TEMP=str(directory), TMP=str(directory))
            require_ffmpeg_binary_identity(request.encoder.binary)
            process = subprocess.Popen(command,stdin=source,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                close_fds=True,start_new_session=False,creationflags=0,env=environment)
            reader = threading.Thread(target=drain,daemon=True); reader.start()
            options = {'id3v2_version':'4'} if request.format == 'mp3' else {'rf64':'auto'}
            expected_codec = 'mp3' if request.format == 'mp3' else codec
            with av.open(ForwardOnlyMediaInput(process.stdout),format='nut',options={'probesize':'32768','analyzeduration':'1'}) as native, \
                    av.open(output,'w',format=request.format,options=options) as mux:
                if (len(native.streams) != 1 or len(native.streams.audio) != 1
                        or native.streams.audio[0].codec_context.codec.id != av.Codec(expected_codec,'r').id
                        or native.streams.audio[0].codec_context.sample_rate != request.output_rate
                        or len(native.streams.audio[0].codec_context.layout.channels) != request.output_channels):
                    raise ValueError('Unexpected native PCM encoder streams')
                audio = mux.add_stream_from_template(native.streams.audio[0])
                for packet in native.demux(audio=0):
                    if packet.size:
                        if packet.pts is None or packet.dts is None:
                            raise ValueError('Native PCM packet is missing its source clock')
                        packet.stream=audio; mux.mux(packet)
                    elif list(packet.iter_sidedata()):
                        # Empty demux flush sentinels are not muxable packets.
                        # Refuse unexpected side data rather than silently drop it.
                        raise ValueError('Unexpected native empty packet side data')
            output.check(); output.flush()
            if process.wait(timeout=5):
                raise ValueError('Native PCM encoder failed')
            reader.join(timeout=5)
            if reader.is_alive() or reader_errors:
                raise ValueError('PCM encoder diagnostics did not finish')
            require_ffmpeg_binary_identity(request.encoder.binary)
            for root,name,facts in [(source_root,request.raw_name,raw_facts),
                    (directory,'request.json',request_facts)]:
                if list(_version((root/name).lstat())) != facts['file_version']:
                    raise ValueError('PCM worker source changed during encoding')
            if list(_version(os.fstat(source.fileno()))) != raw_facts['file_version']:
                raise ValueError('PCM worker source descriptor changed')
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
        run_pcm_mux_worker(sys.argv[1])
    except Exception as error:
        print('Bounded PCM worker failed: '+str(error)[:500]+' ['+get_pcm_worker_error_context(error)+']',file=sys.stderr)
        raise SystemExit(1)
