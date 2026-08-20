"""What each generation arm does to the text, which 5.3's metric cannot see.

WHY 5.3 NEEDS THIS. That goal answered "is three-pass better?" with attribution
accuracy alone - single 45.5%/58.0% against three-pass 40.3%/40.6% - and
concluded "do not ship three-pass for accuracy". The accuracy numbers are fine.
The problem is what they are computed on: `three_pass_vs_single.norm_text` is

    re.sub(r"[^0-9a-z]+", "", text.lower())

so every quote mark, underscore, dash and apostrophe is deleted before the two
arms are paired. The metric is STRUCTURALLY BLIND to any change in those
characters, and three-pass makes exactly such a change deliberately: on a
fully-quoted line it takes `text[1:-1]` and records `stripped_dialogue_delimiters`.

Measured over the very artifacts 5.3's verdict was computed from, three-pass
does not merely strip some quotes, it strips ALL of them:

    index18            single 460 quoted entries of 3832   three-pass 0 of 2479
    mushoku16          single 657 of 4044                  three-pass 0 of 2056
    owarimonogatari3   single 1033 of 4701                 three-pass 0 of 3929

So the arms differ in 2,150 entries in a way the comparison could not report.

WHICH CHARACTERS ACTUALLY MATTER, measured at the speech boundary rather than
assumed - `speech_text.normalize_for_speech` is what the engine receives:

  "   SURVIVES to the engine. Not in SPEECH_BREAKS. So single-pass sends quote
      characters to TTS and three-pass sends none: a real difference in what is
      synthesised, not only in what is readable.
  _   REMOVED, and replaced by a SENTENCE BREAK. `He said _hello_ softly.`
      reaches the engine as `He said. hello. softly.` - three sentences where
      the author wrote one. It is in SPEECH_BREAKS, so this happens for BOTH
      arms and is not a differentiator between them; it is a separate finding
      about emphasis markup, and a prosody change rather than a deletion.
  -   SURVIVES unchanged. Not a differentiator and not altered.

WHAT THIS PROBE REPORTS. Per book and per arm: how many entries carry each
character class, how many survive `normalize_for_speech`, and the delta between
arms. It makes no accuracy claim and does not re-run any model - it reads
committed scripts.

WHAT IT DOES NOT SETTLE. Whether a listener prefers quotes reaching the engine.
That is goal 7.1's question and needs ears, not a counter.
"""
import argparse
import collections
import json
import os
import re
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from speech_text import normalize_for_speech, SPEECH_BREAKS  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

QUOTES = '"“”「」«»'
CLASSES = {
    "quote": lambda t: any(c in t for c in QUOTES),
    "underscore": lambda t: "_" in t,
    "hyphen_or_dash": lambda t: bool(re.search(r"[-–—―]", t)),
    "asterisk": lambda t: "*" in t,
}
ROOTS = [REPO]


def resolve(path):
    if not path:
        return None
    if os.path.isabs(path):
        return path
    for root in ROOTS:
        candidate = os.path.join(root, path)
        if os.path.exists(candidate):
            return candidate
    return None


def entries_of(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    return doc if isinstance(doc, list) else (doc.get("entries") or [])


def profile(path):
    """-> per-class counts before and after the speech boundary."""
    counts = collections.Counter()
    entries = entries_of(path)
    n = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text") or entry.get("line") or ""
        if not text:
            continue
        n += 1
        try:
            spoken = normalize_for_speech(text)
            spoken = spoken if isinstance(spoken, str) else str(spoken)
        except Exception:                                  # noqa: BLE001
            spoken = text
        for name, test in CLASSES.items():
            if test(text):
                counts[name] += 1
                if test(spoken):
                    counts[name + "_survives_tts"] += 1
    return n, counts


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # RELATIVE BY DEFAULT so the root fallback below can apply. An absolute
    # default built from REPO resolves to the worktree, where the 5.3 run's
    # outputs do not exist - they are untracked and live in the main checkout.
    ap.add_argument("--work", default=os.path.join(
        "ab_test_runtime", "three_pass_vs_single"),
        help="directory holding <book>__<arm>.json from the 5.3 run")
    ap.add_argument("--audio-root", action="append", default=[],
                    help="extra roots to resolve --work against")
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "script_text_fidelity.json"))
    args = ap.parse_args()

    ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    work = resolve(args.work) or args.work
    if not os.path.isdir(work):
        raise SystemExit(f"no such directory: {work}")

    found = collections.defaultdict(dict)
    for name in sorted(os.listdir(work)):
        m = re.match(r"^(?P<book>.+?)__(?P<arm>single|three_pass)\.json$", name)
        if not m:
            continue
        found[m.group("book")][m.group("arm")] = os.path.join(work, name)
    if not found:
        raise SystemExit(f"no <book>__<arm>.json files in {work}")

    books = {}
    for book, arms in sorted(found.items()):
        entry = {}
        for arm, path in sorted(arms.items()):
            n, counts = profile(path)
            entry[arm] = {"entries": n, **{k: counts.get(k, 0) for k in
                                           list(CLASSES) +
                                           [c + "_survives_tts" for c in CLASSES]}}
        # The delta is the point: 5.3 compared these arms and could not see it.
        if "single" in entry and "three_pass" in entry:
            entry["delta_vs_single"] = {
                name: entry["three_pass"][name] - entry["single"][name]
                for name in CLASSES}
        books[book] = entry

    payload = {
        "scope": "what each generation arm does to punctuation, and how much "
                 "of it reaches the TTS engine; no model is run",
        "speech_breaks": SPEECH_BREAKS,
        "note_norm_text_is_blind": (
            "three_pass_vs_single.norm_text deletes every non-alphanumeric "
            "character before pairing lines, so goal 5.3's accuracy figures "
            "cannot reflect anything measured here"),
        "books": books,
        "status": "complete",
    }
    payload["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)

    width = max(len(b) for b in books) + 2
    print(f"{'book':{width}s} {'arm':11s} {'entries':>8s} " +
          " ".join(f"{c:>15s}" for c in CLASSES))
    for book, arms in books.items():
        for arm in ("single", "three_pass"):
            if arm not in arms:
                continue
            a = arms[arm]
            cells = " ".join(
                f"{a[c]:>7d}/{a[c + '_survives_tts']:<7d}" for c in CLASSES)
            print(f"{book:{width}s} {arm:11s} {a['entries']:8d} {cells}")
    print("\n  each cell is 'entries containing it' / 'still containing it at "
          "the TTS boundary'")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
