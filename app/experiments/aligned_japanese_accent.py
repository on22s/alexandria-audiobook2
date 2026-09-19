"""Japanese pitch accent, scored on morae that were actually aligned.

The earlier fused number (expected_prosody_fusion.py, 53.2% direction
agreement) laid morae over voiced time at equal spacing and GOALS 2.9 records
it as falsified. This is the aligned version, the Japanese twin of
contextual_tone_model.py: every mora gets a start and end from a CTC forced
alignment, its f0 is read inside that interval, and the accent pattern the
text predicts is checked mora by mora.

WHY MMS_FA AND NOT A KANA MODEL. pyopenjtalk already gives each mora as
romanised phones (`橋を渡る` -> `h a sh i o w a t a r u`) and the MMS forced
aligner's vocabulary is lowercase latin letters, so the two meet with no kana
normalisation and no new model family. One download, CPU is enough.

THREE SCORES, KEPT APART. `drop` is the H->L fall after the accent nucleus -
the cue that separates 箸 from 橋. `rise` is the initial L->H of any phrase
whose nucleus is not the first mora. `correlation` is the whole H/L template
against the mora medians. Heiban (unaccented) phrases have no nucleus and
never enter `drop`; counting them there would reward a fall the text does
not predict.

The human Kokoro readings are scored first, as the ceiling. If the ceiling
is itself at chance the instrument is wrong, not the arms.
"""
import argparse
import collections
import json
import os
import re
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from experiments.provenance import provenance  # noqa: E402
from experiments.expected_prosody_fusion import get_japanese_template  # noqa: E402

VOWELS = ("a", "i", "u", "e", "o")
# Every phone pyopenjtalk emits, in the letters MMS_FA knows. Two-letter
# phones are two labels; N is the moraic nasal; cl is the geminate closure,
# which has no sound of its own and borrows the next consonant (see
# mora_units). A phone missing here skips the row rather than guessing.
PHONE_LABELS = {p: tuple(p) for p in (
    "a", "i", "u", "e", "o", "k", "g", "s", "z", "t", "d", "n", "h", "b",
    "p", "m", "y", "r", "w", "j", "f", "v", "ky", "gy", "sh", "ch", "ts",
    "ny", "hy", "by", "py", "my", "ry", "dy", "ty")}
PHONE_LABELS["N"] = ("n",)
# A mora is at least this long once the blanks after its token are counted.
TAIL = 0.12


def mora_units(text):
    """-> one entry per mora: {phones, labels, phrase, position}, or None.

    position is OpenJTalk's A: field - negative before the accent nucleus, 0
    at it, positive after - so a phrase whose positions never hit 0 is heiban.
    A mora is its vowel, a moraic N, or a cl closure; consonants ride on the
    vowel that follows them. Same rule as expected_prosody.japanese_accent.
    """
    import pyopenjtalk
    phones = []
    for label in pyopenjtalk.extract_fullcontext(text):
        phone = re.search(r"\-([^+]+)\+", label)
        found = re.search(r"/A:([+-]?\d+)", label)
        if not (phone and found) or phone.group(1) in ("sil", "pau"):
            continue
        phones.append((phone.group(1), int(found.group(1))))
    morae, pending = [], []
    for symbol, position in phones:
        core = symbol.lower()
        if core in VOWELS or symbol == "N" or core == "cl":
            morae.append({"phones": pending + [symbol], "position": position})
            pending = []
        else:
            pending.append(symbol)
    if pending:  # a consonant with no vowel: the text is not speakable as morae
        return None
    # cl borrows the following consonant so the aligner has a sound to find.
    for i, mora in enumerate(morae):
        if mora["phones"][-1].lower() == "cl":
            nxt = morae[i + 1]["phones"][0] if i + 1 < len(morae) else "t"
            nxt = nxt if nxt.lower() not in VOWELS and nxt != "N" else "t"
            mora["phones"] = [nxt]
    phrase = 0
    for i, mora in enumerate(morae):
        if i and mora["position"] != morae[i - 1]["position"] + 1:
            phrase += 1
        mora["phrase"] = phrase
        labels = []
        for symbol in mora["phones"]:
            key = symbol if symbol == "N" else symbol.lower()
            if key not in PHONE_LABELS:
                return None
            labels.extend(PHONE_LABELS[key])
        mora["labels"] = labels
        mora["phones"] = " ".join(mora["phones"])
    return morae


