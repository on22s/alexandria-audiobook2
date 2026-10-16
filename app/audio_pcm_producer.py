"""Disk-backed PCM assembly preserving Pydub's incremental promotion order."""
import array
import math
import sys
import tempfile
from dataclasses import dataclass

from pydub import AudioSegment
from pydub.audio_segment import audioop


PCM_BLOCK_BYTES = 65536


@dataclass(frozen=True)
class PCMSourceRange:
    store: object
    offset: int
    byte_count: int
    channels: int
    frame_rate: int
    sample_width: int
    duration_ms: int

    @property
    def frame_width(self):
        return self.channels * self.sample_width

    def __len__(self):
        return self.duration_ms


def get_pcm_source_byte_count(segment):
    return segment.byte_count if type(segment) is PCMSourceRange else len(segment.raw_data)


def get_pcm_source_shape(segment):
    """Normalize real segments and owned snapshots without materializing PCM."""
    if not isinstance(segment, AudioSegment) and type(segment) is not PCMSourceRange:
        return None
    layout = (segment.channels, segment.frame_rate, segment.sample_width)
    if (any(type(value) is not int or value <= 0 for value in layout)
            or layout[2] not in (1, 2, 4)
            or get_pcm_source_byte_count(segment) % (layout[0] * layout[2])):
        return None
    return layout, get_pcm_source_byte_count(segment) // (layout[0] * layout[2])


def is_plain_pcm_source(segment):
    return type(segment) in (AudioSegment, PCMSourceRange)


