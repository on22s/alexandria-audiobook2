"""Owned, cancellable FFmpeg execution with bounded diagnostic reads."""
from media_process import run_owned_media_check, MediaProcessError
from tts import ensure_audio_export_active


def run_audio_ffmpeg(arguments, cancel_check=None, timeout=600, environment=None, executable="ffmpeg"):
    """Drain diagnostics into bounded RAM; never spool unbounded encoder logs."""
    command = [executable, '-nostdin', '-hide_banner', '-nostats', '-xerror',
               '-threads', '1', '-filter_threads', '1', '-y'] + list(arguments)
    tail = bytearray()
    def consume(line):
        tail.extend((line + '\n').encode('utf-8'))
        if len(tail) > 65536:
            del tail[:-65536]
    try:
        run_owned_media_check(command, lambda: ensure_audio_export_active(cancel_check),
            timeout, consume_line=consume, environment=environment)
    except MediaProcessError as error:
        raise RuntimeError('Audio processing failed: ' +
                           tail.decode('utf-8', errors='replace')[-2048:]) from error
    return tail.decode('utf-8', errors='replace')
