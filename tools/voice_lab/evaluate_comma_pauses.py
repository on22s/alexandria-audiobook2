#!/usr/bin/env python3
"""Does a voice pause at commas? Render fixed public-domain lines, measure each comma.

Renders the comma-bearing lines of app/fixtures/alice_pd_listening.json with one
voice (a LoRA adapter, or a stock custom voice as the comparison) and measures
the pause at every mid-line mark with app/comma_pauses.py. Rates only: the
measure cannot judge a single line (see that module). Writes its own JSON and
never touches evaluate_lora.py's evaluation.json, candidates or their ranking.

GPU work: run through the queue, e.g.
    ./gpu_job.sh comma_eval app/env/bin/python tools/voice_lab/evaluate_comma_pauses.py \\
        --config app/config.json --adapter lora_models/<name>     # -> lora_models/.comma_evaluations/<name>.json
    ./gpu_job.sh comma_eval_stock app/env/bin/python tools/voice_lab/evaluate_comma_pauses.py \\
        --config app/config.json --stock Ryan --out lora_models/.comma_stock_Ryan.json
Fix testing (same lines, one change): --variant "instruct:pause briefly at commas"
or --variant split (each clause rendered alone, joined with aa2's pause rule).
Compare two runs on CPU, no rendering:
    python tools/voice_lab/evaluate_comma_pauses.py --compare a.json b.json
"""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from comma_pauses import SKIPPED_PAUSE_S, get_clip_comma_pauses, get_summary, has_mid_line_mark
from utils import atomic_json_write

LINES_PATH = APP_DIR / "fixtures" / "alice_pd_listening.json"
EVALUATION_SEED = 20261025
VERSION = 1
CLAUSE_BREAK = re.compile(r"(?<=[,;:])\s+")


def get_comma_lines(path=LINES_PATH):
    """-> the fixture's lines with a mark the measure counts (and so a pause to measure)."""
    with open(path, encoding="utf-8") as handle:
        lines = json.load(handle)["lines"]
    return [line for line in lines if has_mid_line_mark(line["text"])]


def get_clauses(text):
    """-> the text split after each , ; : that is followed by whitespace."""
    return [piece for piece in CLAUSE_BREAK.split(text) if piece]


def parse_variant(value):
    """-> ("none"|"instruct"|"split", hint)."""
    if value in (None, "", "none"):
        return "none", ""
    if value == "split":
        return "split", ""
    if value.startswith("instruct:") and value[len("instruct:"):].strip():
        return "instruct", value[len("instruct:"):].strip()
    raise argparse.ArgumentTypeError('variant must be "none", "split" or "instruct:<hint>"')


def get_pairs(first, second):
    """-> [(pause in first, pause in second)] for the same mark of the same line.

    Lines must match by id and spoken text; a line is paired only when both runs
    timed it and found the same number of marks, so marks line up one to one.
    """
    first_lines = {line["id"]: line for line in first["lines"]}
    pairs, unmatched = [], 0
    for line in second["lines"]:
        other = first_lines.get(line["id"])
        if other is None or other["prepared_text"] != line["prepared_text"]:
            raise ValueError(f"runs do not share line {line['id']}; compare runs of the same lines")
        a, b = other["result"].get("pauses"), line["result"].get("pauses")
        if a is None or b is None or len(a) != len(b):
            unmatched += 1
            continue
        pairs.extend((x["pause_s"], y["pause_s"]) for x, y in zip(a, b))
    return pairs, unmatched


def compare(first, second):
    import numpy as np
    from scipy.stats import wilcoxon
    pairs, unmatched = get_pairs(first, second)
    if not pairs:
        raise ValueError("no comparable marks between these runs")
    a = np.array([p[0] for p in pairs]); b = np.array([p[1] for p in pairs])
    report = {"marks_paired": len(pairs), "lines_unpaired": unmatched,
              "median_pause_s": [round(float(np.median(a)), 3), round(float(np.median(b)), 3)],
              "share_skipped": [round(float((a < SKIPPED_PAUSE_S).mean()), 3),
                                round(float((b < SKIPPED_PAUSE_S).mean()), 3)]}
    if np.any(a != b):
        report["wilcoxon_p"] = round(float(wilcoxon(a, b).pvalue), 4)
    return report


