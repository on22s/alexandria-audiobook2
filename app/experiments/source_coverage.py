"""How much of the book actually reaches the script.

WHY NOTHING MEASURED THIS. `review_script.check_text_loss` compares the
review's OUTPUT against the review's INPUT - the batch the entries were built
from - so it is a tautology with respect to the book, and its 0.95 threshold
guards a comparison that structurally cannot see book-level loss. Goal 5.3's
`norm_text` deletes every non-alphanumeric character before pairing arms, so it
cannot see punctuation change either. Between them, no metric in this
repository has ever compared the script to the SOURCE.

WHAT IT FOUND, first run, seven books of the shipped single-pass arm: coverage
0.969 to 0.993, never 1.0. On `index18` that is 2,618 source words that never
reach the script - and the missing tokens are `the`, `a`, `her`, `said`, plus
character names (`kamijou` 57 times, `carissa` 29), so it is ordinary prose and
dialogue, not front matter being dropped on purpose.

WHAT THIS IS NOT. It is a WORD-MULTISET measure and it does not know where a
missing word went. A word absent here may have been deliberately dropped
(headers, page numbers), silently rewritten by the model, or lost at a chunk
seam. `missing_tokens` is reported per book precisely so a human can tell those
apart rather than trusting the ratio. Do not quote the ratio as "N% of the
prose is lost" without reading them.

ORDER IS NOT CHECKED EITHER. Two scripts with the same words in a different
order score identically. That is deliberate: the pipeline legitimately
reorders nothing, but it does re-chunk, and a stricter sequence measure would
report differences that are not losses.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402

# Keep CJK and kana alongside latin/digits: a Japanese or Chinese book must be
# measurable by the same function, and stripping them would score those books
# as total loss rather than reporting a number.
_WORD_SPLIT = re.compile(
    r"[^0-9A-Za-z぀-ヿ㐀-䶿一-鿿豈-﫿]+")


def words(text):
    """-> lowercased word list, punctuation collapsed to separators."""
    return [w for w in _WORD_SPLIT.sub(" ", (text or "").lower()).split() if w]


def script_words(entries):
    return words(" ".join(str(e.get("text") or "") for e in entries))


def coverage(source_text, entries):
    """-> (ratio, missing counter, extra counter, source_len, script_len).

    `missing` is what the source has and the script does not, counted by
    multiset so three lost copies of a word count three times. `extra` is the
    reverse and is reported too: a script LONGER than its source is a
    different failure - the model inventing prose - and a ratio alone hides it.
    """
    src = words(source_text)
    got = script_words(entries)
    cs, cg = collections.Counter(src), collections.Counter(got)
    covered = sum(min(count, cg[word]) for word, count in cs.items())
    missing = collections.Counter(
        {w: c - cg[w] for w, c in cs.items() if c > cg[w]})
    extra = collections.Counter(
        {w: c - cs[w] for w, c in cg.items() if c > cs[w]})
    ratio = covered / len(src) if src else 0.0
    return ratio, missing, extra, len(src), len(got)


def load_entries(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        return data
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ValueError("%s has no entries list" % path)
    return entries


def pair_sources_to_scripts(source_dir, script_dir, suffix):
    """-> [(book, source_path, script_path)] for every pair that exists.

    Pairing is by stem, with a `pdnc_` prefix tolerated on either side because
    the corpora are stored under both spellings. A source with no script is
    SKIPPED and reported, never silently treated as zero coverage.
    """
    def key(name):
        return os.path.splitext(name)[0].lower().replace("pdnc_", "")

    scripts = {}
    for name in os.listdir(script_dir):
        if not name.endswith(suffix):
            continue
        scripts.setdefault(key(name[:-len(suffix)]), os.path.join(script_dir, name))

    # The same book is stored under two spellings - `AHandfulOfDust.txt` and
    # `pdnc_ahandfulofdust.txt` are byte-identical - so pairing naively
    # reported 11 books where there are 7. A count that overstates how many
    # DISTINCT books a measurement covers is the kind of number that gets
    # quoted later, so duplicates are collapsed by content hash and the
    # alternate spellings recorded against the one they duplicate.
    pairs, unpaired, seen = [], [], {}
    duplicates = collections.defaultdict(list)
    for name in sorted(os.listdir(source_dir)):
        if not name.endswith(".txt"):
            continue
        script = scripts.get(key(name))
        if not script:
            unpaired.append(name)
            continue
        path = os.path.join(source_dir, name)
        with open(path, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        if digest in seen:
            duplicates[seen[digest]].append(name)
            continue
        stem = os.path.splitext(name)[0]
        seen[digest] = stem
        pairs.append((stem, path, script))
    return pairs, unpaired, dict(duplicates)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sources", required=True, help="directory of source .txt")
    ap.add_argument("--scripts", required=True, help="directory of script json")
    ap.add_argument("--suffix", default="__single.json",
                    help="script filename suffix identifying the arm")
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-missing", type=int, default=25)
    args = ap.parse_args(argv)

    pairs, unpaired, duplicates = pair_sources_to_scripts(
        args.sources, args.scripts, args.suffix)
    if not pairs:
        raise SystemExit(
            "no source/script pairs found for suffix %r - wrong directory?"
            % args.suffix)

    per_book, ratios = {}, []
    print("%-34s %9s %9s %9s %8s" % ("book", "source", "script", "missing", "cov"))
    for book, source_path, script_path in pairs:
        with open(source_path, encoding="utf-8", errors="replace") as fh:
            source_text = fh.read()
        ratio, missing, extra, n_src, n_got = coverage(
            source_text, load_entries(script_path))
        ratios.append(ratio)
        per_book[book] = {
            "source_words": n_src,
            "script_words": n_got,
            "missing_words": sum(missing.values()),
            "extra_words": sum(extra.values()),
            "coverage": round(ratio, 4),
            "arm": os.path.basename(script_path),
            # The whole point: a ratio without these cannot be interpreted.
            "missing_tokens": dict(missing.most_common(args.top_missing)),
            "extra_tokens": dict(extra.most_common(10)),
        }
        print("%-34s %9d %9d %9d %7.3f"
              % (book, n_src, n_got, sum(missing.values()), ratio))

    worst = min(per_book.items(), key=lambda kv: kv[1]["coverage"])
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "status": "complete",
            "what": "fraction of source words that reach the generated script",
            "provenance": provenance(__file__),
            "arm_suffix": args.suffix,
            "books": len(per_book),
            "coverage_min": round(min(ratios), 4),
            "coverage_max": round(max(ratios), 4),
            "coverage_mean": round(sum(ratios) / len(ratios), 4),
            "worst_book": worst[0],
            "per_book": per_book,
            "unpaired_sources": unpaired,
            "duplicate_sources": duplicates,
            "caveat": ("word-multiset, order-insensitive. A missing word may "
                       "have been dropped on purpose, rewritten by the model, "
                       "or lost at a chunk seam - `missing_tokens` is reported "
                       "so a human can tell those apart. Do not quote the "
                       "ratio as 'N% of the prose is lost' without reading "
                       "them."),
            "why_nothing_saw_this": (
                "review_script.check_text_loss compares the review's output to "
                "the review's INPUT, a tautology with respect to the book; "
                "three_pass_vs_single.norm_text strips every non-alphanumeric "
                "character before pairing. Neither can see book-level loss."),
        }, fh, indent=1, ensure_ascii=False)

    if duplicates:
        print("\n  collapsed %d duplicate source spelling(s): %s"
              % (sum(len(v) for v in duplicates.values()),
                 ", ".join("%s=%s" % (k, "/".join(v))
                           for k, v in list(duplicates.items())[:4])))
    print("\n  %d distinct books | coverage %.3f - %.3f (mean %.3f) | worst: %s"
          % (len(per_book), min(ratios), max(ratios),
             sum(ratios) / len(ratios), worst[0]))
    if unpaired:
        print("  %d source(s) had no matching script: %s"
              % (len(unpaired), ", ".join(unpaired[:5])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
