"""How much of generate_script's output budget is JSON wrapper?

generate_script fails by truncation, not by malformed syntax: a chunk is
rejected when it stops covering the source. Every wrapper token spent on
{"speaker": ..., "text": ..., "instruct": ...} is a token not spent
reproducing the prose the gate measures. This counts, on real annotated
scripts, what the compact codec would give back.

Prints only. It writes no artifact, because a deterministic re-encoding of
existing output is not an experimental arm and does not belong in the
results index.
"""
import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
import line_format  # noqa: E402


def load_entries(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    entries = data if isinstance(data, list) else (
        data.get("entries") or data.get("script") or [])
    return [e for e in entries
            if isinstance(e, dict) and isinstance(e.get("text"), str)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", nargs="*", default=None,
                    help="annotated script JSON files (default: the active "
                         "script plus everything in scripts/)")
    ap.add_argument("--encoding", default="cl100k_base",
                    help="tiktoken encoding used as a PROXY; Qwen's tokenizer "
                         "is not available locally, so treat the ratio as "
                         "indicative and the absolute counts as approximate")
    args = ap.parse_args()

    paths = args.paths or ([os.path.join(REPO, "annotated_script.json")]
                           + sorted(glob.glob(os.path.join(REPO, "scripts", "*.json"))))
    import tiktoken
    enc = tiktoken.get_encoding(args.encoding)
    n = lambda s: len(enc.encode(s))

    totals = {"entries": 0, "pretty": 0, "emitted": 0, "compact": 0,
              "lines": 0, "text": 0}
    print("%-34s %7s %9s %9s %9s %7s" %
          ("script", "entries", "json-2sp", "json-min", "lines", "saved"))
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            entries = load_entries(path)
        except Exception as exc:
            print("  %-32s SKIPPED (%s)" % (os.path.basename(path)[:32], exc))
            continue
        if not entries:
            continue
        # A saving that loses content is not a saving. `text` must be
        # byte-exact - it is what the gate measures against the source. The
        # labels are compared after stripping, because the codec normalises
        # their surrounding whitespace on purpose; 6 of 3,813 entries in one
        # real script carry a trailing space in `instruct`, and dropping whole
        # books over that biased the corpus this measures.
        rendered = line_format.format_entries(entries)
        restored = line_format.parse_entries(rendered)
        if len(restored) != len(entries):
            print("  %-32s ENTRY COUNT CHANGED - not counted"
                  % os.path.basename(path)[:32])
            continue
        lost = [i for i, (a, b) in enumerate(zip(entries, restored))
                if (a.get("text") or "") != (b.get("text") or "")]
        if lost:
            print("  %-32s TEXT ALTERED on %d entries - not counted"
                  % (os.path.basename(path)[:32], len(lost)))
            continue
        normalised = sum(
            1 for a, b in zip(entries, restored)
            if any((a.get(k) or "").strip() != (b.get(k) or "").strip()
                   for k in ("speaker", "instruct")))
        if normalised:
            print("  %-32s %d label(s) differ beyond whitespace - not counted"
                  % (os.path.basename(path)[:32], normalised))
            continue
        # THE BASELINE IS WHAT THE MODEL EMITS, NOT WHAT THE PROMPT SHOWS.
        # All 135 CHUNK responses in logs/llm_responses.log come back as one
        # line with ", " / ": " separators - json.dumps defaults - never the
        # 2-space shape the prompt demonstrates. Measuring against the pretty
        # form would overstate the saving by roughly double.
        pretty = n(json.dumps(entries, ensure_ascii=False, indent=2))
        emitted = n(json.dumps(entries, ensure_ascii=False))
        compact = n(json.dumps(entries, ensure_ascii=False, separators=(",", ":")))
        lines = n(rendered)
        text = sum(n(e["text"]) for e in entries)
        totals["entries"] += len(entries); totals["pretty"] += pretty
        totals["emitted"] += emitted
        totals["compact"] += compact; totals["lines"] += lines; totals["text"] += text
        print("%-34s %7d %9d %9d %9d %6.1f%%" %
              (os.path.basename(path)[:34], len(entries), pretty, compact, lines,
               100.0 * (pretty - lines) / pretty))

    if not totals["entries"]:
        raise SystemExit("no scripts found to measure")
    p, em, c, l, t = (totals[k] for k in
                      ("pretty", "emitted", "compact", "lines", "text"))
    print("\nTOTAL over %d entries" % totals["entries"])
    print("  JSON, 2-space indent (the shape the prompt demonstrates) : %8d tok" % p)
    print("  JSON, as the model ACTUALLY emits it (one line, \", \")   : %8d tok" % em)
    print("  JSON, minified                                          : %8d tok" % c)
    print("  compact lines                                           : %8d tok" % l)
    print("  the prose itself (irreducible)                          : %8d tok" % t)
    print("\n  saving vs EMITTED JSON      : %.1f%%  (%d tokens)   <- the real one"
          % (100.0*(em-l)/em, em-l))
    print("  saving vs demonstrated JSON : %.1f%%  (%d tokens)" % (100.0*(p-l)/p, p-l))
    print("  saving vs minified JSON     : %.1f%%  (%d tokens)" % (100.0*(c-l)/c, c-l))
    print("  overhead left in lines      : %.1f%% of output is not prose"
          % (100.0*(l-t)/l))
    print("\n  NOTE: %s is a proxy tokenizer, not Qwen's." % args.encoding)


if __name__ == "__main__":
    main()
