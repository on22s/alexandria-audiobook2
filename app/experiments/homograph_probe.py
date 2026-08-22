#!/usr/bin/env python3
"""Does the voice say the right one when a spelling has two pronunciations?

WHY NOTHING HERE CAN ALREADY ANSWER THIS. "He read the book" and "He will read
the book" are the same five letters and two different words. A wrong reading
TRANSCRIBES IDENTICALLY, so ASR, WER and CER report a perfect result - the same
blindness goal 2.9 describes for Japanese pitch accent, in English, and
currently unmeasured. Speaker similarity (2.1) is an embedding distance and is
blind to it too. A listener is the only instrument that sees it.

THE PAIRING IS THE INSTRUMENT. Every word appears TWICE, in contexts that force
opposite readings. That buys two things one context cannot:

  - It separates "the model disambiguates" from "the model always says the same
    thing". A model that always reads REED scores 100% on the present-tense half
    and 0% on the past-tense half. One context alone would have called that a
    50% pass.
  - It catches the listener hearing what they expect. Someone marking "correct"
    from context rather than from sound marks BOTH halves correct, which is a
    pattern in the results rather than an invisible bias.

This script only RENDERS and records what was asked for. It does not score:
scoring needs the ear, and the ratings come back separately. Keeping the two
apart means the artifact cannot quietly acquire a verdict nobody listened to.
"""
import argparse
import json
import os
import random
import sys
import time

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

HERE = os.path.dirname(os.path.abspath(__file__))

from experiments.provenance import provenance


def load_words(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)["words"]


def build_items(words, seed):
    """-> one row per (word, reading), shuffled so pairs are not adjacent.

    ADJACENT PAIRS WOULD RUIN IT. Hearing "I read to the children" and then
    immediately "Last winter I read that book" invites comparison rather than
    judgement, and the second answer becomes a function of the first.
    """
    items = []
    for entry in words:
        for key in ("a", "b"):
            side = entry[key]
            items.append({
                "word": entry["word"],
                "reading": key,
                "expected_say": side["say"],
                "expected_gloss": side["gloss"],
                "other_say": entry["b" if key == "a" else "a"]["say"],
                "other_gloss": entry["b" if key == "a" else "a"]["gloss"],
                "sentence": side["sentence"],
            })
    rng = random.Random(seed)
    for _ in range(200):
        rng.shuffle(items)
        if all(items[i]["word"] != items[i + 1]["word"]
               for i in range(len(items) - 1)):
            break
    else:
        raise SystemExit("could not separate the pairs; change the seed")
    return items


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--words", default=os.path.join(HERE, "homograph_words.json"))
    ap.add_argument("--voice", default="serena")
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--work", default="ab_test_runtime/homograph_probe")
    ap.add_argument("--out", default="ab_test_runtime/experiments/homograph_probe.json")
    args = ap.parse_args()

    items = build_items(load_words(args.words), args.seed)
    os.makedirs(args.work, exist_ok=True)

    from tts import TTSEngine
    from experiments.generation import render
    engine = TTSEngine(json.load(open(os.path.join(APP, "config.json"),
                                      encoding="utf-8")))
    voice = {args.voice: {"type": "custom"}}

    started = time.time()
    for index, item in enumerate(items, 1):
        name = "%02d_%s_%s.wav" % (index, item["word"], item["reading"])
        path = os.path.join(args.work, name)
        item["clip"] = path
        if os.path.exists(path) and os.path.getsize(path) > 2000:
            item["rendered"] = "cached"
            continue
        try:
            render(engine, item["sentence"], "", args.voice, voice,
                   {"type": "custom"}, path)
            item["rendered"] = "ok"
        except Exception as exc:                            # noqa: BLE001
            # RECORD the failure into the artifact rather than returning a
            # plausible value for it ([[Rule 21]]): a missing clip must be
            # visible as missing, not silently absent from the round.
            item["rendered"] = "failed"
            item["error"] = str(exc)[:200]
        done = index
        rate = (time.time() - started) / done
        print("  %d/%d  %s %s  %.1fs/clip  ~%.0f min left" % (
            index, len(items), item["word"], item["reading"], rate,
            (len(items) - index) * rate / 60), flush=True)

    doc = {
        "what": "English heteronyms rendered in contexts that force opposite "
                "readings; whether the voice said the right one is a LISTENING "
                "question, and no verdict is recorded here",
        "why": "a wrong reading transcribes identically, so every automated "
               "measure in this repo reports success. Goal 2.9 makes the same "
               "argument for Japanese pitch accent; this is its English case.",
        "voice": args.voice,
        "seed": args.seed,
        "words": len(items) // 2,
        "clips": len(items),
        "rendered_ok": sum(1 for i in items if i.get("rendered") in ("ok", "cached")),
        "failed": sum(1 for i in items if i.get("rendered") == "failed"),
        "rated": False,
        "items": items,
        # Every artifact here identifies how it was produced. The guard in
        # test_producer_provenance caught its absence before this shipped.
        "provenance": provenance(__file__, args),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, indent=1, ensure_ascii=False)
    print("wrote %s  (%d ok, %d failed)" % (args.out, doc["rendered_ok"], doc["failed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
