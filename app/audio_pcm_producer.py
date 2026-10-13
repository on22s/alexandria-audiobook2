"""Disk-backed PCM assembly preserving Pydub's incremental promotion order."""
import array
import tempfile

from pydub import AudioSegment
from pydub.audio_segment import audioop


PCM_BLOCK_BYTES = 65536


def is_bounded_pcm_supported(segments):
    """Keep custom segment behavior and irreducibly large conversions on legacy."""
    if not segments or any(type(segment) is not AudioSegment for segment in segments):
        return False
    layouts = [(segment.channels, segment.frame_rate, segment.sample_width)
               for segment in segments]
    if any(any(type(value) is not int or value <= 0 for value in layout)
           or layout[2] not in (1, 2, 4)
           or len(segment.raw_data) % (layout[0] * layout[2])
           for segment, layout in zip(segments, layouts)):
        return False
    # Silence uses mono/11025 Hz/16-bit. Include every possible prefix layout;
    # a single native resampling step must also fit the block allowance.
    channels = max(layout[0] for layout in layouts)
    rate = max(11025, *(layout[1] for layout in layouts))
    width = max(2, *(layout[2] for layout in layouts))
    source_rate = min([11025] + [layout[1] for segment, layout in zip(segments, layouts)
                                  if segment.raw_data])
    ratio = max(1, (rate + source_rate - 1) // source_rate)
    return channels * width * (ratio + 1) <= PCM_BLOCK_BYTES


def iter_raw_pcm(segment):
    view = memoryview(segment.raw_data)
    frame = segment.channels * segment.sample_width
    step = max(frame, PCM_BLOCK_BYTES // frame * frame)
    for start in range(0, len(view), step):
        yield view[start:start + step]


def iter_default_silence_pcm(duration_ms):
    remaining = max(0, int(11025 * (duration_ms / 1000.0))) * 2
    while remaining:
        count = min(remaining, PCM_BLOCK_BYTES)
        yield b'\0' * count
        remaining -= count


def iter_converted_pcm(pieces, source, target, cancel_check):
    channels, rate, width = source
    target_channels, target_rate, target_width = target
    if target_channels != channels and channels != 1:
        raise ValueError('AudioSegment.set_channels only supports mono-to-multi channel and multi-to-mono channel conversion')
    ratio = max(1, (target_rate + rate - 1) // rate)
    frames = PCM_BLOCK_BYTES // (target_channels * max(width, target_width) * (ratio + 1))
    step = max(1, frames) * channels * width
    state = None
    for piece in pieces:
        if len(piece) and frames < 1:
            raise ValueError('PCM conversion exceeds the bounded block allowance')
        for start in range(0, len(piece), step):
            cancel_check()
            data = piece[start:start + step]
            if target_channels != channels:
                if target_channels == 2:
                    data = audioop.tostereo(data, width, 1, 1)
                else:
                    samples = array.array({1: 'b', 2: 'h', 4: 'i'}[width])
                    samples.frombytes(data)
                    output = array.array(samples.typecode, [0]) * (len(samples) * target_channels)
                    for channel in range(target_channels):
                        output[channel::target_channels] = samples
                    data = output.tobytes()
            if rate != target_rate and len(data):
                data, state = audioop.ratecv(data, width, target_channels, rate, target_rate, state)
            if width != target_width:
                data = audioop.lin2lin(data, width, target_width)
            yield data


class PCMProducer:
    """Spool prefixes instead of joining or promoting a full in-memory buffer."""

    def __init__(self, directory, cancel_check):
        self.directory = directory
        self.cancel_check = cancel_check
        self.layout = None
        self.byte_count = 0
        self.stream = tempfile.TemporaryFile(dir=directory)

    def append(self, pieces, layout):
        target = layout if self.layout is None else tuple(
            max(a, b) for a, b in zip(self.layout, layout))
        if self.layout is not None and self.layout != target:
            replacement = tempfile.TemporaryFile(dir=self.directory)
            try:
                for data in iter_converted_pcm(self.read(), self.layout, target, self.cancel_check):
                    replacement.write(data)
            except BaseException:
                try:
                    replacement.close()
                except BaseException:
                    pass
                raise
            try:
                self.stream.close()
            except BaseException:
                try:
                    replacement.close()
                except BaseException:
                    pass
                raise
            self.stream = replacement
        for data in iter_converted_pcm(pieces, layout, target, self.cancel_check):
            self.stream.write(data)
        self.layout = target
        self.byte_count = self.stream.tell()

    def get_frame_count(self):
        return self.byte_count // (self.layout[0] * self.layout[2])

    def read(self):
        self.stream.seek(0)
        frame = self.layout[0] * self.layout[2]
        while True:
            self.cancel_check()
            data = self.stream.read(max(frame, PCM_BLOCK_BYTES // frame * frame))
            if not data:
                break
            yield data

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.close()
        else:
            try:
                self.close()
            except BaseException:
                pass
        return False