def render_lines(args, lines, work):
    import numpy as np
    import soundfile as sf
    from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot
    from config_settings import load_app_config
    from evaluate_lora import apply_evaluation_seed
    from lora_evidence import get_tts_model_settings
    from speech_text import get_speech_preparation
    from tts import SAME_SPEAKER_PAUSE_MS, TTSEngine, get_pause_duration_ms
    from whisper_cpp_runtime import get_whisper_cpp_runtime

    config = load_app_config(args.config)
    engine = TTSEngine(config)
    runtime = get_whisper_cpp_runtime(args.asr_root)
    if not runtime["available"]:
        raise SystemExit(f"whisper.cpp Small.en was not found under {args.asr_root}; pass --asr-root <main checkout>")
    runtime = {**runtime, "binary": str(Path(runtime["binary"]).resolve()), "model": str(Path(runtime["model"]).resolve())}
    kind, hint = args.variant
    adapter = Path(args.adapter).resolve() if args.adapter else None

    def render(snapshot, generation, text, instruct, seed, path):
        apply_evaluation_seed(seed)
        if adapter:
            voice = {"type": "lora", "adapter_id": adapter.name, "adapter_path": snapshot,
                     "adapter_generation_sha256": generation, "seed": seed}
        else:
            voice = {"type": "custom", "voice": args.stock, "seed": str(seed)}
        if engine.generate_voice(text=text, instruct_text=instruct, speaker="_comma_probe_",
                                 voice_config={"_comma_probe_": voice}, output_path=str(path)) is False:
            raise RuntimeError(f"generation failed: {path.name}")

    def run(snapshot, generation):
        records = []
        for index, line in enumerate(lines):
            seed = EVALUATION_SEED + index
            instruct = f"{line.get('instruct', '')} {hint}".strip() if kind == "instruct" else line.get("instruct", "")
            path = work / f"{line['id']}.wav"
            if kind == "split":
                pieces = []
                for part, clause in enumerate(get_clauses(line["text"])):
                    piece = work / f"{line['id']}__{part}.wav"
                    render(snapshot, generation, clause, instruct, seed, piece)
                    audio, rate = sf.read(piece, dtype="float32")
                    pieces.append(audio if audio.ndim == 1 else audio.mean(axis=1))
                # aa2's own rule between same-speaker chunks, as Play Sequence and export apply it.
                gap = get_pause_duration_ms({"speaker": "_comma_probe_"}, {"speaker": "_comma_probe_"},
                                            same_speaker_pause_ms=SAME_SPEAKER_PAUSE_MS)
                silence = np.zeros(int(rate * gap / 1000), dtype="float32")
                joined = [pieces[0]]
                for audio in pieces[1:]:
                    joined += [silence, audio]
                sf.write(path, np.concatenate(joined), rate)
            else:
                render(snapshot, generation, line["text"], instruct, seed, path)
            prepared = get_speech_preparation(line["text"], instruct)["text"]
            records.append({"id": line["id"], "text": line["text"], "prepared_text": prepared,
                            "instruct": instruct, "seed": seed, "audio_file": path.name,
                            "result": get_clip_comma_pauses(path, prepared, runtime)})
            print(f"{line['id']}: {records[-1]['result']}", flush=True)
        return records

    if adapter:
        with ensure_adapter_generation_snapshot(str(adapter), validate=True) as (snapshot, generation):
            records = run(snapshot, generation)
        voice = {"adapter": adapter.name, "adapter_generation_sha256": generation}
    else:
        records = run(None, None)
        voice = {"stock": args.stock}
    return {"version": VERSION, "voice": voice, "variant": {"kind": kind, "hint": hint},
            "seed": EVALUATION_SEED, "synthesis_settings": get_tts_model_settings(config),
            "lines_source": str(LINES_PATH.relative_to(ROOT)), "evaluated_at": time.time(),
            "lines": records, "summary": get_summary([record["result"] for record in records])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    voice = parser.add_mutually_exclusive_group()
    voice.add_argument("--adapter", help="lora_models/<name> directory")
    voice.add_argument("--stock", nargs="?", const="Ryan", help="stock custom voice (default Ryan)")
    parser.add_argument("--config", help="app config.json used to build the engine")
    parser.add_argument("--variant", type=parse_variant, default=("none", ""))
    parser.add_argument("--out", help="result JSON (default lora_models/.comma_evaluations/<adapter>[__variant].json)")
    parser.add_argument("--compare", nargs=2, metavar=("FIRST", "SECOND"))
    parser.add_argument("--asr-root", default=str(ROOT),
                        help="checkout holding whisper.cpp/ and models/ (a worktree has neither)")
    args = parser.parse_args(argv)
    if args.compare:
        runs = []
        for path in args.compare:
            with open(path, encoding="utf-8") as handle:
                runs.append(json.load(handle))
        print(json.dumps(compare(*runs), indent=1))
        return 0
    if not (args.adapter or args.stock) or not args.config:
        parser.error("rendering needs --config and one of --adapter or --stock")
    if args.stock and not args.out:
        parser.error("--stock needs --out")
    suffix = "" if args.variant[0] == "none" else f"__{args.variant[0]}"
    # Beside the adapters, never inside one: an adapter folder is a validated checkpoint.
    out = Path(args.out or Path(args.adapter).resolve().parent / ".comma_evaluations"
               / f"{Path(args.adapter).name}{suffix}.json").resolve()
    work = out.parent / (out.stem + "_wavs")
    work.mkdir(parents=True, exist_ok=True)
    result = render_lines(args, get_comma_lines(), work)
    atomic_json_write(result, str(out))
    print(json.dumps(result["summary"]), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
