"""Shared bootstrap helpers for Alexandria Atheris fuzz harnesses.

These harnesses live under tools/fuzz/ (outside app/tests/) so unittest
discovery never collects or runs them. They are Linux-only and require
`app/requirements-fuzz.txt` (`atheris==2.3.0`).
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import types
import xml.etree.ElementTree as ET
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"
CORPUS_ROOT = Path(__file__).resolve().parent / "corpus"

_SCRIPT_ROUTER_PATH = (APP_DIR / "routers" / "script.py").resolve()
_EPUB_PACKAGE_PATH = (APP_DIR / "epub_package.py").resolve()
_ALEXANDRIA_ALIGNMENT_PATH = (REPO_ROOT / "alexandria_alignment.py").resolve()
_ZIPFILE_PATH = Path(zipfile.__file__).resolve()
_ELEMENTTREE_PATH = Path(ET.__file__).resolve()

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
    """Build libFuzzer argv with safe defaults unless the caller overrides them.

    libFuzzer requires ``-flag=value`` syntax (single dash, equals sign). Space-
    separated flags such as ``-max_total_time 60`` are rejected instead of
    silently treating ``60`` as a corpus directory.

    When the caller does not pass any positional corpus directory or replay
    file, libFuzzer receives a fresh temporary scratch directory first followed
    by the checked-in seed directory, so newly discovered units are written to
    the scratch directory and never dirty ``tools/fuzz/corpus/``.

    ``atheris.Fuzz()`` terminates the process inside libFuzzer, so Python
    ``atexit`` handlers and ``finally`` blocks after it never run: the scratch
    directory is deliberately left behind and its path is printed so a local
    run can be cleaned up by hand. CI always passes an explicit corpus
    directory (see ``.github/workflows/fuzz.yml``) and leaves nothing behind.
    """
    if not argv:
        args: list[str] = ["fuzz"]
    else:
        args = [argv[0]]

    flags: set[str] = set()
    has_positional = False
    after_double_dash = False

    for idx, arg in enumerate(argv[1:], start=1):
        if after_double_dash:
            has_positional = True
            args.append(arg)
            continue
        if arg == "--":
            after_double_dash = True
            args.append(arg)
            continue
        if arg in ("-h", "-help", "--help"):
            flags.add("-help")
            args.append("-help=1")
            continue
        if arg.startswith("-"):
            if "=" not in arg:
                next_arg = argv[idx + 1] if idx + 1 < len(argv) else None
                hint = (
                    f"{arg.lstrip('-')}={next_arg}"
                    if next_arg is not None and not next_arg.startswith("-")
                    else f"{arg.lstrip('-')}=<value>"
                )
                raise SystemExit(
                    f"Invalid libFuzzer flag {arg!r}: libFuzzer requires '-flag=value' "
                    f"syntax (use '-{hint}' instead of space-separated arguments)."
                )
            if arg.startswith("--"):
                raise SystemExit(
                    f"Invalid libFuzzer flag {arg!r}: libFuzzer requires single-dash "
                    f"'-flag=value' syntax (use '-{arg.lstrip('-')}' instead)."
                )
            flags.add(arg.split("=", 1)[0])
            args.append(arg)
        else:
            has_positional = True
            args.append(arg)

    if "-timeout" not in flags:
        args.append(f"-timeout={timeout_seconds}")
    if "-rss_limit_mb" not in flags:
        args.append(f"-rss_limit_mb={rss_limit_mb}")
    if "-max_len" not in flags:
        args.append(f"-max_len={max_len}")

    if not has_positional and "-help" not in flags:
        seed_dir = CORPUS_ROOT / corpus_name
        if seed_dir.is_dir():
            scratch = tempfile.mkdtemp(prefix=f"aa2-fuzz-{corpus_name}-")
            print(
                f"aa2-fuzz: writing grown corpus units to {scratch} "
                "(remove it when done; atheris.Fuzz does not run atexit handlers)",
                file=sys.stderr,
            )
            # First corpus directory receives new inputs; seed_dir stays read-only.
            args.append(scratch)
            args.append(str(seed_dir))
    return args


def _traceback_frames(exc: BaseException) -> list[types.TracebackType]:
    frames: list[types.TracebackType] = []
    tb = exc.__traceback__
    while tb is not None:
        frames.append(tb)
        tb = tb.tb_next
    return frames


def _resolved_path(filename: str) -> Path | None:
    if not filename or filename.startswith("<"):
        return None
    try:
        return Path(filename).resolve()
    except OSError:
        return None


# Known-bug and stdlib-refusal filters below match the *throwing* function by
# source file and function name, plus the exception type. They deliberately do
# not decode CPython bytecode at ``tb.tb_lasti``: opcode names and call layouts
# differ across interpreter versions (``LOAD_METHOD`` is gone in 3.12,
# ``CALL_METHOD`` in 3.13), and Atheris rewrites the code objects it
# instruments, so an instruction window is a CPython-3.10-only assumption.
# Each filtered function has one reachable failure site (or, for
# get_epub_xhtml_text, two decode statements that are the same documented bug),
# so file+function+type is exactly as narrow there while staying portable
# across CPython versions and Atheris instrumentation.
def _is_zipfile_frame(tb: types.TracebackType) -> bool:
    return _resolved_path(tb.tb_frame.f_code.co_filename) == _ZIPFILE_PATH


def _is_elementtree_frame(tb: types.TracebackType) -> bool:
    return _resolved_path(tb.tb_frame.f_code.co_filename) == _ELEMENTTREE_PATH


def _is_ebooklib_epub_frame(tb: types.TracebackType) -> bool:
    parts = Path(tb.tb_frame.f_code.co_filename).parts
    return len(parts) >= 2 and parts[-2] == "ebooklib" and parts[-1] == "epub.py"


def is_expected_zip_runtime_error(exc: RuntimeError) -> bool:
    """Return whether a RuntimeError is stdlib zipfile's encrypted/compression refusal."""
    msg = str(exc).lower()
    if not ("encrypted" in msg or "password" in msg or "compression" in msg):
        return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return (
        _is_zipfile_frame(last)
        and last.tb_frame.f_code.co_name in ("open", "__init__", "_check_compression")
    )


