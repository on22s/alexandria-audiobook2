"""Does an FP8 checkpoint load its scales, and does it ever stop generating?

WHY THIS EXISTS. qwen38-multientry-thinking-low-8k spent 12.5 hours of H100 on
2026-09-07 and produced no valid answer: 36 of 36 generations ended
finish_reason=length at exactly max_tokens=8000, zero ended on `stop`. Every
window came back PassExhausted and both arms scored 0. It was killed at 36/80.

The log's first page carries a candidate mechanism, and the reason for this
probe:

    Qwen3_5ForConditionalGeneration LOAD REPORT from: .../Qwen3.8-27B-FP8
    model.language_model.layers.{0...63}.mlp.gate_proj.weight_scale_inv | UNEXPECTED

`weight_scale_inv` is the per-tensor dequantization scale for an FP8 weight.
UNEXPECTED means the loader did not place it, across all 64 layers, for one
projection in every MLP block. A gate projection dequantized without its scale
is wrong by a constant factor per tensor - which is exactly the kind of damage
that produces fluent-looking tokens that never reach a stop token.

THAT IS A HYPOTHESIS, NOT A MEASUREMENT (Rule 19). The load report is measured;
"which is why the output is garbage" is a story about it, and stories of that
shape have been wrong here before. The FP8 kernel preflight PASSED on that run,
so the obvious explanation - a missing kernel - is already ruled out and the
next one deserves a test rather than a paragraph.

WHAT THIS MEASURES. Two checkpoints of the SAME model, same prompt, same
decoding, same seed. For each: what the loader reported, and whether generation
terminates on its own. One variable, and no adapter, gold, or book in the path
- because none of those differed between the runs that worked and the run that
did not, and every one of them is a way for the comparison to go wrong.

Cheap on purpose. Two loads and two short generations answer it; the run that
raised the question cost 12.5 hours.
"""

import argparse
import contextlib
import io
import json
import os
import re
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

# A prompt with an obvious, short, terminable answer. A model that cannot stop
# here has nothing to do with attribution difficulty.
PROMPT = ("Answer with one short sentence and then stop.\n\n"
          "Question: Who is speaking in the line 'I will not go,' said Marta?\n"
          "Answer:")

UNPLACED = re.compile(r"^(?P<key>\S+)\s*\|\s*(?P<status>UNEXPECTED|MISSING|MISMATCH)",
                      re.MULTILINE)


def scale_findings(report):
    """-> [{key, status}] for every tensor the loader did not place.

    Reads the report the loader PRINTS, because that is where the finding
    appeared and a probe that inspects something else is not checking the same
    thing. Returns [] for a clean load, so "loaded clean" and "never looked"
    stay distinguishable in the artifact.
    """
    out = []
    for match in UNPLACED.finditer(report or ""):
        key = match.group("key")
        out.append({"key": key, "status": match.group("status"),
                    "is_scale": "scale" in key})
    return out


def run_one(model_path, max_new_tokens, seed):
    """Load one checkpoint, generate once, report what happened."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    started = time.time()
    # The load report goes to stderr as the weights are placed; capturing it is
    # the only way to see it from inside the process that triggered it.
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        tokenizer = AutoTokenizer.from_pretrained(model_path)
        model = AutoModelForCausalLM.from_pretrained(
            model_path, dtype="auto", device_map="auto")
    report = buffer.getvalue()
    load_s = time.time() - started

    torch.manual_seed(seed)
    inputs = tokenizer(PROMPT, return_tensors="pt").to(model.device)
    started = time.time()
    with torch.no_grad():
        output = model.generate(**inputs, max_new_tokens=max_new_tokens,
                                do_sample=False,
                                pad_token_id=tokenizer.eos_token_id)
    generate_s = time.time() - started

    generated = output[0][inputs["input_ids"].shape[1]:]
    produced = int(generated.shape[0])
    # THE MEASUREMENT. A run that stops on its own has produced fewer tokens
    # than the cap; one that hits the cap never emitted a stop token. This is
    # the same distinction the eval logged as finish_reason, derived here
    # without an inference server in the way.
    stopped = produced < max_new_tokens
    findings = scale_findings(report)
    return {
        "model": model_path,
        "loaded": True,
        "load_seconds": round(load_s, 1),
        "generate_seconds": round(generate_s, 1),
        "tokens_requested": max_new_tokens,
        "tokens_produced": produced,
        "stopped_on_its_own": stopped,
        "finish_reason": "stop" if stopped else "length",
        "text": tokenizer.decode(generated, skip_special_tokens=True),
        "unplaced_tensors": findings,
        "unplaced_scale_count": sum(1 for f in findings if f["is_scale"]),
        "load_report": report[:4000],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", required=True,
                        help="checkpoint paths to compare, e.g. an FP8 and a "
                             "BF16 copy of the same model")
    parser.add_argument("--max-new-tokens", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from utils import atomic_json_write
    from experiments.provenance import provenance

    arms = []
    for path in args.models:
        try:
            arms.append(run_one(path, args.max_new_tokens, args.seed))
        except Exception as error:                          # noqa: BLE001
            # RECORD the failure into the artifact rather than returning a
            # plausible-looking arm. A checkpoint that will not load is a
            # result about that checkpoint; a fallback that hands back a normal
            # answer is how a config error becomes a believable non-result.
            arms.append({"model": path, "loaded": False,
                         "error": f"{type(error).__name__}: {error}"})
        print(json.dumps(arms[-1], default=str)[:400], flush=True)

    loaded = [a for a in arms if a.get("loaded")]
    payload = {
        "probe": "fp8_scale_load",
        "provenance": provenance(__file__, args),
        "prompt": PROMPT,
        "seed": args.seed,
        "arms": arms,
        # Stated as the two facts, separately, because the whole point is to
        # see whether they move together.
        "summary": {
            "stopped_on_its_own": {a["model"]: a["stopped_on_its_own"]
                                   for a in loaded},
            "unplaced_scale_count": {a["model"]: a["unplaced_scale_count"]
                                     for a in loaded},
        },
    }
    atomic_json_write(args.out, payload)
    print("wrote " + args.out, flush=True)


if __name__ == "__main__":
    main()
