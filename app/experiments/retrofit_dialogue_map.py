"""Put the source-derived dialogue map onto scripts generated before it existed.

WHY. Until 1f6be7a a generated script recorded no fact about which of its lines
were speech, and the punctuation that might have implied it was removed
unevenly - 61% of quoted spans surviving on one book, 2% on another. Every
measurement that needed "is this line dialogue" had to guess from that
punctuation, and the guess was wrong three times in one day: apostrophes,
Japanese corner brackets, and the stripped book. `measure_dialogue_attribution`
found 22 spoken lines in a 6,173-entry book and reported 59.1%; it now refuses
such books, which is right, and is why it measures 1 of 29 saved books.

THE MAP DOES NOT COME FROM THE MODEL, so it does not need the model re-run.
`dialogue_spans.mark_entries` locates each entry's text in the SOURCE and reads
the convention from there. Measured on the worst case in the library -
arc4_volume10wn, the 2%-retention book - it still locates 89.4% of entries and
marks 770 spoken. So 29 books can be re-founded on evidence for the cost of
some CPU, instead of ~37 hours of regeneration.

IT DOES NOT TOUCH scripts/. That directory is the user's saved-book library,
not an experiment output. Retrofitted copies are written to a separate corpus
and the originals are left exactly as they are.

MATCHING A SCRIPT TO ITS SOURCE IS ITSELF A GUESS, so it is made falsifiable.
Filenames do not map ("Arc 1 - Volume 1.json" against 50 candidate .txt files)
and no manifest records the pairing, so the source is chosen by CONTENT: sample
entry texts, count how many appear verbatim in each candidate, and require a
clear winner. A script whose best candidate is weak, or which two sources match
equally well, is REFUSED and named - a wrongly paired book would produce a
confident map of the wrong novel, which is worse than no map at all.
"""
import argparse
import json
import os
import random
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(APP)
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "experiments"))

from dialogue_spans import detect_convention, mark_entries  # noqa: E402
from experiments.provenance import provenance  # noqa: E402

# A sampled text must be long enough to be distinctive: "Yes." appears in every
# novel ever written and would match all candidates equally.
MIN_SAMPLE_CHARS = 40

# MATCH ON A FORM NEITHER SIDE CAN HAVE ALTERED. The first version tested exact
# substrings and refused all 29 scripts at 0.0 containment - correctly refusing,
# but for the wrong reason: generation REMOVES the outermost quotes, so a line
# the model produced is not a substring of the source that produced it. That is
# the very defect being repaired here, and the matcher had walked straight into
# it. Comparing quote-stripped, whitespace-collapsed text asks the question the
# probe means to ask - is this the same prose - rather than "did this book
# survive our own pipeline unaltered".
_STRIP = dict.fromkeys(map(ord, '"\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u00ab\u00bb'), None)


def loose(text):
    return " ".join((text or "").translate(_STRIP).split())
SAMPLES = 40
MIN_CONTAINMENT = 0.50      # of sampled lines found verbatim in the winner
MIN_MARGIN = 2.0            # winner must beat the runner-up by this factor
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


def load_entries(path):
    with open(path, encoding="utf-8") as handle:
        doc = json.load(handle)
    if isinstance(doc, list):
        return [dict(e) for e in doc if isinstance(e, dict)]
    entries = doc.get("entries")
    if isinstance(entries, list):
        return [dict(e) for e in entries if isinstance(e, dict)]
    # Checkpoints keep their entries one level down, and the finished script for
    # a book can be absent while its checkpoint survives.
    out = []
    for chunk in doc.get("accepted_chunks") or []:
        for entry in chunk.get("entries") or []:
            if isinstance(entry, dict):
                out.append(dict(entry))
    return out


def samples_of(entries, rng):
    texts = [e.get("text") or "" for e in entries]
    long_enough = [t for t in texts if len(t) >= MIN_SAMPLE_CHARS]
    if len(long_enough) <= SAMPLES:
        return long_enough
    return rng.sample(long_enough, SAMPLES)


loose_sources = {}


