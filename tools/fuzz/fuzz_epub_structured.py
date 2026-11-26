#!/usr/bin/env python3
"""Atheris fuzz harness: structure-aware EPUB extraction (`routers.script.extract_epub_text`).

Builds a valid ZIP EPUB container from FuzzedDataProvider and mutates container,
OPF manifest/spine, nav/NCX documents, encodings, XML prologs, and XHTML bodies
so coverage reaches deep into the EPUB/XHTML/TOC extractor.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
import zipfile

import atheris

from _common import (
    EPUB_INSTRUMENT_MODULES,
    configure_sys_path,
    default_fuzz_argv,
    is_expected_missing_member_key_error,
    is_known_xhtml_encoding_error,
    is_known_xml_toc_encoding_error,
    require_linux,
    set_headless_env,
)
from _epub_builder import build_structured_epub

require_linux()
set_headless_env()
configure_sys_path()

with atheris.instrument_imports(include=EPUB_INSTRUMENT_MODULES):
    from routers.script import extract_epub_text, normalize_extracted_text


_EXPECTED_REFUSALS = (
    zipfile.BadZipFile,
    ET.ParseError,
    ValueError,
)


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    epub = build_structured_epub(fdp, parity_mode=False)
    try:
        text, report, navigation_only = extract_epub_text(
            "structured.epub",
            archive_bytes=epub.archive_bytes,
            include_report=True,
            include_navigation_only=True,
        )
    except _EXPECTED_REFUSALS:
        return
    except KeyError as exc:
        if is_expected_missing_member_key_error(exc):
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

    plain = extract_epub_text("structured.epub", archive_bytes=epub.archive_bytes, include_report=False)
    if plain != text:
        raise AssertionError("include_report=True changed extracted text")


def main() -> None:
    argv = default_fuzz_argv(sys.argv, corpus_name="epub_structured")
    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
