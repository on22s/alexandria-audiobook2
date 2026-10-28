"""Do left-padded CustomVoice batches produce NaN on Apple MPS, and does eager fix it?

Where this comes from (#1650). On a rented M4 (torch 2.7.0, qwen-tts 0.1.1,
transformers 4.57.3, float32) both two-line CustomVoice sub-batches failed with
"probability tensor contains either inf, nan or element < 0"; each of the four
lines later rendered on its own. qwen-tts left-pads batched inputs, so padding
positions attend to no key at all. The hypothesis is that MPS's default SDPA
kernel turns those fully masked rows into NaN, which reaches sampling.

The app now renders one line per request on MPS and loads eager attention
there. This probe tests the hypothesis those two changes rest on; neither change
depends on it, and lifting the one-line cap needs its evidence.

FOR EACH attention backend, sdpa then eager, it loads the CustomVoice model on
MPS, renders the same seeded cases and unloads before the next backend:

    padded_mixed    two lines of different length and speaker (the issue's shape)
    padded_short    two very short lines of different length
    padded_long     one word beside a long sentence: padding by construction
    equal_control   one line twice, two speakers: a batch with no padding
    single_*        every padded line alone

Each case records the exception text, the real token length of every input
(characters do not decide padding; tokens do), whether every sample is finite,
and the samples and duration per output. Each backend also records the
attention implementation every sub-model actually loaded, so the eager arm is
labelled by what ran rather than by what was asked for. Finite is not correct
speech: the issue's own 11-second "Yes," finished finite and transcribed as
other words. Results are written after every case, so an interrupted run keeps
what it measured.

Readings, fixed before running (`get_reading`), checked in this order:

  any out-of-memory error               inconclusive; free memory and rerun
  eager arm loaded any non-eager part   the probe did not test eager; fix it first
  a padded case's tokens are equal      padding was not exercised for that case
  any single line fails                 the cap alone is not enough; report it
  sdpa padded fail, eager padded pass   cause confirmed; a later change may lift
                                        the MPS one-line cap with eager loaded
  both padded fail                      padding/attention is not the whole story;
                                        keep the cap
  neither fails                         not reproduced on this run; keep the cap
                                        and say so
  eager fails, sdpa passes              suspect the probe before believing it

RUN IT ONLY ON AN IDLE MAC. It loads a 1.7B model twice in float32 (about 7 GB
each, one at a time). On the rented Mac, never start it alongside another
diagnostic or model job, and never stop one to make room.
"""
import argparse
import gc
import math
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
BACKENDS = ("sdpa", "eager")
# Synthetic lines from the issue's own test book; no third-party text.
CASES = (
    ("padded_mixed", ["Are you ready?", "asked Mira."], ["Ryan", "Aiden"]),
    ("padded_short", ["Yes,", "said Rowan."], ["Aiden", "Ryan"]),
    ("padded_long", ["Yes,", "She closed the notebook, looked up at the clock on the far "
                     "wall, and said nothing at all for a long while."], ["Ryan", "Aiden"]),
    ("equal_control", ["Are you ready?", "Are you ready?"], ["Ryan", "Aiden"]),
)


def get_probe_cases():
    """-> [(name, texts, speakers)]: each batch, then each padded line alone."""
    cases = list(CASES)
    seen = set()
    for name, texts, speakers in CASES:
        if name.startswith("padded"):
            for text, speaker in zip(texts, speakers):
                if (text, speaker) not in seen:
                    seen.add((text, speaker))
                    cases.append((f"single_{len(seen)}", [text], [speaker]))
    return cases


def is_finite_audio(wav):
    """True for a non-empty waveform whose every sample is a finite number."""
    try:
        values = [float(value) for value in list(wav)]
    except (TypeError, ValueError):
        return False
    return bool(values) and all(math.isfinite(value) for value in values)


def get_case_result(wavs, rate, error=None):
    """-> one case's record. An exception or any non-finite output is a failure."""
    if error is not None:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:500], "outputs": []}
    outputs = [{"samples": len(wav), "finite": is_finite_audio(wav),
                "seconds": round(len(wav) / rate, 3) if rate else None} for wav in wavs]
    return {"ok": bool(outputs) and all(o["finite"] for o in outputs),
            "error": None, "outputs": outputs}


