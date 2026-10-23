"""Finite export limits shared by request validation and silent-PCM assembly.

Pydub's AudioSegment.silent() produces 11,025 Hz mono 16-bit PCM, so the
760,000 ms export pause limit corresponds to 16,758,000 bytes (about 16 MiB).
"""

SILENCE_FRAME_RATE = 11025
SILENCE_SAMPLE_WIDTH = 2
MAX_PAUSE_MS = 760_000


def get_pause_pcm_bytes(duration_ms):
    """Return the PCM bytes AudioSegment.silent() allocates for this pause."""
    frames = int(SILENCE_FRAME_RATE * (duration_ms / 1000.0))
    return max(0, frames) * SILENCE_SAMPLE_WIDTH


def ensure_pause_allowed(duration_ms):
    """Reject a pause whose whole-buffer silence exceeds the export limit."""
    if duration_ms > MAX_PAUSE_MS:
        raise ValueError(
            f"An audio pause of {duration_ms} ms needs "
            f"{get_pause_pcm_bytes(duration_ms)} bytes of silence; exports allow at most "
            f"{MAX_PAUSE_MS} ms ({get_pause_pcm_bytes(MAX_PAUSE_MS)} bytes). "
            "Shorten the pause before exporting.")