def is_expected_missing_member_key_error(exc: KeyError) -> bool:
    """Return whether a KeyError is stdlib zipfile.ZipFile.getinfo's missing-member refusal."""
    if not (bool(exc.args) and isinstance(exc.args[0], str) and "There is no item named" in exc.args[0]):
        return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return _is_zipfile_frame(last) and last.tb_frame.f_code.co_name == "getinfo"


def is_expected_zip_bz2_error(exc: OSError) -> bool:
    """Return whether OSError is stdlib bz2's corrupt-stream error during zipfile decompression."""
    if "Invalid data stream" not in str(exc):
        return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return _is_zipfile_frame(last) and last.tb_frame.f_code.co_name == "_read1"


def is_expected_zip_filename_unicode_error(exc: UnicodeError) -> bool:
    """Return whether UnicodeDecodeError is stdlib zipfile rejecting corrupt UTF-8 header filenames."""
    if not isinstance(exc, UnicodeDecodeError):
        return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return _is_zipfile_frame(last) and last.tb_frame.f_code.co_name in ("_RealGetContents", "open")


# A ZIP whose central directory points at a negative/out-of-range member
# offset makes the underlying file object's seek() raise ValueError ("negative
# seek value -N" / "seek out of range") from inside zipfile's read path. That
# is stdlib's malformed-archive refusal, the same class as BadZipFile, not an
# application failure: the upload route already answers it with HTTP 400.
_ZIP_CORRUPT_OFFSET_VALUE_ERROR_PREFIXES = (
    "negative seek value ",
    "seek out of range",
)


