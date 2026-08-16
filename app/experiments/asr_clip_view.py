"""Look at the clips an ASR arm scored worst, next to what it heard.

WHY THIS EXISTS. Japanese sits at 28% CER on base, large-v3 and the hybrid
alike - three backends spanning a 20x model-size range landing within one
point of each other. That rules out model capacity and leaves two candidates
that a mean cannot tell apart:

    the audio does not match its transcript   (the dataset's alignment)
    the transcript is right and CER is wrong  (orthography: a model writing
                                               わたし where the reference has
                                               私 has the word right and
                                               scores as total failure)

The first is visible and audible in seconds; the second is only visible in
the text. So this puts both on one page: the waveform and spectrogram of the
clip, the reference, what the model actually returned, and the clip itself to
play.

NOT A METRIC, deliberately - the same position `voice_compare_view` takes,
whose drawing primitives this reuses rather than re-deriving. Eyes are for
catching the gross failure a number hides: a clip that is half silence, one
that starts mid-word, one whose transcript belongs to the sentence after it.

Self-contained HTML, audio inlined, openable anywhere.
"""
import argparse
import html
import io
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def draw_clip(path, sr=22050):
    """-> base64 PNG: waveform over mel-spectrogram for one clip."""
    import base64
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    import librosa
    from voice_compare_view import load_audio

    y, sr = load_audio(path, sr=sr)
    fig, axes = plt.subplots(2, 1, figsize=(11, 4.2), sharex=True,
                             gridspec_kw={"height_ratios": [1, 2]})
    times = np.arange(len(y)) / float(sr)
    axes[0].plot(times, y, linewidth=0.4, color="#2b6cb0")
    axes[0].set_ylabel("amplitude")
    axes[0].set_xlim(0, max(times[-1], 1e-3))
    # Silence at the head or tail is the signature of a boundary cut in the
    # wrong place, so keep the true amplitude scale rather than normalising it
    # away.
    axes[0].set_ylim(-1.0, 1.0)
    mel = librosa.feature.melspectrogram(y=y, sr=sr, n_mels=96)
    axes[1].imshow(librosa.power_to_db(mel, ref=np.max), origin="lower",
                   aspect="auto", cmap="magma",
                   extent=[0, max(times[-1], 1e-3), 0, 96])
    axes[1].set_ylabel("mel bin")
    axes[1].set_xlabel("seconds")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=84)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--artifact", required=True,
                    help="an asr_backends result written with --keep-hypotheses")
    ap.add_argument("--backend", default=None,
                    help="which arm to view (default: the first with hypotheses)")
    ap.add_argument("--clips", type=int, default=10)
    ap.add_argument("--pick", default="worst", choices=("worst", "best", "spread"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "asr_clip_view", "index.html"))
    args = ap.parse_args()

    with open(args.artifact, encoding="utf-8") as handle:
        doc = json.load(handle)
    arms = {name: rec for name, rec in doc.get("results", {}).items()
            if rec.get("hypotheses")}
    if not arms:
        sys.exit("no hypotheses in this artifact - rerun asr_backends with "
                 "--keep-hypotheses; a stored mean cannot be re-examined")
    name = args.backend or sorted(arms)[0]
    if name not in arms:
        sys.exit(f"{name} has no hypotheses; available: {sorted(arms)}")
    rows = sorted(arms[name]["hypotheses"], key=lambda r: r["wer"])
    if args.pick == "worst":
        picked = rows[::-1][:args.clips]
    elif args.pick == "best":
        picked = rows[:args.clips]
    else:
        step = max(1, len(rows) // max(args.clips, 1))
        picked = rows[::step][:args.clips]

    build = doc.get("build")
    wavs = {}
    if build:
        build_path = build if os.path.isabs(build) else os.path.join(REPO, build)
        if os.path.exists(build_path):
            with open(build_path, encoding="utf-8") as handle:
                wavs = {r["id"]: r["human_wav"] for r in json.load(handle)["test"]}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    from voice_compare_view import audio_tag
    parts = [
        "<style>body{font:14px/1.5 system-ui;margin:2rem;max-width:1100px}"
        "figure{margin:0 0 2.5rem}img{width:100%}"
        "table{border-collapse:collapse;width:100%;margin:.4rem 0}"
        "td{padding:.35rem .5rem;vertical-align:top;border-top:1px solid #ddd}"
        "td.k{width:7rem;color:#666}.cer{font-weight:600}</style>",
        f"<h1>{html.escape(name)} — {args.pick} {len(picked)} of {len(rows)} clips</h1>",
        f"<p>Reference against what the model returned. CER is character-level "
        f"for Japanese, so orthographic variation counts as error even when "
        f"the word is right.</p>",
    ]
    for row in picked:
        wav = wavs.get(row["id"])
        wav_path = os.path.join(REPO, wav) if wav else None
        parts.append("<figure>")
        parts.append(f"<h3>{html.escape(row['id'])} "
                     f"<span class=cer>CER {row['wer']*100:.1f}%</span></h3>")
        if wav_path and os.path.exists(wav_path):
            parts.append(f'<img src="data:image/png;base64,{draw_clip(wav_path)}">')
            parts.append(audio_tag(wav_path))
        else:
            parts.append("<p><em>audio not found for this id</em></p>")
        parts.append("<table>"
                     f"<tr><td class=k>reference</td><td>{html.escape(row['reference'])}</td></tr>"
                     f"<tr><td class=k>heard</td><td>{html.escape(row['hypothesis'])}</td></tr>"
                     "</table></figure>")
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(parts))
    print(f"wrote {args.out} ({len(picked)} clips from {name})")


if __name__ == "__main__":
    main()
