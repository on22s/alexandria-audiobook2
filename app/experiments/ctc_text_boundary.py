"""Align known transcript lines to continuous audio with CTC segmentation."""
import argparse
import json
import os
import statistics
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asr_backends import build_alignment_probe, score_alignment
from utils import atomic_json_write
from experiments.provenance import provenance


def get_segments(texts, raw_segments):
    if len(texts) != len(raw_segments):
        raise ValueError("CTC returned a different number of segments than transcript lines")
    return [(float(start), float(end), text)
            for text, (start, end, _score) in zip(texts, raw_segments)]


def get_log_probs(output):
    return output.logits.log_softmax(dim=-1)


def align_lines(wav, texts, model_name, device):
    import librosa
    import torch
    import torchaudio
    from transformers import AutoModelForCTC, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModelForCTC.from_pretrained(model_name).to(device).eval()
    speech, _ = librosa.load(wav, sr=processor.feature_extractor.sampling_rate)
    inputs = processor(speech, sampling_rate=processor.feature_extractor.sampling_rate,
                       return_tensors="pt")
    with torch.inference_mode():
        logits = get_log_probs(model(inputs.input_values.to(device)))[0].cpu().numpy()

    token_lines = [processor.tokenizer(text, add_special_tokens=False).input_ids
                   for text in texts]
    targets = torch.tensor([sum(token_lines, [])], dtype=torch.int32)
    path, scores = torchaudio.functional.forced_align(
        torch.from_numpy(logits)[None], targets, blank=processor.tokenizer.pad_token_id)
    spans = torchaudio.functional.merge_tokens(path[0], scores[0],
                                                blank=processor.tokenizer.pad_token_id)
    if len(spans) != targets.shape[1]:
        raise RuntimeError(f"aligned {len(spans)} tokens for {targets.shape[1]} targets")
    frame_seconds = len(speech) / processor.feature_extractor.sampling_rate / len(logits)
    raw_segments, offset = [], 0
    for ids in token_lines:
        selected = spans[offset:offset + len(ids)]
        raw_segments.append((selected[0].start * frame_seconds,
                             selected[-1].end * frame_seconds,
                             statistics.mean(float(span.score) for span in selected)))
        offset += len(ids)
    return get_segments(texts, raw_segments)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--model", default="jonatasgrosman/wav2vec2-large-xlsr-53-japanese")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    build = json.load(open(args.build, encoding="utf-8"))
    rows = build["test"][:args.limit]
    probe = os.path.join(REPO, "ab_test_runtime", "ctc_boundary", f"ja_n{len(rows)}.wav")
    os.makedirs(os.path.dirname(probe), exist_ok=True)
    wav, truth = build_alignment_probe(rows, probe)
    predicted = align_lines(wav, [row["text"] for row in truth], args.model, args.device)
    result = score_alignment(truth, predicted)
    document = {"build": os.path.relpath(args.build, REPO), "limit": len(rows),
                "model": args.model, "device": args.device,
                "method": "known-text CTC segmentation", "alignment": result,
                "provenance": provenance(__file__, args)}
    atomic_json_write(document, args.out)
    print(json.dumps(document, indent=2, ensure_ascii=False))
    if not result.get("scored"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
