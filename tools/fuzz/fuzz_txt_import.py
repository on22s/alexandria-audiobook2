#!/usr/bin/env python3
"""Atheris fuzz harness: TXT decoding, newline/invisible normalization, and preflight.

Exercises:
- source_encoding.get_decoded_source_text
- source_encoding.get_normalized_source_newlines
- routers.script.normalize_extracted_text
- three_pass_generate.prepare_source_text
"""
from __future__ import annotations

import contextlib
import io
import sys

import atheris

from _common import (
    TXT_INSTRUMENT_MODULES,
    configure_sys_path,
    default_fuzz_argv,
    require_linux,
    set_headless_env,
)

require_linux()
set_headless_env()
configure_sys_path()

with atheris.instrument_imports(include=TXT_INSTRUMENT_MODULES):
    from routers.script import normalize_extracted_text
    from source_encoding import get_decoded_source_text, get_normalized_source_newlines
    from three_pass_generate import prepare_source_text


_ALLOWED_ENCODINGS = frozenset({"utf-8", "cp1252", "utf-8/replace"})
_INVISIBLE_CHARS = ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\u202f", "\u2009")
_EXPECTED_PREPARE_REFUSAL_SUBSTRINGS = (
    "replacement-character density",
    "unsafe control characters",
    "replacement characters, above the shared limit",
)


class _NullWriter(io.TextIOBase):
    def write(self, s: str) -> int:
        return len(s)


_NULL_WRITER = _NullWriter()


def _check_prepare(text: str) -> None:
    try:
        with contextlib.redirect_stdout(_NULL_WRITER):
            prepared, audit = prepare_source_text(text)
    except ValueError as exc:
        msg = str(exc)
        if any(snippet in msg for snippet in _EXPECTED_PREPARE_REFUSAL_SUBSTRINGS):
            return
        raise
    if not isinstance(prepared, str) or not isinstance(audit, dict):
        raise AssertionError("prepare_source_text returned unexpected types")
    for key in ("repaired", "residual", "unresolved"):
        value = audit.get(key)
        if type(value) is not int or value < 0:
            raise AssertionError(f"prepare_source_text audit[{key!r}] invalid: {value!r}")


def test_one_input(data: bytes) -> None:
    decoded, encoding = get_decoded_source_text(data)
    if encoding not in _ALLOWED_ENCODINGS or not isinstance(decoded, str):
        raise AssertionError(f"unexpected decode result: encoding={encoding!r}")

    lf_text = get_normalized_source_newlines(decoded)
    if "\r" in lf_text:
        raise AssertionError("CR survived get_normalized_source_newlines")
    if get_normalized_source_newlines(lf_text) != lf_text:
        raise AssertionError("get_normalized_source_newlines is not idempotent")

    norm_text = normalize_extracted_text(lf_text)
    if "\n\n\n" in norm_text:
        raise AssertionError("triple newline survived normalize_extracted_text")
    for ch in _INVISIBLE_CHARS:
        if ch in norm_text:
            raise AssertionError(f"invisible character U+{ord(ch):04X} survived normalize_extracted_text")
    if normalize_extracted_text(norm_text) != norm_text:
        raise AssertionError("normalize_extracted_text is not idempotent")

    # Exercise prepare_source_text on both raw LF-normalized TXT input and
    # extracted/normalized text.
    _check_prepare(lf_text)
    if norm_text != lf_text:
        _check_prepare(norm_text)


def main() -> None:
    argv = default_fuzz_argv(sys.argv, corpus_name="txt_import")
    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
