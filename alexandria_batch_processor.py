#!/usr/bin/env python3
"""
Alexandria Batch Processor - Process multiple audiobooks sequentially
Processes one audio file at a time, creating complete datasets for each
"""

import os
import sys
import time
import shutil
import argparse
import subprocess
import json
import re
import logging
import threading
import math
import tempfile
import hashlib
import zipfile
from datetime import datetime
from pathlib import Path

from gpu_stats import run_rocm_smi_json
sys.path.append(str(Path(__file__).resolve().parent / "app"))
from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock

# Setup logging
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"alexandria_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("alexandria_batch")
logger.setLevel(logging.DEBUG)

# File handler (detailed)
fh = logging.FileHandler(log_file)
fh.setLevel(logging.DEBUG)
file_format = logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s')
fh.setFormatter(file_format)

# Console handler (info and above)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
console_format = logging.Formatter('[%(levelname)s] %(message)s')
ch.setFormatter(console_format)

logger.addHandler(fh)
logger.addHandler(ch)

logger.info("=" * 70)
logger.info("Alexandria Batch Processor - Sequential Audiobook Processing")
logger.info("=" * 70)
logger.info(f"Log file: {log_file}")

def get_gpu_stats():
    """Get current GPU memory and utilization stats."""
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None

    stats = {}
    try:
        # NOTE: per-process torch memory stats were dropped here — the main script
        # runs in a subprocess, so the parent always reads 0, and log_gpu_stats
        # only ever logged utilization_percent anyway.

        # Try to get utilization via rocm-smi for AMD GPUs
        data = run_rocm_smi_json(["--showuse"], rocm_smi_path="/opt/rocm/bin/rocm-smi")
        stats['utilization_percent'] = None
        if data:
            # rocm-smi format: {"card0": {"GPU use (%)": "value"}}
            for card_data in data.values():
                gpu_use_str = card_data.get('GPU use (%)', 'N/A')
                if gpu_use_str != 'N/A':
                    stats['utilization_percent'] = float(gpu_use_str)
                break  # Just get first GPU

    except Exception as e:
        logger.warning(f"Could not get GPU stats: {e}")
        return None

    return stats

def log_gpu_stats(label=""):
    """Log GPU memory and utilization statistics."""
    stats = get_gpu_stats()
    if not stats:
        return

    label_str = f" ({label})" if label else ""
    logger.info(f"GPU Usage{label_str}:")
    # Note: Parent process memory will show 0 since main script runs in subprocess
    if stats.get('utilization_percent') is not None:
        logger.info(f"  └─ GPU Utilization: {stats['utilization_percent']:.1f}%")
    else:
        logger.info(f"  └─ GPU Utilization: (rocm-smi unavailable)")

def format_duration(seconds):
    """Format seconds as Xh Ym (or smaller unit when applicable).

    Byte-for-byte identical to alexandria_preparer_rocm_compatible.py's
    version - this orchestrator launches that script as a subprocess per
    book, so their log lines interleave and must format durations the same
    way. See FIXED.md F-093.
    """
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {mins}m"
    elif mins > 0:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"

# Filename noise tokens common in audiobook/EPUB filenames but carrying
# no information about WHICH book it is. Dropped before fuzzy scoring so
# "Hero of Ages-converted" and "Hero of Ages.epub" line up by their title
# tokens alone.
_FILENAME_NOISE_TOKENS = frozenset({
    # Audiobook pipeline / format markers
    'converted', 'audiobook', 'audio', 'unabridged', 'abridged',
    'final', 'edited', 'corrected', 'release', 'mp3', 'wav', 'm4b', 'flac',
    # Edition / publisher markers seen in real EPUB filenames
    'kobo', 'darkhorse', 'yenpress', 'audible', 'tor', 'edition',
    'volume', 'vol', 'book', 'chapter', 'part',
    # z-library decorators
    'library', 'lib', 'sk', 'org',
})


def _normalize_filename_tokens(stem):
    """Lowercase the stem, split on non-alphanumeric, drop noise + pure
    numbers. Returns a list of meaningful title tokens.

    Pure-digit tokens (volume numbers, series indices, year stamps) drop
    out separately so 'Book 01' and 'Book 1' tokenise to the same set.
    """
    tokens = re.findall(r'[a-z0-9]+', stem.lower())
    return [t for t in tokens if t not in _FILENAME_NOISE_TOKENS and not t.isdigit()]


def _fuzzy_score(audio_tokens, book_tokens):
    """F1-style score between two token sets — symmetric and discourages
    BOTH spurious matches (high precision needs most book words in audio)
    AND over-broad book names (high recall needs most audio words in book).

    Returns 0.0 if either side is empty. F1 = 2PR / (P + R).
    """
    if not audio_tokens or not book_tokens:
        return 0.0
    a, b = set(audio_tokens), set(book_tokens)
    common = a & b
    if not common:
        return 0.0
    precision = len(common) / len(b)
    recall    = len(common) / len(a)
    return 2 * precision * recall / (precision + recall)


