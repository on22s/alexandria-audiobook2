"""Align known transcript lines to continuous audio with CTC segmentation."""
import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from asr_backends import build_alignment_probe, score_alignment
from utils import atomic_json_write


def get_segments(texts, raw_segments):
    if len(texts) != len(raw_segments):
        raise ValueError("CTC returned a different number of segments than transcript lines")
    return [(float(start), float(end), text)
            for text, (start, end, _score) in zip(texts, raw_segments)]


def align_lines(wav, texts, model_name, device):
    import librosa
    import torch
    from ctc_segmentation import (CtcSegmentationParameters, ctc_segmentation,
                                  determine_utterance_segments,
                                  prepare_text)
    from transformers import AutoModelForCTC, AutoProcessor

    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModelForCTC.from_pretrained(model_name).to(device).eval()
    speech, _ = librosa.load(wav, sr=processor.feature_extractor.sampling_rate)
    inputs = processor(speech, sampling_rate=processor.feature_extractor.sampling_rate,
                       return_tensors="pt")
    with torch.inference_mode():
        logits = model(inputs.input_values.to(device)).log_softmax(dim=-1)[0].cpu().numpy()

    config = CtcSegmentationParameters(char_list=processor.tokenizer.convert_ids_to_tokens(
        range(model.config.vocab_size)))
    config.index_duration = len(speech) / processor.feature_extractor.sampling_rate / len(logits)
    ground_truth, utt_begin = prepare_text(config, texts)
    timings, char_probs, state_list = ctc_segmentation(config, logits, ground_truth)
    segments = determine_utterance_segments(config, utt_begin, char_probs, timings, texts)
    return get_segments(texts, segments)


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
    wav, truth = build_alignment_probe(rows, probe)
    predicted = align_lines(wav, [row["text"] for row in truth], args.model, args.device)
    result = score_alignment(truth, predicted)
    document = {"build": os.path.relpath(args.build, REPO), "limit": len(rows),
                "model": args.model, "device": args.device,
                "method": "known-text CTC segmentation", "alignment": result}
    atomic_json_write(document, args.out)
    print(json.dumps(document, indent=2, ensure_ascii=False))
    if not result.get("scored"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
