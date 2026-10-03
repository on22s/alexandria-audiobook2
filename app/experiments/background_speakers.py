"""Issue #653, Phase 0: how are unnamed (background) speakers attributed today?

Measurement only. Nothing here changes generation; it re-scores artifacts that
already exist on disk so the design decision in #653 rests on numbers.

WHAT COUNTS AS UNNAMED. PDNC names every speaker, including people the novel
never names, and it names them descriptively and persistently: "THE DETECTIVE"
speaks 30 quotes across The Sport of the Gods, "THE FIRST/SECOND/THIRD
HOUSEMAID" are numbered within one scene. A gold label is *descriptive* here
when its form says so - it opens with an article or quantifier, is one of the
app's own generic words (`utils.is_generic_speaker`), or is a group
(`speaker_identity.is_group_speaker_label`). Form is not use: THE INVISIBLE MAN
is descriptive in form and the protagonist in use. Rather than judge use by
hand, every result is split by how many quotes the gold label carries, which is
measured, and the labels that are titles-used-as-names are listed in
`data/background_speakers_title_names.json` - a judgement, kept apart from the
rule so it can be corrected without touching the rule.

THREE SUBCOMMANDS
  eval       every `lora_serving_eval__*.json`: on gold-descriptive rows, was
             the answer correct, an abstention (UNKNOWN/blank), a different
             descriptive label, or a NAMED character? These runs hand the model
             the gold roster, unnamed people included - an oracle condition.
  threepass  full three-pass checkpoints on PDNC books: the product, with the
             cast it built itself. Same four outcomes, plus whether the
             gold label's person ever reached the cast at all.
  labels     the owner's generated scripts: every distinct speaker label with
             the signals the #653 brief proposed, per DISTINCT volume (repeat
             generations of one volume are grouped, not summed). The sheet
             written beside the artifact is for hand correction and holds book
             text, so it goes outside the repository.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.provenance import provenance               # noqa: E402
from generation_checkpoint_deltas import load_generation_delta_checkpoint  # noqa: E402
from speaker_identity import is_group_speaker_label          # noqa: E402
from utils import is_generic_speaker                         # noqa: E402

REPO = os.path.dirname(APP)
EXPERIMENTS = os.path.join(REPO, "ab_test_runtime", "experiments")
TITLE_NAMES_PATH = os.path.join(APP, "experiments", "data",
                                "background_speakers_title_names.json")

_DESCRIPTIVE_OPENING = re.compile(
    r"^(THE|A|AN|ONE|ANOTHER|SOME|TWO|THREE|SEVERAL|OTHER)\b")
_NUMBERED = re.compile(r"[\s_#]+\d+$")
ABSTAIN = {"", "UNKNOWN", "NONE", "NULL"}
QUOTE_BANDS = ((1, 2), (3, 9), (10, 10 ** 9))


def is_descriptive_label(label):
    """True if a speaker label's FORM describes rather than names someone."""
    value = " ".join((label or "").upper().split())
    if not value or value in ABSTAIN or value in {"NARRATOR", "NARR"}:
        return False
    return bool(_DESCRIPTIVE_OPENING.match(value)
                or is_generic_speaker(_NUMBERED.sub("", value))
                or is_group_speaker_label(value))


def get_title_names(path=TITLE_NAMES_PATH):
    """-> {book: {label}} labels descriptive in form that work as names."""
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    return {book: {entry["label"].upper() for entry in entries}
            for book, entries in doc["books"].items()}


def get_label_list(section, path=TITLE_NAMES_PATH):
    """-> {book: {label}} from one hand-kept section of the judgement file."""
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    return {book: {label.upper() for label in labels} for book, labels in
            doc[section]["books"].items()}


DESCRIPTIVE_OVERRIDES = get_label_list("descriptive_without_article")
DESCRIPTORS_OF_NAMED = get_label_list("descriptors_of_named")


def get_band(count):
    for low, high in QUOTE_BANDS:
        if low <= count <= high:
            return f"{low}-{high}" if high < 10 ** 9 else f"{low}+"
    return "0"


def get_outcome(predicted, correct):
    """One outcome per answered row; the four are exhaustive and exclusive."""
    if correct:
        return "correct"
    value = " ".join((predicted or "").upper().split())
    if value in ABSTAIN:
        return "abstain"
    if is_descriptive_label(value):
        return "other_descriptive"
    return "named"


def _empty():
    return collections.Counter()


