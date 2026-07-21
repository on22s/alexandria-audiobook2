#!/usr/bin/env python3
"""Rank the Vulkan-vs-ROCm runtime A/B.

Walks ab_test_runtime/<backend>/<model>/rep<N>/, reading each run's three-pass
manifest (per-pass elapsed + resolution counts + status) and its run.log
(per-call `completion=... took ...s` lines) to derive completion tokens/sec.
Prints a per-run table, per-(backend,model) means, and a head-to-head ranking of
the two backends on speed and quality.

Usage:  env/bin/python ../ab_test_runtime/analyze_runtime_ab.py
        (or pass a root dir as argv[1])
"""
import glob
import json
import os
import re
import statistics
import sys

TOOK_RE = re.compile(r"completion=(\d+)\b.*?took\s+([\d.]+)s")
MANIFEST_GLOB = "*.threepass_manifest.json"


def parse_log_tokens_per_sec(log_path):
    """Sum completion tokens and generation seconds across every LLM call in a
    run log -> aggregate completion tokens/sec (backend speed, retry-agnostic)."""
    if not os.path.exists(log_path):
        return None
    total_tok = total_s = 0
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = TOOK_RE.search(line)
            if m:
                total_tok += int(m.group(1))
                total_s += float(m.group(2))
    if total_s <= 0:
        return None
    return {"completion_tokens": total_tok, "gen_seconds": round(total_s, 1),
            "tokens_per_sec": round(total_tok / total_s, 2)}


def load_run(rep_dir):
    manifests = sorted(glob.glob(os.path.join(rep_dir, MANIFEST_GLOB)))
    manifest = None
    ambiguous_manifest = len(manifests) > 1
    if len(manifests) == 1:
        try:
            manifest = json.load(open(manifests[0], encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
    logs = sorted(glob.glob(os.path.join(rep_dir, "*.log")))
    speed = parse_log_tokens_per_sec(logs[0]) if len(logs) == 1 else None
    passes = (manifest or {}).get("passes", {})
    valid_passes = (isinstance(passes, dict) and passes
                    and all(isinstance(p, dict) for p in passes.values()))
    elapsed = ([p.get("elapsed_s") for p in passes.values()]
               if valid_passes else [])
    wall = (sum(elapsed) if elapsed
            and all(isinstance(v, (int, float)) for v in elapsed) else None)
    if (manifest or {}).get("legacy_resume"):
        wall = None
    counts = (manifest or {}).get("counts", {})
    if not isinstance(counts, dict):
        counts = {}
    return {
        "status": ("ambiguous_manifest" if ambiguous_manifest
                   else (manifest or {}).get("status", "no_manifest")),
        "failed_pass": (manifest or {}).get("failed_pass"),
        "wall_s": round(wall, 1) if wall is not None else None,
        "near_miss": counts.get("near_miss_accepted"),
        "rescued": counts.get("context_rescued"),
        "recombined": counts.get("split_recombined"),
        "tok_per_sec": speed["tokens_per_sec"] if speed else None,
        "gen_s": speed["gen_seconds"] if speed else None,
    }


def collect(root):
    rows = []
    for backend in sorted(os.listdir(root)):
        bdir = os.path.join(root, backend)
        if not os.path.isdir(bdir):
            continue
        for model in sorted(os.listdir(bdir)):
            mdir = os.path.join(bdir, model)
            if not os.path.isdir(mdir):
                continue
            for rep in sorted(os.listdir(mdir)):
                rdir = os.path.join(mdir, rep)
                if os.path.isdir(rdir):
                    rows.append({"backend": backend, "model": model, "rep": rep,
                                 **load_run(rdir)})
    return rows


def _mean(values):
    nums = [v for v in values if isinstance(v, (int, float))]
    return round(statistics.mean(nums), 2) if nums else None


def _display(value):
    return "-" if value is None else str(value)


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
    rows = collect(root)
    if not rows:
        print(f"No runs found under {root}")
        return

    print(f"\n=== Per-run ({len(rows)} runs) ===")
    hdr = f"{'backend':7} {'model':9} {'rep':5} {'status':10} {'wall_s':>8} {'tok/s':>7} {'nearMiss':>8} {'rescue':>6}"
    print(hdr)
    for r in sorted(rows, key=lambda r: (r["backend"], r["model"], r["rep"])):
        print(f"{r['backend']:7} {r['model'][:9]:9} {r['rep']:5} {r['status'][:10]:10} "
              f"{_display(r['wall_s']):>8} {_display(r['tok_per_sec']):>7} "
              f"{str(r['near_miss'] if r['near_miss'] is not None else '-'):>8} "
              f"{str(r['rescued'] if r['rescued'] is not None else '-'):>6}")

    # Aggregate per (backend, model).
    keys = sorted({(r["backend"], r["model"]) for r in rows})
    agg = {}
    print("\n=== Mean per backend x model ===")
    print(f"{'backend':7} {'model':9} {'complete':9} {'wall_s':>9} {'tok/s':>8} {'rescue':>7}")
    for backend, model in keys:
        grp = [r for r in rows if r["backend"] == backend and r["model"] == model]
        completes = sum(r["status"] == "complete" for r in grp)
        a = {"complete_rate": f"{completes}/{len(grp)}",
             "wall_s": _mean([r["wall_s"] for r in grp]),
             "tok_per_sec": _mean([r["tok_per_sec"] for r in grp]),
             "rescue": _mean([r["rescued"] for r in grp])}
        agg[(backend, model)] = a
        print(f"{backend:7} {model[:9]:9} {a['complete_rate']:9} "
              f"{_display(a['wall_s']):>9} {_display(a['tok_per_sec']):>8} "
              f"{str(a['rescue'] if a['rescue'] is not None else '-'):>7}")

    # Head-to-head: for each model, compare vulkan vs rocm.
    models = sorted({m for _, m in keys})
    backends = sorted({b for b, _ in keys})
    if set(backends) >= {"vulkan", "rocm"}:
        print("\n=== Head-to-head (vulkan vs rocm) ===")
        for model in models:
            v = agg.get(("vulkan", model)); r = agg.get(("rocm", model))
            if not v or not r:
                continue
            speed = _winner(v["tok_per_sec"], r["tok_per_sec"], higher_better=True)
            wall = _winner(v["wall_s"], r["wall_s"], higher_better=False)
            print(f"\n {model}")
            print(f"   speed (tok/s): vulkan {v['tok_per_sec']}  vs  rocm {r['tok_per_sec']}"
                  f"   -> {speed}")
            print(f"   wall  (s)    : vulkan {v['wall_s']}  vs  rocm {r['wall_s']}"
                  f"   -> {wall}")
            print(f"   complete     : vulkan {v['complete_rate']}  vs  rocm {r['complete_rate']}")
            print(f"   mean rescues : vulkan {v['rescue']}  vs  rocm {r['rescue']}"
                  "   (fewer = cleaner)")


def _winner(v, r, higher_better):
    if v is None or r is None:
        return "insufficient data"
    if v == r:
        return "tie"
    v_wins = (v > r) if higher_better else (v < r)
    pct = abs(v - r) / max(abs(v), abs(r)) * 100
    return f"{'VULKAN' if v_wins else 'ROCM'} by {pct:.1f}%"


if __name__ == "__main__":
    main()
