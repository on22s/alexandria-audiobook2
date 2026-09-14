"""Build PDNC-shaped adapter rows from RiQuA (Papay & Padó, LREC 2020).

RiQuA is 5,963 hand-annotated quotations in 15 documents from 11
19th-century works (Chekhov, Dickens, Doyle, Flaubert, Twain, Austen), each
with its speaker SPAN, addressee and cue, in brat format (`.txt` + `.ann`).
The paper says the corpus is "publicly available for use, modification, and
experimentation"; the tarball carries no licence file, and the texts are
public domain. That wording is recorded in the manifest as what was
verified - it is not a named licence.

What becomes a row:

- direct quotations only (span starts with a quote mark); the product
  never emits an indirect quotation as SPOKEN;
- speaker span that names ONE person - contains a capital letter, is not a
  pronoun, not a group or a clause, at most four words - so the label is the
  text's own, not a coreference guess (RiQuA reports 40% of speaker spans
  are pronouns; those rows are dropped). "The chemist" and "Homais" stay
  two roster names: they are the text's own labels and folding them would
  be a coreference guess;
- quotations nested inside another quotation are dropped;
- `austen_emma_*` is dropped: Emma is an evaluation fixture here.

Roster per document = the distinct named speaker and addressee spans, with
a span that is a whole-word part of exactly one longer span folded into it
(`Holmes` -> `Sherlock Holmes`). Context = the text unit before and after
the quotation, typed SPOKEN when it is another quotation and NARRATOR
otherwise, truncated to --context-chars; the same shape `dracor_trainset.py`
writes, so `distill_train.py` consumes it unchanged.
"""
import argparse
import collections
import glob
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))

PRONOUNS = {"he", "she", "i", "you", "they", "we", "him", "her", "it", "me",
            "us", "them", "himself", "herself", "myself", "one", "who"}
EXCLUDED_DOCS = ("austen_emma_1", "austen_emma_2", "austen_emma_3")
QUOTE_MARKS = "\"'“‘”’"
AVAILABILITY = ("paper: 'publicly available for use, modification, and "
                "experimentation' (Papay & Padó 2020, LREC); no licence file "
                "in riqua.tar.gz; source texts public domain")


def parse_ann(text):
    """-> (spans {id: (type, start, end, surface)}, relations [(type, arg1, arg2)])."""
    spans, rels = {}, []
    for line in text.splitlines():
        parts = line.rstrip("\n").split("\t")
        if parts[0].startswith("T"):
            typ, start, end = parts[1].split()[0], int(parts[1].split()[1]), int(parts[1].split()[-1])
            spans[parts[0]] = (typ, start, end, parts[2] if len(parts) > 2 else "")
        elif parts[0].startswith("R"):
            rel, a1, a2 = parts[1].split()
            rels.append((rel, a1.split(":", 1)[1], a2.split(":", 1)[1]))
    return spans, rels


PRONOUNS |= {"his", "hers", "their", "my", "your", "our"}
MAX_NAME_WORDS = 4


def clean_name(surface):
    return " ".join(surface.split()).strip(" .,;:" + QUOTE_MARKS)


def is_named(surface):
    """A span that names one person: has a capital letter, is not a pronoun,
    is not a group or a clause ("Mrs. Cratchit and the girls", "Charles, who
    was in bed"), and is at most MAX_NAME_WORDS long."""
    s = clean_name(surface)
    if not re.search(r"[A-Z]", s) or s.lower() in PRONOUNS:
        return False
    if re.search(r"(?<!\w)and(?!\w)|,", s) or len(s.split()) > MAX_NAME_WORDS:
        return False
    return True


def canonical_map(names):
    """Fold a name that is a whole-word part of exactly one longer name into it."""
    out = {}
    for n in names:
        longer = [m for m in names if m != n and re.search(r"(?<!\w)%s(?!\w)" % re.escape(n), m)]
        out[n] = longer[0] if len(longer) == 1 else n
    return out


def strip_quotes(s):
    return s.strip().strip(QUOTE_MARKS).strip()


