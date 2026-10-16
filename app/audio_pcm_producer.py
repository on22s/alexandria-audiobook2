"""Disk-backed PCM assembly preserving Pydub's incremental promotion order."""
import array
import math
import sys
import tempfile

from pydub import AudioSegment
from pydub.audio_segment import audioop


PCM_BLOCK_BYTES = 65536


def is_bounded_pcm_supported(segments):
    """Keep custom segment behavior and very wide frames on legacy."""
    if not segments or any(type(segment) is not AudioSegment for segment in segments):
        return False
    layouts = [(segment.channels, segment.frame_rate, segment.sample_width)
               for segment in segments]
    if any(any(type(value) is not int or value <= 0 for value in layout)
           or layout[2] not in (1, 2, 4)
           or len(segment.raw_data) % (layout[0] * layout[2])
           for segment, layout in zip(segments, layouts)):
        return False
    # Preserve the existing minimum frame allowance, including default silence.
    channels = max(layout[0] for layout in layouts)
    width = max(2, *(layout[2] for layout in layouts))
    return channels * width * 2 <= PCM_BLOCK_BYTES



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
