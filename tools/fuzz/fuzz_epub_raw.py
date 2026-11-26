#!/usr/bin/env python3
"""Atheris fuzz harness: raw-byte EPUB extraction (`routers.script.extract_epub_text`).

Feeds arbitrary fuzzed bytes directly into `extract_epub_text(..., archive_bytes=data)`.
Documented refusal exceptions (malformed ZIP/XML/HTML/archive-limit ValueErrors)
are ignored; unexpected exceptions, hangs, memory blow-ups, or report/non-report
text divergence crash the harness.
"""
from __future__ import annotations

import lzma
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib

import atheris

from _common import (
    EPUB_INSTRUMENT_MODULES,
    configure_sys_path,
    default_fuzz_argv,
    is_expected_missing_member_key_error,
    is_expected_zip_bz2_error,
    is_expected_zip_runtime_error,
    is_known_empty_zip_member_index_error,
    is_known_xhtml_encoding_error,
    is_known_xml_toc_encoding_error,
    require_linux,
    set_headless_env,
)

require_linux()
set_headless_env()
configure_sys_path()

with atheris.instrument_imports(include=EPUB_INSTRUMENT_MODULES):
    from routers.script import extract_epub_text, normalize_extracted_text


_EXPECTED_REFUSALS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    EOFError,
    lzma.LZMAError,
    NotImplementedError,
    ET.ParseError,
    ValueError,
)


def test_one_input(data: bytes) -> None:
    try:
        text, report, navigation_only = extract_epub_text(
            "fuzz.epub",
            archive_bytes=data,
            include_report=True,
            include_navigation_only=True,
        )
    except _EXPECTED_REFUSALS:
        return
    except RuntimeError as exc:
        if is_expected_zip_runtime_error(exc):
            return
        raise
    except KeyError as exc:
        if is_expected_missing_member_key_error(exc):
            return
        raise
    except OSError as exc:
        if is_expected_zip_bz2_error(exc):
            return
        raise
    except IndexError as exc:
        # Excluded pending fix for #2367 (empty-filename ZIP member in validate_epub_archive).
        if is_known_empty_zip_member_index_error(exc):
            return
        raise
    except (LookupError, UnicodeDecodeError) as exc:
        # Excluded pending fix for #2368 (get_epub_xhtml_text) and #2370 (ET.fromstring nav/NCX).
        if is_known_xhtml_encoding_error(exc) or is_known_xml_toc_encoding_error(exc):
            return
        raise

    if not isinstance(text, str) or not isinstance(report, dict) or not isinstance(navigation_only, bool):
        raise AssertionError("extract_epub_text returned unexpected types")
    if normalize_extracted_text(text) != text:
        raise AssertionError("extract_epub_text returned non-normalized text")
    if navigation_only and text:
        raise AssertionError("navigation_only=True must not return non-empty text")

    plain = extract_epub_text("fuzz.epub", archive_bytes=data, include_report=False)
    if plain != text:
        raise AssertionError("include_report=True changed extracted text")


def main() -> None:
    argv = default_fuzz_argv(sys.argv, corpus_name="epub_raw")
    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
