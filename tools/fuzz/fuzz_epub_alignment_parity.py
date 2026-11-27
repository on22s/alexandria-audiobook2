#!/usr/bin/env python3
"""Atheris fuzz harness: nav-decision parity between `extract_epub_text` and `alexandria_alignment.load_epub`.

Generates structure-aware synthetic EPUBs with unmistakable prose sentinels in
both the navigation candidate document and story chapters, then verifies that
`alexandria_alignment.load_epub` and `routers.script.extract_epub_text` agree
on whether the navigation candidate is skipped as a navigation document
(PR #2348 parity).
"""
from __future__ import annotations

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
    is_expected_epub_extract_value_error,
    is_expected_load_epub_value_error,
    is_expected_missing_member_key_error,
    is_known_ebooklib_parse_nav_index_error,
    is_known_package_xml_encoding_error,
    is_known_xml_toc_encoding_error,
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

# atheris.Fuzz() terminates the process inside libFuzzer, so atexit handlers
# and TemporaryDirectory finalizers never run. In fuzzing mode a single scratch
# EPUB is created lazily and its directory left behind for removal by hand. In
# replay mode (a positional input file) each call uses a throwaway directory
# that is removed before the call returns, so replaying seeds or a crash
# artifact leaves no aa2-fuzz-parity-* directories.
_SCRATCH_DIR: str | None = None
_SCRATCH_EPUB: Path | None = None

_EXPECTED_EXTRACT_REFUSALS = (
    zipfile.BadZipFile,
    ET.ParseError,
)
_EXPECTED_LOAD_REFUSALS = (
    zipfile.BadZipFile,
    ET.ParseError,
    ebooklib_epub.EpubException,
    lxml_etree.XMLSyntaxError,
)


def _load_epub_from_bytes(raw: bytes) -> str:
    """Write ``raw`` to a file load_epub can read and return its text.

    Fuzzing reuses the lazily-created scratch file; replay creates and removes
    a private temporary directory per call so nothing is left behind.
    """
    if _SCRATCH_EPUB is not None:
        _SCRATCH_EPUB.write_bytes(raw)
        return alexandria_alignment.load_epub(str(_SCRATCH_EPUB))
    with tempfile.TemporaryDirectory(prefix="aa2-fuzz-parity-") as directory:
        payload = Path(directory) / "parity.epub"
        payload.write_bytes(raw)
        return alexandria_alignment.load_epub(str(payload))


# Known-issue scope for parity mode (`build_structured_epub(..., parity_mode=True)`):
# - Handled explicitly below:
#   * #2350: non-navigation `nav_linear="no"` spine candidates (note: story
#     chapters deliberately never generate `chapter_linear="no"` in parity_mode,
#     so #2350 is only reachable via the nav-candidate itemref).
#   * #2369: non-exact `application/xhtml+xml` spine items (e.g. `text/html`).
#   * #2371: `ebooklib._parse_nav` `IndexError` on multi-token/non-toc `<nav>`.
# - Not excluded here because the parity builder cannot produce them:
#   * #2367: every archive entry written by `_epub_builder` has a non-empty path
#     (never `""`).
#   * #2368: `parity_mode=True` sets `allow_tricky_encodings=False`, so chapters
#     are always plain UTF-8 without XML encoding declarations.
#   * #2370 & #2375: `parity_mode=True` hardcodes `encoding="utf-8"` in
#     `META-INF/container.xml`, OPF, `nav.xhtml`, and `toc.ncx`.
# If the builder is ever widened to emit those inputs in parity mode, any crash
# will surface immediately instead of being silently masked here.


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
    except UnicodeError:
        # UnicodeError subclasses ValueError; never swallow unexpected codec
        # errors as refusal ValueErrors.
        raise
    except ValueError as exc:
        # Only the extractor's documented malformed-archive/XML/HTML refusals
        # (plus the tracked encoding-declaration bugs) are ignorable; an
        # unexpected ValueError must crash the harness.
        if (
            is_expected_epub_extract_value_error(exc)
            or is_known_xml_toc_encoding_error(exc)
            or is_known_package_xml_encoding_error(exc)
        ):
            extract_error = exc
        else:
            raise
    except _EXPECTED_EXTRACT_REFUSALS as exc:
        extract_error = exc
    except KeyError as exc:
        if is_expected_missing_member_key_error(exc):
            extract_error = exc
        else:
            raise

    # 2. Run alexandria_alignment.load_epub on the exact same bytes
    load_error = None
    aligned_text = None
    try:
        aligned_text = _load_epub_from_bytes(spec.archive_bytes)
    except UnicodeError:
        # UnicodeError subclasses ValueError; never swallow unexpected codec
        # errors as refusal ValueErrors.
        raise
    except ValueError as exc:
        # load_epub documents only its archive-admission and two spine
        # refusals; anything else must crash the harness.
        if is_expected_load_epub_value_error(exc):
            load_error = exc
        else:
            raise
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


def _is_replay(argv: list[str]) -> bool:
    """Return whether libFuzzer is replaying explicit input files.

    Replay passes existing files as positional arguments; fuzzing passes a
    corpus directory (or nothing, in which case ``default_fuzz_argv`` adds a
    scratch corpus). A directory is not a replay.
    """
    return any(
        not arg.startswith("-") and Path(arg).is_file()
        for arg in argv[1:]
    )


def main() -> None:
    global _SCRATCH_DIR, _SCRATCH_EPUB
    argv = default_fuzz_argv(sys.argv, corpus_name="epub_parity")
    if "-help=1" not in argv and not _is_replay(argv):
        _SCRATCH_DIR = tempfile.mkdtemp(prefix="aa2-fuzz-parity-")
        _SCRATCH_EPUB = Path(_SCRATCH_DIR) / "parity.epub"
        print(f"aa2-fuzz: parity scratch EPUB under {_SCRATCH_DIR} "
              "(remove it when done)", file=sys.stderr)
    atheris.Setup(argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
