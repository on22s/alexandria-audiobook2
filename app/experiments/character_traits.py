"""#653 follow-up: where should a character's gender and age come from?

The reporter asked for pass 2 to return `gender` and `age_group` on every line.
The cheaper alternative is one request per book: the cast list returns them per
character. This measures both against PDNC's per-character gender labels
(M/F; X and U are not genders and are excluded, reported) before either is
built. PDNC has no age labels, so age is reported as agreement, not accuracy.

ARMS
  A  cast list with traits    one request per book (`cast` subcommand)
  B  per line in pass 2       a prompt preset asks every answer for the two
                              fields; `run-line` keeps them on the named
                              entries (in-process patch - the product's
                              converter keeps only the speaker)
  C  local inference          `routers.voices._infer_character_traits` on the
                              character's label, free (`score` computes it)

THE GATE, fixed before any run (2026-10-03). Choose B only if ALL hold:
  1. B's per-character gender accuracy (modal over its correctly attributed
     lines) >= A's, pooled over books and runs;
  2. B's who-speaks named accuracy (cast_alias) is no lower than the baseline
     cast runs (#727 cast + crowd_ab cast, same configuration) minus their
     spread, capped at 0.5 points;
  3. B's pass-2 model tokens rise no more than 25% over the baseline runs.
Otherwise A. Each arm runs at least twice.
"""
import argparse
import ast
import collections
import csv
import json
import os
import re
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.provenance import provenance  # noqa: E402

GENDERS = ("male", "female", "genderless", "unknown")
AGE_GROUPS = ("child", "teen", "young_adult", "adult", "middle_aged", "elderly", "ageless",
              "unknown")
PDNC_GENDER = {"M": "male", "F": "female"}
MAX_NAMED_DROP = 0.5
MAX_TOKEN_RISE_PCT = 25.0

CAST_PROMPT = (
    "Below is a complete novel. List every character who SPEAKS a quoted line at least "
    "once. Give each as the name a reader would use for them; if the text never names "
    "them, use the description the text uses (for example \"THE STRANGER\", \"THE "
    "LANDLADY\"). For each character give every other name or description the text uses "
    "for the same person, their gender (one of: " + ", ".join(GENDERS).upper() + ") and "
    "their age group (one of: " + ", ".join(AGE_GROUPS).upper() + "), as the text "
    "establishes them; use UNKNOWN when the text does not say. Answer with only JSON: "
    '[{"name": "...", "aliases": ["...", ...], "gender": "...", "age_group": "..."}, ...], '
    "UPPERCASE.\n\n")

LINE_RULE = (
    "\n\nALSO, for every entry give the speaker's \"gender\" (one of: "
    + ", ".join(GENDERS).upper() + ") and \"age_group\" (one of: "
    + ", ".join(AGE_GROUPS).upper() + ") as the speaker is at that point in the story; "
    "use UNKNOWN when the text does not establish it, and UNKNOWN for narration entries. "
    'Each object is then {"n", "speaker", "gender", "age_group"}.')


def get_normalized(value, allowed):
    value = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return value if value in allowed else "unknown"


def parse_trait_cast(content):
    """The cast JSON with traits kept (cast_list.parse_cast_list drops them)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip())
    data = json.loads(text)
    if not isinstance(data, list) or not all(isinstance(x, dict) and x.get("name") for x in data):
        raise ValueError("reply is not a list of characters")
    return [{"name": str(x["name"]).strip().upper(),
             "aliases": [str(a).strip().upper() for a in x.get("aliases") or [] if str(a).strip()],
             "gender": get_normalized(x.get("gender"), GENDERS),
             "age_group": get_normalized(x.get("age_group"), AGE_GROUPS)} for x in data]


def run_cast(args):
    from config_settings import load_app_config
    from core import llm_timeout_seconds
    from llm_provider import make_run_client
    from lmstudio_settings import get_active_llm_config
    from three_pass_generate import get_prepared_source
    from utils import get_app_config_path, get_runtime_data_dir
    root = os.path.dirname(APP)
    config = load_app_config(get_app_config_path(get_runtime_data_dir(root), root, APP))
    llm = get_active_llm_config(config)
    client = make_run_client(config, llm, llm_timeout_seconds())
    text, _ = get_prepared_source(args.source, True)
    started = time.time()
    for attempt in range(1, 4):
        reply = client.chat.completions.create(
            model=llm.get("model_name"), temperature=0, max_tokens=10000,
            messages=[{"role": "user", "content": CAST_PROMPT + text}])
        if reply.choices[0].finish_reason != "length":
            break
    cast = parse_trait_cast(reply.choices[0].message.content)
    usage = getattr(reply, "usage", None)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"cast": cast, "provenance": provenance(
            __file__, args, attempts=attempt, elapsed_s=round(time.time() - started, 1),
            usage={"prompt_tokens": getattr(usage, "prompt_tokens", None),
                   "completion_tokens": getattr(usage, "completion_tokens", None)})},
            handle, indent=1)
    print(f"{len(cast)} characters -> {args.out}")


def make_line_data_dir(source_dir, target_dir):
    """A profile whose pass-2 preset asks every answer for the two fields."""
    from attribution_prompt_variants import MICHEL2_INSTRUCTION, MICHEL2_SYSTEM
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(source_dir, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    config["prompt_presets"] = [{
        "name": "michel2_full_traits", "variant": "michel2_full",
        "description": "michel2_full + per-line gender/age (#653 experiment)",
        "system_prompt": MICHEL2_SYSTEM + LINE_RULE,
        "user_prompt": MICHEL2_INSTRUCTION.replace('{"n", "speaker"}',
                                                   '{"n", "speaker", "gender", "age_group"}'),
        "example": ""}]
    config.setdefault("prompts", {})["attribution_preset"] = "michel2_full_traits"
    with open(os.path.join(target_dir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=1)


def install_line_traits():
    """Keep the model's gender/age on each named entry (product keeps speaker only)."""
    import three_pass_generate as tp
    original = tp.get_named_from_answer

    def with_traits(frozen_batch, ordered, cast=None):
        out = original(frozen_batch, ordered, cast)
        return [{**entry, "line_gender": get_normalized(item.get("gender"), GENDERS),
                 "line_age_group": get_normalized(item.get("age_group"), AGE_GROUPS)}
                for entry, item in zip(out, ordered)]
    tp.get_named_from_answer = with_traits


def load_pdnc_genders(pdnc_dir, book_folder):
    """-> {UPPER name or alias: gender or None}, None for X/U."""
    genders = {}
    with open(os.path.join(pdnc_dir, "data", book_folder, "character_info.csv"),
              encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            gender = PDNC_GENDER.get(row["Gender"])
            names = {row["Main Name"]}
            try:
                names |= set(ast.literal_eval(row["Aliases"]))   # PDNC stores a set literal
            except (ValueError, SyntaxError):
                pass
            for name in names:
                genders.setdefault(str(name).strip().upper(), gender)
    return genders


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("cast")
    p.add_argument("source")
    p.add_argument("--out", required=True)
    p = sub.add_parser("make-line-data-dir")
    p.add_argument("source_dir")
    p.add_argument("target_dir")
    p = sub.add_parser("run-line", help="background_speakers_ab with per-line traits kept")
    args, rest = parser.parse_known_args(argv)
    if args.command == "cast":
        run_cast(args)
    elif args.command == "make-line-data-dir":
        make_line_data_dir(args.source_dir, args.target_dir)
    else:
        install_line_traits()
        from experiments import background_speakers_ab
        background_speakers_ab.main(rest)


if __name__ == "__main__":
    main()
