"""Shared bootstrap helpers for Alexandria Atheris fuzz harnesses.

These harnesses live under tools/fuzz/ (outside app/tests/) so unittest
discovery never collects or runs them. They are Linux-only and require
`app/requirements-fuzz.txt` (`atheris==2.3.0`).
"""
from __future__ import annotations

import atexit
import dis
import functools
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

# Scratch corpus directories created for default invocations (so libFuzzer never
# writes newly discovered corpus units into the checked-in seed directory).
_SCRATCH_CORPUS_DIRS: list[tempfile.TemporaryDirectory[str]] = []


def _cleanup_scratch_corpus_dirs() -> None:
    while _SCRATCH_CORPUS_DIRS:
        scratch = _SCRATCH_CORPUS_DIRS.pop()
        scratch.cleanup()


atexit.register(_cleanup_scratch_corpus_dirs)

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
            scratch = tempfile.TemporaryDirectory(prefix=f"aa2-fuzz-{corpus_name}-")
            _SCRATCH_CORPUS_DIRS.append(scratch)
            # First corpus directory receives new inputs; seed_dir stays read-only.
            args.append(scratch.name)
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


@functools.lru_cache(maxsize=64)
def _code_instructions(code: types.CodeType) -> tuple[tuple[int, str, object], ...]:
    """Cache (offset, opname, argval) tuples for a code object.

    Atheris bytecode instrumentation shifts `tb.tb_lineno` on instrumented
    frames, so call-site matching inspects the actual executing instruction
    window at `tb.tb_lasti` instead of relying on source line numbers.
    """
    return tuple((ins.offset, ins.opname, ins.argval) for ins in dis.get_instructions(code))


def _bytecode_tail(tb: types.TracebackType, count: int) -> tuple[tuple[str, object], ...]:
    insns = _code_instructions(tb.tb_frame.f_code)
    for idx, (offset, _opname, _argval) in enumerate(insns):
        if offset == tb.tb_lasti:
            start = max(0, idx - count + 1)
            return tuple((op, val) for _off, op, val in insns[start:idx + 1])
    return ()


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


def is_known_empty_zip_member_index_error(exc: IndexError) -> bool:
    """Known bug #2367: validate_epub_archive -> ZipInfo.is_dir() IndexError on '' filename."""
    frames = _traceback_frames(exc)
    if len(frames) < 2:
        return False
    caller, last = frames[-2], frames[-1]
    return (
        _is_zipfile_frame(last)
        and last.tb_frame.f_code.co_name == "is_dir"
        and _resolved_path(caller.tb_frame.f_code.co_filename) == _ALEXANDRIA_ALIGNMENT_PATH
        and caller.tb_frame.f_code.co_name == "validate_epub_archive"
        and _bytecode_tail(caller, 2) == (("LOAD_METHOD", "is_dir"), ("CALL_METHOD", 0))
    )


def is_known_xhtml_encoding_error(exc: BaseException) -> bool:
    """Known bug #2368: get_epub_xhtml_text decode call-site LookupError/UnicodeError on bad XML encoding."""
    if not isinstance(exc, (LookupError, UnicodeError)):
        return False
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    if (
        _resolved_path(last.tb_frame.f_code.co_filename) != _SCRIPT_ROUTER_PATH
        or last.tb_frame.f_code.co_name != "get_epub_xhtml_text"
    ):
        return False
    # Exact call site 1: `declaration.group(1).decode("ascii")`
    if (
        isinstance(exc, UnicodeDecodeError)
        and _bytecode_tail(last, 3)
        == (("LOAD_METHOD", "decode"), ("LOAD_CONST", "ascii"), ("CALL_METHOD", 1))
    ):
        return True
    # Exact call site 2: `data.decode(encoding, errors="replace")`
    return _bytecode_tail(last, 6) == (
        ("LOAD_FAST", "data"),
        ("LOAD_ATTR", "decode"),
        ("LOAD_FAST", "encoding"),
        ("LOAD_CONST", "replace"),
        ("LOAD_CONST", ("errors",)),
        ("CALL_FUNCTION_KW", 2),
    )


def is_known_xml_toc_encoding_error(exc: BaseException) -> bool:
    """Known bug #2370: _get_epub3_toc_entries / _get_epub2_toc_entries ET.fromstring LookupError."""
    if not isinstance(exc, LookupError):
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
    """Known bug #2375: get_epub_package ET.fromstring LookupError on unknown OPF/container.xml encoding."""
    if not isinstance(exc, LookupError):
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
    """Known bug #2371: ebooklib._parse_nav xpath('//nav[@*="toc"]')[0] IndexError."""
    frames = _traceback_frames(exc)
    if not frames:
        return False
    last = frames[-1]
    return (
        _is_ebooklib_epub_frame(last)
        and last.tb_frame.f_code.co_name == "_parse_nav"
        and _bytecode_tail(last, 5)
        == (
            ("LOAD_METHOD", "xpath"),
            ("LOAD_CONST", "//nav[@*='toc']"),
            ("CALL_METHOD", 1),
            ("LOAD_CONST", 0),
            ("BINARY_SUBSCR", None),
        )
    )


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
