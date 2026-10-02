"""Shared production acoustic operations and pitch availability rules."""
import math


def is_measured_pitch(mean_f0):
    try:
        return not isinstance(mean_f0, bool) and math.isfinite(mean_f0) and mean_f0 > 0
    except (TypeError, ValueError):
        return False


def get_pitch_gender_estimate(mean_f0):
    if not is_measured_pitch(mean_f0):
        return "unknown"
    return "female" if mean_f0 >= 165 else "male"


def get_profiler_acoustic_operations(y, sr):
    """The production operations also define the benchmark's timing surface."""
    import librosa
    return {
        "pyin": lambda: librosa.pyin(y, fmin=50, fmax=400, sr=sr),
        "rms": lambda: librosa.feature.rms(y=y),
        "centroid": lambda: librosa.feature.spectral_centroid(y=y, sr=sr),
        "rolloff": lambda: librosa.feature.spectral_rolloff(y=y, sr=sr, roll_percent=0.85),
        "harmonic": lambda: librosa.feature.rms(y=librosa.effects.harmonic(y, margin=2.0)),
        "flatness": lambda: librosa.feature.spectral_flatness(y=y),
        "onset": lambda: librosa.onset.onset_detect(y=y, sr=sr, units="time"),
    }
