#!/usr/bin/env python3
"""Look at the clips a transcribe-back scored worst, beside what was heard.

WHY. Goal 6.5 asks that an audio arm's clips have a RENDERED VIEW before its
numbers are believed. On 2026-09-04 four sets of audio numbers were produced -
a stock-voice control, a transcribe-back over 2,834 clips, a wider run on book
text, and a prose/non-prose split reporting 32.08% WER on non-prose - and not
one clip had been looked at. "Non-prose is twenty times worse" was a number
with no mechanism behind it.

WHAT IT SHOWS. Waveform, the text the model was asked to say, and the text
whisper heard, for the worst-scoring clips of a tts_output_validation or
prose_vs_nonprose artifact. Reuses asr_clip_view's draw_clip rather than
drawing its own (Rule 15): two renderers would drift, and the point is to look
at audio the same way every time. It embeds the PNG only, exactly as that
viewer does - an earlier draft also called an `audio_tag` helper that does not
exist there, and both renders raised AttributeError AFTER the first one had
been reported as working. The first had simply had no text to lay out.
"""
import argparse
import html
import importlib.util
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _asr_view():
    """Import asr_clip_view for its drawing primitives, not its CLI."""
    path = os.path.join(REPO, "app", "experiments", "asr_clip_view.py")
    spec = importlib.util.spec_from_file_location("asr_clip_view", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rows_of(doc):
    """-> scoreable rows from either artifact shape."""
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("wav"):
            continue
        words = r.get("words") or 0
        if not words:
            continue
        out.append({
            "wav": r["wav"],
            "wer": (r.get("errors") or 0) / words,
            # `source` is what was asked. Older artifacts predate it and
            # stored only counts, so fall back to the expected side of the
            # per-error pairs - partial, and labelled as such by the caller.
            "reference": r.get("source") or r.get("text") or "",
            "hypothesis": r.get("transcript") or "",
            "detail": r.get("detail") if isinstance(r.get("detail"), list) else [],
            "cls": r.get("class") or "",
            "truncated": bool(r.get("possible_truncation")),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--clips", type=int, default=8)
    ap.add_argument("--pick", choices=("worst", "best", "truncated"),
                    default="worst")
    ap.add_argument("--audio-root", default="",
                    help="where the wavs live, if not beside this checkout")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    doc = json.load(open(args.artifact, encoding="utf-8"))
    rows = rows_of(doc)
    if not rows:
        sys.exit("no scoreable clips with audio in this artifact")
    if args.pick == "truncated":
        picked = [r for r in rows if r["truncated"]][:args.clips]
        if not picked:
            sys.exit("no clip in this artifact is marked truncated")
    else:
        rows.sort(key=lambda r: r["wer"], reverse=(args.pick == "worst"))
        picked = rows[:args.clips]

    view = _asr_view()
    name = os.path.basename(args.artifact)
    parts = ["<meta charset=utf-8><style>body{font-family:sans-serif;max-width:60em;"
             "margin:2em auto}figure{border-top:1px solid #ccc;padding-top:1em}"
             "td.k{color:#666;padding-right:1em;vertical-align:top}"
             ".cer{color:#a00;font-weight:normal}</style>",
             f"<h1>{html.escape(name)} — {args.pick} {len(picked)} of {len(rows)}</h1>"]
    for r in picked:
        wav = r["wav"] if os.path.isabs(r["wav"]) else os.path.join(REPO, r["wav"])
        if not os.path.exists(wav) and args.audio_root:
            wav = os.path.join(args.audio_root, os.path.basename(r["wav"]))
        label = f"WER {r['wer']*100:.1f}%"
        if r["cls"]:
            label += f" · {r['cls']}"
        if r["truncated"]:
            label += " · TRUNCATED"
        parts.append("<figure>")
        parts.append(f"<h3><span class=cer>{html.escape(label)}</span></h3>")
        if os.path.exists(wav):
            parts.append(f'<img src="data:image/png;base64,{view.draw_clip(wav)}">')
        else:
            parts.append("<p><em>audio not found</em></p>")
        parts.append("<table>"
                     f"<tr><td class=k>asked</td><td>{html.escape(r['reference'])}</td></tr>"
                     f"<tr><td class=k>heard</td><td>{html.escape(r['hypothesis'])}</td></tr>"
                     + "".join(
                         f"<tr><td class=k>{html.escape(str(e.get('kind')))}</td><td>"
                         f"{html.escape(str(e.get('expected')))} &rarr; "
                         f"<b>{html.escape(str(e.get('heard')))}</b></td></tr>"
                         for e in r["detail"][:6])
                     + "</table></figure>")
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    open(args.out, "w", encoding="utf-8").write("\n".join(parts))
    print(f"wrote {args.out} ({len(picked)} clips)")


if __name__ == "__main__":
    main()
