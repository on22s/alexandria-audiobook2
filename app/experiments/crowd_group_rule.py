"""#653: can a listed group answer for its anonymous members?

On the reporter's light-novel chapter (Academy's Weakest ch. 3) his own cast
list named THE STUDENTS, yet all seven crowd lines stayed UNKNOWN - the model
itself answered UNKNOWN (its raw replies), nothing rejected the group. The
inference is rule 7 of the pass-2 prompt ("If the speaker is genuinely
unknowable, use UNKNOWN"): an anonymous student is unknowable as a person
even when the crowd is listed. This tests one change to that rule, as a
custom prompt preset in a separate data directory - the product is untouched.

SUBCOMMANDS
  make-data-dir  copy a profile's config.json and select the group-rule preset
  score-chapter  per-run accuracy on a hand-labelled chapter (gold kept outside
                 the repository: it is keyed to copyrighted text)
  gate           the verdict from the chapter scores and the PDNC A/B

THE GATE, fixed before any run (2026-10-03). The group rule passes if
  1. on the chapter, the mean share of crowd lines answered with the group
     rises above the baseline's (which is 0 of 7 in every run so far), AND
  2. on PDNC (background_speakers ab output), pooled named accuracy
     (cast_alias) is no lower than the baseline minus the baseline's
     run-to-run spread, capped at 0.5 points - the rule #729 used - AND
  3. pooled unnamed accuracy (cast_alias) obeys the same bound.

RESULT OF CHECK 1 (2026-10-03, 3 runs per arm): both arms 7/7 in every run,
so it cannot discriminate. Two identical DIRECT baseline runs then gave 7/7
and 0/7: the crowd outcome is all-or-nothing per run, and the baseline loses
all crowd lines in some runs (2 of 6 so far). The question is that rate.

SECOND TEST, registered before its runs (2026-10-03): 10 further runs per arm
on the same shared pass 1. Count runs whose crowd score is 0/7. The group rule
passes check 1 if it has FEWER such runs than the baseline. Checks 2 and 3
(PDNC) are unchanged and run only if this one passes.
"""
import argparse
import json
import os
import re
import shutil
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from attribution_prompt_variants import MICHEL2_INSTRUCTION, MICHEL2_SYSTEM  # noqa: E402
from experiments.provenance import provenance                               # noqa: E402

PRESET_NAME = "michel2_full_groups"
RULE_7 = ('7. If the speaker is genuinely unknowable, use "UNKNOWN". A wrong name is worse than '
          'UNKNOWN.')
GROUP_RULE_7 = ('7. A line said by an unnamed member of a group on the roster (for example THE '
                'STUDENTS, THE CROWD, THE GUARDS) takes that group\'s name. Use "UNKNOWN" only '
                'when no listed person or group fits. A wrong name is worse than UNKNOWN.')
MAX_NAMED_DROP = 0.5


def get_group_rule_system():
    """MICHEL2_SYSTEM (where michel2's numbered rules live) with rule 7
    replaced - and nothing else changed."""
    flat = MICHEL2_SYSTEM
    if flat.count(RULE_7) != 1:
        raise ValueError("rule 7 is not where this experiment expects it; the prompt changed")
    return flat.replace(RULE_7, GROUP_RULE_7)