def _rates(counter):
    n = sum(counter.values())
    return {"n": n, **{k: counter[k] for k in sorted(counter)},
            **{f"{k}_pct": round(100 * counter[k] / n, 1)
               for k in sorted(counter)}} if n else {"n": 0}


_ALSO = re.compile(r"\s*\(ALSO:[^)]*\)\s*$")
_ARTICLE = re.compile(r"^(THE|A|AN)\s+")


def get_loose_form(label):
    """'A PRIEST (ALSO: PRIEST)' -> 'PRIEST'. Article and the model's
    '(ALSO: ...)' habit carry no identity for an unnamed speaker."""
    return _ARTICLE.sub("", _ALSO.sub("", " ".join((label or "").upper().split())))


def is_same_speaker(left, right, aliases, book_labels=()):
    """Alias-aware match against the CURRENT gold's alias groups.

    Falls back to the loose form only when that form names ONE gold person in
    the book: The Sun Also Rises has both A WAITER and THE WAITER, and an
    answer of "WAITER" cannot be credited to either.
    """
    left, right = (left or "").upper().strip(), (right or "").upper().strip()
    if left == right or any(left in g and right in g for g in aliases):
        return True
    owners = {}
    for label in book_labels:
        group = next((g for g in aliases if label in g), {label})
        for name in group | {label}:
            owners.setdefault(get_loose_form(name), set()).add(min(group))
    want = owners.get(get_loose_form(right), set())
    got = owners.get(get_loose_form(left), set())
    return len(want) == 1 and want == got


def get_gold_class(book, gold, gold_index, title_names):
    """-> title_name | descriptor_of_named | descriptive | named."""
    if gold in title_names.get(book, ()):
        return "title_name"
    if not (is_descriptive_label(gold)
            or gold in DESCRIPTIVE_OVERRIDES.get(book, ())):
        return "named"
    # PDNC sometimes labels a NAMED person by description until the text
    # reveals him (WOODEN-LEGGED MAN is JONATHAN SMALL, in the gold's own
    # aliases). That is alias work, not the unnamed-extra problem. Listed by
    # hand: inferring it from "has a non-descriptive alias" also caught bare
    # role nouns (FOOTMAN, STATIONMASTER) and moved 20 real extras out.
    if gold in DESCRIPTORS_OF_NAMED.get(book, ()):
        return "descriptor_of_named"
    return "descriptive"


def score_rows(rows, gold_index, title_names):
    """Score one artifact arm's rows by the class of their gold speaker.

    Correctness is recomputed against the current gold aliases rather than
    read from each artifact: 1,484 stored flags were scored against older
    alias lists, and one policy across every artifact is the point (Rule 10).
    The band is the gold label's size in the whole book, not in this sample.
    """
    label_counts = gold_index["counts"]
    out = {"descriptive": _empty(), "title_name": _empty(), "named": _empty(),
           "descriptor_of_named": _empty(),
           "descriptive_by_band": collections.defaultdict(_empty),
           "named_answered_descriptive_or_abstain": 0, "examples": []}
    for row in rows:
        book = row["id"].split(":", 1)[0]
        gold = (row.get("expected") or "").upper().strip()
        correct = is_same_speaker(row.get("predicted"), gold,
                                  gold_index["aliases"].get(book, ()),
                                  gold_index["labels"].get(book, ()))
        outcome = get_outcome(row.get("predicted"), correct)
        gold_class = get_gold_class(book, gold, gold_index, title_names)
        if gold_class in ("title_name", "descriptor_of_named"):
            out[gold_class][outcome] += 1
        elif gold_class == "descriptive":
            out["descriptive"][outcome] += 1
            if (book, gold) not in label_counts:
                # Not a size of zero: the gold that would say how big this
                # label is was never loaded. Guessing a band here is how a
                # missing fixture became 1,084 "zero-quote" rows on first run.
                raise KeyError(f"no gold count for {book}/{gold}; is "
                               f"--fixtures pointing at the full PDNC gold?")
            band = get_band(label_counts[(book, gold)])
            out["descriptive_by_band"][band][outcome] += 1
            if outcome == "named" and len(out["examples"]) < 12:
                out["examples"].append({"book": book, "gold": gold,
                                        "predicted": row.get("predicted"),
                                        "line": (row.get("line") or "")[:120]})
        else:
            out["named"][outcome] += 1
            if outcome in ("abstain", "other_descriptive"):
                out["named_answered_descriptive_or_abstain"] += 1
    return out