def get_source_candidates(source_folder):
    """Read and tokenize eligible source filenames once for a batch."""
    if not source_folder or not os.path.isdir(source_folder):
        return []
    return [(entry.path, _normalize_filename_tokens(Path(entry.name).stem))
            for entry in sorted(os.scandir(source_folder), key=lambda item: item.name.lower())
            if entry.is_file() and entry.name.lower().endswith(('.epub', '.txt'))]


def get_source_match_report(audio_file, source_folder, fuzzy_threshold=0.50,
                            source_candidates=None):
    """Return filename evidence, retaining exact EPUB-before-TXT precedence.

    Fuzzy alternatives within 0.05 of the best score require acceptance;
    that margin is a filename policy, not calibrated content confidence.
    """
    report = {"source": None, "score": 0.0, "method": "missing",
              "alternatives": [], "ambiguous": False}
    if not source_folder or not os.path.isdir(source_folder):
        return report
    stem = Path(audio_file).stem
    for ext in ('.epub', '.txt'):
        candidate = Path(source_folder) / (stem + ext)
        if candidate.is_file():
            return {**report, "source": str(candidate), "score": 1.0, "method": "exact"}
    tokens = _normalize_filename_tokens(stem)
    candidates = source_candidates if source_candidates is not None else get_source_candidates(source_folder)
    ranked = sorted(((path, _fuzzy_score(tokens, words)) for path, words in candidates),
                    key=lambda item: (-item[1], item[0].lower()))
    if not ranked or ranked[0][1] < fuzzy_threshold:
        return report
    best, score = ranked[0]
    alternatives = [{"source": path, "score": value} for path, value in ranked[1:]
                    if value >= fuzzy_threshold and score - value <= 0.05]
    return {"source": best, "score": score, "method": "fuzzy",
            "alternatives": alternatives, "ambiguous": bool(alternatives)}


def _find_source_for(audio_file, source_folder, fuzzy_threshold=0.50,
                     source_candidates=None):
    report = get_source_match_report(audio_file, source_folder, fuzzy_threshold, source_candidates)
    if report["ambiguous"]:
        raise ValueError(f"Ambiguous source match for {audio_file}; use an explicit --source")
    return report["source"]


def check_disk_space(path, required_gb_per_file, num_files):
    """Check if disk has enough space for batch processing."""
    try:
        stat = shutil.disk_usage(path)
        free_gb = stat.free / (1024 ** 3)
        required_gb = required_gb_per_file * num_files

        logger.info(f"▶ Disk space check:")
        logger.info(f"  ├─ Available: {free_gb:.1f} GB")
        logger.info(f"  ├─ Estimated needed: ~{required_gb:.1f} GB ({required_gb_per_file} GB/file × {num_files} files)")

        if free_gb < required_gb:
            logger.error(f"  └─ Insufficient disk space at {os.path.abspath(path)}: "
                         f"need {math.ceil((required_gb - free_gb) * 1024 ** 3)} more bytes")
            return False
        else:
            logger.info(f"  └─ ✓ Sufficient disk space")
            return True
    except Exception as e:
        logger.error(f"Disk space check failed at {os.path.abspath(path)}: {e}")
        return False



def get_audio_duration_seconds(path):
    """Read container duration without decoding the audiobook."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10, check=True)
        duration = float(result.stdout.strip())
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid container duration")
        return duration
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        logger.warning(f"Duration unavailable for {path}; retaining disk estimate floor: {error}")
        return None


def get_disk_probe_path(path):
    """Resolve the filesystem of a scratch/output path before it exists."""
    probe = os.path.realpath(os.path.abspath(path))
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            raise OSError(f"Cannot locate a filesystem for {path}")
        probe = parent
    return probe


def save_batch_receipt(path, data):
    """Replace a receipt only after the complete JSON has reached disk."""
    directory = os.path.dirname(path) or "."
    fd, temporary = tempfile.mkstemp(prefix=".batch_results_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as receipt:
            json.dump(data, receipt, indent=2)
            receipt.flush()
            os.fsync(receipt.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def get_output_name(audio_file):
    """Keep dataset names distinct for same-stem audio at different paths."""
    audio_path = Path(audio_file)
    source_id = hashlib.sha256(os.fsencode(os.path.realpath(audio_path))).hexdigest()[:12]
    return f"alexandria_dataset_{audio_path.stem}_{source_id}.zip"


def get_source_identity(audio_file):
    path = Path(audio_file)
    stat = path.stat()
    return {"path": os.path.realpath(path), "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns}


def get_output_volumes(output_name):
    """Find the base archive and any speaker/style/volume-suffixed archives."""
    output = Path(output_name)
    return sorted(str(path) for path in output.parent.iterdir()
                  if path.is_file() and path.suffix == ".zip" and
                  (path.name == output.name or path.stem.startswith(output.stem + "_")))


def get_volume_state(output_name):
    return {path: (os.stat(path).st_size, os.stat(path).st_mtime_ns)
            for path in get_output_volumes(output_name)}


def is_complete_dataset_zip(path):
    """Accept a prior output only when its ZIP and dataset payload are readable."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "metadata.jsonl" not in names or not any(
                    name.startswith(("train/", "val/")) and name.endswith(".wav")
                    for name in names):
                return False
            return archive.testzip() is None and bool(archive.read("metadata.jsonl").strip())
    except (OSError, zipfile.BadZipFile, RuntimeError):
        return False


