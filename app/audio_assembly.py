"""Buffered, no-crossfade audio assembly with incremental Pydub format promotion."""
from pydub import AudioSegment


class AudioSegmentBuilder:
    """Match repeated ``segment + next_segment`` without recopying every prefix.

    Promote the accumulated audio only when a new segment raises its format.
    Promoting all inputs up front would change resampling and rounding when
    several increasing sample rates occur in the original sequence.
    """

    def __init__(self, segment):
        self._segment = segment
        self._parts = [segment.raw_data]

    def append(self, segment):
        current = self._segment
        channels = max(current.channels, segment.channels)
        frame_rate = max(current.frame_rate, segment.frame_rate)
        sample_width = max(current.sample_width, segment.sample_width)
        if (channels, frame_rate, sample_width) != (
                current.channels, current.frame_rate, current.sample_width):
            current = (self.finish().set_channels(channels)
                       .set_frame_rate(frame_rate).set_sample_width(sample_width))
            self._segment = current
            self._parts = [current.raw_data]
        segment = (segment.set_channels(channels)
                   .set_frame_rate(frame_rate).set_sample_width(sample_width))
        self._parts.append(segment.raw_data)

    def finish(self):
        if len(self._parts) == 1:
            return self._segment
        self._segment = AudioSegment(
            data=b"".join(self._parts), sample_width=self._segment.sample_width,
            frame_rate=self._segment.frame_rate, channels=self._segment.channels)
        self._parts = [self._segment.raw_data]
        return self._segment
