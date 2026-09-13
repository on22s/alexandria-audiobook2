"""Build speaker-balanced adapter training data from DraCor play scripts.

Plays come with the speaker of every line as part of the text itself
(`<sp who="#id">` in TEI), so the labels need no annotator and the sources are
public domain. The rows are shaped exactly like `pdnc_balanced_trainset.py`'s
so `distill_train.build_examples` consumes them unchanged.

Two things are deliberate:

- Speaker labels are stripped from the neighbouring lines a row sees as
  context. A play's alternation pattern is not a novel's (the alternation
  constraint was wrong 46% of the time on novels), so the adapter is only
  given the words, never who said the line before.
- Each corpus is fetched as the GitHub archive of its DraCor repository, not
  through the dracor.org API, which does not serve every corpus (the CC0
  Victorian corpus `lacy` is absent from it).
"""
import argparse
import collections
import hashlib
import io
import json
import os
import random
import re
import sys
import tarfile
import urllib.request
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.pdnc_balanced_trainset import balanced_sample  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

DEFAULT_ROOT = os.path.join(REPO, "ab_test_runtime", "corpora", "dracor")
NS = "{http://www.tei-c.org/ns/1.0}"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
ARCHIVE = "https://github.com/dracor-org/%sdracor/archive/refs/heads/main.tar.gz"
# Fallback when a TEI header carries no <langUsage>; DraCor corpus -> ISO 639-1.
CORPUS_LANGUAGE = {"lacy": "en", "am": "en", "indie": "en", "eng": "en",
                   "shake": "en", "ger": "de", "gersh": "de", "rus": "ru",
                   "dutch": "nl", "pol": "pl", "ibs": "no", "ar": "es",
                   "span": "es", "cal": "es", "ro": "ro", "u": "uk", "yi": "yi",
                   "fre": "fr", "ita": "it", "swe": "sv", "cze": "cs",
                   "hun": "hu", "greek": "el", "rom": "la", "neolat": "la"}
FREE_LICENCE = re.compile(r"publicdomain/zero|cc0|public.domain|licenses/by(-sa)?/",
                          re.IGNORECASE)
RESTRICTED_LICENCE = re.compile(r"by-nc|-nd\b|nc-sa", re.IGNORECASE)
# A <person> that is really a crowd or an unnamed role: lacy encodes
# "[Multiple speakers]", "[Servant]", "All.", "Omnes." this way, as persons.
COLLECTIVE = re.compile(r"^\[|^(ALL|OMNES|BOTH|CHORUS|SEVERAL)\b|\bVOICES?$")