def get_completed_volumes(audio_file, output_name):
    """Return recorded, verified volumes or an empty list when resume is unsafe."""
    marker = output_name + ".complete.json"
    try:
        with open(marker, encoding="utf-8") as file:
            record = json.load(file)
        volumes = record["volumes"]
        if record["source"] != get_source_identity(audio_file):
            return []
        available = set(get_output_volumes(output_name))
        if not volumes or any(path not in available or not is_complete_dataset_zip(path)
                              for path in volumes):
            return []
        return volumes
    except (OSError, ValueError, KeyError, TypeError):
        return []

class BatchProcessor:
    SUPPORTED_FORMATS = {'.wav', '.mp3', '.m4a', '.flac', '.ogg'}

    def __init__(self, model_path, chunk_size=10.0, language="en", force=False,
                 fallback_model=None, source_folder=None, source_path=None,
                 source_threshold=0.65, keep_unaligned=False, allow_no_source=False,
                 skip_disk_check=False):
        self.model_path = model_path
        self.fallback_model = fallback_model
        self.chunk_size = chunk_size
        self.language = language
        self.force = force  # If True, reprocess even if output exists
        # Source-guided mode is per-file: each audio file looks up a matching
        # source by basename in source_folder, OR uses source_path if that's
        # set (apply same source to every audio file in the batch). None of
        # the source args reach the preparer when source_state is None for
        # a given file → that run stays in legacy ASR-only mode.
        self.source_folder    = source_folder
        self.source_path      = source_path
        self.source_threshold = source_threshold
        self.keep_unaligned   = keep_unaligned
        self.allow_no_source = allow_no_source
        self.skip_disk_check = skip_disk_check
        self.source_matches = {}
        self.results = {
            "succeeded": [],
            "failed": [],
            "skipped": []
        }
        self.total_time = 0
        self.batch_start_time = None
        self.audio_durations = {}
        self.output_bytes_per_second = 0
        self.disk_refused = False

    def validate_files(self, audio_files):
        """Validate all audio files and skip already-processed ones."""
        logger.info("▶ Validating input files...")

        # Fail fast on a typo'd model path before doing any per-file
        # validation work (or accumulating skip reasons that would otherwise
        # never make it into batch_results_*.json - sys.exit(1) below skips
        # print_summary entirely).
        if not self.model_path or not os.path.exists(self.model_path):
            logger.error(f"Model file not found: {self.model_path}")
            logger.error("Cannot proceed without model")
            sys.exit(1)

        if self.fallback_model and not os.path.exists(self.fallback_model):
            logger.error(f"Fallback model not found: {self.fallback_model}")
            logger.error("Either fix the path or omit --fallback-model")
            sys.exit(1)

        valid_files = []

        for audio_file in audio_files:
            audio_path = Path(audio_file)

            if not audio_path.is_file():
                logger.error(f"  ✗ Audio file not found: {audio_file}")
                self.results["skipped"].append({
                    "file": audio_file,
                    "reason": "Audio file not found"
                })
                continue

            if audio_path.suffix.lower() not in self.SUPPORTED_FORMATS:
                logger.warning(f"  ⚠ Unsupported format: {audio_file}")
                self.results["skipped"].append({
                    "file": audio_file,
                    "reason": "Unsupported audio format"
                })
                continue

            # Check if output already exists (resume capability)
            expected_output = get_output_name(audio_file)
            completed_volumes = get_completed_volumes(audio_file, expected_output) if not self.force else []
            if completed_volumes:
                output_size_mb = sum(os.path.getsize(path) for path in completed_volumes) / (1024 * 1024)
                logger.info(f"  ⊘ {audio_path.name} → already processed: {len(completed_volumes)} ZIP volume(s) ({output_size_mb:.1f} MB)")
                self.results["skipped"].append({
                    "file": audio_file,
                    "output": completed_volumes[0],
                    "outputs": completed_volumes,
                    "output_size_mb": output_size_mb,
                    "reason": "Already processed (use --force to reprocess)"
                })
                continue
            if (os.path.exists(expected_output + ".complete.json") or
                    get_output_volumes(expected_output)) and not self.force:
                logger.warning(f"  ⚠ Dataset completion record missing or invalid, reprocessing: {expected_output}")

            file_size_mb = audio_path.stat().st_size / (1024 * 1024)
            logger.info(f"  ✓ {audio_path.name} ({file_size_mb:.1f} MB)")
            valid_files.append(audio_file)

        logger.info(f"  ├─ Model: {Path(self.model_path).name}")
        if self.fallback_model:
            logger.info(f"  ├─ Fallback model: {Path(self.fallback_model).name}")
        # Source-mode summary so user sees up front how files matched up
        if self.source_path:
            logger.info(f"  ├─ Source-guided: {Path(self.source_path).name} "
                        f"(applied to every audio file)")
        elif self.source_folder:
            self.ensure_source_mapping(valid_files)
        if self.source_path or self.source_folder:
            logger.info(f"  ├─ Source threshold: {self.source_threshold:.2f} "
                        f"({'keep-unaligned' if self.keep_unaligned else 'strict-drop'})")
        logger.info(f"  └─ Files to process: {len(valid_files)}/{len(audio_files)} (skipped: {len(self.results['skipped'])})")

        return valid_files

    def get_disk_estimate_gb(self, audio_file):
        duration = self.audio_durations.get(audio_file)
        estimate = self.output_bytes_per_second * duration if duration else 0
        return max(0.5, estimate / 1024 ** 3)

    def get_scratch_estimates_gb(self, audio_files):
        # Native long-book peak: 24k PCM16 + 16k float32 + 24k diarization
        # float32 backing. Whisper.cpp additionally writes 16k PCM16 in TMP.
        # Fixed allowances cover headers/metadata; these remain estimates.
        durations = [self.audio_durations.get(path) or 0 for path in audio_files]
        duration = max(durations, default=0)
        return (0.5 + duration * 208000 / 1024 ** 3,
                0.25 + duration * 32000 / 1024 ** 3)

    def ensure_disk_space(self, audio_file, required_gb, scratch_files=None):
        if self.skip_disk_check:
            logger.warning("Disk-space admission bypassed by explicit --skip-disk-check "
                           "for %s; available capacity is not verified", audio_file)
            return True
        directory = os.path.dirname(os.path.abspath(get_output_name(audio_file)))
        try:
            files = [audio_file] if scratch_files is None else scratch_files
            for path in files:
                if path not in self.audio_durations:
                    self.audio_durations[path] = get_audio_duration_seconds(path)
                if not self.audio_durations[path]:
                    logger.warning("Scratch duration unavailable for %s; using only the "
                                   "fixed allowance, which may underestimate long audio", path)
            dataset_gb, temp_gb = self.get_scratch_estimates_gb(files)
            logger.info("Scratch reserve is conservative: rechecks retain the full "
                        "estimate even when some scratch files already occupy disk.")
            budgets = {}
            components = ((directory, "remaining ZIP output", required_gb),
                          (os.path.join(os.getcwd(), "dataset_temp"), "dataset audio scratch", dataset_gb),
                          (tempfile.gettempdir(), "Whisper.cpp temporary audio", temp_gb))
            for path, label, amount in components:
                probe = get_disk_probe_path(path)
                device = os.stat(probe).st_dev
                budget = budgets.setdefault(device, {"path": probe, "gb": 0, "parts": []})
                budget["gb"] += amount
                budget["parts"].append(f"{label}: {amount:.3f} GiB at {os.path.abspath(path)}")
            for budget in budgets.values():
                logger.info("Disk estimate components: %s", "; ".join(budget["parts"]))
                if not check_disk_space(budget["path"], required_gb_per_file=budget["gb"], num_files=1):
                    directory = budget["path"]
                    break
            else:
                return True
        except OSError as error:
            logger.error("Cannot probe scratch/output filesystem: %s", error)
        self.disk_refused = True
        self.results["failed"].append({
            "file": audio_file, "reason": f"Disk space admission refused at {directory}"})
        return False

    def ensure_source_mapping(self, audio_files):
        """Display and admit a complete mapping before any processing."""
        candidates = get_source_candidates(self.source_folder)
        reports = {audio: get_source_match_report(audio, self.source_folder,
                    source_candidates=candidates) for audio in audio_files}
        for audio, report in reports.items():
            logger.info(f"Source mapping: {Path(audio).name} -> {report['source'] or 'ASR-only'} "
                        f"(filename score {report['score']:.3f}, {report['method']})")
            for alternative in report['alternatives']:
                logger.info(f"  Alternative: {alternative['source']} "
                            f"(filename score {alternative['score']:.3f})")
        if reports and sys.stdin.isatty():
            try:
                accepted = input("Confirm this entire source mapping (including alternatives/ASR-only)? [yes/no] ")
            except EOFError:
                accepted = ""
            if accepted.strip().lower() != 'yes':
                raise ValueError("Source mapping not confirmed; no processing started")
        else:
            ambiguous = [audio for audio, report in reports.items() if report['ambiguous']]
            if ambiguous:
                raise ValueError("Ambiguous source matches: " + ', '.join(ambiguous) + "; use explicit --source")
            missing = [audio for audio, report in reports.items() if report['source'] is None]
            if missing and not self.allow_no_source:
                raise ValueError("No source match: " + ', '.join(missing) + "; explicitly use --allow-no-source for ASR-only")
        self.source_matches.update({audio: report['source'] for audio, report in reports.items()})

    def process_file(self, audio_file, file_index, total_files):
        """Process a single audio file with real-time output streaming."""
        if not self.skip_disk_check and audio_file not in self.audio_durations:
            self.audio_durations[audio_file] = get_audio_duration_seconds(audio_file)
        if not self.ensure_disk_space(audio_file, self.get_disk_estimate_gb(audio_file)):
            return
        file_size = os.path.getsize(audio_file) / (1024 * 1024)

        logger.info("=" * 70)
        logger.info(f"▶ Processing [{file_index}/{total_files}] {Path(audio_file).name}")
        logger.info(f"  ├─ Size: {file_size:.1f} MB")
        logger.info(f"  ├─ Model: {Path(self.model_path).name}")
        logger.info(f"  ├─ Chunk size: {self.chunk_size}s")

        # Show overall batch ETA based on completed files
        if file_index > 1 and len(self.results["succeeded"]) > 0:
            avg_time = self.total_time / len(self.results["succeeded"])
            remaining_files = total_files - file_index + 1
            eta_secs = avg_time * remaining_files
            logger.info(f"  └─ Batch ETA: ~{format_duration(eta_secs)} ({remaining_files} files remaining)")
        else:
            logger.info(f"  └─ Batch ETA: calculating after first file completes...")
        logger.info("=" * 70)

        output_name = get_output_name(audio_file)
        previous_volumes = get_volume_state(output_name)
        source_identity = get_source_identity(audio_file)
        cmd = [
            sys.executable,
            "-u",  # Unbuffered output for real-time streaming
            str(Path(__file__).resolve().with_name("alexandria_preparer_rocm_compatible.py")),
            "--audio", audio_file,
            "--model", self.model_path,
            "--chunk-size", str(self.chunk_size),
            "--lang", self.language,
            "--output", output_name,
        ]
        if self.fallback_model:
            cmd.extend(["--fallback-model", self.fallback_model])
        # Pass --resume unless --force was set; the preparer's source-marker check
        # ensures we won't accidentally resume into a different file's partial work.
        if not self.force:
            cmd.append("--resume")

        # ── Source-guided mode: per-file source lookup ────────────────────────
        # `--source` takes precedence (single source applied to every file).
        # Otherwise `--source-folder` looks up a sibling file by basename. If
        # neither is set, or no match found, the preparer runs in legacy mode.
        matched_source = None
        if self.source_path:
            matched_source = self.source_path
        elif self.source_folder:
            matched_source = self.source_matches.get(audio_file)
            if audio_file not in self.source_matches:
                self.ensure_source_mapping([audio_file])
                matched_source = self.source_matches[audio_file]
            if matched_source is None:
                logger.warning(
                    f"  ⚠ No source match in {self.source_folder} for "
                    f"{Path(audio_file).stem!r} — running in legacy ASR-only mode"
                )
        if matched_source:
            cmd.extend(["--source", matched_source,
                        "--source-threshold", str(self.source_threshold)])
            if self.keep_unaligned:
                cmd.append("--keep-unaligned")
            logger.info(f"  ├─ Source-guided: {Path(matched_source).name} "
                        f"(threshold {self.source_threshold:.2f}, "
                        f"{'keep-unaligned' if self.keep_unaligned else 'strict-drop'})")

        start_time = time.monotonic()
        logger.info(f"Starting subprocess at {datetime.now().strftime('%H:%M:%S')}...")
        logger.info("─" * 70 + " [subprocess output begins]")

        process = None
        last_stderr_lines = []
        try:
            # A failed re-run must not leave an old success marker authorizing
            # whatever subset of volumes the child may overwrite.
            marker = output_name + ".complete.json"
            if os.path.exists(marker):
                os.unlink(marker)
            # Use Popen for real-time output streaming
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout for ordering
                text=True,
                bufsize=1,  # Line-buffered
            )

            # Stream output line by line in real-time. `for line in process.stdout`
            # blocks on readline, so an inline deadline check can't fire while the
            # child hangs silently (no output). Use a watchdog thread that kills
            # the process at the deadline regardless of whether it's emitting.
            timeout_secs = 3600 * 24  # 24 hour hard cap
            deadline = time.monotonic() + timeout_secs
            timed_out = threading.Event()

            def _watchdog():
                while process.poll() is None:
                    if time.monotonic() > deadline:
                        timed_out.set()
                        process.kill()
                        return
                    time.sleep(5)

            watchdog = threading.Thread(target=_watchdog, daemon=True)
            watchdog.start()

            for line in process.stdout:
                line = line.rstrip()
                if line:
                    print(line, flush=True)  # Real-time display
                    if re.search(r"Volume \d+ saved:", line):
                        # The preparer emits this only after atomic ZIP publication.
                        current = get_volume_state(output_name)
                        published_bytes = sum(state[0] for path, state in current.items()
                                              if previous_volumes.get(path) != state)
                        remaining_gb = max(0.5, self.get_disk_estimate_gb(audio_file)
                                           - published_bytes / 1024 ** 3)
                        if not self.ensure_disk_space(audio_file, remaining_gb):
                            process.kill()
                            process.wait()
                            return
                    # Keep last 20 lines for error context
                    last_stderr_lines.append(line)
                    if len(last_stderr_lines) > 20:
                        last_stderr_lines.pop(0)

            process.wait()
            if timed_out.is_set():
                raise subprocess.TimeoutExpired(cmd, timeout_secs)
            returncode = process.returncode

            logger.info("─" * 70 + " [subprocess output ends]")

            elapsed_secs = time.monotonic() - start_time
            time_str = format_duration(elapsed_secs)

            if returncode == 0:
                logger.info(f"✓ SUCCESS: {Path(audio_file).name} processed ({time_str})")

                current_volumes = get_volume_state(output_name)
                new_volumes = [path for path, state in current_volumes.items()
                               if previous_volumes.get(path) != state]
                if (new_volumes and get_source_identity(audio_file) == source_identity and
                        all(is_complete_dataset_zip(path) for path in new_volumes)):
                    save_batch_receipt(output_name + ".complete.json", {
                        "source": source_identity,
                        "volumes": new_volumes,
                    })
                    output_bytes = sum(os.path.getsize(path) for path in new_volumes)
                    duration = self.audio_durations.get(audio_file)
                    if duration:
                        self.output_bytes_per_second = max(
                            self.output_bytes_per_second, output_bytes / duration)
                    output_size = output_bytes / (1024 * 1024)
                    logger.info(f"  ├─ Output: {len(new_volumes)} ZIP volume(s) ({output_size:.1f} MB)")
                    logger.info(f"  └─ Time: {time_str}")

                    self.results["succeeded"].append({
                        "file": audio_file,
                        "output": new_volumes[0],
                        "outputs": new_volumes,
                        "output_size_mb": output_size,
                        "time": time_str,
                        "time_seconds": elapsed_secs
                    })
                else:
                    logger.warning(f"⚠ No complete output ZIP volumes created: {output_name}")
                    self.results["failed"].append({
                        "file": audio_file,
                        "reason": "No complete output ZIP volumes created",
                        "time": time_str
                    })

            else:
                logger.error(f"✗ FAILED: {Path(audio_file).name} (return code: {returncode})")
                if last_stderr_lines:
                    logger.error(f"  Last output lines:")
                    for line in last_stderr_lines[-10:]:
                        logger.error(f"    {line}")

                self.results["failed"].append({
                    "file": audio_file,
                    "return_code": returncode,
                    "time": time_str
                })

            self.total_time += elapsed_secs

        except subprocess.TimeoutExpired:
            logger.error(f"✗ TIMEOUT: {Path(audio_file).name} exceeded 24 hours")
            if process:
                process.kill()
                process.wait()
            self.results["failed"].append({
                "file": audio_file,
                "reason": "Timeout (>24 hours)"
            })
        except KeyboardInterrupt:
            logger.warning(f"⚠ INTERRUPTED: User cancelled processing of {Path(audio_file).name}")
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            self.results["failed"].append({
                "file": audio_file,
                "reason": "User interrupted (KeyboardInterrupt)"
            })
            raise  # Re-raise to stop the batch
        except Exception as e:
            logger.error(f"✗ ERROR: {Path(audio_file).name} - {e}")
            if process:
                process.kill()
                process.wait()  # reap the killed child (mirrors the Timeout handler)
            self.results["failed"].append({
                "file": audio_file,
                "reason": str(e)
            })

    def is_batch_successful(self):
        return not self.results["failed"] and all(
            item.get("reason", "").startswith("Already processed")
            for item in self.results["skipped"])

    def run(self, audio_files):
        """Process all audio files sequentially."""
        self.batch_start_time = time.monotonic()
        logger.info(f"\n▶ Starting batch processing: {len(audio_files)} files")
        log_gpu_stats("batch start")

        # Validate all files first (also skips already-processed)
        try:
            valid_files = self.validate_files(audio_files)
        except ValueError as exc:
            logger.error(str(exc))
            return False

        if not valid_files:
            # Only report success when EVERY skip was the already-processed case.
            # A "File not found" / "Unsupported audio format" skip is a real
            # failure and must return False so a wrapping script doesn't see rc=0
            # having processed nothing.
            all_already = bool(self.results["skipped"]) and self.is_batch_successful()
            if all_already:
                logger.info(f"All {len(self.results['skipped'])} files already processed (use --force to reprocess)")
                self.print_summary()
                return True
            logger.error("No valid files to process (see skipped/failed reasons above)")
            return False

        self.disk_refused = False
        self.audio_durations = {path: get_audio_duration_seconds(path) for path in valid_files}

        # Process each file
        try:
            for idx, audio_file in enumerate(valid_files, 1):
                if self.ensure_disk_space(
                        audio_file, sum(self.get_disk_estimate_gb(path)
                                        for path in valid_files[idx - 1:]),
                        scratch_files=valid_files[idx - 1:]):
                    self.process_file(audio_file, idx, len(valid_files))
                if self.disk_refused:
                    self.results["skipped"].extend({
                        "file": path,
                        "reason": "Not started: batch stopped after disk-space refusal"
                    } for path in valid_files[idx:])
                    break

                # Show overall batch progress after each file
                if len(valid_files) > 1:
                    completed = len(self.results["succeeded"]) + len(self.results["failed"])
                    progress_pct = (completed / len(valid_files)) * 100
                    elapsed = time.monotonic() - self.batch_start_time
                    logger.info(f"\n📊 Batch progress: {completed}/{len(valid_files)} files ({progress_pct:.1f}%) | Total elapsed: {format_duration(elapsed)}")

                # Small pause between files
                if idx < len(valid_files):
                    logger.info(f"⏳ Waiting before next file...\n")
                    log_gpu_stats(f"between files {idx}/{len(valid_files)}")
                    time.sleep(2)
        except KeyboardInterrupt:
            logger.warning("\n⚠ Batch processing interrupted by user")
            logger.info("Completed files retained - rerun batch to resume from interruption point")
            self.results["failed"].append({"file": "<interrupted>", "error": "user interrupted batch"})

        # Summary
        self.print_summary()
        return self.is_batch_successful()

    def print_summary(self):
        """Print processing summary."""
        logger.info("\n" + "=" * 70)
        logger.info("BATCH PROCESSING SUMMARY")
        logger.info("=" * 70)
        log_gpu_stats("batch complete")

        # Overall stats
        total_files = len(self.results["succeeded"]) + len(self.results["failed"]) + len(self.results["skipped"])
        total_time_str = format_duration(self.total_time)

        # Wall-clock time (includes pauses between files)
        wall_time = time.monotonic() - self.batch_start_time if self.batch_start_time else self.total_time
        wall_time_str = format_duration(wall_time)

        logger.info(f"Total files accounted for: {total_files}")
        logger.info(f"Total processing time: {total_time_str}")
        if wall_time > self.total_time + 5:
            logger.info(f"Total wall-clock time: {wall_time_str}")
        logger.info("")

        # Succeeded
        if self.results["succeeded"]:
            logger.info(f"✓ SUCCEEDED: {len(self.results['succeeded'])}")
            total_output_size = 0
            for item in self.results["succeeded"]:
                logger.info(f"  ├─ {Path(item['file']).name}")
                logger.info(f"  │  ├─ Output: {item['output']} ({item['output_size_mb']:.1f} MB)")
                logger.info(f"  │  └─ Time: {item['time']}")
                total_output_size += item['output_size_mb']
            logger.info(f"  └─ Total output size: {total_output_size:.1f} MB\n")
        else:
            logger.info("✓ SUCCEEDED: 0\n")

        # Failed
        if self.results["failed"]:
            logger.warning(f"✗ FAILED: {len(self.results['failed'])}")
            for item in self.results["failed"]:
                logger.warning(f"  ├─ {Path(item['file']).name}")
                if "reason" in item:
                    logger.warning(f"  │  └─ Reason: {item['reason']}")
                else:
                    logger.warning(f"  │  └─ Return code: {item.get('return_code', 'Unknown')}")
            logger.warning("")
        else:
            logger.info("✗ FAILED: 0\n")

        # Skipped
        if self.results["skipped"]:
            logger.info(f"⊘ SKIPPED: {len(self.results['skipped'])}")
            for item in self.results["skipped"]:
                logger.info(f"  ├─ {Path(item['file']).name}")
                logger.info(f"  │  └─ Reason: {item['reason']}")
            logger.info("")

        # Save results to JSON
        results_file = f"batch_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        save_batch_receipt(results_file, {
            "timestamp": datetime.now().isoformat(),
            "total_time_seconds": self.total_time,
            "results": self.results,
            "skip_disk_check": self.skip_disk_check
        })
        logger.info(f"Results saved to: {results_file}")
        logger.info("=" * 70)

