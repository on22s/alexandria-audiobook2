"""Pure mastering settings and filter policy, shared with isolated workers."""
import math
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class MasteringOptions(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_default=True)
    loudness: FiniteFloat = Field(default=-18, ge=-24, le=-14)
    true_peak: FiniteFloat = Field(default=-2, ge=-9, le=-1)


def get_mastering_options(options):
    if options is None:
        return None
    return MasteringOptions.model_validate(options).model_dump()


def get_mastering_filter(options, measured=None, *, output_channels=None):
    options = get_mastering_options(options)
    if output_channels is not None and (type(output_channels) is not int or output_channels not in (1, 2)):
        raise ValueError('Unsupported mastering channel conversion.')
    value = f"loudnorm=I={options['loudness']}:TP={options['true_peak']}:LRA=11"
    if measured is not None:
        measurements = [(option, float(measured[key]), low, high) for option, key, low, high in [
            ('measured_I', 'input_i', -99, 0), ('measured_TP', 'input_tp', -99, 99),
            ('measured_LRA', 'input_lra', 0, 99), ('measured_thresh', 'input_thresh', -99, 0),
            ('offset', 'target_offset', -99, 99)]]
        if not all(math.isfinite(number) for _, number, _, _ in measurements):
            raise ValueError('Audio has no finite loudness measurement (silence or invalid audio).')
        # Valid observations can exceed FFmpeg's measured-option domains. Keep
        # them in the report; use an unmeasured pass instead of clamping them or
        # feeding only part of the measured bundle into the render filter.
        if all(low <= number <= high for _, number, low, high in measurements):
            for option, number, _, _ in measurements:
                value += f':{option}={number}'
            value += ':linear=true'
    prefix = ('aformat=channel_layouts=' + ('mono' if output_channels == 1 else 'stereo') + ','
              if output_channels is not None else '')
    return prefix + value + ':print_format=json'
