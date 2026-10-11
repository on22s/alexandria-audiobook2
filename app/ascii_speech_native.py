"""Explicit experimental opt-in; Python remains the speech policy authority."""
import importlib
import logging
import os

import speech_policy

_FLAG = "AA2_EXPERIMENTAL_ASCII_NATIVE"
IS_REQUESTED = os.environ.get(_FLAG) == "1"
_EXPECTED_FUNCTIONS = speech_policy._ASCII_NATIVE_FUNCTIONS
_EXPECTED_CODES = speech_policy._ASCII_NATIVE_CODES
_UNSET = object()
_backend = _UNSET
_policy = None
_status = "disabled"
logger = logging.getLogger(__name__)


def get_ascii_native_status():
    """Report this worker's state without importing or building a backend."""
    return {"experimental": True, "requested": IS_REQUESTED,
            "state": _status, "native_input_range": [64, 4096],
            "platform_qualification": "Linux CPython 3.10 local review only"}


def try_verbalize_ascii(text, speech_module):
    """Return native output or None to use the unchanged Python helper.

    Once a backend is absent or a policy invalidates, restart the worker to
    retry. Unexpected native errors and interrupts always propagate.
    """
    global _backend, _policy, _status
    if not IS_REQUESTED:
        return None
    if type(text) is not str or not 64 <= len(text) <= 4096 or not text.isascii():
        return None
    if _backend is _UNSET:
        try:
            backend = importlib.import_module("aa2_ascii_native")
        except (ImportError, OSError):
            _backend = None
            _status = "unavailable_python_fallback"
            logger.warning("Experimental ASCII native backend unavailable; using Python")
            return None
        if type(getattr(backend, "POLICY_API", None)) is not int or backend.POLICY_API != 1:
            _backend = None
            _status = "incompatible_python_fallback"
            logger.warning("Experimental ASCII native API incompatible; using Python")
            return None
        _backend = backend
        _policy = backend.create_policy(speech_policy, speech_module, _EXPECTED_FUNCTIONS, _EXPECTED_CODES)
        _status = "active" if _policy is not None else "invalidated_python_fallback"
    if _policy is None:
        return None
    # Native validation and translation stay under one GIL-held call. No
    # Python callback or detached work separates policy validation from use.
    result = _policy.translate_checked(text, speech_policy, speech_module)
    if result is None:
        _policy = None
        _status = "invalidated_python_fallback"
        logger.warning("Experimental ASCII policy changed; using Python until worker restart")
        return None
    if type(result) is not tuple or len(result) != 2:
        raise ValueError("Invalid experimental ASCII native result")
    translated, counts = result
    if type(translated) is not str or type(counts) is not list or len(counts) > 128:
        raise ValueError("Invalid experimental ASCII native evidence")
    previous, total = -1, 0
    for row in counts:
        if (type(row) is not tuple or len(row) != 3
                or any(type(value) is not int for value in row)
                or not previous < row[0] < 128 or row[1] not in (1, 2)
                or not 0 < row[2] <= len(text)):
            raise ValueError("Invalid experimental ASCII native count")
        previous = row[0]
        total += row[2]
    if total > len(text):
        raise ValueError("Invalid experimental ASCII native total")
    spoken, dropped, drop_count = [], [], 0
    for codepoint, kind, count in counts:
        if kind == 1:
            spoken.append(chr(codepoint))
        else:
            dropped.append(chr(codepoint))
            drop_count += count
    transformations = []
    if spoken:
        transformations.append({"type": "verbalized_symbol", "symbols": spoken})
    if dropped:
        transformations.append({"type": "dropped_unspeakable", "symbols": dropped,
                                "count": drop_count})
    return translated, transformations
