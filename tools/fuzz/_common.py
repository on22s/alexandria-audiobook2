"""Shared bootstrap helpers for Alexandria Atheris fuzz harnesses.

These harnesses live under tools/fuzz/ (outside app/tests/) so unittest
discovery never collects or runs them. They are Linux-only and require
`app/requirements-fuzz.txt` (`atheris==2.3.0`).
"""
from __future__ import annotations

import os
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"

# Keep instrumentation scoped to the target importer/normalizer modules so
# startup stays fast (~1 s) and coverage feedback stays focused instead of
# instrumenting FastAPI, Pydantic, NumPy, or stdlib networking.
EPUB_INSTRUMENT_MODULES = [
    "routers.script",
    "epub_package",
    "epub_extraction_report",
    "ruby_text",
    "alexandria_alignment",
    "source_normalization",
]

TXT_INSTRUMENT_MODULES = [
    "source_encoding",
    "routers.script",
    "three_pass_generate",
    "repair_source_encoding",
    "script_preflight",
    "silent_word_characters",
    "speech_text",
    "utils",
    "source_normalization",
]

PARITY_INSTRUMENT_MODULES = [
    "routers.script",
    "epub_package",
    "epub_extraction_report",
    "ruby_text",
    "alexandria_alignment",
]


def configure_sys_path() -> None:
    """Expose app/ and repo root without requiring callers to set PYTHONPATH."""
    for entry in (str(APP_DIR), str(REPO_ROOT)):
        if entry not in sys.path:
            sys.path.insert(0, entry)


def default_fuzz_argv(argv: list[str], *, corpus_name: str,
                      timeout_seconds: int = 5,
                      rss_limit_mb: int = 2048,
                      max_len: int = 65536) -> list[str]:
    """Build libFuzzer argv with safe defaults unless the caller overrides them."""
    args = list(argv)
    flags = {arg.split("=", 1)[0] for arg in args[1:] if arg.startswith("-")}
    if "-timeout" not in flags:
        args.append(f"-timeout={timeout_seconds}")
    if "-rss_limit_mb" not in flags:
        args.append(f"-rss_limit_mb={rss_limit_mb}")
    if "-max_len" not in flags:
        args.append(f"-max_len={max_len}")
    # Only append the checked-in seed corpus if the caller did not specify any
    # positional corpus directory or replay file.
    has_positional = any(not arg.startswith("-") for arg in args[1:])
    if not has_positional:
        seed_dir = CORPUS_ROOT / corpus_name
        if seed_dir.is_dir():
            args.append(str(seed_dir))
    return args


def is_expected_zip_runtime_error(exc: RuntimeError) -> bool:
    """Return whether a RuntimeError is stdlib zipfile's encrypted-member refusal."""
    msg = str(exc).lower()
    return "encrypted" in msg or "password" in msg or "compression" in msg


def is_expected_missing_member_key_error(exc: KeyError) -> bool:
    """Return whether a KeyError is stdlib zipfile's missing-member refusal."""
    return bool(exc.args) and isinstance(exc.args[0], str) and "There is no item named" in exc.args[0]


def is_known_empty_zip_member_index_error(exc: IndexError) -> bool:
    """Known bug #2367: validate_epub_archive -> ZipInfo.is_dir() IndexError on '' filename."""
    tb = exc.__traceback__
    seen_validate = False
    last_name = None
    while tb is not None:
        name = tb.tb_frame.f_code.co_name
        if name == "validate_epub_archive":
            seen_validate = True
        last_name = name
        tb = tb.tb_next
    return seen_validate and last_name == "is_dir"


def is_known_xhtml_encoding_error(exc: BaseException) -> bool:
    """Known bug #2368: get_epub_xhtml_text LookupError/UnicodeDecodeError on bad XML encoding."""
    if not isinstance(exc, (LookupError, UnicodeDecodeError)):
        return False
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "get_epub_xhtml_text":
            return True
        tb = tb.tb_next
    return False


def is_known_xml_toc_encoding_error(exc: BaseException) -> bool:
    """Known bug #2370: ET.fromstring LookupError on unknown XML encoding in nav/NCX."""
    if not isinstance(exc, LookupError):
        return False
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name in ("_get_epub3_toc_entries", "_get_epub2_toc_entries", "get_epub_package"):
            return True
        tb = tb.tb_next
    return False


def is_expected_zip_bz2_error(exc: OSError) -> bool:
    """Return whether OSError is stdlib bz2's corrupt-stream error during zipfile decompression."""
    if "Invalid data stream" not in str(exc):
        return False
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "_read1" and "zipfile" in tb.tb_frame.f_code.co_filename:
            return True
        tb = tb.tb_next
    return False


def is_known_ebooklib_parse_nav_index_error(exc: IndexError) -> bool:
    """Known bug #2371: ebooklib._parse_nav IndexError on multi-token or non-toc <nav>."""
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "_parse_nav" and "ebooklib" in tb.tb_frame.f_code.co_filename:
            return True
        tb = tb.tb_next
    return False


def require_linux() -> None:
    if sys.platform != "linux":
        raise SystemExit(f"Atheris fuzz harnesses are Linux-only (current platform: {sys.platform})")


def set_headless_env() -> None:
    """Prevent accidental GPU/network probing when importing app modules."""
    os.environ.setdefault("ALEXANDRIA_GPU_LOCK_HELD", "0")
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("HIP_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("ROCR_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