def load_gold_index(fixtures):
    """-> {"counts": {(book key, LABEL): quotes}, "aliases": {book: [set]}}."""
    counts, aliases = collections.Counter(), {}
    for path in sorted(glob.glob(os.path.join(fixtures,
                                              "attribution_gold_pdnc_*.json"))):
        if "_w3200" in path:
            continue
        key = os.path.basename(path)[len("attribution_gold_"):-len(".json")]
        with open(path, encoding="utf-8") as handle:
            gold = json.load(handle)
        for entry in gold["entries"]:
            counts[(key, entry["expected_speaker"].upper().strip())] += 1
        aliases[key] = [{n.upper().strip() for n in group}
                        for group in gold.get("aliases", [])]
    labels = collections.defaultdict(set)
    for book, label in counts:
        labels[book].add(label)
    return {"counts": counts, "aliases": aliases, "labels": dict(labels)}


def run_eval(args):
    title_names = get_title_names()
    gold_index = load_gold_index(args.fixtures)
    results, skipped = [], []
    for path in sorted(glob.glob(os.path.join(args.experiments,
                                              "lora_serving_eval__*.json"))):
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError) as exc:
            skipped.append({"artifact": name, "reason": type(exc).__name__})
            continue
        rows = doc.get("rows") if isinstance(doc, dict) else None
        if not isinstance(rows, list) or not rows:
            skipped.append({"artifact": name, "reason": "no rows"})
            continue
        meta = doc.get("meta") or {}
        by_arm = collections.defaultdict(list)
        for row in rows:
            if str(row.get("id", "")).startswith("pdnc_") and "expected" in row:
                by_arm[row.get("arm") or "base"].append(row)
        if not by_arm:
            skipped.append({"artifact": name, "reason": "no PDNC rows"})
            continue
        for arm, arm_rows in sorted(by_arm.items()):
            scored = score_rows(arm_rows, gold_index, title_names)
            results.append({
                "artifact": name, "arm": arm, "model": meta.get("model"),
                "roster_mode": (meta.get("decoding") or {}).get("roster_mode"),
                "rows": len(arm_rows),
                "descriptive": _rates(scored["descriptive"]),
                "title_name": _rates(scored["title_name"]),
                "descriptor_of_named": _rates(scored["descriptor_of_named"]),
                "named": _rates(scored["named"]),
                "descriptive_by_band": {band: _rates(c) for band, c in
                                        sorted(scored["descriptive_by_band"].items())},
                "named_answered_descriptive_or_abstain":
                    scored["named_answered_descriptive_or_abstain"],
                "named_error_examples": scored["examples"]})
    return {"rows": results, "skipped": skipped,
            "summary": summarize_eval(results)}


