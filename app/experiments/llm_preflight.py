"""Is there actually an LLM to talk to? Ask before spending an hour finding out.

WHY THIS EXISTS. On 2026-08-16 the PR #308 narration remeasurement ran with
nothing listening on port 8090. It recorded `rc: 1` for both books and wrote an
artifact with an empty `results` list, which reads as "the experiment failed" -
a result worth investigating - when what actually happened was that the engine
was absent. The distinction matters because the two call for opposite
responses: a failed experiment is evidence, a missing server is an hour of
nothing. It stayed undiagnosed for a day.

`gpu_job.sh` could not have caught it. It serialises access to the card and
propagates exit codes; it has no idea whether a server exists. That is the
right division - most jobs here are TTS and need no LLM - so this is a separate
check the LLM jobs opt into.

WHY IT SENDS A REAL COMPLETION rather than pinging /v1/models. A loaded server
answers /v1/models while still being unable to produce text: wrong model
loaded, context too small for the prompts, adapter missing. The endpoint says
"a server is here", which is not the question.

WHY max_tokens IS GENEROUS. Qwen3 emits reasoning tokens before its answer, so
a small budget returns an EMPTY string with finish_reason='length' and HTTP
200. Measured here: `max_tokens=16` gave `''`, and `max_tokens=400` gave 'OK'
after 156 completion tokens. A preflight with a tight budget would fail on a
perfectly healthy server.

WHY NOT lmstudio_settings.get_current_status. Against llama.cpp it reports
`{"available": true, "loaded": false, "context_length": null}` for a model that
answers in 3.4 seconds - it probes an LM Studio API that llama.cpp does not
serve and reads the absence as "not loaded". Right answer for LM Studio, wrong
question for this stack, and this project runs llama.cpp.
"""
import argparse
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")

# Shaped like the real work - attribute a quoted line - so the check exercises
# the same behaviour the experiments depend on rather than a greeting.
PROBE = ('Who speaks the quoted line? Answer with the name only.\n\n'
         'Aiko frowned. "You promised you would wait," she said.')


def get_endpoint(config_path):
    """-> (base_url, model_name) for whichever mode config.json selects."""
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    mode = config.get("llm_mode", "local")
    section = config.get(f"llm_{mode}") or {}
    if not section.get("base_url"):
        raise RuntimeError(
            f"config.json selects llm_mode={mode!r} but llm_{mode} has no "
            "base_url, so there is no endpoint to check")
    return section["base_url"], section.get("model_name")


def check(base_url, model, timeout=180, max_tokens=400):
    """-> (ok, detail). Never raises: a preflight that crashes is a preflight
    that tells you nothing about the server."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        return False, f"openai package unavailable: {exc}"

    client = OpenAI(base_url=base_url, api_key="local", timeout=timeout)
    try:
        served = [m.id for m in client.models.list().data]
    except Exception as exc:                                # noqa: BLE001
        return False, (f"no server answering at {base_url} "
                       f"({type(exc).__name__}: {str(exc)[:120]})")

    started = time.time()
    try:
        reply = client.chat.completions.create(
            model=model, temperature=0, max_tokens=max_tokens,
            messages=[{"role": "user", "content": PROBE}])
    except Exception as exc:                                # noqa: BLE001
        return False, (f"server at {base_url} is up but refused a completion "
                       f"({type(exc).__name__}: {str(exc)[:120]}); served "
                       f"models: {served}")
    elapsed = time.time() - started
    choice = reply.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        return False, (f"server returned an EMPTY completion in {elapsed:.1f}s "
                       f"(finish_reason={choice.finish_reason}). With "
                       f"finish_reason='length' this is the reasoning-token "
                       f"trap - raise --max-tokens above {max_tokens}.")
    return True, (f"{base_url} answered in {elapsed:.1f}s "
                  f"(finish={choice.finish_reason}, "
                  f"{reply.usage.completion_tokens} completion tokens): "
                  f"{content[:60]!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=os.path.join(APP, "config.json"))
    ap.add_argument("--timeout", type=float, default=180)
    ap.add_argument("--max-tokens", type=int, default=400)
    ap.add_argument("--quiet", action="store_true",
                    help="print only on failure")
    args = ap.parse_args()

    try:
        base_url, model = get_endpoint(args.config)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"llm_preflight: {exc}", file=sys.stderr)
        return 1

    ok, detail = check(base_url, model, args.timeout, args.max_tokens)
    if ok:
        if not args.quiet:
            print(f"llm_preflight: OK - {detail}")
        return 0
    print(f"llm_preflight: NOT READY - {detail}", file=sys.stderr)
    print("llm_preflight: start one with", file=sys.stderr)
    print("  LLAMA_MODEL=<path-to.gguf> ./ensure_llama_server.sh", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
