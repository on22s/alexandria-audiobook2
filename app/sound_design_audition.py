"""Short, exact-frame auditions of verified immutable sound assets."""
import io
import wave

from sound_design_assets import SoundDesignAssetStore
from sound_design_cues import _require_digest, _require_int, MAX_RATE, MAX_SECONDS

AUDITION_SECONDS = 10


def get_sound_asset_audition(root, asset_id, sha256, *, start_frame=0, cancel_check=None):
    """Return a bounded WAV only after the retained-source checks complete."""
    _require_digest(sha256, 'Selected asset fingerprint')
    _require_int(start_frame, 0, MAX_RATE * MAX_SECONDS, 'Audition start frame')
    with SoundDesignAssetStore(root).open_canonical(asset_id, cancel_check=cancel_check) as (stream, facts):
        if facts['sha256'] != sha256:
            raise ValueError('Selected sound asset fingerprint changed')
        if start_frame >= facts['frames']:
            raise ValueError('Audition starts outside the sound asset')
        frames = min(facts['frames'] - start_frame, AUDITION_SECONDS * facts['frame_rate'])
        frame_bytes = facts['channels'] * facts['sample_width']
        stream.seek(start_frame * frame_bytes)
        raw = stream.read(frames * frame_bytes)
        if len(raw) != frames * frame_bytes:
            raise ValueError('Sound asset changed during audition')
        output = io.BytesIO()
        with wave.open(output, 'wb') as audio:
            audio.setparams((facts['channels'], facts['sample_width'], facts['frame_rate'],
                             frames, 'NONE', 'not compressed'))
            audio.writeframes(raw)
        result = output.getvalue()
    return result