def document_rows(txt, ann, doc, context_chars):
    spans, rels = parse_ann(ann)
    speaker_of = {q: e for rel, e, q in rels if rel == "Speaker"}
    addressees = {e for rel, e, q in rels if rel == "Addressee"}
    quotes = sorted((s, e, sid) for sid, (typ, s, e, _) in spans.items() if typ == "Quotation")
    # drop quotations nested in another quotation
    outer, last_end = [], -1
    for s, e, sid in quotes:
        if s < last_end:
            continue
        outer.append((s, e, sid))
        last_end = e
    names = {clean_name(spans[e][3]) for e in set(speaker_of.values()) | addressees
             if is_named(spans[e][3])}
    canon = canonical_map(names)
    roster = sorted({canon[n].upper() for n in names})
    # units: quotations and the narration between them, in document order
    units, pos = [], 0
    for s, e, sid in outer:
        gap = txt[pos:s].strip()
        if gap:
            units.append({"type": "NARRATOR", "text": gap, "id": None})
        units.append({"type": "SPOKEN", "text": strip_quotes(txt[s:e]), "id": sid, "raw": txt[s:e]})
        pos = e
    tail = txt[pos:].strip()
    if tail:
        units.append({"type": "NARRATOR", "text": tail, "id": None})
    counts = collections.Counter()
    labelled = {}
    for u in units:
        sid = u["id"]
        if sid and sid in speaker_of and u["raw"].lstrip()[:1] in QUOTE_MARKS:
            surface = clean_name(spans[speaker_of[sid]][3])
            if is_named(surface):
                labelled[sid] = canon[surface].upper()
                counts[labelled[sid]] += 1
    ranked = [n for n, _ in counts.most_common()]
    third = max(1, -(-len(ranked) // 3))
    category = {n: ("major" if i < third else "intermediate" if i < 2 * third else "minor")
                for i, n in enumerate(ranked)}
    rows, rejected = [], collections.Counter()
    for i, u in enumerate(units):
        if u["type"] != "SPOKEN":
            continue
        if u["id"] not in speaker_of:
            rejected["no_speaker"] += 1
        elif u["raw"].lstrip()[:1] not in QUOTE_MARKS:
            rejected["indirect"] += 1
        elif u["id"] not in labelled:
            rejected["pronoun_or_unnamed_speaker"] += 1
        else:
            before = units[i - 1] if i else {"type": "NARRATOR", "text": ""}
            after = units[i + 1] if i + 1 < len(units) else {"type": "NARRATOR", "text": ""}
            rows.append({
                "segment_index": i, "roster": roster,
                "context": [
                    {"type": before["type"], "text": before["text"][-context_chars:].strip()},
                    {"type": "SPOKEN", "text": u["text"], "target": True},
                    {"type": after["type"], "text": after["text"][:context_chars].strip()},
                ],
                "line": u["text"], "teacher": labelled[u["id"]],
                "speaker_category": category[labelled[u["id"]]],
                "quote_structure": "continuous", "book": "riqua_" + doc, "language": "en",
            })
    return rows, dict(rejected), roster


def main():
    from experiments.provenance import provenance
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--riqua", required=True, help="riqua/merged directory")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--context-chars", type=int, default=400)
    args = ap.parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    manifest = {"availability": AVAILABILITY, "excluded": list(EXCLUDED_DOCS),
                "author_overlap_note": "doyle_* share an author with the thesignofthefour "
                                       "evaluation fixture; different works",
                "documents": {}, "rows": 0, "provenance": provenance(__file__, args)}
    for txt_path in sorted(glob.glob(os.path.join(args.riqua, "*.txt"))):
        doc = os.path.basename(txt_path)[:-4]
        if doc in EXCLUDED_DOCS:
            continue
        txt = open(txt_path, encoding="utf-8").read()
        ann = open(txt_path[:-4] + ".ann", encoding="utf-8").read()
        rows, rejected, roster = document_rows(txt, ann, doc, args.context_chars)
        with open(os.path.join(args.out_dir, "train__riqua_%s.jsonl" % doc), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        manifest["documents"][doc] = {"rows": len(rows), "rejected": rejected, "roster": len(roster)}
        manifest["rows"] += len(rows)
        print("%-20s rows %4d roster %3d rejected %s" % (doc, len(rows), len(roster), rejected))
    with open(os.path.join(args.out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1)
    print("total rows", manifest["rows"])


if __name__ == "__main__":
    main()
