"""Bounded owned take decoding with the Editor's pinned Pydub PCM choices."""
import io
import json
import re
from pathlib import Path

from pydub import AudioSegment
from pydub.audio_segment import fix_wav_headers
from audio_take_history import _open_regular, MAX_AUDIO_BYTES
from audio_source_io import read_bounded_audio_hash
from audio_pcm_decode import decode_audio_pcm
from audio_validation import validate_wav_container
from json_metadata import get_unique_json_object
from media_process import run_owned_media_check
from project_scope_files import capture_scope_file
from sound_design_pcm import MAX_CLIP_BYTES
from tts import ExportCancelled, ensure_audio_export_active
from audio_generation_owner import AudioGenerationCancelled


def load_sound_design_take(root, relative, *, cancel_check=None, max_pcm_bytes=MAX_CLIP_BYTES,
                           expected_audio_sha256=None):
    """Decode a confined selected take; return only after source/content fences.

    This is a bounded single-clip allocation, not an RSS bound. The caller budgets
    retained clips/staging separately. Format policy mirrors ProjectManager's
    explicit MP3/FLAC decoder choices and Pydub's PCM-width selection elsewhere.
    """
    if type(max_pcm_bytes) is not int or not 1 <= max_pcm_bytes <= MAX_CLIP_BYTES:
        raise ValueError('Invalid take PCM allowance')
    check=lambda:ensure_audio_export_active(cancel_check)
    before,_=capture_scope_file(root,relative,limit=MAX_AUDIO_BYTES,cancel_check=check)
    if not before['exists'] or not before['size']:
        raise ValueError('Selected take is missing or empty')
    if expected_audio_sha256 is not None and before['sha256'] != expected_audio_sha256:
        raise ValueError('Selected take changed before decoding')
    path=Path(root)/relative
    with _open_regular(path,Path(root),MAX_AUDIO_BYTES)[0] as source:
        validate_wav_container(source,'Selected take',cancel_check=cancel_check)
        def run(command,limit):
            # Discard buffered read-ahead before the child shares the descriptor.
            source.seek(0,2)
            source.seek(0)
            return run_owned_media_check(command,check,300,output_limit=limit,
                input_source=source,cancellation_errors=(ExportCancelled,AudioGenerationCancelled))
        raw=run(['ffprobe','-v','error','-select_streams','a:0','-show_entries',
            'stream=sample_rate,channels,sample_fmt,bits_per_sample,codec_name','-of','json','fd:'],65536)
        document=json.loads(raw,object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite take metadata')))
        streams=document.get('streams') if isinstance(document,dict) else None
        if not isinstance(streams,list) or len(streams)!=1 or not isinstance(streams[0],dict):
            raise ValueError('Selected take needs one audio stream')
        row=streams[0];rate=row.get('sample_rate');channels=row.get('channels')
        if (not isinstance(rate,str) or not re.fullmatch(r'[0-9]{1,6}',rate)
                or not 0<int(rate)<=384000 or type(channels) is not int or channels not in (1,2)):
            raise ValueError('Unsupported selected take geometry')
        extension=path.suffix.lower();bits=row.get('bits_per_sample',0)
        if type(bits) is not int:raise ValueError('Invalid selected take PCM width')
        if extension in ('.mp3','.flac'):
            bits=16  # Explicit input codec skips Pydub probe; WAV encoder defaults to s16.
        elif not bits:
            fmt=row.get('sample_fmt')
            if fmt in ('flt','fltp'):bits=32
            elif fmt in ('dbl','dblp'):bits=64
            elif isinstance(fmt,str) and re.fullmatch(r'[su](8|16|32)p?',fmt):
                bits=int(re.search(r'\d+',fmt)[0])
        if bits not in (8,16,24,32):raise ValueError('Unsupported selected take PCM width')
        width=4 if bits==24 else bits//8
        max_frames=min(max_pcm_bytes//(channels*width),int(rate)*3600)
        # Validate finite original PCM before integer conversion can hide NaN/Inf.
        facts=decode_audio_pcm(source,path,frames=max_frames,sample_rate=int(rate),
            channels=channels,source_limit=MAX_AUDIO_BYTES,check_active=check,
            cancellation_errors=(ExportCancelled,AudioGenerationCancelled),exact_frames=False)
        codec='pcm_u8' if bits==8 else f'pcm_s{bits}le'
        # Pydub expands 24-bit samples to four bytes; bound that expansion too.
        allowance=max_pcm_bytes if bits!=24 else max_pcm_bytes//4*3
        command=['ffmpeg','-nostdin','-v','error','-xerror','-err_detect','explode']
        if extension in ('.mp3','.flac'):command+=['-f',extension[1:],'-acodec',extension[1:]]
        command+=['-i','fd:','-map','0:a:0','-vn','-sn','-dn','-threads','1',
                  '-acodec',codec,'-f','wav','pipe:1']
        encoded=bytearray(run(command,allowance+65536))
        fix_wav_headers(encoded)
        segment=AudioSegment.from_file(io.BytesIO(encoded),format='wav')
        if (not segment.raw_data or len(segment.raw_data)>max_pcm_bytes
                or len(segment.raw_data)!=facts['frames']*channels*width
                or segment.frame_rate!=int(rate) or segment.channels!=channels):
            raise ValueError('Selected take exceeds its PCM allowance or changed geometry')
        digest,size=read_bounded_audio_hash(source,MAX_AUDIO_BYTES,check)
        after,_=capture_scope_file(root,relative,limit=MAX_AUDIO_BYTES,cancel_check=check)
        if after!=before or digest!=before['sha256'] or size!=before['size']:
            raise ValueError('Selected take changed while decoding')
    check()
    return segment