def main():
    parser = argparse.ArgumentParser(
        description="Alexandria Batch Processor - Process multiple audiobooks sequentially"
    )

    parser.add_argument(
        "audio_files",
        nargs="*",
        help="Audio files to process (optional if --folder is used)"
    )
    parser.add_argument(
        "--folder",
        metavar="DIR",
        help="Folder to scan for audio files (.wav .mp3 .m4a .flac .ogg); "
             "combined with any individually listed files"
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Primary GGUF model path (recommended: Qwen2.5-14B-Instruct-Q6_K.gguf)"
    )
    parser.add_argument(
        "--fallback-model",
        help="Optional fallback GGUF model if --model fails to load "
             "(e.g., Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf)"
    )
    parser.add_argument(
        "--chunk-size",
        type=float,
        default=10.0,
        help="Target chunk size in seconds (default: 10.0)"
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="Language code (default: en)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess files even if a valid dataset ZIP already exists"
    )

    parser.add_argument(
        "--skip-disk-check", action="store_true",
        help="Explicitly bypass all batch disk-space admission checks; capacity is not verified"
    )

    # ── Source-guided chunking (forwarded to preparer) ────────────────────────
    # When --source-folder is set, the batch processor looks for a matching
    # source file (basename + .epub or .txt) for each audio file and passes
    # it to the preparer via --source. Audio files without a matching source
    # require explicit ASR-only acceptance before processing. Per-file matching
    # is by stem (e.g. audio/Book1.wav → sources/Book1.epub).
    parser.add_argument(
        "--source-folder",
        metavar="DIR",
        help="Folder containing source .epub or .txt files (matched by audio "
             "basename). Each audio file gets --source <matched-file> passed "
             "to the preparer. Unmatched files require --allow-no-source or interactive confirmation."
    )
    parser.add_argument(
        "--source",
        metavar="PATH",
        help="Single source file applied to EVERY audio file in this batch. "
             "Mutually exclusive with --source-folder."
    )
    parser.add_argument(
        "--source-threshold",
        type=float,
        default=0.65,
        metavar="N",
        help="Minimum alignment ratio to keep a chunk when using --source / "
             "--source-folder (default: 0.65). Forwarded to the preparer."
    )
    parser.add_argument(
        "--keep-unaligned",
        action="store_true",
        help="When using --source / --source-folder, keep low-confidence "
             "chunks (use ASR text) instead of dropping them. Forwarded."
    )

    parser.add_argument("--allow-no-source", action="store_true",
                        help="Explicitly allow unmatched --source-folder files to run ASR-only")

    args = parser.parse_args()

    if args.source and args.source_folder:
        parser.error("--source and --source-folder are mutually exclusive")

    audio_files = list(args.audio_files)

    if args.folder:
        folder = Path(args.folder)
        if not folder.is_dir():
            print(f"Error: --folder path is not a directory: {args.folder}", file=sys.stderr)
            sys.exit(1)
        found = sorted(str(p) for p in folder.iterdir()
                       if p.is_file() and p.suffix.lower() in BatchProcessor.SUPPORTED_FORMATS)
        if not found:
            print(f"Error: no supported audio files found in {args.folder}", file=sys.stderr)
            sys.exit(1)
        audio_files.extend(found)

    if not audio_files:
        parser.error("provide at least one audio file or use --folder")

    # Sanity-check source flags before doing anything expensive.
    if args.source and not os.path.isfile(args.source):
        parser.error(f"--source: file does not exist: {args.source}")
    if args.source_folder and not os.path.isdir(args.source_folder):
        parser.error(f"--source-folder: directory does not exist: {args.source_folder}")
    if not math.isfinite(args.chunk_size) or args.chunk_size <= 0:
        parser.error("--chunk-size must be a finite positive number")
    if not math.isfinite(args.source_threshold) or not 0 <= args.source_threshold <= 1:
        parser.error("--source-threshold must be a finite number between 0 and 1")

    processor = BatchProcessor(
        model_path=args.model,
        chunk_size=args.chunk_size,
        language=args.lang,
        force=args.force,
        fallback_model=args.fallback_model,
        source_folder=args.source_folder,
        source_path=args.source,
        source_threshold=args.source_threshold,
        keep_unaligned=args.keep_unaligned,
        allow_no_source=args.allow_no_source,
        skip_disk_check=args.skip_disk_check,
    )

    try:
        gpu_lease = acquire_gpu_lock()
    except (OSError, RuntimeError) as error:
        parser.error(str(error))
    try:
        success = processor.run(audio_files)
    finally:
        release_gpu_lock(gpu_lease)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
