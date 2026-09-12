"""Goal 4.1: does the +50% LoRA generation cost come from the adapter being
applied UNMERGED?

tts._init_local_lora wraps the talker in PeftModel.from_pretrained and leaves
it there, so every decoder step computes W.x + B.A.x for every targeted module.
peft's merge_and_unload folds B.A into W once; the per-step cost then equals the
stock model's. The engine already reloads the whole base model when the adapter
changes, so merging costs nothing it does not already pay.

Same engine, same adapter, same seed, same lines, three arms in one process:
stock voice (no adapter), LoRA unmerged (as shipped), LoRA merged. Reports
generation seconds / audio seconds per line (LOWER is better, 4.1's metric).

    python experiments/lora_merge_speed_probe.py --adapter lora_models/<name> \
        --lines 6 --out ../ab_test_runtime/experiments/lora_merge_speed_probe.json
"""
import argparse, json, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LINES = [
    "The rain had not stopped for three days, and the road to the village was mud to the ankle.",
    "\"You can't be serious,\" she said, though the look on his face told her he was.",
    "He counted the coins twice, then a third time, and the number did not change.",
    "Somewhere behind the hill a bell rang, slow and uneven, like a hand that had forgotten the rhythm.",
    "Nobody had asked the boy his name, and by the time anyone thought to, he had gone.",
    "\"Then it's settled,\" the captain said. \"We leave at first light, whatever the weather.\"",
    "The letter was short, and every word of it had been chosen to hurt.",
    "It was not courage, exactly. It was the absence of anywhere else to go.",
]


def time_arm(engine, generate, lines, out_dir, label):
    import soundfile as sf
    rows = []
    for i, text in enumerate(lines):
        path = os.path.join(out_dir, f"{label}_{i}.wav")
        t0 = time.time()
        ok = generate(text, path)
        gen = time.time() - t0
        if not ok or not os.path.exists(path):
            rows.append({"line": i, "ok": False, "gen_s": gen})
            continue
        info = sf.info(path)
        audio_s = info.frames / info.samplerate
        rows.append({"line": i, "ok": True, "gen_s": round(gen, 3),
                     "audio_s": round(audio_s, 3),
                     "gen_over_audio": round(gen / audio_s, 3) if audio_s else None})
        print(f"  {label} line {i}: {gen:.2f}s for {audio_s:.2f}s audio "
              f"({gen / audio_s:.2f}x)", flush=True)
    return rows


def summarize(rows):
    ratios = sorted(r["gen_over_audio"] for r in rows if r.get("ok") and r.get("gen_over_audio"))
    if not ratios:
        return {"n": 0}
    return {"n": len(ratios), "median": ratios[len(ratios) // 2],
            "worst": ratios[-1], "best": ratios[0]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapter", required=True, help="lora_models/<name> directory")
    ap.add_argument("--lines", type=int, default=6)
    ap.add_argument("--seed", type=int, default=20260912)
    ap.add_argument("--out", required=True)
    ap.add_argument("--work", default=None, help="where the wavs go (default: next to --out)")
    args = ap.parse_args()

    from tts import TTSEngine, _resolve_asset_path
    adapter = args.adapter if os.path.isabs(args.adapter) else _resolve_asset_path(args.adapter)
    if not os.path.isdir(adapter):
        sys.exit(f"no adapter at {adapter}")
    work = args.work or os.path.splitext(args.out)[0] + "_wavs"
    os.makedirs(work, exist_ok=True)
    lines = LINES[:args.lines]
    engine = TTSEngine(json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"), encoding="utf-8")))

    result = {"experiment": "lora_merge_speed_probe", "adapter": os.path.basename(adapter),
              "seed": args.seed, "lines": lines, "arms": {}, "started": time.time()}

    # Warm-up: the first generation pays model load and any compile; it is
    # not a measurement of either arm, so it is run and discarded.
    voice = {"adapter_path": adapter, "seed": args.seed}
    print("warm-up (LoRA unmerged, discarded)", flush=True)
    engine.generate_lora_voice(lines[0], "", voice, os.path.join(work, "warmup.wav"))

    print("arm: lora_unmerged (as shipped)", flush=True)
    result["arms"]["lora_unmerged"] = time_arm(
        engine, lambda t, p: engine.generate_lora_voice(t, "", voice, p), lines, work, "unmerged")

    # Merge B.A into W in place. Everything else - prompt cache, tokenizer,
    # codec - is untouched, so the only thing that changes is the per-step
    # matmul count.
    model = engine._local_lora_model
    t0 = time.time()
    model.model.talker = model.model.talker.merge_and_unload()
    model.model.talker.eval()
    result["merge_s"] = round(time.time() - t0, 3)
    print(f"merged in {result['merge_s']}s", flush=True)
    engine.generate_lora_voice(lines[0], "", voice, os.path.join(work, "warmup_merged.wav"))
    print("arm: lora_merged", flush=True)
    result["arms"]["lora_merged"] = time_arm(
        engine, lambda t, p: engine.generate_lora_voice(t, "", voice, p), lines, work, "merged")

    # Stock voice on the same engine, for the 4.1 table's other row.
    stock_cfg = {"PROBE": {"type": "custom", "voice": "Ryan", "seed": str(args.seed)}}
    print("arm: stock (custom voice, no adapter)", flush=True)
    try:
        engine.generate_custom_voice(lines[0], "", "PROBE", stock_cfg, os.path.join(work, "warmup_stock.wav"))
        result["arms"]["stock"] = time_arm(
            engine, lambda t, p: engine.generate_custom_voice(t, "", "PROBE", stock_cfg, p), lines, work, "stock")
    except Exception as exc:  # the stock path is a control; its absence is recorded, not hidden
        result["arms"]["stock"] = {"error": f"{type(exc).__name__}: {exc}"}

    result["summary"] = {arm: summarize(rows) for arm, rows in result["arms"].items()
                         if isinstance(rows, list)}
    result["finished"] = time.time()
    from experiments.provenance import provenance
    result["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result["summary"], indent=1))


if __name__ == "__main__":
    main()
