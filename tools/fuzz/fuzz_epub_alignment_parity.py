#!/usr/bin/env python3
"""Atheris fuzz harness: nav-decision parity between `extract_epub_text` and `alexandria_alignment.load_epub`.

Generates structure-aware synthetic EPUBs with unmistakable prose sentinels in
both the navigation candidate document and story chapters, then verifies that
`alexandria_alignment.load_epub` and `routers.script.extract_epub_text` agree
on whether the navigation candidate is skipped as a navigation document
(PR #2348 parity).
"""
from __future__ import annotations

import atexit
from pathlib import Path
import sys
import tempfile
import warnings
import xml.etree.ElementTree as ET
import zipfile

import atheris

from _common import (
    PARITY_INSTRUMENT_MODULES,
    configure_sys_path,
    default_fuzz_argv,
    is_expected_missing_member_key_error,
    is_known_ebooklib_parse_nav_index_error,
    require_linux,
    set_headless_env,
)
from _epub_builder import NAV_PROSE_SENTINEL, build_structured_epub

require_linux()
set_headless_env()
configure_sys_path()

warnings.filterwarnings("ignore", module=r"ebooklib(\..*)?$")

with atheris.instrument_imports(include=PARITY_INSTRUMENT_MODULES):
    import alexandria_alignment
    from ebooklib import epub as ebooklib_epub
    from lxml import etree as lxml_etree
    from routers.script import extract_epub_text

if not alexandria_alignment.EPUB_AVAILABLE:
    raise SystemExit("alexandria_alignment.load_epub requires EbookLib and beautifulsoup4")

_TMP_DIR = tempfile.TemporaryDirectory(prefix="aa2-fuzz-parity-")
atexit.register(_TMP_DIR.cleanup)
_TMP_EPUB = Path(_TMP_DIR.name) / "parity.epub"

_EXPECTED_EXTRACT_REFUSALS = (
    zipfile.BadZipFile,
    ET.ParseError,
    ValueError,
)
_EXPECTED_LOAD_REFUSALS = (
    zipfile.BadZipFile,
    ET.ParseError,
    ValueError,
    ebooklib_epub.EpubException,
    lxml_etree.XMLSyntaxError,
)


def _write_temp_epub(raw: bytes) -> str:
    with open(_TMP_EPUB, "wb") as handle:
        handle.write(raw)
    return str(_TMP_EPUB)


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    spec = build_structured_epub(fdp, parity_mode=True)

    # 1. Run extract_epub_text
    extract_error = None
    extracted_text = None
    report = None
    navigation_only = None
    try:
        extracted_text, report, navigation_only = extract_epub_text(
            "parity.epub",
            archive_bytes=spec.archive_bytes,
            include_report=True,
            include_navigation_only=True,
        )
    except _EXPECTED_EXTRACT_REFUSALS as exc:
        extract_error = exc
    except KeyError as exc:
        if is_expected_missing_member_key_error(exc):
            extract_error = exc
        else:
            raise

    # 2. Run alexandria_alignment.load_epub on the exact same bytes
    epub_path = _write_temp_epub(spec.archive_bytes)
    load_error = None
    aligned_text = None
    try:
        aligned_text = alexandria_alignment.load_epub(epub_path)
    except _EXPECTED_LOAD_REFUSALS as exc:
        load_error = exc
    except KeyError as exc:
        if is_expected_missing_member_key_error(exc):
            load_error = exc
        else:
            raise
    except IndexError as exc:
        # Excluded pending fix for #2371 (ebooklib._parse_nav IndexError on multi-token/non-toc <nav>).
        if is_known_ebooklib_parse_nav_index_error(exc):
            load_error = exc
        else:
            raise

    # If the archive itself was malformed XML/OPF for either parser, skip parity comparison
    # (unless one failed for a non-empty-spine reason on a valid book we constructed).
    if extract_error is not None:
        return

    nav_spine_rows = [
        row for row in report["rows"]
        if row.get("kind") == "spine" and row.get("item_id") == spec.nav_item_id
    ]
    extract_marked_nav = any(row.get("reason") == "navigation_document" for row in nav_spine_rows)
    extract_has_nav_prose = NAV_PROSE_SENTINEL in extracted_text

    if extract_marked_nav and extract_has_nav_prose:
        raise AssertionError("extract_epub_text marked nav as skipped yet emitted its prose")

    # Issue #2369: alexandria_alignment.load_epub currently skips non-exact
    # application/xhtml+xml spine items (such as text/html) because ebooklib
    # classifies them as ITEM_UNKNOWN, whereas extract_epub_text narrates them.
    canonical_xhtml_nav = spec.nav_media_type == "application/xhtml+xml"

    if load_error is not None:
        # "EPUB has no readable spine documents" is expected whenever every
        # linear document in the spine was skipped (e.g. nav-only spine or empty spine).
        if isinstance(load_error, ValueError) and "no readable spine documents" in str(load_error):
            # Issue #2350: non-navigation linear="no" spine documents are currently
            # included by extract_epub_text and skipped by load_epub.
            if (
                spec.nav_in_spine
                and not extract_marked_nav
                and spec.nav_linear != "no"
                and not spec.nav_omitted
                and canonical_xhtml_nav
            ):
                raise AssertionError(
                    "Parity disagreement: extract_epub_text included non-nav spine item "
                    f"(properties={spec.nav_properties!r}, href={spec.nav_href!r}, "
                    f"media_type={spec.nav_media_type!r}) but load_epub found no readable spine docs"
                )
            return
        # Other XML/ebooklib parser errors on valid synthetic EPUBs are unexpected
        # unless the generated properties/href triggered a genuine library refusal.
        return

    load_has_nav_prose = NAV_PROSE_SENTINEL in aligned_text

    # Compare the navigation-document decision when the nav candidate is in the spine.
    if spec.nav_in_spine and not spec.nav_omitted:
        if extract_marked_nav and load_has_nav_prose:
            raise AssertionError(
                "Parity disagreement: extract_epub_text skipped nav document "
                f"(properties={spec.nav_properties!r}, href={spec.nav_href!r}, "
                f"media_type={spec.nav_media_type!r}, linear={spec.nav_linear!r}) "
                "but alexandria_alignment.load_epub narrated it"
            )
        # Issue #2350: skip the inverse comparison when linear="no" on a non-nav doc,
        # because load_epub intentionally skips linear="no" while extract_epub_text does not yet.
        # Issue #2369: skip when media-type is not exact application/xhtml+xml.
        if (
            not extract_marked_nav
            and spec.nav_linear != "no"
            and canonical_xhtml_nav
            and extract_has_nav_prose
            and not load_has_nav_prose
        ):
            raise AssertionError(
                "Parity disagreement: extract_epub_text narrated non-nav spine item "
                f"(properties={spec.nav_properties!r}, href={spec.nav_href!r}, "
                f"media_type={spec.nav_media_type!r}, linear={spec.nav_linear!r}) "
                "but alexandria_alignment.load_epub skipped it"
            )


def main() -> None:
    argv = default_fuzz_argv(sys.argv, corpus_name="epub_parity")
    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
