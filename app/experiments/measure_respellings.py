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

from utils import atomic_json_write  # noqa: E402

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

# THE -eh ROW IS THE TABLE'S WEAKEST CHOICE, and this is measured rather than
# suspected. Over 7,607 rescored terms, whole-word recovery is 15.0% overall
# but 6.6% for any word containing an -eh mora against 18.0% for words without
# one - and the gap survives a length control at every length:
#
#     kana length      with -eh      without
#          2             9.1%         28.5%
#          3             7.7%         19.5%
#          4             6.5%         15.2%
#          5             1.7%          6.7%
#
# A consistent 2.3-3.9x penalty on 2,031 terms. What English orthography an
# unadorned /e/ should get is not obvious - "seh" may read as a schwa - so the
# alternatives are measured rather than argued. `--e-spelling` swaps the row's
# vowel so the same pipeline can score each candidate on the same terms.
E_SPELLINGS = {"eh": "eh", "e": "e", "ay": "ay", "ei": "ei"}


def e_row(spelling):
    """-> MORA_SPELLING with the -eh row's vowel replaced by `spelling`."""
    if spelling == "eh":
        return dict(MORA_SPELLING)
    return {k: (v[:-2] + spelling if v.endswith("eh") and len(v) > 2
                else spelling if v == "eh" else v)
            for k, v in MORA_SPELLING.items()}
CARRIER = "She paused and said {word} before going on."
_ARGS = None


def respell(kana, table=None):
    """-> a hyphenated English-looking spelling, or None if unmappable.

    `table` defaults to MORA_SPELLING; --e-spelling supplies a variant so the
    -eh row can be measured against alternatives on identical terms.

    Returns None rather than a partial guess when a kana is not in the table:
    a half-respelled word is a new word, and would be measured as if it were
    the candidate.
    """
    table = table or MORA_SPELLING
    parts, index = [], 0
    while index < len(kana):
        char = kana[index]
        nxt = kana[index + 1] if index + 1 < len(kana) else ""
        if nxt in SMALL_Y and char in table:
            base = table[char]
            parts.append(base[0] + SMALL_Y[nxt])
            index += 2
            continue
        if char == "ッ":                       # geminate: doubles what follows
            following = kana[index + 1] if index + 1 < len(kana) else ""
            if following in table:
                parts.append(table[following][0])
            index += 1
            continue
        if char == "ー":                       # long vowel: hold the last one
            if parts:
                parts[-1] = parts[-1] + parts[-1][-1]
            index += 1
            continue
        if char not in table:
            return None
        parts.append(table[char])
        index += 1
    return "-".join(parts) if len(parts) >= 2 else None



# RULE B: fewer, more English-looking syllables. Rule A spells mora by mora,
# which gives `seh-n-seh-ee` where a person writing a pronunciation guide would
# write `sen-say`. Whether that matters is an empirical question, and the
# measurement of rule A answers half of it: respelling only helps where the
# plain form is already wrong (0-0.2 overlap band, +0.246 mean; the 0.8-1.0
# band scored 0% and -0.564). So rule B is tested ONLY on the terms rule A
# failed to rescue in that low band - the population where a respelling has
# something to fix and rule A did not fix it.
# Merged on rule A's OUTPUT, not on the kana. The first attempt matched kana
# pairs like エイ and never fired for センセイ, which is セ+イ - so it only
# stripped hyphens and produced `sehnsehee`, plainly worse. Merging the
# spellings instead catches the case that matters: a syllable ending in a
# vowel followed by a bare vowel is one English syllable.
# A RULE, NOT A TABLE. The first attempt listed pairs and fired for `seh`+`ee`
# while missing `pah`+`ee`, so senpai came back None. What actually happens is
# that a syllable's trailing vowel absorbs the following bare vowel, and that
# is expressible once:
#
#     seh + ee -> say      pah + ee -> pie      koh + oo -> koh
VOWEL_ABSORB = {("eh", "ee"): "ay", ("ah", "ee"): "ie", ("oh", "oo"): "oh",
                ("oo", "oo"): "oo", ("ee", "ee"): "ee", ("ah", "oo"): "ow"}


def respell_b(kana):
    """-> rule A with vowel sequences absorbed into English syllables.

    `seh-n-seh-ee` becomes `seh-n-say`, which is what a person writing a
    pronunciation guide would put. Whether the model cares is the question
    this rule exists to answer - rule A already showed that respelling helps
    only where the plain spelling is wrong, so this is tested only there.
    """
    base = respell(kana)
    if not base:
        return None
    parts, out, index = base.split("-"), [], 0
    while index < len(parts):
        current = parts[index]
        nxt = parts[index + 1] if index + 1 < len(parts) else None
        tail = current[-2:]
        if nxt is not None and (tail, nxt) in VOWEL_ABSORB:
            out.append(current[:-2] + VOWEL_ABSORB[(tail, nxt)])
            index += 2
            continue
        out.append(current)
        index += 1
    merged = "-".join(out)
    return merged if merged != base else None