def get_token_lengths(model, texts):
    """-> the token length qwen-tts will left-pad each input to, per text."""
    ids = model._tokenize_texts([model._build_assistant_text(text) for text in texts])
    return [int(item.shape[-1]) for item in ids]


def get_loaded_backends(model):
    """-> {module name: attention implementation} for every configured sub-model."""
    loaded, seen = {}, set()
    for name, module in model.model.named_modules():
        config = getattr(module, "config", None)
        if config is None or id(config) in seen:
            continue
        seen.add(id(config))
        loaded[name or "<root>"] = getattr(config, "_attn_implementation", None)
    return loaded


def get_reading(results):
    """-> the pre-registered reading for {backend: {"loaded": {...}, "cases": {...}}}."""
    def cases(backend):
        return results.get(backend, {}).get("cases", {})
    def failed(backend, prefix):
        return any(not result["ok"] for name, result in cases(backend).items()
                   if name.startswith(prefix))
    every = [result for backend in BACKENDS for result in cases(backend).values()]
    if any("out of memory" in (result["error"] or "").lower() for result in every):
        return "inconclusive_memory"
    if any(value not in (None, "eager") for value in results.get("eager", {}).get("loaded", {}).values()):
        return "backend_not_applied"
    if any(name.startswith("padded") and len(set(result.get("token_lengths") or [0])) < 2
           for backend in BACKENDS for name, result in cases(backend).items()):
        return "padding_not_exercised"
    if failed("sdpa", "single") or failed("eager", "single"):
        return "single_line_failure"
    sdpa, eager = failed("sdpa", "padded"), failed("eager", "padded")
    if sdpa and not eager:
        return "cause_confirmed"
    if sdpa and eager:
        return "padding_not_whole_story"
    if eager:
        return "suspect_probe"
    return "not_reproduced"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--max-new-tokens", type=int, default=2048)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "mps_batch_padding_probe.json"))
    args = ap.parse_args()

    import torch
    if not (getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()):
        sys.exit("refusing: Apple MPS is not available on this machine")
    from qwen_tts import Qwen3TTSModel
    from experiments.provenance import provenance
    from utils import atomic_json_write

    results = {}
    versions = {"torch": torch.__version__, "transformers": __import__("transformers").__version__,
                "qwen_tts": getattr(sys.modules.get("qwen_tts"), "__version__", None)}
    def write(status, reading=None):
        atomic_json_write({"status": status, "reading": reading, "results": results,
                           "versions": versions, "provenance": provenance(__file__, args)}, args.out)
    for backend in BACKENDS:
        print(f"\n== loading {MODEL_ID} on mps, float32, attention={backend}")
        model = Qwen3TTSModel.from_pretrained(
            MODEL_ID, dtype=torch.float32, device_map="mps", attn_implementation=backend)
        results[backend] = {"loaded": get_loaded_backends(model), "cases": {}}
        print(f"  loaded attention: {sorted(set(results[backend]['loaded'].values()), key=str)}")
        for name, texts, speakers in get_probe_cases():
            torch.manual_seed(args.seed)
            started = time.time()
            try:
                wavs, rate = model.generate_custom_voice(
                    text=texts, speaker=speakers, language=["English"] * len(texts),
                    non_streaming_mode=True, max_new_tokens=args.max_new_tokens)
                record = get_case_result(wavs, rate)
            except Exception as error:   # recorded, not hidden: this is the measurement
                record = get_case_result([], 0, error)
            record["texts"], record["speakers"] = texts, speakers
            record["token_lengths"] = get_token_lengths(model, texts)
            record["elapsed_s"] = round(time.time() - started, 2)
            results[backend]["cases"][name] = record
            write("partial")
            print(f"  {name:14} {'ok' if record['ok'] else 'FAIL'}  "
                  f"{record['error'] or [o['seconds'] for o in record['outputs']]}")
        del model
        gc.collect()
        torch.mps.empty_cache()

    reading = get_reading(results)
    write("complete", reading)
    print(f"\nreading: {reading}\nwrote {args.out}")


if __name__ == "__main__":
    main()
