"""Owned book/take capture and exact export PCM for a private sound mix."""
from contextlib import contextmanager
import json
import math
from pathlib import Path
import time

from audio_pcm_producer import PCMSourceStore
from json_metadata import get_unique_json_object
from project_scope_admission import ensure_audio_scope_admission
from project_scope_files import capture_scope_file
from sound_design_context import (capture_sound_design_context, capture_sound_design_anchor,
                                  require_sound_design_context_files_current)
from sound_design_cues import _require_digest, _require_int
from sound_design_pcm import (save_sound_design_dry_pcm, get_sound_design_pause_metadata,
                              MAX_CAPTURE_BYTES, MAX_CLIP_BYTES)
from sound_design_take_decode import load_sound_design_take
from tts import (DEFAULT_PAUSE_MS, SAME_SPEAKER_PAUSE_MS, iter_timeline,
                 ensure_audio_export_active)


@contextmanager
def ensure_sound_design_audio_capture(root, directory, expected_context, *,
                                      cancel_check=None, max_staged_bytes=MAX_CAPTURE_BYTES,
                                      timeout=300):
    """Keep one owned snapshot alive through private rendering, then fence again.

    The timeout is a single deadline for capture and all rendering inside it.
    Nothing may be published inside this context: its successful exit is part of
    validation. The caller owns directory quota (including retained old stages),
    cancellation and output cleanup. No missing or corrupt dialogue is skipped.
    """
    _require_digest(expected_context,'Expected sound context')
    _require_int(max_staged_bytes,1,MAX_CAPTURE_BYTES,'Sound capture allowance')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=3600:
        raise ValueError('Invalid sound capture deadline')
    root=Path(root).absolute();directory=Path(directory).absolute()
    if root not in directory.parents or directory.resolve()!=directory or not directory.is_dir():
        raise ValueError('Sound capture needs private project staging')
    deadline=time.monotonic()+timeout
    def check():
        ensure_audio_export_active(cancel_check)
        if time.monotonic()>=deadline:raise TimeoutError('Sound capture exceeded its deadline')
    with ensure_audio_scope_admission(root), PCMSourceStore(directory,check) as sources:
        check();snapshot=capture_sound_design_context(root,check_active=check)
        if snapshot['context_revision']!=expected_context:
            raise ValueError('Sound context changed before capture; refresh review')
        proof,raw=capture_scope_file(root,'config.json',include_bytes=True,limit=2*1024**2,cancel_check=check)
        if proof!=snapshot['binding']['files']['config.json']:
            raise ValueError('Sound settings changed before capture')
        config=json.loads(raw,object_pairs_hook=get_unique_json_object) if raw is not None else {}
        if not isinstance(config,dict) or not isinstance(config.get('tts',{}),dict):
            raise ValueError('Invalid sound pause settings')
        settings=config.get('tts',{})
        pauses=(settings.get('pause_between_speakers_ms',DEFAULT_PAUSE_MS),
                settings.get('pause_same_speaker_ms',SAME_SPEAKER_PAUSE_MS))
        anchors=[];segments=[];rows=snapshot['chunks']
        get_sound_design_pause_metadata(rows, pauses)
        def decoded():
            for row in rows:
                check()
                anchor=capture_sound_design_anchor(root,snapshot,row['uid'],check_active=check)
                available=min(MAX_CLIP_BYTES,max_staged_bytes-sources.byte_count)
                if available<=0:raise ValueError('Sound capture staging is full')
                segment=load_sound_design_take(root,anchor['audio']['path'],cancel_check=check,
                    max_pcm_bytes=available,expected_audio_sha256=anchor['selection']['audio_sha256'])
                anchors.append(anchor)
                yield row,segment
                del segment
        for row,segment,_ in iter_timeline(decoded(),*pauses,cancel_check=check):
            if sources.byte_count+len(segment.raw_data)>max_staged_bytes:
                raise ValueError('Sound capture staging is full')
            segments.append(sources.save_segment(segment))
            del segment
        dry,plan=save_sound_design_dry_pcm(sources,rows,segments,directory,pauses,
            cancel_check=check,max_staged_bytes=max_staged_bytes)
        def fence():
            check()
            current=capture_sound_design_context(root,check_active=check)
            if current['context_revision']!=snapshot['context_revision']:
                raise ValueError('Sound context changed during capture')
            for row,anchor in zip(rows,anchors):
                if capture_sound_design_anchor(root,current,row['uid'],check_active=check)!=anchor:
                    raise ValueError('Selected sound take changed during capture')
                require_sound_design_context_files_current(root,current,anchor)
            check()
        fence()
        yield dict(context=snapshot,dialogue=[a['selection'] for a in anchors],
                   dry=dry,pcm_plan=plan,sources=sources,check_active=check)
        fence()