def fetch_corpus(name, root):
    """Download one DraCor corpus archive into root/<name>; return its dir."""
    target = os.path.join(root, name)
    if os.path.isdir(os.path.join(target, "tei")):
        return target
    url = ARCHIVE % name
    with urllib.request.urlopen(url, timeout=120) as response:
        payload = response.read()
    os.makedirs(target, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = [m for m in archive.getmembers()
                   if "/tei/" in m.name and m.name.endswith(".xml")]
        for member in members:
            member.name = os.path.join("tei", os.path.basename(member.name))
        archive.extractall(target, members=members)
    with open(os.path.join(target, "archive.json"), "w", encoding="utf-8") as handle:
        json.dump({"url": url, "sha256": hashlib.sha256(payload).hexdigest(),
                   "tei_files": len(members)}, handle, indent=1)
    return target


BLOCK = {NS + "p", NS + "l", NS + "ab", NS + "lb", NS + "lg"}


def _text_without(element, skip):
    """Flatten an element's text, dropping the whole subtree of `skip` tags.

    Block children (verse lines, paragraphs) are joined with a space so two
    <l> lines do not run together into one word.
    """
    parts = [element.text or ""]
    for child in element:
        if child.tag not in skip:
            parts.append(" " if child.tag in BLOCK else "")
            parts.append(_text_without(child, skip))
        parts.append(child.tail or "")
    return "".join(parts)


def _clean(text):
    return re.sub(r"\s+", " ", text).strip()


def parse_play(xml_text):
    """Return a play's roster, ordered text units and rejection counts."""
    root = ET.fromstring(xml_text)
    title = _clean(_text_without(root.find(".//%stitleStmt/%stitle" % (NS, NS)), ()))
    language = root.find(".//%slangUsage/%slanguage" % (NS, NS))
    language = language.get("ident") if language is not None else None
    # A header may carry several <licence> elements, one per layer (GerDraCor:
    # CC0 for the TEI next to CC BY 3.0 for the TextGrid source text). The
    # target URL is the licence; the prose beside it often says "believed to
    # be in the public domain" whatever the URL grants. Any NC/ND layer
    # refuses the play; otherwise one free layer accepts it.
    licences = [(el.get("target") or _text_without(el, ()))
                for el in root.iter(NS + "licence")]
    if not licences:
        licence_ok = None
    elif any(RESTRICTED_LICENCE.search(x) for x in licences):
        licence_ok = False
    else:
        licence_ok = any(FREE_LICENCE.search(x) for x in licences)
    roster, groups = {}, set()
    for person in root.iter(NS + "person"):
        name = person.find(NS + "persName")
        name = _clean(_text_without(name, ())) if name is not None else ""
        name = name.rstrip(" .,;:").upper()
        if not person.get(XML_ID) or not name:
            continue
        if COLLECTIVE.search(name):
            groups.add(person.get(XML_ID))
        else:
            roster[person.get(XML_ID)] = name
    for group in root.iter(NS + "personGrp"):
        if group.get(XML_ID):
            groups.add(group.get(XML_ID))
    units, rejected = [], collections.Counter()

    def walk(element):
        # A speech is one unit and is not descended into, so a <stage> inside
        # it is dropped with the speaker label rather than becoming narration.
        for child in element:
            if child.tag == NS + "sp":
                text = _clean(_text_without(child, (NS + "speaker", NS + "stage")))
                label = child.find(NS + "speaker")
                label = _clean(_text_without(label, ())) if label is not None else ""
                # coyne-whatwilltheysay repeats "Jaco." inside the <p> after
                # the <speaker>; only a repeat of the speech's own label goes.
                if label and text.startswith(label):
                    text = text[len(label):].strip()
                if not text:
                    rejected["empty"] += 1
                    continue
                ids = [w.lstrip("#") for w in (child.get("who") or "").split()]
                unit = {"type": "SPOKEN", "text": text}
                if len(ids) != 1:
                    rejected["multi_speaker"] += 1
                elif ids[0] in groups:
                    rejected["group"] += 1
                elif ids[0] not in roster:
                    rejected["unknown_speaker"] += 1
                else:
                    unit["who"] = ids[0]
                units.append(unit)
            elif child.tag == NS + "stage":
                text = _clean(_text_without(child, ()))
                if text:
                    units.append({"type": "NARRATOR", "text": text})
            else:
                walk(child)

    walk(root.find(".//%sbody" % NS))
    return {"title": title, "language": language, "licence_ok": licence_ok,
            "licences": licences, "roster": roster, "groups": groups, "units": units,
            "rejected": dict(rejected)}


def speaker_categories(units):
    """Rank speakers by share of speeches into major/intermediate/minor."""
    counts = collections.Counter(u["who"] for u in units
                                 if u["type"] == "SPOKEN" and u.get("who"))
    ranked = [who for who, _ in counts.most_common()]
    third = max(1, -(-len(ranked) // 3))
    return {who: ("major" if i < third else
                  "intermediate" if i < 2 * third else "minor")
            for i, who in enumerate(ranked)}


def play_rows(play, corpus, play_name, context_chars):
    """Return one adapter row per attributable speech, PDNC-shaped."""
    roster = sorted(set(play["roster"].values()))
    categories = speaker_categories(play["units"])
    language = play["language"] or CORPUS_LANGUAGE.get(corpus, "und")
    units, rows = play["units"], []
    for index, unit in enumerate(units):
        if unit["type"] != "SPOKEN" or not unit.get("who"):
            continue
        before = units[index - 1] if index else {"type": "NARRATOR", "text": ""}
        after = units[index + 1] if index + 1 < len(units) else {"type": "NARRATOR", "text": ""}
        rows.append({
            "segment_index": index, "roster": roster,
            "context": [
                {"type": before["type"], "text": before["text"][-context_chars:].strip()
                 if context_chars else ""},
                {"type": "SPOKEN", "text": unit["text"], "target": True},
                {"type": after["type"], "text": after["text"][:context_chars].strip()},
            ],
            "line": unit["text"], "teacher": play["roster"][unit["who"]],
            "speaker_category": categories[unit["who"]],
            "quote_structure": "continuous",
            "book": "dracor_%s_%s" % (corpus, play_name), "language": language,
        })
    return rows


def language_round_robin(rows_by_language, total, rng):
    """Take rows one language at a time so every language gets an equal share."""
    pools = {language: list(rows) for language, rows in rows_by_language.items()}
    for rows in pools.values():
        rng.shuffle(rows)
    selected = []
    while len(selected) < total and any(pools.values()):
        for language in sorted(pools):
            if pools[language] and len(selected) < total:
                row = dict(pools[language].pop())
                row["language"] = language
                selected.append(row)
    rng.shuffle(selected)
    return selected


def _token_counter():
    try:
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-14B")
        return "qwen3_tokens", lambda text: len(tokenizer(text)["input_ids"])
    except Exception:  # noqa: BLE001 - the count is descriptive, chars will do
        return "chars", len


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpora", nargs="+", required=True,
                        help="DraCor corpus names, e.g. lacy am ger rus")
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--per-play", type=int, default=60)
    parser.add_argument("--per-speaker", type=int, default=10)
    parser.add_argument("--total-rows", type=int, default=4000)
    parser.add_argument("--context-chars", type=int, default=400)
    parser.add_argument("--max-roster", type=int, default=120,
                        help="skip plays with more named characters than this; "
                             "PDNC's largest novel roster is 113, and Kraus's "
                             "Die letzten Tage der Menschheit has 810")
    parser.add_argument("--seed", type=int, default=20260913)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    by_language, plays, skipped = collections.defaultdict(list), [], []
    for corpus in args.corpora:
        folder = fetch_corpus(corpus, args.root)
        for filename in sorted(os.listdir(os.path.join(folder, "tei"))):
            if not filename.endswith(".xml") or filename == "corpus.xml":
                continue
            with open(os.path.join(folder, "tei", filename), encoding="utf-8") as handle:
                play = parse_play(handle.read())
            name = filename[:-4]
            if play["licence_ok"] is False or not 2 <= len(play["roster"]) <= args.max_roster:
                skipped.append({"corpus": corpus, "play": name,
                                "reason": "licence" if play["licence_ok"] is False else "roster",
                                "roster": len(play["roster"])})
                continue
            rows = balanced_sample(play_rows(play, corpus, name, args.context_chars),
                                   args.per_play, args.per_speaker, rng)
            if not rows:
                skipped.append({"corpus": corpus, "play": name, "reason": "no_rows"})
                continue
            by_language[rows[0]["language"]].extend(rows)
            plays.append({"corpus": corpus, "play": name, "title": play["title"],
                          "language": rows[0]["language"],
                          "licence": play["licence_ok"], "licences": play["licences"],
                          "candidate_rows": len(rows),
                          "rejected_speeches": play["rejected"]})
    selected = language_round_robin(by_language, args.total_rows, rng)

    os.makedirs(args.out_dir, exist_ok=True)
    by_book = collections.defaultdict(list)
    for row in selected:
        by_book[row["book"]].append(row)
    for book, rows in sorted(by_book.items()):
        with open(os.path.join(args.out_dir, "train__%s.jsonl" % book), "w",
                  encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    unit, count = _token_counter()
    per_language = collections.defaultdict(lambda: {"rows": 0, "plays": set(), unit: 0})
    for row in selected:
        entry = per_language[row["language"]]
        entry["rows"] += 1
        entry["plays"].add(row["book"])
        entry[unit] += count(" ".join(part["text"] for part in row["context"]))
    for entry in per_language.values():
        entry["plays"] = len(entry["plays"])
    for play in plays:
        play["rows"] = len(by_book.get("dracor_%s_%s" % (play["corpus"], play["play"]), []))
    document = {"seed": args.seed, "corpora": args.corpora, "per_play": args.per_play,
                "per_speaker": args.per_speaker, "total_rows": len(selected),
                "requested_rows": args.total_rows, "context_chars": args.context_chars,
                "max_roster": args.max_roster,
                "languages": dict(per_language), "plays": plays, "skipped": skipped,
                "provenance": provenance(__file__, args)}
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
    print(json.dumps({"total_rows": len(selected), "plays": len(by_book),
                      "languages": {k: v["rows"] for k, v in per_language.items()},
                      "skipped": len(skipped)}, indent=1))


if __name__ == "__main__":
    main()
