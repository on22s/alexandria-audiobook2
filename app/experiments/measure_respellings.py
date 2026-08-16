"""Does a respelling make the TTS say the word right? Measured, term by term.

WHAT THIS IS FOR. pronunciation.json ships its entries EMPTY on purpose: a
respelling is added when it has been measured to help, never because it looked
right. The corpus scan produced 9,381 candidate terms. This is the measurement
that decides which of them deserve an entry.

HOW A TERM IS JUDGED. The word is rendered twice inside the same carrier
sentence - once as the book spells it, once respelled - and both are
transcribed with a JAPANESE ASR. The question is not "was the English
intelligible" but "did the Japanese ASR hear the Japanese word", so the
comparison is against the term's kana reading:

    kawaii            -> コイイ      wrong
    kah-wah-ee        -> カワイイ    right, and that is a real improvement

AND WHY NOT WER. The module docstring of pronunciation.py already warns about
this and it is worth repeating where the measuring happens: scoring the
transcript against the ORIGINAL text punishes a respelling that works, because
a correctly-pronounced Japanese word transcribes as Japanese rather than as
the Latin spelling the book used. So the score here is kana agreement with the
expected reading, not word error rate against the sentence.

WHAT IT CANNOT SETTLE. Whether the result sounds natural in an English
sentence, or merely accurate. An ASR agreeing is evidence that the phonemes
moved the right way; it is not a listener. Entries are proposed here and
confirmed by ear.

RESPELLINGS ARE DERIVED, NOT INVENTED. Each mora of the kana reading maps to
an English-looking spelling through one table, so the same input always gives
the same candidate and a bad rule can be corrected in one place rather than
per word.

CHECKPOINTED, because this runs for hours over thousands of terms and will be
interrupted. Every term's result is written as it completes.
"""
import argparse
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Kana -> an English reader's spelling. Deliberately blunt: the aim is to stop
# an English-trained model applying English orthography to a Japanese word,
# not to encode a phonetic alphabet.
MORA_SPELLING = {
    "ア": "ah", "イ": "ee", "ウ": "oo", "エ": "eh", "オ": "oh",
    "カ": "kah", "キ": "kee", "ク": "koo", "ケ": "keh", "コ": "koh",
    "サ": "sah", "シ": "shee", "ス": "soo", "セ": "seh", "ソ": "soh",
    "タ": "tah", "チ": "chee", "ツ": "tsoo", "テ": "teh", "ト": "toh",
    "ナ": "nah", "ニ": "nee", "ヌ": "noo", "ネ": "neh", "ノ": "noh",
    "ハ": "hah", "ヒ": "hee", "フ": "foo", "ヘ": "heh", "ホ": "hoh",
    "マ": "mah", "ミ": "mee", "ム": "moo", "メ": "meh", "モ": "moh",
    "ヤ": "yah", "ユ": "yoo", "ヨ": "yoh",
    "ラ": "rah", "リ": "ree", "ル": "roo", "レ": "reh", "ロ": "roh",
    "ワ": "wah", "ヲ": "oh", "ン": "n",
    "ガ": "gah", "ギ": "gee", "グ": "goo", "ゲ": "geh", "ゴ": "goh",
    "ザ": "zah", "ジ": "jee", "ズ": "zoo", "ゼ": "zeh", "ゾ": "zoh",
    "ダ": "dah", "ヂ": "jee", "ヅ": "zoo", "デ": "deh", "ド": "doh",
    "バ": "bah", "ビ": "bee", "ブ": "boo", "ベ": "beh", "ボ": "boh",
    "パ": "pah", "ピ": "pee", "プ": "poo", "ペ": "peh", "ポ": "poh",
}
SMALL_Y = {"ャ": "ya", "ュ": "yu", "ョ": "yo"}
CARRIER = "She paused and said {word} before going on."


def respell(kana):
    """-> a hyphenated English-looking spelling, or None if unmappable.

    Returns None rather than a partial guess when a kana is not in the table:
    a half-respelled word is a new word, and would be measured as if it were
    the candidate.
    """
    parts, index = [], 0
    while index < len(kana):
        char = kana[index]
        nxt = kana[index + 1] if index + 1 < len(kana) else ""
        if nxt in SMALL_Y and char in MORA_SPELLING:
            base = MORA_SPELLING[char]
            parts.append(base[0] + SMALL_Y[nxt])
            index += 2
            continue
        if char == "ッ":                       # geminate: doubles what follows
            following = kana[index + 1] if index + 1 < len(kana) else ""
            if following in MORA_SPELLING:
                parts.append(MORA_SPELLING[following][0])
            index += 1
            continue
        if char == "ー":                       # long vowel: hold the last one
            if parts:
                parts[-1] = parts[-1] + parts[-1][-1]
            index += 1
            continue
        if char not in MORA_SPELLING:
            return None
        parts.append(MORA_SPELLING[char])
        index += 1
    return "-".join(parts) if len(parts) >= 2 else None


