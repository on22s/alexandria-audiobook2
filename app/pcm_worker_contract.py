"""Shared strict PCM geometry and captured file identity for media workers."""
import math
from pathlib import Path
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

from audio_mastering_filter import MasteringOptions, get_mastering_options

MAX_PCM_BYTES = 4 * 1024**3
MAX_PCM_SECONDS = 86400
Filename = Annotated[StrictStr, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')]
VersionPart = Annotated[StrictInt, Field(ge=0, le=2**64-1)]


class WorkerFileIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: list[VersionPart] = Field(min_length=5, max_length=5)
    sha256: StrictStr = Field(pattern=r'^[0-9a-f]{64}$')


class PCMInputShape(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    raw_name: Filename
    frames: StrictInt = Field(ge=1, le=192000 * MAX_PCM_SECONDS)
    frame_rate: StrictInt = Field(ge=1, le=192000)
    sample_width: StrictInt
    channels: StrictInt = Field(ge=1, le=2)
    output_rate: StrictInt = Field(ge=1, le=192000)
    output_channels: StrictInt = Field(ge=1, le=2)

    @model_validator(mode='after')
    def validate_pcm_shape(self):
        if (self.sample_width not in (1,2,4)
                or self.frames > self.frame_rate * MAX_PCM_SECONDS
                or self.frames*self.channels*self.sample_width > MAX_PCM_BYTES):
            raise ValueError('Invalid worker PCM shape')
        return self


def get_duration_encode_timeout(duration_seconds):
    """Finite per-encode allowance shared by native and legacy media paths."""
    if isinstance(duration_seconds, bool):
        raise ValueError('Encode duration must be a finite non-negative number')
    try:
        duration = float(duration_seconds)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('Encode duration must be a finite non-negative number') from error
    if not math.isfinite(duration) or duration < 0:
        raise ValueError('Encode duration must be a finite non-negative number')
    timeout = max(600.0, duration * 2 + 30)
    if not math.isfinite(timeout):
        raise ValueError('Encode duration produces a non-finite timeout')
    return timeout


def get_pcm_encode_timeout(shape):
    """Finite per-encode allowance, not an entire multi-pass route deadline."""
    shape = PCMInputShape.model_validate(shape)
    return get_duration_encode_timeout(shape.frames / shape.frame_rate)


def get_pcm_output_shape(format_name, shape, output_limit):
    """Apply the existing output-clock policy and reject impossible WAV quotas."""
    from audio_encoding_layout import get_audio_encoding_layout
    shape = PCMInputShape.model_validate(shape)
    layout = get_audio_encoding_layout(format_name,shape.frame_rate,shape.channels)
    if (shape.output_rate != layout['frame_rate'] or shape.output_channels != layout['channels']):
        raise ValueError('PCM output differs from the approved encoding layout')
    # WAV keeps original PCM geometry. This is a necessary minimum, not an
    # estimate replacing the guarded writer's exact header/write allowance.
    if format_name == 'wav' and shape.frames*shape.channels*shape.sample_width + 44 > output_limit:
        raise ValueError('Insufficient WAV output allowance')
    return shape


class PCMWorkerMastering(BaseModel):
    """JSON-only measured mastering contract shared by native media workers."""
    model_config = ConfigDict(extra='forbid', strict=True)
    mastering: MasteringOptions | None = None
    measured: dict[StrictStr, float] | None = None

    @field_validator('mastering', 'measured', mode='before')
    @classmethod
    def validate_finite_numbers(cls, value):
        try:
            valid = value is None or (type(value) is dict and all(
                type(number) in (int, float) and math.isfinite(number)
                for number in value.values()))
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError('Mastering settings and observations require finite numbers')
        return value

    @model_validator(mode='after')
    def validate_mastering_pair(self):
        if (self.mastering is None) != (self.measured is None):
            raise ValueError('Mastering requires its complete measurement pass')
        if self.mastering is not None:
            get_mastering_options(self.mastering.model_dump())
        if self.measured is not None and set(self.measured) != {
                'input_i','input_tp','input_lra','input_thresh','target_offset'}:
            raise ValueError('Invalid worker mastering observations')
        return self


def get_pcm_worker_error_context(error):
    """Return bounded code locations, without filesystem roots or local values."""
    locations = []
    cursor = error.__traceback__
    while cursor is not None:
        code = cursor.tb_frame.f_code
        locations.append(f'{Path(code.co_filename).name[:80]}:{cursor.tb_lineno} in {code.co_name[:80]}')
        locations = locations[-4:]
        cursor = cursor.tb_next
    return ' -> '.join(locations)
