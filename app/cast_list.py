"""The whole-book cast list that three_pass_generate.py --cast-file reads (#653).

Why: pass 2's roster admits only names the text capitalises, so a character the
book never names ("the detective", "the stranger") can never be on it, and the
model answers UNKNOWN or hands the line to a named neighbour. Measured on five
PDNC novels (#727, DeepSeek v4-pro): unnamed speakers 0% -> 71.5%, named 69.0% ->
93.2%, lines left UNKNOWN 722 -> 91, from one request per book (~$0.05).

One request over the whole prepared text, through the active profile's client -
so `transport: "manual"` turns it into a paste on the Script tab with no code
here. Refuses a source over the limit rather than truncating it.

    python -u cast_list.py book.txt --out cast.json [--no-strip-front-matter]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time

from utils import atomic_json_write

PROMPT = ("Below is a complete novel. List every character who SPEAKS a quoted line at least "
          "once. Give each as the name a reader would use for them; if the text never names "
          "them, use the description the text uses (for example \"THE STRANGER\", \"THE "
          "LANDLADY\"). For each character give every other name or description the text uses "
          "for the same person. Answer with only JSON: "
          '[{"name": "...", "aliases": ["...", ...]}, ...], UPPERCASE.\n\n')
MAX_ATTEMPTS = 3
MAX_SOURCE_CHARS = 1_200_000    # about 300k tokens
CAST_LISTS_DIRNAME = "cast_lists"


def parse_cast_list(content):
    """The JSON list from a reply, with or without a code fence; raises if there is none."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(x, dict) and x.get("name") for x in data):
        raise ValueError("reply is not a list of {name, aliases}")
    return [{"name": str(x["name"]).strip().upper(),
             "aliases": [str(a).strip().upper() for a in x.get("aliases") or [] if str(a).strip()]}
            for x in data]


def request_cast_list(client, model, text, max_tokens, max_attempts=MAX_ATTEMPTS):
    """(cast, response, attempts) from one call, retried when the reply ran to the token cap.

    A reply that stops at max_tokens is a runaway (Anne of Green Gables, 2026-09-29: 40 names
    cycled 6.5 times until the cap cut a string in half; the same call at temperature 0 later
    answered with 70 characters in 2,295 tokens), so raising the cap does not help and the
    reply is never parsed or repaired. One policy on every attempt: length -> discard and ask
    again; any other finish reason is parsed and a bad parse raises at once. Raises after
    max_attempts truncated replies, saying so.
    """
    for attempt in range(1, max_attempts + 1):
        r = client.chat.completions.create(model=model, temperature=0, max_tokens=max_tokens,
                                           messages=[{"role": "user", "content": PROMPT + text}])
        if r.choices[0].finish_reason != "length":
            return parse_cast_list(r.choices[0].message.content), r, attempt
        print(f"attempt {attempt}/{max_attempts}: reply hit max_tokens={max_tokens}; discarded",
              file=sys.stderr)
    raise RuntimeError(f"cast list truncated at max_tokens={max_tokens} on all {max_attempts} "
                       "attempts (finish_reason=length); not parsing a cut-off reply")


def get_cast_list_path(input_file, data_dir):
    """Where a source's cast list lives: keyed by the source's BYTES, not its name.

    Single and batch generation and a re-upload of the same file all find the
    same list, and an edited source never picks up a list built for old text.
    """
    digest = hashlib.sha256()
    with open(input_file, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return os.path.join(data_dir, CAST_LISTS_DIRNAME, digest.hexdigest() + ".json")


def save_cast_list(path, cast, provenance=None):
    """Validate with the generator's own validator, then write atomically.

    Raises ValueError and writes nothing when generation would refuse the list.
    """
    from three_pass_generate import get_cast_from_data
    get_cast_from_data(cast)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_json_write({"cast": cast, "provenance": provenance or {}}, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source")
    parser.add_argument("--out", required=True)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--strip-front-matter", action=argparse.BooleanOptionalAction,
                        default=True)
    args = parser.parse_args(argv)

    from config_settings import load_app_config
    from core import llm_timeout_seconds
    from llm_provider import make_run_client
    from lmstudio_settings import get_active_llm_config
    from three_pass_generate import get_prepared_source
    from utils import get_app_config_path, get_runtime_data_dir

    try:
        text, _ = get_prepared_source(args.source, args.strip_front_matter, report=print)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    app_dir = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(app_dir)
    config = load_app_config(get_app_config_path(get_runtime_data_dir(root), root, app_dir))
    llm = get_active_llm_config(config)
    manual = llm.get("transport") == "manual"
    if len(text) > MAX_SOURCE_CHARS:
        print(f"Error: the book is {len(text):,} characters, over the {MAX_SOURCE_CHARS:,} "
              "a cast list request allows. It needs the whole book at once and is "
              "never built from a truncated text.")
        return 1
    print(f"Building a cast list from {len(text):,} characters"
          + (" (manual mode: paste the request into your model)" if manual else ""), flush=True)
    client = make_run_client(config, llm, llm_timeout_seconds())
    started = time.time()
    try:
        cast, response, attempts = request_cast_list(
            client, llm.get("model_name"), text, args.max_tokens)
    except RuntimeError as exc:
        print(f"Error: {exc}")
        return 1
    except ValueError as exc:
        print(f"Error: the reply was not the JSON list that was asked for ({exc}). "
              "Nothing was saved; build it again.")
        return 1
    usage = getattr(response, "usage", None)
    provenance = {
        "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": llm.get("model_name"), "transport": llm.get("transport", "http"),
        "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
        "source": os.path.basename(args.source),
        "prepared_source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "strip_front_matter": args.strip_front_matter,
        "finish_reason": response.choices[0].finish_reason, "attempts": attempts,
        "usage": {"prompt_tokens": getattr(usage, "prompt_tokens", None),
                  "completion_tokens": getattr(usage, "completion_tokens", None)},
        "elapsed_s": round(time.time() - started, 1)}
    try:
        save_cast_list(args.out, cast, provenance)
    except ValueError as exc:
        print(f"Error: the model's list was refused: {exc}")
        return 1
    print(f"Cast list: {len(cast)} characters -> {os.path.basename(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