class PCMSourceStore:
    """Own raw/trimmed source snapshots for one export, beside its destination."""
    def __init__(self, directory, cancel_check):
        self.cancel_check = cancel_check
        self.stream = tempfile.TemporaryFile(prefix='.source-pcm-', dir=directory)
        self.byte_count = 0

    def save_segment(self, segment):
        # Actual custom objects and unknown/very wide layouts keep their old
        # behavior. This is a staging decision, never a new format rejection.
        if type(segment) is not AudioSegment:
            return segment
        shape = get_pcm_source_shape(segment)
        if (shape is None or segment.frame_width != segment.channels * segment.sample_width
                or segment.channels * max(2, segment.sample_width) * 2 > PCM_BLOCK_BYTES):
            return segment
        self.cancel_check()
        offset = self.byte_count
        # Reads for the trim phase move the shared handle. Append only after
        # the committed high-water mark, never after the last range read.
        self.stream.seek(offset)
        view = memoryview(segment.raw_data)
        try:
            step = PCM_BLOCK_BYTES // segment.frame_width * segment.frame_width
            for start in range(0, len(view), step):
                self.cancel_check()
                block = view[start:start + step]
                if self.stream.write(block) != len(block):
                    raise OSError('Incomplete source PCM snapshot write')
            size = len(view)
        finally:
            view.release()
        self.byte_count += size
        return PCMSourceRange(self, offset, size, *shape[0], len(segment))

    def iter_pcm(self, source, block_bytes):
        if (source.store is not self or self.stream is None
                or source.offset < 0 or source.byte_count < 0
                or source.offset + source.byte_count > self.byte_count):
            raise ValueError('Source PCM snapshot is unavailable')
        step = max(source.frame_width, block_bytes // source.frame_width * source.frame_width)
        position = 0
        while position < source.byte_count:
            self.cancel_check()
            count = min(step, source.byte_count - position)
            self.stream.seek(source.offset + position)
            block = self.stream.read(count)
            if len(block) != count:
                raise OSError('Incomplete source PCM snapshot read')
            yield block
            position += count

    def load_segment(self, source):
        if type(source) is not PCMSourceRange:
            return source
        data = b''.join(self.iter_pcm(source, PCM_BLOCK_BYTES))
        return AudioSegment(data=data, channels=source.channels,
                            frame_rate=source.frame_rate, sample_width=source.sample_width)

    def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

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


def is_bounded_pcm_supported(segments):
    """Keep custom segment behavior and very wide frames on legacy."""
    if not segments or any(not is_plain_pcm_source(segment) or get_pcm_source_shape(segment) is None
                           for segment in segments):
        return False
    layouts = [(segment.channels, segment.frame_rate, segment.sample_width)
               for segment in segments]
    # Preserve the existing minimum frame allowance, including default silence.
    channels = max(layout[0] for layout in layouts)
    width = max(2, *(layout[2] for layout in layouts))
    return channels * width * 2 <= PCM_BLOCK_BYTES



def iter_raw_pcm(segment, block_bytes=PCM_BLOCK_BYTES):
    if type(segment) is PCMSourceRange:
        yield from segment.store.iter_pcm(segment, block_bytes)
        return
    view = memoryview(segment.raw_data)
    frame = segment.channels * segment.sample_width
    step = max(frame, block_bytes // frame * frame)
    for start in range(0, len(view), step):
        yield view[start:start + step]


def iter_default_silence_pcm(duration_ms):
    remaining = max(0, int(11025 * (duration_ms / 1000.0))) * 2
    while remaining:
        count = min(remaining, PCM_BLOCK_BYTES)
        yield b'\0' * count
        remaining -= count


def _iter_python_ratecv(pieces, width, channels, inrate, outrate,
                        cancel_check, block_bytes):
    """Mirror audioop's default filter without allocating a rate-sized buffer.

    The termination value exposes the native-shaped state for differential tests.
    Each logical conversion starts fresh; input pieces do not reset the state.
    """
    common = math.gcd(inrate, outrate)
    input_step, output_step = inrate // common, outrate // common
    phase = -output_step
    previous = [0] * channels
    current = [0] * channels
    initialized = False
    shift = (4 - width) * 8
    frame_bytes = channels * width
    output = bytearray()
    for piece in pieces:
        if not piece:
            continue
        if not initialized:
            # Validate C arguments at the original first nonempty rate call.
            # Empty input requests no native output buffer, even at huge ratios.
            _, state = audioop.ratecv(b'', width, channels, inrate, outrate, None)
            phase, pairs = state
            previous = [pair[0] for pair in pairs]
            current = [pair[1] for pair in pairs]
            initialized = True
        if len(piece) % frame_bytes:
            raise audioop.error('not a whole number of frames')
        for offset in range(0, len(piece), frame_bytes):
            cancel_check()
            previous, current = current, previous
            for channel in range(channels):
                start = offset + channel * width
                current[channel] = int.from_bytes(
                    piece[start:start + width], sys.byteorder, signed=True) << shift
            phase += output_step
            while phase >= 0:
                cancel_check()
                if len(output) + frame_bytes > block_bytes:
                    yield bytes(output)
                    output.clear()
                for old, new in zip(previous, current):
                    # C truncates the double interpolation before shifting width.
                    value = int((float(old) * float(phase)
                                 + float(new) * float(output_step - phase))
                                / float(output_step))
                    output.extend((value >> shift).to_bytes(
                        width, sys.byteorder, signed=True))
                phase -= input_step
    if output:
        cancel_check()
        yield bytes(output)
    return phase, tuple(zip(previous, current))


def iter_converted_pcm(pieces, source, target, cancel_check):
    channels, rate, width = source
    target_channels, target_rate, target_width = target
    if target_channels != channels and channels != 1:
        raise ValueError('AudioSegment.set_channels only supports mono-to-multi channel and multi-to-mono channel conversion')
    frame_bytes = target_channels * max(width, target_width)
    ratio = max(1, (target_rate + rate - 1) // rate)
    frames = max(1, PCM_BLOCK_BYTES // (frame_bytes * (ratio + 1)))
    native_ratecv = True
    if rate != target_rate:
        common = math.gcd(rate, target_rate)
        input_step, output_step = rate // common, target_rate // common
        # audioop allocates ceil(N / input_step) * output_step frames,
        # even when its actual output is only a single sample.
        groups = (PCM_BLOCK_BYTES // (output_step * frame_bytes)
                  if input_step > 0 and output_step > 0 else 0)
        native_ratecv = groups > 0
        if native_ratecv:
            frames = min(frames, groups * input_step)
    step = frames * channels * width

    def iter_promoted():
        for piece in pieces:
            if len(piece) and frame_bytes * 2 > PCM_BLOCK_BYTES:
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
                yield data

    promoted = iter_promoted()
    if not native_ratecv:
        block_bytes = PCM_BLOCK_BYTES // frame_bytes * target_channels * width
        converted = _iter_python_ratecv(promoted, width, target_channels,
                                        rate, target_rate, cancel_check, block_bytes)
    else:
        def iter_native():
            state = None
            for data in promoted:
                if rate != target_rate and len(data):
                    data, state = audioop.ratecv(
                        data, width, target_channels, rate, target_rate, state)
                yield data
        converted = iter_native()
    for data in converted:
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
