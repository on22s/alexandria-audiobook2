"""Extract Japanese quotations from Aozora text, ready for attribution annotation.

WHY. Every method result in GOALS.md is measured on ENGLISH prose. The four
"Japanese light novels" are English translations - `attribution_gold_mushoku16`
line 0 reads "In the future when things get tough..." - so an application built
for Japanese audiobooks has never been evaluated on a Japanese sentence. The
one annotated Japanese corpus, BCCWJ-SpeakersInfo, is behind a NINJAL
registration. Aozora Bunko is public domain and needs no permission.

WHAT THIS IS AND IS NOT. This extracts quotations with their context and
records what local evidence sits beside each one. It does NOT assign speakers:
there is no gold here and nothing in the output may be scored as if there
were. It produces the annotation-ready half, plus a measurement that is
interesting on its own - how often Japanese prose names the speaker beside the
quote at all, which is the analogue of PDNC's Explicit category and is not
known for this language in this project.

WHY THE SHAPE DIFFERS FROM THE OTHER READERS. RiQuA and WP2021 hand out one
quote per line or per numbered window. Japanese literary prose embeds quotes
INSIDE a paragraph - 「愉快ですね」と私は大きな声を出した - so quotes are
located by character offset in the whole text, and context is a character
window either side, matching the 3200-character convention the PDNC w3200
fixtures use.

A PROPERTY THIS LANGUAGE HAS AND THE OTHERS DO NOT. First-person narration is
pervasive: in `kokoro` the narrator is 私 and speaks constantly. A pronoun
subject is a legitimate ANSWER in Japanese in a way it is not in the Chinese
corpora, so `pronoun_subject` is counted separately rather than discarded -
deciding it is noise would throw away the commonest case in the corpus.
"""
import argparse
import collections
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

# Speech verbs in the forms that actually follow a quote in literary Japanese.
# Kept explicit rather than stemmed: a stemmer would need a tokeniser, and the
# closed set of post-quote forms is small and checkable by eye.
SPEECH_VERBS = (
    "言った", "云った", "いった", "答えた", "訊いた", "聞いた", "尋ねた",
    "叫んだ", "呟いた", "つぶやいた", "囁いた", "続けた", "笑った",
    "応えた", "返した", "告げた", "話した", "語った", "呼んだ", "怒鳴った",
    "言う", "云う", "いう", "答える", "訊く", "聞く", "尋ねる", "叫ぶ",
    "言いました", "答えました", "聞きました",
)
# Bare pronouns that can head a subject. In Japanese these are frequently the
# CORRECT answer, unlike in the Chinese corpora, so they are labelled not cut.
PRONOUNS = ("私", "僕", "俺", "わたし", "ぼく", "おれ", "彼", "彼女", "自分")
QUOTE = re.compile(r"「([^「」]*)」")
# `と` plus a subject marker is the canonical post-quote attribution frame.
ATTRIB_AFTER = re.compile(r"^\s*と(.{0,24}?)(?:は|が|も)")
# Japanese puts the subject BEFORE the quote at least as often as after:
#     ...姿勢を改めた先生は、「もう帰りませんか」といって私を促した
# A first version of this looked only after the quote and scored that row
# `none`, which made "no local evidence" read 70.7% when it was really "no
# POST-quote frame". Hand-reading eight `none` rows is what exposed it.
ATTRIB_BEFORE = re.compile(r"([^\s。、「」]{1,12})(?:は|が)[、\s]*$")


def strip_aozora(text):
    """-> the body, with Aozora's header, ruby and annotation markup removed.

    Ruby (漢字《かんじ》), input notes ［＃...］ and the ｜ ruby anchor all sit
    INSIDE quotations, so leaving them in corrupts the quote text itself
    rather than merely adding noise around it.
    """
    text = re.sub(r"《[^》]*》", "", text)
    text = re.sub(r"［＃[^］]*］", "", text)
    text = text.replace("｜", "")
    # The header ends at the first rule of full-width dashes; some files have
    # none, so fall back to the whole text rather than returning nothing.
    parts = re.split(r"-{10,}\n", text)
    if len(parts) >= 3:
        text = parts[2]
    return text


def classify_mention(mention):
    """-> `pronoun_subject` or `named_subject` for a subject string."""
    if any(mention.startswith(p) or mention == p for p in PRONOUNS):
        return "pronoun_subject"
    return "named_subject"