def pick_source(entries, sources, rng):
    """-> (path, containment, margin) or (None, best, margin) when unclear."""
    probes = samples_of(entries, rng)
    if not probes:
        return None, 0.0, 0.0
    probes = [loose(p) for p in probes]
    probes = [p for p in probes if len(p) >= MIN_SAMPLE_CHARS]
    if not probes:
        return None, 0.0, 0.0
    scored = []
    for path, text in sources.items():
        hits = sum(1 for p in probes if p in loose_sources[path])
        scored.append((hits / len(probes), path))
    scored.sort(reverse=True)
    best, best_path = scored[0]
    runner = scored[1][0] if len(scored) > 1 else 0.0
    margin = best / runner if runner else float("inf")
    if best < MIN_CONTAINMENT or margin < MIN_MARGIN:
        return None, round(best, 4), (None if margin == float("inf")
                                      else round(margin, 3))
    return best_path, round(best, 4), (None if margin == float("inf")
                                       else round(margin, 3))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--scripts", default="scripts",
                    help="directory of generated scripts to retrofit (read-only)")
    ap.add_argument("--sources", action="append", default=[],
                    help="directory of source .txt files. Repeatable.")
    ap.add_argument("--out-dir", default=os.path.join(
        "ab_test_runtime", "retrofit_dialogue_map"))
    ap.add_argument("--audio-root", action="append", default=[])
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "retrofit_dialogue_map.json"))
    args = ap.parse_args()

    ROOTS[:] = [r for r in (args.audio_root or []) if r] or [REPO]
    rng = random.Random(args.seed)
    scripts_dir = resolve(args.scripts) or args.scripts
    if not os.path.isdir(scripts_dir):
        raise SystemExit(f"no such directory: {scripts_dir}")

    source_dirs = [resolve(d) or d for d in (args.sources or [
        os.path.join("ab_test_runtime", "results",
                     "collect_all_20260722-155801", "inputs")])]
    sources = {}
    for directory in source_dirs:
        if not directory or not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory)):
            if name.endswith(".txt"):
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8", errors="replace") as handle:
                    sources[path] = handle.read()
    if not sources:
        raise SystemExit(f"no source .txt files under {source_dirs}")
    # Normalised once, not once per script: 29 scripts against 8 sources would
    # otherwise re-strip several megabytes of prose 232 times.
    loose_sources.update({path: loose(text) for path, text in sources.items()})
    print(f"{len(sources)} candidate sources")

    out_dir = resolve(args.out_dir) or os.path.join(ROOTS[0], args.out_dir)
    os.makedirs(out_dir, exist_ok=True)

    rows, refused = [], []
    for name in sorted(os.listdir(scripts_dir)):
        if not name.endswith(".json"):
            continue
        if any(k in name for k in ("checkpoint", "quality", ".bak",
                                   "voice_config", "manifest")):
            continue
        path = os.path.join(scripts_dir, name)
        try:
            entries = load_entries(path)
        except (OSError, ValueError):
            continue
        if not entries:
            continue

        src_path, containment, margin = pick_source(entries, sources, rng)
        if not src_path:
            refused.append({"script": name, "best_containment": containment,
                            "margin": margin,
                            "reason": "no source matched clearly enough"})
            print(f"  REFUSED {name[:44]:44s} best containment {containment}")
            continue

        text = sources[src_path]
        convention = detect_convention(text)
        if not convention:
            refused.append({"script": name, "source": os.path.basename(src_path),
                            "reason": "convention could not be determined"})
            print(f"  REFUSED {name[:44]:44s} convention unknown")
            continue

        marked = mark_entries(entries, text, convention)
        located = sum(1 for e in marked if "spoken" in e)
        spoken = sum(1 for e in marked if e.get("spoken"))
        out_path = os.path.join(out_dir, name)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(marked, handle, ensure_ascii=False)
        rows.append({
            "script": name, "source": os.path.basename(src_path),
            "containment": containment, "margin": margin,
            "convention": convention, "entries": len(marked),
            "located": located, "spoken": spoken,
            "located_rate": round(located / len(marked), 4) if marked else None,
        })
        print(f"  {name[:44]:44s} {os.path.basename(src_path)[:22]:22s} "
              f"{located:5d}/{len(marked):<5d} = {100.0 * located / max(len(marked), 1):5.1f}% "
              f"located, {spoken} spoken")

    payload = {
        "scope": "source-derived dialogue map applied to scripts generated "
                 "before it existed; originals are not modified",
        "min_containment": MIN_CONTAINMENT, "min_margin": MIN_MARGIN,
        "seed": args.seed, "out_dir": os.path.relpath(out_dir, ROOTS[0]),
        "retrofitted": rows, "refused": refused,
        "status": "complete",
    }
    payload["provenance"] = provenance(__file__, args)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    print(f"\n  retrofitted {len(rows)}, refused {len(refused)}")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
