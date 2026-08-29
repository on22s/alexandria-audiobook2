"""Learn Mandarin tone contours from aligned human speech, including context."""
import argparse
import collections
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.provenance import provenance  # noqa: E402


def get_tones(text):
    from pypinyin import Style, pinyin
    return [int(item[0][-1]) if item[0][-1].isdigit() else 5
            for item in pinyin(text, style=Style.TONE3, neutral_tone_with_five=True)]


def get_contexts(tones):
    return [(tones[i - 1] if i else 0, tone,
             tones[i + 1] if i + 1 < len(tones) else 0)
            for i, tone in enumerate(tones)]


def get_unit_intervals(spans, duration, total_frames):
    centers = [(span.start + span.end) / 2 for span in spans]
    scale = duration / total_frames
    centers = [center * scale for center in centers]
    edges = [0.0] + [(left + right) / 2
                     for left, right in zip(centers, centers[1:])] + [duration]
    return list(zip(edges[:-1], edges[1:]))


def fit_means(rows, key):
    import numpy as np
    grouped = collections.defaultdict(list)
    for row in rows:
        grouped[row[key]].append(row["contour"])
    return {group: np.mean(values, axis=0).tolist() for group, values in grouped.items()}


def score(rows, means, key, fallback):
    import numpy as np
    correlations = []
    for row in rows:
        expected = means.get(row[key], fallback.get(row["tone"]))
        if expected is None:
            continue
        actual, expected = np.asarray(row["contour"]), np.asarray(expected)
        if actual.std() > 1e-9 and expected.std() > 1e-9:
            correlations.append(float(np.corrcoef(actual, expected)[0, 1]))
    return correlations


def get_audio_path(source, arm):
    return source.get("human_wav") if arm == "human" else source.get(f"{arm}_wav")


def extract_rows(document, model_name, device, limit, arm="human"):
    import librosa
    import numpy as np
    import torch
    import torchaudio
    from transformers import AutoModelForCTC, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModelForCTC.from_pretrained(model_name).to(device).eval()
    output = []
    for source in document["rows"][:limit or None]:
        relative = get_audio_path(source, arm)
        if not relative:
            continue
        path = os.path.join(REPO, relative)
        speech, rate = librosa.load(path, sr=processor.feature_extractor.sampling_rate)
        inputs = processor(speech, sampling_rate=rate, return_tensors="pt")
        with torch.inference_mode():
            emissions = model(inputs.input_values.to(device)).logits.log_softmax(-1).cpu()
        token_ids = processor.tokenizer(source["text"], add_special_tokens=False).input_ids
        tones = get_tones(source["text"])
        if len(token_ids) != len(tones):
            continue
        targets = torch.tensor([token_ids], dtype=torch.int32)
        path_ids, scores = torchaudio.functional.forced_align(
            emissions, targets, blank=processor.tokenizer.pad_token_id)
        spans = torchaudio.functional.merge_tokens(
            path_ids[0], scores[0], blank=processor.tokenizer.pad_token_id)
        if len(spans) != len(tones):
            continue
        f0 = librosa.pyin(speech, fmin=60, fmax=400, sr=rate)[0]
        f0_seconds = len(speech) / rate / len(f0)
        intervals = get_unit_intervals(spans, len(speech) / rate, emissions.shape[1])
        for tone, context, (start, end) in zip(tones, get_contexts(tones), intervals):
            chunk = f0[max(0, int(start / f0_seconds)):max(1, int(end / f0_seconds))]
            chunk = chunk[np.isfinite(chunk) & (chunk > 0)]
            if len(chunk) < 3:
                continue
            contour = np.interp(np.linspace(0, 1, 5), np.linspace(0, 1, len(chunk)),
                                12 * np.log2(chunk))
            contour -= contour.mean()
            output.append({"id": source["id"], "tone": tone,
                           "context": "-".join(map(str, context)),
                           "contour": contour.tolist()})
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generated", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--train", type=int, default=100)
    parser.add_argument("--arms", nargs="*", default=["clone", "lora"])
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--model", default="jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn")
    args = parser.parse_args()
    document = json.load(open(args.generated, encoding="utf-8"))
    rows = extract_rows(document, args.model, args.device, args.limit)
    train_ids = {row["id"] for row in document["rows"][:args.train]}
    train, test = [r for r in rows if r["id"] in train_ids], [r for r in rows if r["id"] not in train_ids]
    tone_means, context_means = fit_means(train, "tone"), fit_means(train, "context")
    tone_scores = score(test, tone_means, "tone", tone_means)
    context_scores = score(test, context_means, "context", tone_means)
    generated = {}
    for arm in args.arms:
        arm_rows = extract_rows(document, args.model, args.device, args.limit, arm=arm)
        held_out = [row for row in arm_rows if row["id"] not in train_ids]
        arm_scores = score(held_out, context_means, "context", tone_means)
        generated[arm] = {"units": len(held_out),
                          "contextual_correlation_mean": statistics.mean(arm_scores)
                          if arm_scores else None}
    result = {"source": os.path.relpath(args.generated, REPO), "model": args.model,
              "train_units": len(train), "test_units": len(test),
              "tone_only_correlation_mean": statistics.mean(tone_scores) if tone_scores else None,
              "contextual_correlation_mean": statistics.mean(context_scores) if context_scores else None,
              "contextual_groups": len(context_means),
              "generated": generated,
              "advance": bool(tone_scores and context_scores and
                              statistics.mean(context_scores) > statistics.mean(tone_scores)),
              "provenance": provenance(__file__, args)}
    from utils import atomic_json_write
    atomic_json_write(result, args.out)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
