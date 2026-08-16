"""Run the full frozen-text three-pass path on one production chunk."""

import argparse
import json
import os
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--chunk", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from openai import OpenAI
    from chunk_quality import validate_chunk_quality
    from generate_script import LLMGenParams, get_preprocessed_source, split_into_chunks
    from three_pass_generate import run_three_pass
    from utils import atomic_json_write

    with open(args.source, encoding="utf-8") as handle:
        book = handle.read()
    with open(os.path.join(APP, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    generation = config.get("generation") or {}
    book, _ = get_preprocessed_source(book)
    chunks = split_into_chunks(book, generation.get("chunk_size", 6000))
    source = chunks[args.chunk - 1]
    from lmstudio_settings import get_active_llm_config
    llm = get_active_llm_config(config)
    params = LLMGenParams(
        max_tokens=generation.get("max_tokens", 10000),
        temperature=generation.get("temperature", 0.6),
        top_p=generation.get("top_p", 0.8), top_k=generation.get("top_k"),
        min_p=generation.get("min_p"), presegment_quotes=True,
        segment_temperature=0.0, attribute_temperature=0.0,
        instruct_temperature=generation.get("three_pass_instruct_temperature", 0.1))
    entries = run_three_pass(
        OpenAI(base_url=llm.get("base_url"), api_key=llm.get("api_key") or "local"),
        llm.get("model_name"), source, params, len(source) + 1,
        on_exhaustion="fail", output_path=args.out + ".entries.json",
        endpoint=llm.get("base_url"))
    quality = validate_chunk_quality(source, entries)
    atomic_json_write({
        "experiment": "three_pass_chunk_probe", "source": os.path.abspath(args.source),
        "chunk": args.chunk, "total_chunks": len(chunks), "entry_count": len(entries),
        "passed": quality["passed"], "quality": quality,
        "unknown_speakers": sum(entry.get("speaker") == "UNKNOWN" for entry in entries),
        "default_instructs": sum(entry.get("instruct") == "Neutral, even narration."
                                 for entry in entries),
    }, args.out)
    print(f"three-pass chunk {args.chunk}: {'PASS' if quality['passed'] else 'FAIL'}")


if __name__ == "__main__":
    main()
