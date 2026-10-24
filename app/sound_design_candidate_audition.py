"""Bounded WAV excerpts from a verified full candidate; never media approval."""
import io

from audio_streaming import write_producer_wav
from sound_design_candidate import ensure_sound_candidate_open
from sound_design_cues import _require_int, MAX_RATE, MAX_SECONDS
from tts import ensure_audio_export_active

MAX_AUDITION_SECONDS = 30
MAX_AUDITION_BYTES = 48 * 1024**2


class _CandidateWindow:
    """Adapt a pinned descriptor to the existing exact-frame WAV writer."""
    def __init__(self, stream, layout, start, frames, check):
        self.stream, self.layout = stream, layout
        self.start, self.frames, self.check = start, frames, check

    def get_frame_count(self):
        return self.frames

    def read(self):
        frame_bytes = self.layout[0] * self.layout[2]
        remaining = self.frames * frame_bytes
        self.stream.seek(self.start * frame_bytes)
        block_bytes = (65536 // frame_bytes) * frame_bytes
        while remaining:
            self.check()
            block = self.stream.read(min(block_bytes, remaining))
            if not block or len(block) % frame_bytes:
                raise ValueError('Candidate audition PCM is incomplete')
            remaining -= len(block)
            yield block


def read_sound_candidate_audition(root, stage_name, expected_revision, start_frame,
                                  end_frame, *, cancel_check=None):
    """Return a complete, fenced WAV window and its full-candidate identity.

    Every frame of the candidate is addressable, but each response is bounded.
    Hearing one excerpt does not approve the unreviewed remainder. Callers must
    run this blocking work in the existing worker and separately check current
    book/source/take bindings before approval or delivery.
    """
    check = lambda: ensure_audio_export_active(cancel_check)
    # Coarse integer checks precede candidate I/O; actual clock checks follow
    # only after bounded structural admission of the stored candidate.
    _require_int(start_frame, 0, MAX_RATE * MAX_SECONDS, 'Audition start')
    _require_int(end_frame, start_frame + 1, MAX_RATE * MAX_SECONDS, 'Audition end')
    with ensure_sound_candidate_open(root, stage_name, expected_revision,
                                     cancel_check=cancel_check) as (document, stream):
        plan = document['binding']['pcm_plan']
        layout = (plan['channels'], plan['frame_rate'], plan['sample_width'])
        frames = end_frame - start_frame
        if end_frame > plan['frames'] or frames > layout[1] * MAX_AUDITION_SECONDS:
            raise ValueError('Audition range exceeds the candidate or duration limit')
        if frames * layout[0] * layout[2] + 44 > MAX_AUDITION_BYTES:
            raise ValueError('Audition exceeds its response byte allowance')
        output = io.BytesIO()
        producer = _CandidateWindow(stream, layout, start_frame, frames, check)
        write_producer_wav(output, producer, (layout, frames), cancel_check)
        payload = output.getvalue()
        if len(payload) != frames * layout[0] * layout[2] + 44:
            raise ValueError('Audition WAV disagrees with its exact frame plan')
    check()
    return dict(candidate_revision=expected_revision, scope=[start_frame,end_frame],
                frame_rate=layout[1], wav=payload)