def transcribe(wav, binary, model, language="ja"):
    out = subprocess.run([binary, "-m", model, "-f", wav, "-l", language,
                          "-np", "-nt"], capture_output=True, text=True,
                         timeout=180)
    return " ".join(out.stdout.split())


def scattered_overlap(expected, heard):
    """Fraction of the expected kana present ANYWHERE in the transcript.

    THIS WAS THE HEADLINE SCORE AND IT SHOULD NOT HAVE BEEN. It asks whether
    each character appears somewhere, in any order, so タナカ scores a perfect
    1.0 against a transcript holding タ, ナ and カ in three unrelated words.
    Re-scored over the 5,880-term run: of 768 terms scoring a perfect 1.0
    after respelling, only 51% actually contained the word. The other half
    were mispronunciations counted as successes -

        futaba  wanted フタバ  heard フォータバー  ("foh-tah-bah")  scored 1.00
        seiichi wanted セイイチ heard セイチー      (dropped a mora) scored 1.00
        saya    wanted サヤ    heard サイヤー      ("sai-yaa")      scored 1.00

    which are precisely the failures a pronunciation lexicon exists to catch.
    It reported respelling rescuing 38% of badly-pronounced words where
    `recovers_word` says 4%.

    KEPT, NOT DELETED, because the gap between the two is informative: a high
    scattered score with no contiguous match means "close but wrong", which
    calls for a different lexicon entry than "nothing like it". Record it,
    never lead with it.
    """
    if not expected:
        return 0.0
    return sum(1 for ch in expected if ch in heard) / len(expected)


def longest_common_run(expected, heard):
    """-> the longest run of `expected` appearing unbroken in `heard`, as a
    fraction. 1.0 means the whole word survived; 0.75 on セイイチ vs セイチー
    says three of its four morae did, which is a near miss rather than a miss.
    """
    if not expected:
        return 0.0
    best = 0
    for start in range(len(expected)):
        for end in range(len(expected), start + best, -1):
            if expected[start:end] in heard:
                best = end - start
                break
    return best / len(expected)