def phrases_of(morae):
    """-> [{moras, accent, start}] in mora_units order (accent 0 = heiban)."""
    out, start = [], 0
    for i, m in enumerate(morae):
        if i and m["phrase"] != morae[i - 1]["phrase"]:
            out.append(_phrase(morae[start:i], start))
            start = i
    out.append(_phrase(morae[start:], start))
    return out


def _phrase(group, start):
    positions = [m["position"] for m in group]
    return {"moras": len(group), "start": start,
            "accent": (positions.index(0) + 1) if 0 in positions else 0}


def score_phrase(phrase, medians):
    """-> {drop, rise, correlation} for one accent phrase, each None when the
    morae it needs were not voiced. `medians` are the phrase's mora f0 medians
    in semitones, None where unvoiced."""
    import numpy as np
    count, accent = phrase["moras"], phrase["accent"]
    drop = rise = corr = None
    if accent and accent < count and medians[accent - 1] is not None \
            and medians[accent] is not None:
        drop = medians[accent - 1] > medians[accent]
    if accent != 1 and count >= 2 and medians[0] is not None and medians[1] is not None:
        rise = medians[0] < medians[1]
    template = get_japanese_template([{"moras": count, "accent": accent}])
    keep = [i for i, v in enumerate(medians) if v is not None]
    if len(keep) >= 3:
        t = np.asarray([template[i] for i in keep], dtype="float64")
        m = np.asarray([medians[i] for i in keep], dtype="float64")
        if t.std() > 1e-9 and m.std() > 1e-9:
            corr = float(np.corrcoef(t, m)[0, 1])
    return {"drop": drop, "rise": rise, "correlation": corr}


def aggregate(rows):
    """-> per-arm totals over score_phrase results, Nones ignored."""
    drops = [p["drop"] for r in rows for p in r["phrases"] if p["drop"] is not None]
    rises = [p["rise"] for r in rows for p in r["phrases"] if p["rise"] is not None]
    corrs = [p["correlation"] for r in rows for p in r["phrases"]
             if p["correlation"] is not None]
    morae = sum(r["morae"] for r in rows)
    voiced = sum(r["morae_voiced"] for r in rows)
    return {"rows": len(rows),
            "phrases": sum(len(r["phrases"]) for r in rows),
            "drop_phrases": len(drops),
            "drop_agreement": (sum(drops) / len(drops)) if drops else None,
            "rise_phrases": len(rises),
            "rise_agreement": (sum(rises) / len(rises)) if rises else None,
            "correlation_phrases": len(corrs),
            "correlation_mean": statistics.mean(corrs) if corrs else None,
            "morae": morae, "morae_voiced": voiced,
            "coverage": (voiced / morae) if morae else None,
            "alignment_score_mean": statistics.mean(
                r["alignment_score"] for r in rows) if rows else None}


