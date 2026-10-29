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
  either arm has no matching backend    the probe did not establish the backend
  a case is missing                     the comparison is incomplete
  token lengths unavailable             the measurement instrument failed
  invalid rate or output count          the model output contract failed
  no case padded in both arms          padding was not exercised; cases with
                                        equal tokens are excluded from the
                                        padding comparison
  any single line fails                 the cap alone is not enough; report it
  equal-length control fails            the failure is not isolated to padding
  sdpa failure has a different error    the observed failure was not the issue's
  sdpa padded fail, eager padded pass   association observed; not proof of cause
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
import numbers
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

MODEL_ID = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"
BACKENDS = ("sdpa", "eager")
ATTENTION_CLASSES = frozenset({"Qwen3TTSTalkerAttention", "Qwen3TTSAttention"})
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


def resolve_model_snapshot(download):
    """Resolve one hub revision to use for both arms; reject an ambiguous path."""
    path = os.fspath(download(repo_id=MODEL_ID))
    revision = os.path.basename(os.path.normpath(path))
    if not re.fullmatch(r"[0-9a-f]{40}", revision) or not os.path.isdir(path):
        raise ValueError("model snapshot is not an identifiable hub revision")
    return path, revision


def is_finite_audio(wav):
    """True for a non-empty waveform whose every sample is a finite number."""
    try:
        values = [float(value) for value in list(wav)]
    except (TypeError, ValueError):
        return False
    return bool(values) and all(math.isfinite(value) for value in values)


def get_case_result(wavs, rate, error=None, expected_count=None):
    """-> one case's record. An exception or any non-finite output is a failure."""
    if error is not None:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:500], "outputs": []}
    if (isinstance(rate, bool) or not isinstance(rate, numbers.Real)
            or not math.isfinite(rate) or rate <= 0):
        return {"ok": False, "error": "output contract: invalid sample rate", "outputs": []}
    if (not isinstance(wavs, (list, tuple)) or len(wavs) == 0 or
            (expected_count is not None and len(wavs) != expected_count)):
        return {"ok": False, "error": "output contract: wrong waveform count", "outputs": []}
    try:
        outputs = [{"samples": len(wav), "finite": is_finite_audio(wav),
                    "seconds": round(len(wav) / rate, 3)} for wav in wavs]
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "output contract: malformed waveforms", "outputs": []}
    return {"ok": all(o["finite"] for o in outputs),
            "error": None, "outputs": outputs}


def get_token_lengths(model, texts):
    """-> the token length qwen-tts will left-pad each input to, per text."""
    ids = model._tokenize_texts([model._build_assistant_text(text) for text in texts])
    return [int(item.shape[-1]) for item in ids]


def get_loaded_backends(model):
    """-> backend for each Qwen talker/code-predictor attention module."""
    loaded = {}
    for name, module in model.model.named_modules():
        cls = type(module).__name__
        if cls not in ATTENTION_CLASSES:
            continue
        config = getattr(module, "config", None)
        loaded[f"{cls}:{name}"] = getattr(config, "_attn_implementation", None)
    return loaded