def summarize_eval(results):
    """Per arm, pooled: what share of wrong answers on unnamed rows named
    someone? Keyed by arm because the artifact audit pairs summary to rows."""
    by_arm = collections.defaultdict(list)
    for row in results:
        by_arm[row["arm"]].append(row)
    outcomes = ("correct", "abstain", "other_descriptive", "named")
    summary = {}
    for arm, rows in sorted(by_arm.items()):
        pooled = {"descriptive": _empty(), "named": _empty()}
        shares = []
        for row in rows:
            for key in pooled:
                pooled[key].update({k: v for k, v in row[key].items()
                                    if k in outcomes})
            d = row["descriptive"]
            wrong = d.get("n", 0) - d.get("correct", 0)
            if d.get("n", 0) >= 30 and wrong:
                shares.append(d.get("named", 0) / wrong)
        shares.sort()
        summary[arm] = {
            "artifacts": len(rows),
            "pooled_descriptive": _rates(pooled["descriptive"]),
            "pooled_named": _rates(pooled["named"]),
            "artifacts_with_30plus_descriptive_rows": len(shares),
            "share_of_wrong_answers_that_name_someone": {
                "min": round(shares[0], 3) if shares else None,
                "median": round(shares[len(shares) // 2], 3) if shares else None,
                "max": round(shares[-1], 3) if shares else None}}
    return summary


def _normalize(text):
    return re.sub(r"\W+", "", text or "").lower()


def run_threepass(args):
    title_names = get_title_names()
    fixtures = args.fixtures
    gold_index = load_gold_index(fixtures)
    results = []
    for path in args.checkpoints:
        # The repo's loader, not json.load: a checkpoint written by a current
        # run is an indexed-delta document whose header alone has no entries,
        # and reading it raw scores a finished book as zero located lines.
        checkpoint = load_generation_delta_checkpoint(path)
        book = os.path.basename(path).split("__", 1)[0]
        with open(os.path.join(fixtures, f"attribution_gold_{book}.json"),
                  encoding="utf-8") as handle:
            gold = json.load(handle)
        aliases = [{n.upper() for n in group} for group in gold.get("aliases", [])]

        def same(left, right):
            left, right = (left or "").upper(), (right or "").upper()
            return left == right or any(left in g and right in g for g in aliases)

        # Located the way tools/audit/pipeline_repeat_scoring.py locates: a
        # gold line counts only when its text occurs exactly once in the
        # segmentation. Unlocated rows are reported, not scored as wrong -
        # this asks what happened to unnamed speakers, not overall accuracy.
        occurrences = collections.Counter(
            _normalize(e.get("text")) for e in checkpoint.get("segmented") or [])
        speakers = {}
        for entry in (e for e in checkpoint.get("named") or [] if e):
            speakers.setdefault(_normalize(entry.get("text")), entry.get("speaker"))
        rows, unlocated = [], 0
        for entry in gold["entries"]:
            key = _normalize(entry["line"])
            if occurrences.get(key) != 1 or key not in speakers:
                unlocated += 1
                continue
            rows.append({"id": f"{book}:{entry['id']}", "line": entry["line"],
                         "expected": entry["expected_speaker"],
                         "predicted": speakers[key],
                         "correct": same(speakers[key], entry["expected_speaker"])})
        cast = collections.Counter(
            (e.get("speaker") or "").upper()
            for e in checkpoint.get("named") or [] if e)
        scored = score_rows(rows, gold_index, title_names)
        results.append({
            "checkpoint": os.path.relpath(path, REPO) if path.startswith(REPO)
            else os.path.basename(path),
            "book": book, "model": (checkpoint.get("fingerprint") or {}).get("model_name"),
            "gold_rows": len(gold["entries"]), "located": len(rows),
            "unlocated": unlocated,
            "descriptive": _rates(scored["descriptive"]),
            "title_name": _rates(scored["title_name"]),
            "descriptor_of_named": _rates(scored["descriptor_of_named"]),
            "named": _rates(scored["named"]),
            "descriptive_by_band": {band: _rates(c) for band, c in
                                    sorted(scored["descriptive_by_band"].items())},
            "named_error_examples": scored["examples"],
            "output_labels": {
                "distinct": len(cast) - ("NARRATOR" in cast),
                "descriptive": sorted(l for l in cast if is_descriptive_label(l)),
                "abstain_lines": sum(n for l, n in cast.items() if l in ABSTAIN)}})
    return {"rows": results}


# ---- labels: the owner's generated scripts ------------------------------

_VOLUME_SUFFIX = re.compile(r"_\d+$")


def get_volume(path):
    """'Arc 3 - Volume 4_3.json' -> 'Arc 3 - Volume 4' (repeat generations)."""
    return _VOLUME_SUFFIX.sub("", os.path.basename(path)[:-len(".json")])


def get_case_counts(token, texts):
    """-> (mid-sentence capitalized, lowercase) occurrences of one word."""
    # Mid-sentence only: after a lowercase word or a comma. A capital at a
    # sentence start says nothing about whether the word is a name.
    pattern = re.compile(rf"(?<=[a-z,;]\s)({re.escape(token)})\b", re.IGNORECASE)
    capital = lower = 0
    for text in texts:
        for match in pattern.finditer(text):
            word = match.group(1)
            if word[0].isupper():
                capital += 1
            elif word.islower():
                lower += 1
    return capital, lower


def run_labels(args):
    volumes = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(args.scripts, "*.json"))):
        if ".json." in os.path.basename(path) or path.endswith(".voice_config.json"):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(doc, list) and len(doc) >= args.min_entries:
            volumes[get_volume(path)].append((os.path.basename(path), doc))

    labels = {}
    for volume, runs in sorted(volumes.items()):
        texts = [e.get("text") or "" for _, doc in runs[:1] for e in doc]
        for run_name, doc in runs:
            counts = collections.Counter(
                (e.get("speaker") or "").strip().upper() for e in doc)
            for label, n in counts.items():
                if label in ("NARRATOR", ""):
                    continue
                row = labels.setdefault(label, {"label": label, "runs": {},
                                                "volumes": set(), "context": []})
                row["runs"][run_name] = n
                row["volumes"].add(volume)
                if len(row["context"]) < 3:
                    row["context"].extend(
                        e["text"][:160] for e in doc
                        if (e.get("speaker") or "").strip().upper() == label
                    )
                    row["context"] = row["context"][:3]
        # One text per volume is enough for capitalization: repeats share it.
        for row in labels.values():
            if volume in row["volumes"] and "case" not in row:
                head = re.findall(r"[A-Za-z][A-Za-z'-]+", row["label"])
                head = [w for w in head if w.upper() not in
                        ("THE", "A", "AN", "OF", "VOICE", "GROUP")]
                capital, lower = (get_case_counts(head[-1], texts)
                                  if head else (0, 0))
                row["case"] = {"word": head[-1] if head else None,
                               "capital": capital, "lower": lower}

    out = []
    for row in sorted(labels.values(), key=lambda r: -sum(r["runs"].values())):
        per_volume = collections.defaultdict(list)
        for run_name, n in row["runs"].items():
            per_volume[get_volume(run_name)].append(n)
        case = row.get("case") or {}
        capital, lower = case.get("capital", 0), case.get("lower", 0)
        out.append({
            "label": row["label"],
            "volumes": len(row["volumes"]),
            # The max over a volume's repeat runs: summing repeats counted the
            # same passages several times in the brief's 492.
            "lines_max_per_volume": {v: max(ns) for v, ns in sorted(per_volume.items())},
            "lines_by_run": row["runs"],
            "signals": {
                "structural": bool(_DESCRIPTIVE_OPENING.match(row["label"])
                                   or _NUMBERED.search(row["label"])
                                   or "(" in row["label"]),
                "generic_word": is_generic_speaker(_NUMBERED.sub("", row["label"])),
                "group": is_group_speaker_label(row["label"]),
                "case_word": case.get("word"),
                "case_capital": capital, "case_lower": lower,
                "case_says_basic": bool(lower > capital),
            },
            "context": row["context"]})
    return {"rows": out, "volumes": {v: [r for r, _ in runs]
                                     for v, runs in sorted(volumes.items())}}