def is_expected_zip_value_error(exc: BaseException) -> bool:
    """Return whether ValueError is stdlib zipfile/BytesIO's corrupt-offset refusal."""
    if not isinstance(exc, ValueError):
        return False
    if not str(exc).startswith(_ZIP_CORRUPT_OFFSET_VALUE_ERROR_PREFIXES):
        return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    return _is_zipfile_frame(frames[-1])


def is_known_empty_zip_member_index_error(exc: IndexError) -> bool:
    """Known bug #2367: validate_epub_archive -> ZipInfo.is_dir() IndexError on '' filename.

    ``validate_epub_archive`` calls ``member.is_dir()`` at exactly one site, so
    file+function+type identifies the documented call site without bytecode.
    """
    frames = _traceback_frames(exc)
    if len(frames) < 2:
        return False
    caller, last = frames[-2], frames[-1]
    return (
        _is_zipfile_frame(last)
        and last.tb_frame.f_code.co_name == "is_dir"
        and _resolved_path(caller.tb_frame.f_code.co_filename) == _ALEXANDRIA_ALIGNMENT_PATH
        and caller.tb_frame.f_code.co_name == "validate_epub_archive"
    )


# codecs.lookup() (used by bytes.decode) raises ValueError with this message
# when the codec name itself contains NUL, e.g. an XHTML XML declaration of
# `encoding="ut\x00f-8"`. get_epub_xhtml_text passes the declaration bytes to
# bytes.decode, so this is the same tracked bug as #2368.
_EMBEDDED_NULL_VALUE_ERROR = "embedded null character"


def is_known_xhtml_encoding_error(exc: BaseException) -> bool:
    """Known bug #2368: get_epub_xhtml_text decode error on a bad XML encoding declaration.

    Both documented failure sites (the ``encoding="..."`` declaration decode
    and the resource ``data.decode(...)``) live in this one function, and it has
    no other language-level decode failure, so the function frame is the scope.
    Besides LookupError/UnicodeError, ``codecs`` raises ``ValueError("embedded
    null character")`` when the declaration-supplied codec name contains NUL.
    """
    if not isinstance(exc, (LookupError, UnicodeError)):
        if not (isinstance(exc, ValueError) and str(exc) == _EMBEDDED_NULL_VALUE_ERROR):
            return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return (
        _resolved_path(last.tb_frame.f_code.co_filename) == _SCRIPT_ROUTER_PATH
        and last.tb_frame.f_code.co_name == "get_epub_xhtml_text"
    )


# ET.fromstring(bytes) refuses an XML encoding declaration it cannot process in
# two ways on CPython 3.10: LookupError("unknown encoding: ...") for a codec
# expat does not know, and ValueError("multi-byte encodings are not supported")
# for a declared encoding expat cannot stream (utf-16-le/utf-16-be/shift_jis/
# ...; a BOM-carrying utf-16 document is fine). Both are the same tracked
# encoding-declaration bug in the caller that only catches ET.ParseError, so
# the #2370 / #2375 predicates accept both exception types.
_MULTIBYTE_ENCODING_VALUE_ERROR = "multi-byte encodings are not supported"


def _is_et_encoding_refusal(exc: BaseException) -> bool:
    if isinstance(exc, LookupError):
        return True
    return isinstance(exc, ValueError) and str(exc) == _MULTIBYTE_ENCODING_VALUE_ERROR


def is_known_xml_toc_encoding_error(exc: BaseException) -> bool:
    """Known bug #2370: _get_epub3_toc_entries / _get_epub2_toc_entries ET.fromstring encoding refusal."""
    if not _is_et_encoding_refusal(exc):
        return False
    frames = _traceback_frames(exc)
    if len(frames) < 2:
        return False
    caller, last = frames[-2], frames[-1]
    return (
        _is_elementtree_frame(last)
        and last.tb_frame.f_code.co_name == "XML"
        and _resolved_path(caller.tb_frame.f_code.co_filename) == _SCRIPT_ROUTER_PATH
        and caller.tb_frame.f_code.co_name in ("_get_epub3_toc_entries", "_get_epub2_toc_entries")
    )