class Aligner:
    def __init__(self, device):
        import torch
        from torchaudio.pipelines import MMS_FA
        self.torch = torch
        self.device = device
        self.model = MMS_FA.get_model(with_star=False).to(device).eval()
        self.rate = MMS_FA.sample_rate
        self.ids = MMS_FA.get_dict(star=None)

    def align(self, speech, morae):
        """-> ([(start, end)] seconds per mora, mean frame log-prob)."""
        import torchaudio.functional as F
        torch = self.torch
        with torch.inference_mode():
            emissions, _ = self.model(torch.tensor(speech)[None].to(self.device))
            emissions = emissions.log_softmax(-1).cpu()
        labels = [l for m in morae for l in m["labels"]]
        targets = torch.tensor([[self.ids[l] for l in labels]], dtype=torch.int32)
        path, scores = F.forced_align(emissions, targets, blank=0)
        spans = F.merge_tokens(path[0], scores[0], blank=0)
        if len(spans) != len(labels):
            return None, None
        seconds = len(speech) / self.rate / emissions.shape[1]
        intervals, i = [], 0
        for m in morae:
            first, last = spans[i], spans[i + len(m["labels"]) - 1]
            intervals.append([first.start * seconds, (last.end + 1) * seconds])
            i += len(m["labels"])
        # CTC puts a token on two or three frames and leaves the rest of the
        # sound as blanks, so a bare vowel or N came out 40 ms long with one
        # f0 frame in it. The blanks up to the next mora belong to this one;
        # extend into them, capped so a pause is never counted as a vowel.
        for j, (start, end) in enumerate(intervals):
            limit = intervals[j + 1][0] if j + 1 < len(intervals) else end + TAIL
            intervals[j][1] = min(max(end, start + TAIL), limit)
        mean_score = float(sum(s.score for s in spans) / len(spans))
        return [tuple(iv) for iv in intervals], mean_score


def mora_medians(speech, rate, intervals):
    import librosa
    import numpy as np
    f0 = librosa.pyin(speech, fmin=60, fmax=400, sr=rate)[0]
    per_frame = len(speech) / rate / len(f0)
    out = []
    for start, end in intervals:
        chunk = f0[int(start / per_frame):max(int(start / per_frame) + 1, int(end / per_frame))]
        chunk = chunk[np.isfinite(chunk) & (chunk > 0)]
        out.append(float(np.median(12 * np.log2(chunk))) if len(chunk) >= 2 else None)
    return out


def score_row(aligner, source, arm, audio_root=REPO):
    """-> one scored row, or {"skipped": reason}."""
    import librosa
    relative = source.get("human_wav") if arm == "human" else source.get(f"{arm}_wav")
    if not relative:
        return {"id": source["id"], "skipped": "no audio"}
    morae = mora_units(source["text"])
    if not morae:
        return {"id": source["id"], "skipped": "unmapped"}
    speech, rate = librosa.load(os.path.join(audio_root, relative), sr=aligner.rate)
    intervals, align_score = aligner.align(speech, morae)
    if intervals is None:
        return {"id": source["id"], "skipped": "alignment"}
    medians = mora_medians(speech, rate, intervals)
    phrases = []
    for phrase in phrases_of(morae):
        span = medians[phrase["start"]:phrase["start"] + phrase["moras"]]
        phrases.append({**phrase, **score_phrase(phrase, span)})
    return {"id": source["id"], "wav": relative, "alignment_score": align_score,
            "morae": len(morae), "morae_voiced": sum(m is not None for m in medians),
            "intervals": [[round(s, 3), round(e, 3)] for s, e in intervals],
            "medians": [None if m is None else round(m, 2) for m in medians],
            "phrases": phrases}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--arms", nargs="*", default=["human", "clone", "lora"])
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--audio-root", default=REPO, dest="audio_root",
                        help="checkout the wav paths in --generated are relative to")
    args = parser.parse_args()
    document = json.load(open(args.generated, encoding="utf-8"))
    aligner = Aligner(args.device)
    arms, detail = {}, {}
    for arm in args.arms:
        rows, skipped = [], collections.Counter()
        for source in document["rows"][:args.limit or None]:
            row = score_row(aligner, source, arm, args.audio_root)
            if "skipped" in row:
                skipped[row["skipped"]] += 1
            else:
                rows.append(row)
        arms[arm] = {**aggregate(rows), "skipped": dict(skipped)}
        detail[arm] = rows
        print(arm, json.dumps(arms[arm]))
    result = {"source": os.path.relpath(args.generated, REPO), "language": "ja",
              "aligner": "torchaudio MMS_FA", "arms": arms, "rows": detail,
              "provenance": provenance(__file__, args)}
    from utils import atomic_json_write
    atomic_json_write(result, args.out)


if __name__ == "__main__":
    main()