def make_data_dir(source_dir, target_dir):
    os.makedirs(target_dir, exist_ok=True)
    with open(os.path.join(source_dir, "config.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    config["prompt_presets"] = [p for p in config.get("prompt_presets") or []
                                if p.get("name") != PRESET_NAME] + [{
        "name": PRESET_NAME, "variant": "michel2_full",
        "description": "michel2_full; rule 7 lets a listed group answer for its members (#653)",
        "system_prompt": get_group_rule_system(), "user_prompt": MICHEL2_INSTRUCTION,
        "example": ""}]
    config.setdefault("prompts", {})["attribution_preset"] = PRESET_NAME
    with open(os.path.join(target_dir, "config.json"), "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=1)


def _normalize(text):
    return re.sub(r"\W+", "", text or "").lower()


def score_chapter(gold, named):
    """-> per-class {n, correct} for one run. `gold` is [{"line", "speaker",
    "class"}]; a gold speaker matches the run's label or any listed alias."""
    by_text = {}
    for entry in named:
        if entry:
            by_text.setdefault(_normalize(entry.get("text")), (entry.get("speaker") or "").upper())
    out = {}
    for row in gold:
        key = _normalize(row["line"])
        answer = by_text.get(key) or next(
            (speaker for text, speaker in by_text.items() if key and key in text), None)
        accepted = {name.upper() for name in [row["speaker"], *row.get("aliases", [])]}
        cls = out.setdefault(row["class"], {"n": 0, "correct": 0, "answers": {}})
        cls["n"] += 1
        cls["correct"] += answer in accepted
        cls["answers"][str(answer)] = cls["answers"].get(str(answer), 0) + 1
    return out


def get_gate(chapter, pdnc, baseline, candidate, repeat):
    """The pre-registered verdict, with the numbers it used."""
    def crowd_share(arm):
        runs = chapter[arm]
        return round(sum(r["crowd"]["correct"] for r in runs)
                     / sum(r["crowd"]["n"] for r in runs), 3)
    checks = {"crowd_share": {arm: crowd_share(arm) for arm in chapter}}
    checks["crowd_pass"] = checks["crowd_share"][candidate] > checks["crowd_share"][baseline]
    for kind in ("named", "descriptive"):
        key = f"{kind}_cast_alias_pct"
        base, cand = pdnc[baseline][key], pdnc[candidate][key]
        spread = abs(base - pdnc[repeat][key]) if repeat in pdnc else 0.0
        allowed = min(spread, MAX_NAMED_DROP)
        checks[f"{kind}_delta_pts"] = round(cand - base, 2)
        checks[f"{kind}_spread_pts"] = round(spread, 2)
        checks[f"{kind}_pass"] = cand - base >= -allowed
    checks["passes"] = checks["crowd_pass"] and checks["named_pass"] and checks["descriptive_pass"]
    return checks


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("make-data-dir")
    p.add_argument("source_dir")
    p.add_argument("target_dir")
    p = sub.add_parser("score-chapter")
    p.add_argument("--gold", required=True)
    p.add_argument("--run", action="append", required=True, help="ARM=CHECKPOINT, repeatable")
    p.add_argument("--output", required=True)
    p = sub.add_parser("gate")
    p.add_argument("--chapter", required=True)
    p.add_argument("--pdnc", required=True, help="background_speakers ab output")
    p.add_argument("--baseline", default="cast")
    p.add_argument("--candidate", default="groups")
    p.add_argument("--repeat", default="cast_repeat")
    p.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if args.command == "make-data-dir":
        make_data_dir(args.source_dir, args.target_dir)
        print(f"{args.target_dir}: preset {PRESET_NAME} selected")
        return
    if args.command == "score-chapter":
        from generation_checkpoint_deltas import load_generation_delta_checkpoint
        with open(args.gold, encoding="utf-8") as handle:
            gold = json.load(handle)
        runs = {}
        for spec in args.run:
            arm, path = spec.split("=", 1)
            named = load_generation_delta_checkpoint(path).get("named") or []
            runs.setdefault(arm, []).append({"checkpoint": os.path.basename(path),
                                             **score_chapter(gold, named)})
        doc = {"runs": runs, "provenance": provenance(__file__, args)}
    else:
        with open(args.chapter, encoding="utf-8") as handle:
            chapter = json.load(handle)["runs"]
        with open(args.pdnc, encoding="utf-8") as handle:
            pdnc = json.load(handle)["summary"]
        doc = {"gate": get_gate(chapter, pdnc, args.baseline, args.candidate, args.repeat),
               "provenance": provenance(__file__, args)}
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1)
    print(json.dumps({k: v for k, v in doc.items() if k != "provenance"}, indent=1)[:3000])


if __name__ == "__main__":
    main()
