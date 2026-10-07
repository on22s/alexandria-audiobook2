"""Numeric admission rules shared by preparer HTTP requests and CLI arguments."""
import math


def validate_preparer_numeric_booleans(settings):
    for name in ('chunk_size', 'min_chunk_duration', 'min_confidence',
                 'val_split', 'source_threshold', 'batch_size', 'zip_max_files',
                 'source_start', 'min_snr'):
        if isinstance(settings.get(name), bool):
            raise ValueError(f'--{name.replace("_", "-")} must be numeric, not boolean')


def validate_preparer_numeric_settings(settings):
    validate_preparer_numeric_booleans(settings)
    for name in ('chunk_size', 'min_chunk_duration'):
        value = settings.get(name)
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError(f'--{name.replace("_", "-")} must be finite and positive')
    for name in ('min_confidence', 'val_split', 'source_threshold'):
        value = settings.get(name)
        if value is not None and (not math.isfinite(value) or not 0 <= value <= 1):
            raise ValueError(f'--{name.replace("_", "-")} must be between 0 and 1')
    for name in ('batch_size', 'zip_max_files'):
        value = settings.get(name)
        if value is not None and (not isinstance(value, int) or value <= 0):
            raise ValueError(f'--{name.replace("_", "-")} must be positive')
    for name in ('source_start', 'min_snr'):
        value = settings.get(name)
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError(f'--{name.replace("_", "-")} must be nonnegative')