def write_sheet(rows, path):
    import csv
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["label", "volumes", "max_lines_one_volume",
                         "structural", "generic_word", "group", "case_word",
                         "case_capital", "case_lower", "case_says_basic",
                         "YOUR_CLASS (named/basic/descriptor)",
                         "context_1", "context_2", "context_3"])
        for row in rows:
            s = row["signals"]
            ctx = row["context"] + [""] * (3 - len(row["context"]))
            writer.writerow([row["label"], row["volumes"],
                             max(row["lines_max_per_volume"].values()),
                             s["structural"], s["generic_word"], s["group"],
                             s["case_word"], s["case_capital"], s["case_lower"],
                             s["case_says_basic"], "", *ctx])


def run_ab(args):
    """Score the #653 A/B: every book x arm under background_speakers_ab.

    Named accuracy is reported twice. `strict` uses the gold aliases only.
    `cast_alias` also credits an answer whose group in the book's cast list
    holds the gold speaker (BRENDA LAST for gold BRENDA) - the SAME dictionary
    for every arm, so it cannot favour the cast arms by construction. Checked
    by hand on 2026-10-02: about 2 of 643 such credits join two people.
    """
    title_names = get_title_names()
    gold_index = load_gold_index(args.fixtures)
    rows = []
    for book in args.books:
        with open(os.path.join(args.fixtures, f"attribution_gold_{book}.json"),
                  encoding="utf-8") as handle:
            gold = json.load(handle)
        with open(os.path.join(args.casts, f"{book}.cast.json"),
                  encoding="utf-8") as handle:
            cast = json.load(handle)["cast"]
        groups = {c["name"].upper(): {c["name"].upper(),
                                      *(a.upper() for a in c.get("aliases", []))}
                  for c in cast}
        aliases, labels = gold_index["aliases"][book], gold_index["labels"][book]
        for arm in args.arms:
            checkpoint = load_generation_delta_checkpoint(os.path.join(
                args.runs, f"{book}__{arm}.json.threepass_checkpoint.json"))
            named = [e for e in checkpoint.get("named") or [] if e]
            if len(named) != len(checkpoint.get("segmented") or []):
                raise ValueError(f"{book}/{arm}: pass 2 incomplete")
            occurrences = collections.Counter(
                _normalize(e.get("text")) for e in checkpoint["segmented"])
            speakers = {}
            for entry in named:
                speakers.setdefault(_normalize(entry.get("text")), entry.get("speaker"))
            counts = collections.defaultdict(collections.Counter)
            for entry in gold["entries"]:
                key = _normalize(entry["line"])
                if occurrences.get(key) != 1 or key not in speakers:
                    continue
                expected = entry["expected_speaker"].upper().strip()
                predicted = (speakers[key] or "").upper().strip()
                strict = is_same_speaker(predicted, expected, aliases, labels)
                credited = strict or any(
                    is_same_speaker(name, expected, aliases, labels)
                    for name in groups.get(predicted, ()))
                gold_class = get_gold_class(book, expected, gold_index, title_names)
                gold_class = "named" if gold_class == "descriptor_of_named" else gold_class
                counts[gold_class]["n"] += 1
                counts[gold_class]["strict"] += strict
                counts[gold_class]["cast_alias"] += credited
            rows.append({"book": book, "arm": arm,
                         "model": (checkpoint.get("fingerprint") or {}).get("model_name"),
                         "located": sum(c["n"] for c in counts.values()),
                         "gold_rows": len(gold["entries"]),
                         **{cls: dict(c) for cls, c in counts.items()},
                         "unknown_lines": sum(1 for e in named if (e.get("speaker") or "").upper() == "UNKNOWN"),
                         "unchecked_lines": sum(1 for e in named if e.get("attribution_unchecked"))})
    summary = {}
    for arm in args.arms:
        total = collections.defaultdict(collections.Counter)
        for row in (r for r in rows if r["arm"] == arm):
            for cls in ("descriptive", "named", "title_name"):
                total[cls].update(row.get(cls, {}))
            total["lines"].update({"unknown": row["unknown_lines"],
                                   "unchecked": row["unchecked_lines"]})
        summary[arm] = {
            **{f"{cls}_{kind}_pct": round(100 * total[cls][kind] / total[cls]["n"], 1)
               for cls in ("descriptive", "named", "title_name")
               for kind in ("strict", "cast_alias") if total[cls]["n"]},
            **{f"{cls}_n": total[cls]["n"] for cls in ("descriptive", "named", "title_name")},
            "unknown_lines": total["lines"]["unknown"],
            "unchecked_lines": total["lines"]["unchecked"]}
    return {"rows": rows, "summary": summary}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Most PDNC gold is untracked (the annotations carry no licence), so a
    # worktree holds only a few books; point this at the main checkout's copy.
    parser.add_argument("--fixtures", default=os.path.join(APP, "fixtures"))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("eval")
    p.add_argument("--experiments", default=EXPERIMENTS)
    p.add_argument("--output", default=os.path.join(
        EXPERIMENTS, "background_speakers_eval.json"))
    p = sub.add_parser("threepass")
    p.add_argument("checkpoints", nargs="+")
    p.add_argument("--output", default=os.path.join(
        EXPERIMENTS, "background_speakers_threepass.json"))
    p = sub.add_parser("ab")
    p.add_argument("--runs", required=True, help="background_speakers_ab outputs")
    p.add_argument("--casts", required=True, help="build_cast_list.py outputs")
    p.add_argument("--books", nargs="+", required=True)
    p.add_argument("--arms", nargs="+", required=True)
    p.add_argument("--output", default=os.path.join(
        EXPERIMENTS, "background_speakers_ab.json"))
    p = sub.add_parser("labels")
    p.add_argument("--scripts", required=True)
    p.add_argument("--min-entries", type=int, default=200)
    p.add_argument("--output", required=True)
    p.add_argument("--sheet", required=True,
                   help="CSV for hand correction; holds book text, keep it "
                        "out of the repository")
    args = parser.parse_args(argv)

    if args.command == "labels":
        for target in (args.output, args.sheet):
            if os.path.realpath(target).startswith(os.path.realpath(REPO) + os.sep):
                parser.error(f"{target}: holds book text, write it outside the repo")
    doc = {"eval": run_eval, "threepass": run_threepass, "ab": run_ab,
           "labels": run_labels}[args.command](args)
    if args.command == "labels":
        write_sheet(doc["rows"], args.sheet)
    doc["provenance"] = provenance(__file__, args)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)
    print(json.dumps(doc.get("summary") or {"rows": len(doc["rows"])}, indent=1))


if __name__ == "__main__":
    main()
