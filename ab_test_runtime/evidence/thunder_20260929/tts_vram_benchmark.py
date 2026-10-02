#!/usr/bin/env python3
"""
TTS VRAM benchmark — sweeps sub_batch_max_items and compile_codec to produce
tuning data for the auto-configure tier table in index.html.

Usage (from the app/ directory):
    python tts_vram_benchmark.py                   # default sweep, no compile test
    python tts_vram_benchmark.py --compile         # also benchmark compile_codec=True
    python tts_vram_benchmark.py --sizes 4 8 16    # custom batch size sweep

Outputs:
    benchmark_results.json  — raw per-run results
    benchmark_summary.txt   — tier table ready for copy-paste into _computeAutoSettings
"""

import argparse
from config_settings import load_app_config
from utils import atomic_json_write
import os
import random
import sys
import tempfile
import time

APP_DIR  = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

sys.path.insert(0, APP_DIR)

# ---------------------------------------------------------------------------
# Synthetic chunk generation
# ---------------------------------------------------------------------------

_WORDS = (
    "the quick brown fox jumped over the lazy dog she said quietly into "
    "darkness nothing remained he whispered silence fell across the room "
    "morning light filtered through dusty curtains a voice called out from "
    "somewhere deep within the ancient halls of the crumbling manor house"
).split()

def _make_text(target_chars):
    words = []
    while sum(len(w) + 1 for w in words) < target_chars:
        words.append(random.choice(_WORDS))
    return " ".join(words)[:target_chars]

def make_chunks(n, short_ratio=0.3, long_ratio=0.2):
    """Generate n synthetic chunks with mixed lengths (short/medium/long)."""
    chunks = []
    for i in range(n):
        r = random.random()
        if r < short_ratio:
            chars = random.randint(40, 100)
        elif r < short_ratio + long_ratio:
            chars = random.randint(300, 500)
        else:
            chars = random.randint(120, 280)
        chunks.append({
            "index": i,
            "text": _make_text(chars),
            "instruct": "Neutral, even narration.",
            "speaker": "NARRATOR",
        })
    return chunks

# ---------------------------------------------------------------------------
# VRAM helpers
# ---------------------------------------------------------------------------

def vram_state():
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        alloc = torch.cuda.memory_allocated() / 1e9
        free, total = torch.cuda.mem_get_info()
        return {"allocated_gb": round(alloc, 2),
                "free_gb": round(free / 1e9, 2),
                "total_gb": round(total / 1e9, 1)}
    except Exception as e:
        print(f"Warning: vram_state probe failed: {e}")
        return None

def gpu_name():
    try:
        import torch
        return torch.cuda.get_device_name(0)
    except Exception as e:
        print(f"Warning: gpu_name probe failed: {e}")
        return "unknown"


def get_benchmark_engine_config(tts_config):
    """Return the nested shape TTSEngine expects without mutating app config."""
    benchmark_tts = dict(tts_config)
    benchmark_tts.update({
        "mode": "local",
        "compile_codec": False,
        "sub_batch_enabled": True,
    })
    return {"tts": benchmark_tts}

# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_sweep(engine, voice_config, batch_sizes, output_dir, n_chunks_per_run=32,
              voice_type="custom"):
    """Run _local_batch_custom for each batch size, capture timing and VRAM."""
    results = []

    for max_items in batch_sizes:
        print(f"\n{'='*60}")
        print(f"  Sweep: sub_batch_max_items={max_items}, n_chunks={n_chunks_per_run}")
        print(f"{'='*60}")

        engine.set_sub_batch_size(max_items)
        random.seed(42)
        chunks = make_chunks(n_chunks_per_run)

        import torch
        cuda_ok = torch.cuda.is_available()
        if cuda_ok:
            torch.cuda.reset_peak_memory_stats()
        t0 = time.time()
        if voice_type == "clone":
            batch_results = engine.run_clone_benchmark_batch(
                chunks, voice_config, output_dir, batch_seed=42)
        else:
            batch_results = engine.run_benchmark_batch(
                chunks, voice_config, output_dir, batch_seed=42)
        elapsed = time.time() - t0
        peak_gb = batch_results.get("peak_vram_gb", 0.0)

        n_done = len(batch_results["completed"])
        n_fail = len(batch_results["failed"])

        # Estimate total audio duration from output files
        total_audio = 0.0
        try:
            import soundfile as sf
            for idx in batch_results["completed"]:
                p = os.path.join(output_dir, f"temp_batch_{idx}.wav")
                if os.path.exists(p):
                    info = sf.info(p)
                    total_audio += info.duration
        except Exception as e:
            print(f"Warning: duration estimation failed: {e}")

        rtf = total_audio / elapsed if elapsed > 0 and total_audio > 0 else None

        entry = {
            "sub_batch_max_items": max_items,
            "n_chunks": n_chunks_per_run,
            "completed": n_done,
            "failed": n_fail,
            "elapsed_s": round(elapsed, 1),
            "total_audio_s": round(total_audio, 1),
            "rtf": round(rtf, 2) if rtf else None,
            "peak_vram_gb": round(peak_gb, 2),
            "vram_state_after": vram_state(),
        }
        results.append(entry)
        rtf_str = f"{rtf:.2f}" if rtf else "N/A"
        print(f"  → done={n_done} fail={n_fail} elapsed={elapsed:.1f}s "
              f"audio={total_audio:.1f}s RTF={rtf_str} peak_VRAM={peak_gb:.2f}GB")

        # Clean up output files between runs
        for idx in batch_results["completed"] + [f for f, _ in batch_results["failed"]]:
            p = os.path.join(output_dir, f"temp_batch_{idx}.wav")
            try:
                os.remove(p)
            except FileNotFoundError:
                pass

        engine._clear_gpu_cache()

    return results

# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def print_summary(pre_results, post_results, model_vram_gb, total_gb):
    print("\n" + "="*70)
    print("BENCHMARK SUMMARY")
    print(f"GPU: {gpu_name()}  |  Total VRAM: {total_gb:.1f} GB")
    print(f"Model footprint: ~{model_vram_gb:.2f} GB")
    print(f"Headroom for batching: ~{total_gb - model_vram_gb:.1f} GB")
    print("="*70)

    headers = ["max_items", "peak_GB", "RTF (x RT)", "fail"]
    if post_results:
        headers += ["peak_GB (compiled)", "RTF (compiled)"]

    header_str = "  ".join(f"{h:>18}" for h in headers)
    print(header_str)
    print("-" * len(header_str))

    for i, r in enumerate(pre_results):
        row = [
            str(r["sub_batch_max_items"]),
            f"{r['peak_vram_gb']:.2f}",
            f"{r['rtf']:.2f}" if r["rtf"] else "N/A",
            str(r["failed"]),
        ]
        if post_results:
            pr = post_results[i] if i < len(post_results) else {}
            row += [
                f"{pr.get('peak_vram_gb', 0):.2f}" if pr else "N/A",
                f"{pr['rtf']:.2f}" if pr and pr.get("rtf") else "N/A",
            ]
        print("  ".join(f"{v:>18}" for v in row))

    print("\nTier table recommendation (paste into _computeAutoSettings):")
    print("-"*70)
    # Find max_items that fit within 80% of free headroom without OOM
    headroom = total_gb - model_vram_gb
    for r in pre_results:
        fits = r["peak_vram_gb"] <= headroom * 0.85
        status = "OK " if fits else "OOM-RISK"
        rtf_str = f"{r['rtf']:.2f}x RT" if r["rtf"] else "   N/A  "
        print(f"  max_items={r['sub_batch_max_items']:>3}  "
              f"peak={r['peak_vram_gb']:.2f}GB  RTF={rtf_str}  [{status}]")


def save_benchmark_results(output, out_path):
    """Atomically save results, including to a new nested directory."""
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    atomic_json_write(output, out_path)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="TTS VRAM benchmark")
    parser.add_argument("--sizes", nargs="+", type=int,
                        default=[4, 8, 12, 16, 24],
                        help="sub_batch_max_items values to sweep")
    parser.add_argument("--compile", action="store_true",
                        help="also benchmark with compile_codec=True")
    parser.add_argument("--chunks", type=int, default=32,
                        help="chunks per sweep run (default 32)")
    parser.add_argument("--out", default="benchmark_results.json",
                        help="output JSON path")
    parser.add_argument("--voice-type", choices=("custom", "clone"),
                        default="custom")
    parser.add_argument("--clone-ref-audio")
    parser.add_argument("--clone-ref-text")
    args = parser.parse_args()
    if args.voice_type == "clone" and args.compile:
        parser.error("--compile is not supported for clone sweeps")

    # Load config
    tts_cfg = load_app_config(CONFIG_PATH).get("tts", {})

    # Force local mode and disable compile_codec for the baseline without
    # changing the loaded application config dictionary.
    engine_config = get_benchmark_engine_config(tts_cfg)
    engine_config["tts"]["sub_batch_max_items"] = args.sizes[0]

    from tts import TTSEngine
    print("Initializing TTSEngine (local mode, compile_codec=False)...")
    snap_pre = vram_state()
    engine = TTSEngine(engine_config)

    # Force model load and capture footprint
    print("\nLoading model (this will show VRAM footprint)...")
    if args.voice_type == "clone":
        if not args.clone_ref_audio or not args.clone_ref_text:
            parser.error("clone mode requires --clone-ref-audio and --clone-ref-text")
        _ = engine._init_local_clone()
    else:
        _ = engine._init_local_custom()
    snap_post = vram_state()
    model_vram_gb = (snap_post["allocated_gb"] - snap_pre["allocated_gb"]) if (snap_pre and snap_post) else 0
    total_gb = snap_post["total_gb"] if snap_post else 0
    print(f"\nModel VRAM footprint: {model_vram_gb:.2f} GB  (total GPU: {total_gb:.1f} GB)")
    if args.voice_type == "custom":
        print("Warming model before timed sweeps...")
        engine.ensure_custom_warmup(engine._local_custom_model)
        voice_config = {"NARRATOR": {"type": "custom", "voice": "Ryan"}}
    else:
        voice_config = {"NARRATOR": {"type": "clone", "seed": 42,
                        "ref_audio": args.clone_ref_audio,
                        "ref_text": args.clone_ref_text}}

    with tempfile.TemporaryDirectory() as output_dir:
        print(f"\n--- Baseline sweep (compile_codec=False) ---")
        pre_results = run_sweep(engine, voice_config, args.sizes, output_dir,
                                args.chunks, args.voice_type)

        post_results = []
        if args.compile:
            print(f"\n--- Compiling codec ---")
            engine.enable_codec_compilation()
            print(f"\n--- Post-compile sweep ---")
            post_results = run_sweep(engine, voice_config, args.sizes, output_dir,
                                     args.chunks, args.voice_type)

    print_summary(pre_results, post_results, model_vram_gb, total_gb)

    # Save raw results
    output = {
        "gpu": gpu_name(),
        "total_vram_gb": total_gb,
        "model_vram_gb": round(model_vram_gb, 2),
        "compile_tested": args.compile,
        "voice_type": args.voice_type,
        "baseline": pre_results,
        "compiled": post_results,
    }
    out_path = os.path.join(APP_DIR, args.out)
    save_benchmark_results(output, out_path)
    print(f"\nRaw results saved to: {out_path}")


if __name__ == "__main__":
    main()
