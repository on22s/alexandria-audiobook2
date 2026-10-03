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


def get_character_key(name, pdnc_genders):
    """The PDNC key a predicted name or alias resolves to, or None."""
    value = (name or "").strip().upper()
    return value if value in pdnc_genders else None


def score_cast_traits(cast, pdnc_genders, gold_aliases):
    """-> {pdnc character: (gender, age_group)} from one arm-A cast list."""
    out = {}
    for person in cast:
        for name in [person["name"], *person["aliases"]]:
            key = get_character_key(name, pdnc_genders)
            if key is None:
                group = next((g for g in gold_aliases if name.upper() in g), None)
                key = next((n for n in (group or ()) if n in pdnc_genders), None)
            if key:
                out.setdefault(get_canonical(key, gold_aliases), (person["gender"], person["age_group"]))
                break
    return out


def get_canonical(key, gold_aliases):
    group = next((g for g in gold_aliases if key in g), None)
    return min(group) if group else key


def score_line_traits(checkpoint_path, gold, gold_aliases, labels):
    """-> per character: list of (gender, age) on correctly attributed lines,
    and the per-line gender rows."""
    from experiments.background_speakers import is_same_speaker
    from generation_checkpoint_deltas import load_generation_delta_checkpoint
    named = [e for e in load_generation_delta_checkpoint(checkpoint_path)["named"] if e]
    norm = lambda t: re.sub(r"\W+", "", t or "").lower()
    by_text = {}
    for entry in named:
        by_text.setdefault(norm(entry.get("text")), entry)
    per_character = collections.defaultdict(list)
    for entry in gold["entries"]:
        answer = by_text.get(norm(entry["line"]))
        expected = entry["expected_speaker"].upper()
        if answer and is_same_speaker(answer.get("speaker"), expected, gold_aliases, labels):
            per_character[get_canonical(expected, gold_aliases)].append(
                (answer.get("line_gender", "unknown"), answer.get("line_age_group", "unknown")))
    return per_character


def get_modal(values):
    counts = collections.Counter(v for v in values if v != "unknown")
    return counts.most_common(1)[0][0] if counts else "unknown"


