"""Measure one generation request without retries, splitting, or repair."""

import argparse
import json
import os
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--chunk", type=int, required=True)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from openai import OpenAI
    import generate_script
    from chunk_quality import validate_chunk_quality
    from default_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
    from utils import atomic_json_write

    with open(args.source, encoding="utf-8") as handle:
        book = handle.read()
    with open(os.path.join(APP, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    generation = config.get("generation") or {}
    book, _ = generate_script.get_preprocessed_source(book)
    chunks = generate_script.split_into_chunks(
        book, max_size=generation.get("chunk_size", 6000))
    source = chunks[args.chunk - 1]
    llm = config.get("llm_local") or config.get("llm") or {}
    params = generate_script.LLMGenParams(
        max_tokens=generation.get("max_tokens", 10000),
        temperature=generation.get("temperature", 0.6),
        top_p=generation.get("top_p", 0.8), top_k=generation.get("top_k"),
        min_p=generation.get("min_p"),
        presence_penalty=generation.get("presence_penalty", 0.0), seed=args.seed)
    context = generate_script._build_chunk_context(args.chunk, len(chunks), None)
    attempts = []
    started = time.time()
    entries = generate_script.call_llm_for_entries(
        OpenAI(base_url=llm.get("base_url"), api_key=llm.get("api_key") or "local"),
        llm.get("model_name"), DEFAULT_SYSTEM_PROMPT,
        DEFAULT_USER_PROMPT.format(context=context, chunk=source), params,
        "generation_state_probe_responses.log", args.condition, max_retries=0,
        attempt_observer=attempts.append)
    quality = validate_chunk_quality(source, entries)
    atomic_json_write({
        "experiment": "generation_state_probe", "condition": args.condition,
        "seed": args.seed, "source": os.path.abspath(args.source),
        "chunk": args.chunk, "total_chunks": len(chunks),
        "passed": bool(entries and quality["passed"]),
        "entry_count": len(entries), "quality": quality, "attempts": attempts,
        "seconds": round(time.time() - started, 1),
    }, args.out)
    print(f"{args.condition}: {'PASS' if entries and quality['passed'] else 'FAIL'}")


if __name__ == "__main__":
    main()
