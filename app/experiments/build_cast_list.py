"""Build a cast list for three_pass_generate.py --cast-file, from the whole book in one call.

Why: pass 2's roster admits only names the text capitalises three or more times, so a
character the book never names ("the stranger", The Invisible Man's protagonist) can never
be on it, and the model answers UNKNOWN - 99 of The Invisible Man's lines in stage 0 of the
DeepSeek labelling plan (2026-09-28). One call over the whole text listed him as THE STRANGER
(also THE INVISIBLE MAN, GRIFFIN), and its names and aliases covered 903 of 904 PDNC gold
lines, for $0.047.

Uses the active profile (ALEXANDRIA_DATA_DIR / config.json), including any provider body
(thinking on or off as configured). Refuses a source that would not fit the context rather
than truncating it. Writes {"cast": [...], "provenance": {...}}; three_pass_generate reads
the "cast" list (or a bare list).

    ALEXANDRIA_DATA_DIR=... python experiments/build_cast_list.py book.txt --out book.cast.json
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

PROMPT = ("Below is a complete novel. List every character who SPEAKS a quoted line at least "
          "once. Give each as the name a reader would use for them; if the text never names "
          "them, use the description the text uses (for example \"THE STRANGER\", \"THE "
          "LANDLADY\"). For each character give every other name or description the text uses "
          "for the same person. Answer with only JSON: "
          '[{"name": "...", "aliases": ["...", ...]}, ...], UPPERCASE.\n\n')


def parse_cast(content):
    """The JSON list from a reply, with or without a code fence; raises if there is none."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(x, dict) and x.get("name") for x in data):
        raise ValueError("reply is not a list of {name, aliases}")
    return [{"name": str(x["name"]).strip().upper(),
             "aliases": [str(a).strip().upper() for a in x.get("aliases") or [] if str(a).strip()]}
            for x in data]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--max-source-chars", type=int, default=1_200_000,
                    help="refuse longer sources (about 300k tokens) instead of truncating")
    a = ap.parse_args()
    from config_settings import load_app_config
    from core import llm_timeout_seconds
    from llm_provider import make_run_client
    from lmstudio_settings import get_active_llm_config
    from utils import get_app_config_path, get_runtime_data_dir

    text = open(a.source, encoding="utf-8").read()
    if len(text) > a.max_source_chars:
        sys.exit(f"REFUSING: {len(text)} chars is over --max-source-chars {a.max_source_chars}")
    root = os.path.dirname(APP)
    config = load_app_config(get_app_config_path(get_runtime_data_dir(root), root, APP))
    llm = get_active_llm_config(config)
    client = make_run_client(config, llm, llm_timeout_seconds())
    t0 = time.time()
    r = client.chat.completions.create(model=llm.get("model_name"), temperature=0,
                                       max_tokens=a.max_tokens,
                                       messages=[{"role": "user", "content": PROMPT + text}])
    content = r.choices[0].message.content
    cast = parse_cast(content)
    usage = getattr(r, "usage", None)
    from experiments.provenance import provenance
    out = {"cast": cast, "provenance": provenance(
        __file__, a,
        model=llm.get("model_name"), base_url=llm.get("base_url"),
        provider_extra_body=llm.get("provider_extra_body"),
        prompt_sha256=hashlib.sha256(PROMPT.encode()).hexdigest(),
        source=os.path.abspath(a.source),
        source_sha256=hashlib.sha256(text.encode()).hexdigest(),
        finish_reason=r.choices[0].finish_reason,
        usage={"prompt_tokens": getattr(usage, "prompt_tokens", None),
               "completion_tokens": getattr(usage, "completion_tokens", None)},
        elapsed_s=round(time.time() - t0, 1))}
    tmp = a.out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    os.replace(tmp, a.out)
    print(f"{len(cast)} characters -> {a.out} "
          f"(prompt {out['provenance']['usage']['prompt_tokens']}, "
          f"completion {out['provenance']['usage']['completion_tokens']})")


if __name__ == "__main__":
    main()
