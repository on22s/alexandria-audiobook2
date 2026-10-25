#!/usr/bin/env python3
"""How often does a book's rendered audio pause at its commas? A rate per voice.

Samples rendered chunks whose spoken text has a mid-line mark, measures the
pause at each mark with app/comma_pauses.py, and writes a Markdown report to
the book's reports/ folder, where the Editor's Reports list shows it. CPU only
(whisper.cpp Small.en for word timing, 5-8 s a chunk), English only. Long
chunks are refused by word timing more often (6 of 30 in one novel); refused
chunks are counted, never treated as pauses.

It reports rates per speaker's voice and for the book, never a verdict on one
chunk: the measure cannot judge a single line reliably (see app/comma_pauses.py).
The report quotes no book text, and reports/ is not committed.

    python tools/book_comma_report.py [--data-dir DIR] [--sample 200 | --all]
"""
import argparse
from collections import defaultdict
from datetime import datetime
import json
import os
from pathlib import Path
import random
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from comma_pauses import SKIPPED_PAUSE_S, get_clip_comma_pauses, get_summary, has_mid_line_mark

SAMPLE_SEED = 20261025


def get_eligible_chunks(manager):
    """-> [(row, audio path, spoken text)] for rendered chunks with a mid-line mark."""
    from speech_text import get_speech_preparation
    from utils import is_path_inside
    eligible = []
    for row in manager.load_chunks():
        if row.get("status") != "done" or not row.get("audio_path"):
            continue
        try:
            path = Path(manager.get_chunk_audio_path(row["audio_path"]))
        except ValueError:
            continue
        if not path.is_file() or not is_path_inside(str(path), manager.voicelines_dir):
            continue
        spoken = get_speech_preparation(row.get("text", ""), row.get("instruct"))["text"]
        if has_mid_line_mark(spoken):
            eligible.append((row, path, spoken))
    return eligible


def get_voice_label(speaker, voice_config):
    voice = voice_config.get(speaker) if isinstance(voice_config, dict) else None
    if not isinstance(voice, dict):
        return "no voice assigned"
    kind = voice.get("type", "custom")
    name = voice.get("adapter_id") or voice.get("voice") or voice.get("name") or ""
    return f"{kind}: {name}".rstrip(": ")


def get_report_markdown(book, by_speaker, voices, overall, sampled, eligible, data_dir):
    def row(label, summary):
        if not summary.get("marks"):
            return f"| {label} | {summary['clips']} | {summary['clips_refused']} | 0 | – | – |"
        low, high = summary["share_skipped_ci95"]
        return (f"| {label} | {summary['clips']} | {summary['clips_refused']} | {summary['marks']} | "
                f"{summary['median_pause_s']:.2f} s | {summary['share_skipped']:.0%} ({low:.0%}–{high:.0%}) |")
    lines = [
        f"# Comma pauses: {book}",
        "",
        f"Measured {sampled} of {eligible} rendered chunks that have a mid-line comma, semicolon, colon, "
        f"dash or inner sentence break. Generated {datetime.now():%Y-%m-%d %H:%M}.",
        "",
        "| Voice | Chunks | Timing refused | Marks | Median pause | Pause under "
        f"{SKIPPED_PAUSE_S:.2f} s (95% CI) |",
        "|---|---|---|---|---|---|",
        row("**Whole book**", overall),
    ]
    for speaker in sorted(by_speaker, key=lambda name: -len(by_speaker[name])):
        lines.append(row(f"{speaker} ({voices[speaker]})", get_summary(by_speaker[speaker])))
    lines += [
        "",
        "## How to read this",
        "",
        "- A pause is the longest stretch at least 30 dB quieter than the clip's loud level within 0.3 s of "
        "the mark. Typical reading pauses about 0.15–0.3 s at a comma.",
        "- Compare voices with each other within one book. There is no fixed target: a stock voice left "
        "37-42% of commas under 0.10 s on public-domain text, and the rate moves with the text.",
        "- Rates only. A single chunk can skip a comma for good reason, and the measure cannot tell one "
        "chunk's skip from noise, so no chunk is flagged.",
        "- English only. A chunk whose word timing was refused is counted as refused, never as a pause.",
        "",
        f"Data folder: `{data_dir}`",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    from project import ProjectManager
    from review_report import _save_report_text  # the reports folder's own atomic writer
    from whisper_cpp_runtime import get_whisper_cpp_runtime
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.environ.get("ALEXANDRIA_DATA_DIR") or str(ROOT))
    size = parser.add_mutually_exclusive_group()
    size.add_argument("--sample", type=int, default=200, help="chunks to measure (default 200)")
    size.add_argument("--all", action="store_true", help="measure every eligible chunk")
    parser.add_argument("--asr-root", default=str(ROOT),
                        help="checkout holding whisper.cpp/ and models/ (a worktree has neither)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).resolve()
    manager = ProjectManager(str(data_dir))
    eligible = get_eligible_chunks(manager)
    if not eligible:
        print("No rendered chunks with a mid-line mark in", data_dir)
        return 1
    chosen = eligible if args.all else random.Random(SAMPLE_SEED).sample(eligible, min(args.sample, len(eligible)))
    runtime = get_whisper_cpp_runtime(args.asr_root)
    if not runtime["available"]:
        print(f"whisper.cpp Small.en was not found under {args.asr_root}; word timing is required. "
              "Pass --asr-root <main checkout> or set ALEXANDRIA_WHISPER_CPP_BIN and ALEXANDRIA_WHISPER_CPP_MODEL.")
        return 1
    runtime = {**runtime, "binary": str(Path(runtime["binary"]).resolve()), "model": str(Path(runtime["model"]).resolve())}
    try:
        voice_config = json.loads((data_dir / "voice_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        voice_config = {}
    by_speaker, voices, results = defaultdict(list), {}, []
    for count, (row, path, spoken) in enumerate(chosen, 1):
        speaker = str(row.get("speaker") or "NARRATOR")
        voices[speaker] = get_voice_label(speaker, voice_config)
        result = get_clip_comma_pauses(path, spoken, runtime)
        by_speaker[speaker].append(result); results.append(result)
        if count % 20 == 0:
            print(f"{count}/{len(chosen)} measured", flush=True)
    try:
        state = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    book = state.get("active_book_id") or Path(state.get("input_file_path") or "book").stem
    report = get_report_markdown(book, by_speaker, voices, get_summary(results), len(chosen), len(eligible), data_dir)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", book)[:60] or "book"
    out = data_dir / "reports" / f"comma_pauses_{slug}_{datetime.now():%Y%m%d-%H%M}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    _save_report_text(str(out), report)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