def transcribe(wav, binary, model, language="ja"):
    out = subprocess.run([binary, "-m", model, "-f", wav, "-l", language,
                          "-np", "-nt"], capture_output=True, text=True,
                         timeout=180)
    return " ".join(out.stdout.split())


def kana_overlap(expected, heard):
    """Fraction of the expected kana present in what was transcribed.

    Crude on purpose. A stricter alignment would imply a precision this
    comparison does not have - the ASR is transcribing one word inside an
    English sentence, and its segmentation of that is not reliable.
    """
    if not expected:
        return 0.0
    return sum(1 for ch in expected if ch in heard) / len(expected)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidates", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_attributed.json"))
    ap.add_argument("--verdict", default="ja",
                    help="only measure terms attributed to this language")
    ap.add_argument("--min-books", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--whisper-cpp-bin", default=os.path.join(
        REPO, "whisper.cpp", "build", "bin", "whisper-cli"))
    ap.add_argument("--whisper-cpp-model", default=os.path.join(
        REPO, "whisper.cpp", "models", "ggml-base.bin"))
    ap.add_argument("--work", default=os.path.join(
        REPO, "ab_test_runtime", "respelling_measure"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "respelling_measure.json"))
    args = ap.parse_args()

    with open(args.candidates, encoding="utf-8") as handle:
        candidates = json.load(handle)["candidates"]
    terms = [c for c in candidates
             if c.get("verdict") == args.verdict and c["books"] >= args.min_books]
    terms.sort(key=lambda c: -c["books"])
    if args.limit:
        terms = terms[:args.limit]
    if not terms:
        sys.exit("no candidates match those filters")

    os.makedirs(args.work, exist_ok=True)
    results = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as handle:
                results = {r["term"]: r for r in json.load(handle)["results"]}
        except (ValueError, KeyError):
            results = {}

    from tts import TTSEngine
    from experiments.generation import render
    engine = TTSEngine(json.load(open(os.path.join(APP, "config.json"),
                                      encoding="utf-8")))
    voice = {"serena": {"type": "custom"}}
    started, done = time.time(), 0

    for index, candidate in enumerate(terms, 1):
        term, kana = candidate["term"], candidate["kana"]
        if term in results:
            continue
        spelled = respell(kana)
        if not spelled:
            results[term] = {"term": term, "kana": kana, "skipped": "unmappable kana"}
            continue
        row = {"term": term, "kana": kana, "respelling": spelled,
               "books": candidate["books"], "series": candidate["series"]}
        for label, word in (("plain", term), ("respelled", spelled)):
            path = os.path.join(args.work, f"{term}_{label}.wav")
            try:
                if not os.path.exists(path):
                    render(engine, CARRIER.format(word=word), "", "serena",
                           voice, {"type": "custom"}, path)
                heard = transcribe(path, args.whisper_cpp_bin, args.whisper_cpp_model)
                row[f"{label}_heard"] = heard
                row[f"{label}_kana_overlap"] = round(kana_overlap(kana, heard), 3)
            except Exception as exc:                        # noqa: BLE001
                row[f"{label}_error"] = str(exc)[:120]
        before = row.get("plain_kana_overlap")
        after = row.get("respelled_kana_overlap")
        if before is not None and after is not None:
            row["delta"] = round(after - before, 3)
            row["helps"] = after > before
        results[term] = row
        done += 1
        if done % 5 == 0:
            rate = (time.time() - started) / done
            left = (len(terms) - index) * rate
            print(f"  {index}/{len(terms)}  {rate:.1f}s/term  ~{left/60:.0f} min left",
                  flush=True)
            _write(args.out, results, terms)
    _write(args.out, results, terms)
    scored = [r for r in results.values() if "delta" in r]
    helped = [r for r in scored if r["helps"]]
    print(f"\nmeasured {len(scored)} terms; a respelling helped {len(helped)}")
    for row in sorted(helped, key=lambda r: -r["delta"])[:15]:
        print(f"   +{row['delta']:.2f}  {row['term']:16} -> {row['respelling']}")
    return 0


def _write(path, results, terms):
    document = {"carrier": CARRIER,
                "note": "kana agreement, not WER: a working respelling makes "
                        "the ASR hear Japanese, which WER would punish",
                "candidates_considered": len(terms),
                "results": list(results.values())}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)


if __name__ == "__main__":
    sys.exit(main())