def is_known_package_xml_encoding_error(exc: BaseException) -> bool:
    """Known bug #2375: get_epub_package ET.fromstring encoding refusal on OPF/container.xml."""
    if not _is_et_encoding_refusal(exc):
        return False
    frames = _traceback_frames(exc)
    if len(frames) < 2:
        return False
    caller, last = frames[-2], frames[-1]
    return (
        _is_elementtree_frame(last)
        and last.tb_frame.f_code.co_name == "XML"
        and _resolved_path(caller.tb_frame.f_code.co_filename) == _EPUB_PACKAGE_PATH
        and caller.tb_frame.f_code.co_name == "get_epub_package"
    )


def is_known_ebooklib_parse_nav_index_error(exc: IndexError) -> bool:
    """Known bug #2371: ebooklib._parse_nav xpath('//nav[@*="toc"]')[0] IndexError.

    ``_parse_nav`` has one reachable IndexError site (the ``[0]`` subscript),
    so file+function+type is the documented call site.
    """
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return (
        _is_ebooklib_epub_frame(last)
        and last.tb_frame.f_code.co_name == "_parse_nav"
    )


# ---------------------------------------------------------------------------
# Documented ValueError refusals.
#
# ValueError is far too broad to catch wholesale: a planted
# `raise ValueError("planted-unexpected-valueerror")` in the code under test
# would be swallowed and the harness would report success. Each tuple below is
# the closed set of refusal messages the fuzzed production functions document,
# matched by prefix so the dynamic suffix (spine index, resource path, parser
# reason) does not matter. Anything else is a genuine unexpected ValueError and
# must crash the harness.
# ---------------------------------------------------------------------------

# alexandria_alignment.validate_epub_archive: archive admission before parsing.
_ARCHIVE_ADMISSION_VALUE_ERROR_PREFIXES = (
    "captured EPUB archive must be bytes",
    "EPUB archive exceeds the 512 MB input limit",
    "EPUB archive has too many entries",
    "EPUB archive expands beyond 256 MB",
    "EPUB archive has an excessive expansion ratio",
)

# routers.script.extract_epub_text and the helpers it reaches:
# app.epub_package.get_epub_package (package structure), and its own resource
# decoding / XHTML extraction refusals.
_EXTRACT_VALUE_ERROR_PREFIXES = _ARCHIVE_ADMISSION_VALUE_ERROR_PREFIXES + (
    "EPUB exceeds source upload limit",
    "Invalid EPUB package member",
    "Unsafe EPUB package member",
    "Invalid EPUB: no rootfile found in container.xml",
    "invalid Unicode: ",
    "invalid XML: ",
    "invalid XML while ",
    "unclosed <",
    "Malformed EPUB resource at spine index ",
)

# alexandria_alignment.load_epub: archive admission, then its own two spine
# refusals. It does not call the extractor, so the extractor messages are
# deliberately absent here.
_LOAD_EPUB_VALUE_ERROR_PREFIXES = _ARCHIVE_ADMISSION_VALUE_ERROR_PREFIXES + (
    "EPUB spine references missing document: ",
    "EPUB has no readable spine documents",
)


def is_expected_epub_extract_value_error(exc: BaseException) -> bool:
    """Return whether a ValueError is a documented extract_epub_text refusal.

    Beyond the production messages, the stdlib zipfile corrupt-offset refusal
    is accepted: it is a malformed-archive refusal like ``BadZipFile`` (raised
    from ``zipfile``'s read path, and the upload route already answers it with
    HTTP 400), not an unexpected extractor failure.
    """
    if not isinstance(exc, ValueError):
        return False
    return str(exc).startswith(_EXTRACT_VALUE_ERROR_PREFIXES) or is_expected_zip_value_error(exc)


def is_expected_load_epub_value_error(exc: BaseException) -> bool:
    """Return whether a ValueError is a documented load_epub refusal."""
    if not isinstance(exc, ValueError):
        return False
    return str(exc).startswith(_LOAD_EPUB_VALUE_ERROR_PREFIXES) or is_expected_zip_value_error(exc)


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