def local_evidence(after, before=""):
    """-> (kind, mention, position) for the evidence sitting beside a quote.

    kind is `named_subject`, `pronoun_subject`, `cue_only` or `none`;
    position is `after`, `before` or `""`. The post-quote frame is checked
    first because it is the less ambiguous of the two - a subject before the
    quote may belong to the surrounding narration rather than to the speech.
    """
    match = ATTRIB_AFTER.match(after)
    if match:
        mention = match.group(1).strip()
        if mention:
            return classify_mention(mention), mention, "after"
    before_match = ATTRIB_BEFORE.search(before)
    if before_match:
        mention = before_match.group(1).strip()
        if mention:
            return classify_mention(mention), mention, "before"
    if any(after.lstrip().startswith("と" + v) or after[:12].find(v) >= 0
           for v in SPEECH_VERBS):
        return "cue_only", "", "after"
    return "none", "", ""


def extract(text, window):
    """-> [entry] for every 「」 quotation, in order of appearance."""
    body = strip_aozora(text)
    entries = []
    for index, match in enumerate(QUOTE.finditer(body)):
        start, end = match.span()
        after = body[end:end + 40]
        kind, mention, position = local_evidence(after, body[max(0, start - 30):start])
        entries.append({
            "id": "%05d" % index,
            "line": match.group(0),
            "quote_text": match.group(1),
            # NO expected_speaker: this file carries no gold and must not be
            # scored as if it did.
            "local_evidence": kind,
            "evidence_mention": mention,
            "evidence_position": position,
            "prev_context": body[max(0, start - window):start],
            "next_context": body[end:end + window],
            "offset": start,
        })
    return entries


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--texts", nargs="+", required=True,
                    help="Aozora .txt files (Shift-JIS or UTF-8)")
    ap.add_argument("--out", required=True, help="directory for skeletons")
    ap.add_argument("--summary", required=True, help="coverage artifact path")
    ap.add_argument("--window", type=int, default=3200)
    args = ap.parse_args(argv)

    os.makedirs(args.out, exist_ok=True)
    per_work, combined = {}, collections.Counter()
    for path in args.texts:
        raw = None
        for encoding in ("utf-8", "cp932", "shift_jis"):
            try:
                with open(path, encoding=encoding) as fh:
                    raw = fh.read()
                break
            except UnicodeDecodeError:
                continue
        if raw is None:
            raise SystemExit("could not decode %s as UTF-8 or Shift-JIS" % path)

        stem = os.path.splitext(os.path.basename(path))[0]
        entries = extract(raw, args.window)
        if not entries:
            raise SystemExit("%s yielded no 「」 quotations - wrong file?" % path)
        counts = collections.Counter(e["local_evidence"] for e in entries)
        combined.update(counts)
        per_work[stem] = {"quotes": len(entries), **dict(counts)}
        with open(os.path.join(args.out, "aozora_quotes_%s.json" % stem),
                  "w", encoding="utf-8") as fh:
            json.dump({
                "work": stem,
                "language": "ja",
                "source": "Aozora Bunko (public domain)",
                "provenance": provenance(__file__),
                "status": "UNANNOTATED - no speakers assigned, not gold",
                "entries": entries,
                "local_evidence_counts": dict(counts),
            }, fh, indent=1, ensure_ascii=False)
        print("  %-12s %4d quotes  %s" % (stem, len(entries), dict(counts)))

    total = sum(combined.values())
    with open(args.summary, "w", encoding="utf-8") as fh:
        json.dump({
            "status": "complete",
            "what": "how much local speaker evidence Japanese prose puts beside a quote",
            "provenance": provenance(__file__),
            "not_gold": ("no speakers are assigned here. These are extraction "
                         "skeletons for annotation, and nothing in them may be "
                         "scored as if it were a gold label."),
            "quotes_total": total,
            "per_work": per_work,
            "local_evidence": dict(combined),
            "local_evidence_share": {k: round(v / total, 4)
                                     for k, v in combined.items()},
            "reading": ("named_subject is the closest analogue of PDNC's "
                        "Explicit category. pronoun_subject is counted apart "
                        "because in Japanese a first-person pronoun is often "
                        "the CORRECT answer, not a failure to name anyone."),
        }, fh, indent=1, ensure_ascii=False)

    print("\n  %d quotations across %d works" % (total, len(per_work)))
    for kind, n in combined.most_common():
        print("    %-16s %5d  %5.1f%%" % (kind, n, 100 * n / total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
