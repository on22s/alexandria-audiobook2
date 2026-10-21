"""Shared integrity validation with optional generated-speech admission."""
import os
import contextlib
import hashlib
import tempfile
from pathlib import Path
import struct
import unicodedata
from utils import file_lock


class GeneratedAudioError(RuntimeError):
    """Generated output failed structural, sample, or speech validation."""


class GeneratedAudioCancelled(GeneratedAudioError):
    """Optional validation cancellation, kept distinct from bad audio."""


def is_audio_file_present(path, resolve_path):
    """Read nonempty file presence through the project's confined-path resolver.

    This is a UI fact, not decoding or generated-audio validation.
    """
    try:
        file = Path(resolve_path(path))
        return file.is_file() and file.stat().st_size > 0
    except (OSError, ValueError, TypeError):
        return False


def validate_finite_audio_values(values, context="audio measurement"):
    """Reject non-finite PCM or metrics without substituting plausible values."""
    import numpy as np
    try:
        finite = bool(np.isfinite(values).all())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{context} contains invalid numeric values") from error
    if not finite:
        raise ValueError(f"{context} contains non-finite values")



def get_audio_lock_target(path):
    """Bound lock metadata and keep unique staging files free of sidecars."""
    absolute = os.path.abspath(os.fspath(path))
    canonical = os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute)))
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()
    stripe = int(digest[:8], 16) % 4096
    uid = os.getuid() if hasattr(os, 'getuid') else 'user'
    return Path(tempfile.gettempdir()) / f'alexandria-audio-locks-{uid}-v1' / f'{stripe:04x}'


@contextlib.contextmanager
def ensure_audio_output(path):
    """Hold a shared kernel lock only around output-file operations."""
    target = get_audio_lock_target(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with file_lock(target):
        yield


def _remove_stale_audio_locked(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def remove_stale_audio(path):
    """Remove an earlier output under the same admission as publication."""
    with ensure_audio_output(path):
        _remove_stale_audio_locked(path)


def save_generated_wav(values, sample_rate, path, context="audio generation"):
    """Clear stale bytes, write PCM and validate while publication is admitted."""
    import numpy as np
    import soundfile as sf
    with ensure_audio_output(path):
        _remove_stale_audio_locked(path)
        values = np.asarray(values)
        if values.ndim not in (1, 2) or (values.ndim == 2 and values.shape[1] not in (1, 2)):
            raise GeneratedAudioError(f"{context} requires mono samples or frames by one or two channels")
        try:
            validate_finite_audio_values(values, context)
        except ValueError as error:
            raise GeneratedAudioError(str(error)) from error
        sf.write(path, values, sample_rate)
        return validate_generated_audio(path, context)


def publish_audio_output(staging_path, output_path, cancelled=None, *, cancel_check=None):
    """Publish a validated private file without racing stale-output cleanup."""
    with ensure_audio_output(output_path):
        if ((cancelled is not None and cancelled.is_set())
                or (cancel_check is not None and cancel_check())):
            return False
        os.replace(staging_path, output_path)
        return True


def get_minimum_speech_duration(text):
    """Reject only gross duration failures, leaving short utterances unrestricted.

    Eighty letters/digits per second is a deliberately generous ceiling, not a
    speaking-rate quality score. Count Han, kana and Hangul as four units so
    compact scripts do not inherit an English character-rate assumption.
    """
    units = 0
    for char in unicodedata.normalize("NFC", text):
        if not char.isalnum():
            continue
        name = unicodedata.name(char, "")
        units += 4 if name.startswith(("CJK", "HIRAGANA", "KATAKANA", "HANGUL")) else 1
    return units / 80 if units >= 80 else 0


def validate_generated_audio(path, context="audio generation", *, expected_text=None, cancel_check=None):
    """Validate all frames, optionally polling a cancellation predicate per block."""
    def check():
        if cancel_check is not None and cancel_check():
            raise GeneratedAudioCancelled(f"{context} validation cancelled")
    check()
    if not os.path.exists(path):
        raise GeneratedAudioError(f"{context} wrote no file: {path}")
    size = os.path.getsize(path)
    if size == 0:
        raise GeneratedAudioError(f"{context} wrote an empty file: {path}")

    try:
        import soundfile as sf
        info = sf.info(path)
        frames, rate = info.frames, info.samplerate
        # Reading the entire stream exercises decoding beyond the header.
        decoded_frames = 0
        peak = 0.0
        with sf.SoundFile(path) as audio:
            while True:
                check()
                block = audio.read(65536)
                if not len(block):
                    break
                validate_finite_audio_values(block, context)
                if expected_text is not None:
                    import numpy as np
                    peak = max(peak, float(np.max(np.abs(block))))
                decoded_frames += len(block)
    except GeneratedAudioCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        raise GeneratedAudioError(
            f"{context} wrote undecodable audio: {type(exc).__name__}: "
            f"{str(exc)[:120]}") from exc
    check()
    if not frames or not rate:
        raise GeneratedAudioError(
            f"{context} wrote no audio frames ({frames} frames @ {rate} Hz)")

    if decoded_frames != frames:
        raise GeneratedAudioError(
            f"{context} wrote incomplete audio: frame count header declares "
            f"{frames}, decoded {decoded_frames}")

    if expected_text is not None:
        if peak < 1e-5:
            raise GeneratedAudioError(f"{context} returned effectively silent speech")
        minimum = get_minimum_speech_duration(expected_text)
        if frames / rate < minimum:
            raise GeneratedAudioError(
                f"{context} returned speech too short for the requested text "
                f"({frames / rate:.3f}s; minimum {minimum:.3f}s)")

    with open(path, "rb") as handle:
        head = handle.read(12)
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        declared = struct.unpack("<I", head[4:8])[0] + 8
        # Wrapped 32-bit lengths remain below the actual large-file extent.
        # A near-limit declaration still cannot exceed a small file's bytes.
        if size < declared:
            raise GeneratedAudioError(
                f"{context} wrote truncated audio: header declares {declared} "
                f"bytes, file is {size} ({declared - size} missing)")
    return path


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