def run_score(args):
    import glob
    from types import SimpleNamespace
    from experiments.background_speakers import load_gold_index, run_ab
    from routers.voices import _infer_character_traits
    gold_index = load_gold_index(args.fixtures)
    rows, pooled = [], collections.Counter()
    age_pairs = collections.Counter()
    for book, folder in zip(args.books, args.folders):
        pdnc = load_pdnc_genders(args.pdnc_dir, folder)
        aliases, labels = gold_index["aliases"][book], gold_index["labels"][book]
        with open(os.path.join(args.fixtures, f"attribution_gold_{book}.json"), encoding="utf-8") as h:
            gold = json.load(h)
        truth = {}
        for label in labels:
            key = get_canonical(label, aliases)
            gender = pdnc.get(label) or next((pdnc.get(n) for g in aliases if label in g for n in g
                                              if pdnc.get(n)), None)
            if gender:
                truth[key] = gender
        arm_a = [score_cast_traits(json.load(open(path, encoding="utf-8"))["cast"], pdnc, aliases)
                 for path in sorted(glob.glob(os.path.join(args.casts_a, f"{book}__cast*.json")))]
        arm_b = [score_line_traits(path, gold, aliases, labels) for path in sorted(
            glob.glob(os.path.join(args.line_runs, f"{book}__line*.json.threepass_checkpoint.json")))]
        for run_index, (a, b) in enumerate(zip(arm_a, arm_b)):
            for character, gender in truth.items():
                a_gender = a.get(character, ("unknown", "unknown"))[0]
                b_values = b.get(character, [])
                b_gender = get_modal([g for g, _ in b_values])
                c_gender = _infer_character_traits(character, None, [])["gender"]
                if a_gender == "unknown" or b_gender == "unknown":
                    pooled["not_in_both"] += 1
                    continue
                pooled["n"] += 1
                pooled["a_correct"] += a_gender == gender
                pooled["b_correct"] += b_gender == gender
                pooled["c_correct"] += c_gender == gender
                pooled["c_unknown"] += c_gender == "unknown"
                pooled["b_lines"] += len(b_values)
                pooled["b_lines_correct"] += sum(g == gender for g, _ in b_values)
                pooled["b_lines_consistent"] += sum(g == b_gender for g, _ in b_values)
                a_age = a.get(character, ("unknown", "unknown"))[1]
                b_age = get_modal([age for _, age in b_values])
                age_pairs["both_known"] += a_age != "unknown" and b_age != "unknown"
                age_pairs["agree"] += a_age == b_age != "unknown"
                rows.append({"book": book, "run": run_index + 1, "character": character,
                             "gold_gender": gender, "a": a_gender, "b": b_gender, "c": c_gender,
                             "b_lines": len(b_values), "a_age": a_age, "b_age": b_age})
    accuracy = {arm: round(100 * pooled[f"{arm}_correct"] / pooled["n"], 1) for arm in "abc"}
    who = {}
    for name, (runs, suffix) in {"line1": (args.line_runs, "line1"), "line2": (args.line_runs, "line2"),
                                 "base1": (args.baseline_runs[0], "cast"),
                                 "base2": (args.baseline_runs[1], "cast")}.items():
        summary = run_ab(SimpleNamespace(fixtures=args.fixtures, runs=runs, casts=args.cast_lists,
                                         books=args.books, arms=[suffix]))["summary"][suffix]
        who[name] = summary["named_cast_alias_pct"]
    tokens = {}
    for name, (logs, suffix) in {"line": (args.line_logs, "line"),
                                 "base1": (args.baseline_logs[0], "cast"),
                                 "base2": (args.baseline_logs[1], "cast")}.items():
        total, files = 0, 0
        for path in glob.glob(os.path.join(logs, f"*__{suffix}*.log")):
            if not any(os.path.basename(path).startswith(b + "__") for b in args.books):
                continue
            files += 1
            for m in re.finditer(r"prompt=(\d+) completion=(\d+)", open(path, errors="replace").read()):
                total += int(m.group(1)) + int(m.group(2))
        tokens[name] = round(total / max(1, files) * len(args.books))
    base_named = (who["base1"] + who["base2"]) / 2
    line_named = (who["line1"] + who["line2"]) / 2
    spread = abs(who["base1"] - who["base2"])
    # tokens[...] is already a per-run figure (total / files x books); dividing
    # the line arm by 2 again reported -47.5% for a +5.0% rise on the interim.
    token_rise = round(100 * (tokens["line"] - (tokens["base1"] + tokens["base2"]) / 2)
                       / ((tokens["base1"] + tokens["base2"]) / 2), 1)
    gate = {"per_character_n": pooled["n"], "not_in_both": pooled["not_in_both"],
            "gender_accuracy_pct": accuracy,
            "b_line_gender_accuracy_pct": round(100 * pooled["b_lines_correct"] / max(1, pooled["b_lines"]), 1),
            "b_line_consistency_pct": round(100 * pooled["b_lines_consistent"] / max(1, pooled["b_lines"]), 1),
            "c_unknown": pooled["c_unknown"],
            "age_agreement_a_vs_b": dict(age_pairs),
            "who_speaks_named_pct": who, "named_delta_pts": round(line_named - base_named, 2),
            "baseline_spread_pts": round(spread, 2), "tokens_per_run": tokens,
            "token_rise_pct": token_rise}
    gate["accuracy_pass"] = accuracy["b"] >= accuracy["a"]
    gate["who_speaks_pass"] = line_named - base_named >= -min(spread, MAX_NAMED_DROP)
    gate["cost_pass"] = token_rise <= MAX_TOKEN_RISE_PCT
    gate["choose"] = "B (per line)" if (gate["accuracy_pass"] and gate["who_speaks_pass"]
                                         and gate["cost_pass"]) else "A (per character)"
    doc = {"gate": gate, "rows": rows, "provenance": provenance(__file__, args)}
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1)
    print(json.dumps(gate, indent=1))


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
    p = sub.add_parser("score")
    p.add_argument("--fixtures", required=True)
    p.add_argument("--pdnc-dir", required=True)
    p.add_argument("--books", nargs="+", required=True)
    p.add_argument("--folders", nargs="+", required=True)
    p.add_argument("--casts-a", required=True)
    p.add_argument("--line-runs", required=True)
    p.add_argument("--line-logs", required=True)
    p.add_argument("--baseline-runs", nargs=2, required=True)
    p.add_argument("--baseline-logs", nargs=2, required=True)
    p.add_argument("--cast-lists", required=True)
    p.add_argument("--output", required=True)
    args, rest = parser.parse_known_args(argv)
    if args.command == "cast":
        run_cast(args)
    elif args.command == "score":
        run_score(args)
    elif args.command == "make-line-data-dir":
        make_line_data_dir(args.source_dir, args.target_dir)
    else:
        install_line_traits()
        from experiments import background_speakers_ab
        background_speakers_ab.main(rest)


if __name__ == "__main__":
    main()