def score_recovery(expected_kana, heard):
    """Did the recognizer actually hear this word? -> dict of three views.

    COMPARES SOUNDS, NOT SPELLING. Both sides are reduced to their kana
    reading first, because a Japanese recognizer writes 人間 where the
    candidate list holds ニンゲン - identical words, zero character overlap.
    That normalization is `asr_backends.to_reading`, the same one that took
    Japanese CER from 28.7% to 9.9%; there is one implementation of it and
    this calls it rather than growing a second.

    Verified on eight hand-checked cases before being adopted: the four real
    recoveries (tanaka, chibi, shimizu, ningen) contain the expected reading
    unbroken, and the four false positives above do not. It separates them
    all.
    """
    from experiments.asr_backends import to_reading
    expected, transcript = to_reading(expected_kana), to_reading(heard or "")
    if expected is None or transcript is None:
        # pykakasi absent. Returning a character-scored number here would be
        # reporting a reading-scored metric that is nothing of the kind - the
        # exact failure that made the old score untrustworthy.
        raise RuntimeError(
            "cannot reduce text to readings (pykakasi unavailable); refusing "
            "to fall back to character scoring, which is the bug this "
            "replaces. pip install pykakasi")
    return {
        # The headline: the whole word, unbroken, in order.
        # `bool(expected)` guards a Python trap - "" is a substring of every
        # string, so a term whose reading reduces to nothing would otherwise
        # be scored as perfectly recovered.
        "recovers_word": bool(expected) and expected in transcript,
        # How much of it survived, for telling a near miss from a miss.
        "closeness": round(longest_common_run(expected, transcript), 3),
        # The old score, recorded for comparison only. See scattered_overlap.
        "scattered": round(scattered_overlap(expected, transcript), 3),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--candidates", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "lexicon_attributed.json"))
    ap.add_argument("--verdict", default="ja",
                    help="only measure terms attributed to this language")
    ap.add_argument("--min-books", type=int, default=20)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rule", choices=("a", "b"), default="a")
    ap.add_argument("--e-spelling", choices=tuple(E_SPELLINGS), default="eh",
                    help="what the -eh row's vowel becomes. Measured, not "
                         "assumed: words containing an -eh mora recover at "
                         "6.6%% against 18.0%% for words without one.")
    ap.add_argument("--only-e-row", action="store_true",
                    help="measure only terms containing an -eh mora, which is "
                         "the population any change to that row can affect")
    ap.add_argument("--only-failed", default=None,
                    help="a prior result file; measure only the terms whose "
                         "plain rendering did not produce the word and which "
                         "the prior rule failed to rescue")
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
    table = e_row(args.e_spelling)
    terms = [c for c in candidates
             if c.get("verdict") == args.verdict and c["books"] >= args.min_books]
    if args.only_e_row:
        e_kana = {k for k, v in MORA_SPELLING.items() if v.endswith("eh")}
        terms = [c for c in terms if any(ch in e_kana for ch in c["kana"])]
    terms.sort(key=lambda c: -c["books"])
    if args.only_failed and os.path.exists(args.only_failed):
        with open(args.only_failed, encoding="utf-8") as handle:
            prior = {r["term"]: r for r in json.load(handle)["results"]}
        # "Failed" means the WORD DID NOT COME OUT, not that a per-character
        # score fell under a threshold. The old filter used
        # `plain_kana_overlap < 0.2`, which both admitted terms the plain
        # rendering had actually said (scattered characters scoring high) and
        # excluded terms it had mangled (scattered characters scoring low on a
        # correct rendering). Prior files written before the rescoring carry
        # only the old field, so they are read through it rather than being
        # silently treated as failures.
        def failed(record):
            if "plain_recovers_word" in record:
                return not record["plain_recovers_word"]
            legacy = record.get("plain_kana_overlap")
            if legacy is None:
                return False
            return legacy < 1.0

        terms = [c for c in terms
                 if (p := prior.get(c["term"]))
                 and failed(p) and not p.get("helps")]
    if args.limit:
        terms = terms[:args.limit]
    if not terms:
        sys.exit("no candidates match those filters")

    global _ARGS
    _ARGS = args
    try:
        from experiments.provenance import provenance
        prov = provenance(__file__, args)
    except Exception as exc:                                    # noqa: BLE001
        prov = {"error": str(exc)[:120]}

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
        spelled = respell_b(kana) if args.rule == 'b' else respell(kana, table)
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
                for field, value in score_recovery(kana, heard).items():
                    row[f"{label}_{field}"] = value
            except Exception as exc:                        # noqa: BLE001
                row[f"{label}_error"] = str(exc)[:120]
        before, after = row.get("plain_recovers_word"), row.get("respelled_recovers_word")
        if before is not None and after is not None:
            # HELPING MEANS THE WORD CAME OUT, not that some of its characters
            # turned up somewhere. `closeness_delta` carries the graded view
            # for terms a respelling improved without fixing.
            row["helps"] = bool(after and not before)
            row["hurts"] = bool(before and not after)
            row["closeness_delta"] = round(
                row["respelled_closeness"] - row["plain_closeness"], 3)
        results[term] = row
        done += 1
        if done % 5 == 0:
            rate = (time.time() - started) / done
            left = (len(terms) - index) * rate
            print(f"  {index}/{len(terms)}  {rate:.1f}s/term  ~{left/60:.0f} min left",
                  flush=True)
            _write(args.out, results, terms, prov)
    _write(args.out, results, terms, prov)
    scored = [r for r in results.values() if "helps" in r]
    helped = [r for r in scored if r["helps"]]
    hurt = [r for r in scored if r.get("hurts")]
    print(f"\nmeasured {len(scored)} terms; a respelling recovered the word for "
          f"{len(helped)} and lost it for {len(hurt)}")
    for row in sorted(helped, key=lambda r: -r["closeness_delta"])[:15]:
        print(f"   +{row['closeness_delta']:.2f}  {row['term']:16} -> {row['respelling']}")
    return 0


def _write(path, results, terms, prov=None):
    document = {"carrier": CARRIER,
                "e_spelling": getattr(_ARGS, "e_spelling", "eh"),
                "note": "scored on READINGS, not characters: `recovers_word` is "
                        "the whole word present unbroken, which is the headline. "
                        "`scattered` is the retired per-character score, kept "
                        "only so the two can be compared - see scattered_overlap",
                "candidates_considered": len(terms),
                # COMPLETE OR PARTIAL, SAID OUT LOUD. _write is the checkpoint
                # and runs every five terms, so every artifact this produces is
                # partial right up until the last one - and nothing recorded
                # which. A 70-minute cap killed the n1200 block at 1129 of 1200
                # terms on 2026-08-18; the file looked exactly like a finished
                # one, was committed as evidence, and the chain's
                # skip-if-exists would have treated it as done forever.
                #
                # Truncation here is not a smaller sample, it is a BIASED one:
                # terms are taken in book-count order, so the missing tail is
                # the rarest words. A reader must not have to compare two
                # numbers in the file to notice that.
                "status": ("complete" if len(results) >= len(terms)
                           else "partial"),
                # Captured ONCE by the caller, not here: _write is the
                # checkpoint and runs every five terms, so recomputing git
                # state would cost a subprocess per checkpoint across a
                # seventeen-hour run.
                "provenance": prov,
                "results": list(results.values())}
    # Atomic: this file is read by other sessions while a multi-hour run is
    # still appending to it, and a plain open() truncates it for the duration
    # of the dump. Every mid-run read was a race until this line.
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    atomic_json_write(document, path)


if __name__ == "__main__":
    sys.exit(main())