def get_reading(results):
    """-> the pre-registered reading for {backend: {"loaded": {...}, "cases": {...}}}."""
    def cases(backend):
        return results.get(backend, {}).get("cases", {})
    def failed(backend, prefix):
        return any(not result["ok"] for name, result in cases(backend).items()
                   if name.startswith(prefix))
    every = [result for backend in BACKENDS for result in cases(backend).values()]
    load_errors = [results.get(backend, {}).get('load_error') for backend in BACKENDS]
    if any('out of memory' in (error or '').lower() for error in load_errors):
        return 'inconclusive_memory'
    if any(load_errors):
        return 'model_load_failure'
    if any(results.get(backend, {}).get('instrument_error') for backend in BACKENDS):
        return 'instrument_failure'
    if any(result.get('instrument_error') for result in every):
        return 'instrument_failure'
    if any("out of memory" in (result["error"] or "").lower() for result in every):
        return "inconclusive_memory"
    for backend in BACKENDS:
        loaded = results.get(backend, {}).get("loaded", {})
        classes = {name.split(':', 1)[0] for name in loaded}
        if classes != ATTENTION_CLASSES or any(value != backend for value in loaded.values()):
            return "backend_not_applied"
    expected = {name for name, _, _ in get_probe_cases()}
    if any(set(cases(backend)) != expected for backend in BACKENDS):
        return "incomplete_cases"
    for backend in BACKENDS:
        for name, texts, _ in get_probe_cases():
            lengths = cases(backend)[name].get("token_lengths")
            if (not isinstance(lengths, list) or len(lengths) != len(texts)
                    or any(type(length) is not int or length <= 0 for length in lengths)):
                return "instrument_failure"
    if any((result.get("error") or "").startswith("output contract:") for result in every):
        return "output_contract_failure"
    padded_names = {name for name in expected if name.startswith('padded')
                    and all(len(set(cases(backend)[name]['token_lengths'])) > 1
                            for backend in BACKENDS)}
    if not padded_names:
        return "padding_not_exercised"
    if failed("sdpa", "single") or failed("eager", "single"):
        return "single_line_failure"
    if any(not cases(backend)["equal_control"]["ok"] or
           len(cases(backend)["equal_control"].get("token_lengths") or []) != 2 or
           len(set(cases(backend)["equal_control"].get("token_lengths") or [])) != 1
           for backend in BACKENDS):
        return "control_failure"
    sdpa, eager = (any(not cases(backend)[name]['ok'] for name in padded_names)
                   for backend in BACKENDS)
    if sdpa and not eager:
        failures = [result for name, result in cases("sdpa").items()
                    if name in padded_names and not result["ok"]]
        if any("probability tensor contains either" not in (result.get("error") or "").lower()
               for result in failures):
            return "unexpected_failure"
        return "padding_backend_association"
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
    from huggingface_hub import snapshot_download
    from experiments.provenance import provenance
    from utils import atomic_json_write

    model_path, model_revision = resolve_model_snapshot(snapshot_download)
    results = {}
    versions = {"torch": torch.__version__, "transformers": __import__("transformers").__version__,
                "qwen_tts": getattr(sys.modules.get("qwen_tts"), "__version__", None)}
    def write(status, reading=None):
        atomic_json_write({"status": status, "reading": reading, "results": results,
                           "model_id": MODEL_ID, "model_revision": model_revision,
                           "versions": versions, "provenance": provenance(__file__, args)}, args.out)
    for backend in BACKENDS:
        print(f"\n== loading {MODEL_ID} on mps, float32, attention={backend}")
        results[backend] = {'loaded': {}, 'cases': {}}
        try:
            model = Qwen3TTSModel.from_pretrained(
                model_path, dtype=torch.float32, device_map="mps", attn_implementation=backend)
        except Exception as error:
            results[backend]['load_error'] = f'{type(error).__name__}: {error}'[:500]
            write('complete', get_reading(results))
            return
        try:
            results[backend]['loaded'] = get_loaded_backends(model)
        except Exception as error:
            results[backend]['instrument_error'] = f'{type(error).__name__}: {error}'[:500]
            write('complete', get_reading(results))
            return
        print(f"  loaded attention: {sorted(set(results[backend]['loaded'].values()), key=str)}")
        for name, texts, speakers in get_probe_cases():
            started = time.time()
            token_lengths = None
            stage = 'measurement'
            try:
                torch.manual_seed(args.seed)
                token_lengths = get_token_lengths(model, texts)
                stage = 'generation'
                wavs, rate = model.generate_custom_voice(
                    text=texts, speaker=speakers, language=["English"] * len(texts),
                    non_streaming_mode=True, max_new_tokens=args.max_new_tokens)
                stage = 'measurement'
                record = get_case_result(wavs, rate, expected_count=len(texts))
            except Exception as error:   # recorded, not hidden: this is the measurement
                record = get_case_result([], 0, error)
                if stage == 'measurement':
                    record['instrument_error'] = True
            record["texts"], record["speakers"] = texts, speakers
            record["token_lengths"] = token_lengths
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
