"""Before Auto-Configure picks more Parallel Workers: does a big batch sound the same, and
how much memory does it really take?

tts_workers_bench (2026-09-28) measured 1.2x real time at 1 worker and 3.9x at 16 on the
RX 9070 XT, peak torch-allocated 6.8 GB at 16, while the wiki quotes ~11.8 GB for 16-line
batches (tts_vram_benchmark). Two open questions:

1. QUALITY. The same 32 lines are rendered through the production batch path
   (engine.run_benchmark_batch) at W = 1, 4 and 16, same seed. Each clip is transcribed
   (whisper base.en, experiments.asr_backends) and scored by WER against its text; each
   W>1 clip is compared with the W=1 clip of the same line by ECAPA cosine
   (voice_reference._speaker_similarities) and by duration ratio.
2. MEMORY. Per W: torch max_memory_allocated, torch max_memory_reserved, and the device's
   peak used memory (total - free from mem_get_info, sampled every 0.2 s by a thread),
   which is what another process on the card actually sees.
"""
import argparse, hashlib, json, os, random, statistics, subprocess, sys, tempfile, threading, time

APP = sys.argv[sys.argv.index("--app") + 1]
sys.path.insert(0, APP)
os.chdir(APP)
import tts_vram_benchmark as vb  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True)
    ap.add_argument("--workers", nargs="+", type=int, default=[1, 4, 16])
    ap.add_argument("--lines", type=int, default=32)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import soundfile as sf, torch
    from config_settings import load_app_config
    from tts import TTSEngine
    from experiments.asr_backends import run_transformers_whisper, word_error_rate
    from voice_reference import _speaker_similarities
    cfg = vb.get_benchmark_engine_config(load_app_config(os.path.join(APP, "config.json")).get("tts", {}))
    engine = TTSEngine(cfg)
    engine._init_local_custom()
    engine.ensure_custom_warmup(engine._local_custom_model)
    voice = {"NARRATOR": {"type": "custom", "voice": "Ryan"}}
    random.seed(42)
    chunks = vb.make_chunks(a.lines)
    texts = {i: c.get("text") for i, c in enumerate(chunks)}
    wav_root = os.path.join(os.path.dirname(os.path.abspath(a.out)), "tts_batch_quality_wavs")
    os.makedirs(wav_root, exist_ok=True)
    rows, wavs = [], {}
    for w in a.workers:
        engine.set_sub_batch_size(w)
        d = os.path.join(wav_root, f"w{w}")
        os.makedirs(d, exist_ok=True)
        with tempfile.TemporaryDirectory() as warm:
            engine.run_benchmark_batch(chunks[:w], voice, warm, batch_seed=42)
        torch.cuda.synchronize(); torch.cuda.reset_peak_memory_stats()
        total = torch.cuda.mem_get_info()[1]; peak = {"used": 0}; stop = threading.Event()
        def sample():
            while not stop.is_set():
                free, tot = torch.cuda.mem_get_info(); peak["used"] = max(peak["used"], tot - free); time.sleep(0.2)
        t = threading.Thread(target=sample, daemon=True); t.start()
        t0 = time.time(); audio = 0.0; failed = 0; allocated_peak_gb = 0.0
        for i in range(0, len(chunks), w):
            res = engine.run_benchmark_batch(chunks[i:i + w], voice, d, batch_seed=42)
            failed += len(res["failed"])
            # Generation resets Torch counters for each sub-batch; its result
            # retains the maximum across those resets, unlike the final counter.
            allocated_peak_gb = max(allocated_peak_gb, res["peak_vram_gb"])
        wall = time.time() - t0; stop.set(); t.join()
        wavs[w] = {}
        for idx in range(len(chunks)):
            p = os.path.join(d, f"temp_batch_{idx}.wav")
            if os.path.exists(p):
                wavs[w][idx] = p; audio += sf.info(p).duration
        rows.append({"workers": w, "clips": len(wavs[w]), "failed": failed, "audio_s": round(audio, 1),
                     "wall_s": round(wall, 1), "x_real_time": round(audio / wall, 2) if wall else None,
                     "torch_max_allocated_gb": round(allocated_peak_gb, 2),
                     "torch_last_sub_batch_peak_reserved_gb": round(torch.cuda.max_memory_reserved() / 1e9, 2),
                     "device_peak_used_gb": round(peak["used"] / 1e9, 2), "device_total_gb": round(total / 1e9, 1)})
        print(rows[-1], flush=True)
    del engine; torch.cuda.empty_cache()
    per = []
    for w in a.workers:
        for idx, p in sorted(wavs[w].items()):
            hyp, _ = run_transformers_whisper(p)
            per.append({"workers": w, "line": idx, "wer": round(word_error_rate(texts[idx], hyp), 3),
                        "seconds": round(sf.info(p).duration, 2), "hyp": hyp[:120]})
    base = min(a.workers)
    pairs, keys = [], []
    for w in a.workers:
        if w == base: continue
        for idx in sorted(set(wavs[w]) & set(wavs[base])):
            pairs.append((wavs[base][idx], wavs[w][idx])); keys.append((w, idx))
    sims = _speaker_similarities(pairs) or [None] * len(pairs)
    sim_by = {k: s for k, s in zip(keys, sims)}
    dur = {(r["workers"], r["line"]): r["seconds"] for r in per}
    summary = []
    for w in a.workers:
        wr = [r["wer"] for r in per if r["workers"] == w]
        s = {"workers": w, "mean_wer": round(statistics.mean(wr), 3) if wr else None,
             "lines_wer_over_0.2": sum(1 for x in wr if x > 0.2)}
        if w != base:
            ss = [sim_by[(w, i)] for i in range(len(chunks)) if sim_by.get((w, i)) is not None]
            rr = [dur[(w, i)] / dur[(base, i)] for i in range(len(chunks)) if (w, i) in dur and (base, i) in dur and dur[(base, i)]]
            s.update({"ecapa_vs_w1_median": round(statistics.median(ss), 3) if ss else None,
                      "ecapa_vs_w1_min": round(min(ss), 3) if ss else None,
                      "duration_ratio_vs_w1_median": round(statistics.median(rr), 3) if rr else None,
                      "duration_ratio_vs_w1_worst": round(max(rr, key=lambda x: abs(x - 1)), 3) if rr else None})
        summary.append(s); print(s, flush=True)
    commit = subprocess.run(["git", "-C", APP, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    json.dump({"render": rows, "quality": summary, "per_clip": per, "gpu": vb.gpu_name(),
               "settings": {"voice": "CustomVoice Ryan", "lines": a.lines, "seed": 42, "asr": "openai/whisper-base.en"},
               "provenance": {"script": os.path.abspath(__file__),
                              "script_sha256": hashlib.sha256(open(__file__, "rb").read()).hexdigest(),
                              "repo_commit": commit, "written": time.strftime("%Y-%m-%dT%H:%M:%S")}},
              open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
