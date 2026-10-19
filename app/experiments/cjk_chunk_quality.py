"""#1332 quality check: do shorter Chinese/Japanese TTS chunks render more faithfully?

    python app/experiments/cjk_chunk_quality.py --config app/config.json \
        --zh ZH.txt --ja JA.txt --rates cjk_speech_rate.json \
        --whisper-bin whisper.cpp/build/bin/whisper-cli \
        --whisper-model whisper.cpp/models/ggml-large-v3.bin \
        --work DIR --out OUT.json

For each language the same contiguous public-domain passage is packed into
chunks twice, at the old 500-character limit and at #1332's measured limit
(500/2.72 Chinese, 500/2.09 Japanese), and every chunk is rendered with each
voice at one seed. Each clip is scored two ways:

- ASR: whisper.cpp large-v3 transcript vs the chunk text, as character error
  rate (Japanese also on kana readings, which removes kanji/kana spelling
  disagreements that are not errors).
- Duration, no ASR: a clip far shorter than its text should take, at the
  measured seconds-per-character for that language and voice, is flagged as
  truncated.

Both arms cover identical text, so their character-weighted totals compare
directly. Caveat: whisper decodes long audio in 30 s windows, which can cost a
long clip accuracy that the TTS never lost; the duration check does not share
that bias. The output holds numbers only.
"""
import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.provenance import provenance
from experiments.cjk_speech_rate import (
    LANGUAGE_TTS, apply_cuda_release, ensure_engine, get_audio_seconds,
    get_char_count, get_clean_text, get_config, get_file_text, get_sentences)
from utils import atomic_json_write

OLD_LIMIT = 500
# The measured factors #1332 ships (seconds per character relative to English).
FACTORS = {"zh": 2.72, "ja": 2.09}
TRUNCATION_RATIO = 0.6
SHORT_TRANSCRIPT_RATIO = 0.8


def get_new_limit(language, base=OLD_LIMIT):
    return max(1, int(base / FACTORS[language]))


def get_passage(sentences, target_chars, max_sentence):
    """-> the first contiguous sentences totalling >= target_chars.

    A sentence longer than `max_sentence` is skipped (with everything after it
    still eligible) so that neither arm has to split mid-sentence."""
    passage, total = [], 0
    for sentence in sentences:
        if len(sentence) > max_sentence:
            continue
        passage.append(sentence)
        total += len(sentence)
        if total >= target_chars:
            break
    return passage


def get_packed_chunks(sentences, limit):
    """-> sentences greedily packed, in order, into chunks of <= limit chars."""
    chunks, current = [], ""
    for sentence in sentences:
        if len(sentence) > limit:
            raise ValueError("sentence longer than the chunk limit")
        if current and len(current) + len(sentence) > limit:
            chunks.append(current)
            current = ""
        current += sentence
    if current:
        chunks.append(current)
    return chunks


def is_truncated(seconds, chars, seconds_per_char):
    return seconds < TRUNCATION_RATIO * chars * seconds_per_char


def get_arm_summary(clips):
    """Character-weighted error totals over one arm's clips."""
    chars = sum(c["chars"] for c in clips)
    summary = {"clips": len(clips), "chars": chars,
               "truncated": sum(c["truncated"] for c in clips),
               "short_transcript": sum(c["short_transcript"] for c in clips)}
    for key in ("cer", "reading_cer"):
        scored = [c for c in clips if c.get(key) is not None]
        if scored:
            weight = sum(c["chars"] for c in scored)
            summary[key] = sum(c[key] * c["chars"] for c in scored) / weight
    return summary


# Whisper writes Chinese in traditional characters unless steered, and every
# 於/于 or 學/学 then scores as an error the speech never made. A simplified-
# Chinese initial prompt is the standard steer; residual variants hit both arms.
ZH_PROMPT = "以下是普通话的句子。"


def get_whisper_text(wav, language, args):
    """-> whisper.cpp transcript (joined segments) for one clip."""
    import subprocess
    cmd = [args.whisper_bin, "-m", args.whisper_model, "-f", wav, "-oj",
           "-of", wav + ".wcpp", "-l", language, "-np"]
    if language == "zh":
        cmd += ["--prompt", ZH_PROMPT]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if out.returncode != 0:
        raise RuntimeError(f"whisper.cpp rc={out.returncode}: {out.stderr[-200:]}")
    with open(wav + ".wcpp.json", encoding="utf-8") as handle:
        doc = json.load(handle)
    return "".join(seg["text"] for seg in doc.get("transcription", []))


def score_clip(wav, text, language, args):
    from experiments.asr_backends import to_reading, word_error_rate
    hypothesis = get_whisper_text(wav, language, args)
    row = {"cer": word_error_rate(text, hypothesis, char_level=True),
           "hyp_chars": get_char_count(hypothesis)}
    if language == "ja":
        ref_kana, hyp_kana = to_reading(text), to_reading(hypothesis)
        if ref_kana is not None and hyp_kana is not None:
            row["reading_cer"] = word_error_rate(" ".join(ref_kana), " ".join(hyp_kana),
                                                 char_level=False)
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--zh", required=True)
    parser.add_argument("--ja", required=True)
    parser.add_argument("--rates", required=True, help="cjk_speech_rate.json")
    parser.add_argument("--whisper-bin", required=True)
    parser.add_argument("--whisper-model", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--chars", type=int, default=2000)
    parser.add_argument("--voices", default="Ryan,Vivian")
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args(argv)
    from experiments.generation import render

    config = get_config(args.config)
    with open(args.rates, encoding="utf-8") as handle:
        rates = json.load(handle)["summary"]["languages"]
    voices = [v for v in args.voices.split(",") if v]
    os.makedirs(args.work, exist_ok=True)
    result = {"languages": {}, "limits": {}}
    for language, source in (("zh", args.zh), ("ja", args.ja)):
        new_limit = get_new_limit(language)
        result["limits"][language] = {"old": OLD_LIMIT, "new": new_limit}
        sentences = get_sentences(get_clean_text(get_file_text(source), language), language)
        passage = get_passage(sentences, args.chars, new_limit)
        arms = {"old": get_packed_chunks(passage, OLD_LIMIT),
                "new": get_packed_chunks(passage, new_limit)}
        engine = ensure_engine(config, LANGUAGE_TTS[language])
        per_language = {"passage_chars": sum(map(len, passage)), "arms": {}}
        try:
            for arm, chunks in arms.items():
                per_voice = {}
                for voice in voices:
                    entry = {"type": "custom", "voice": voice, "seed": str(args.seed)}
                    spc = rates[language]["voices"][voice]["seconds_per_char"]
                    clips = []
                    for index, text in enumerate(chunks):
                        wav = os.path.join(args.work, f"{language}_{arm}_{voice}_{index:03d}.wav")
                        if not os.path.exists(wav):
                            render(engine, text, "", voice, {voice: entry}, entry, wav)
                        seconds = get_audio_seconds(wav)
                        chars = get_char_count(text)
                        clip = {"index": index, "chars": chars, "seconds": round(seconds, 2),
                                "truncated": is_truncated(seconds, chars, spc)}
                        clip.update(score_clip(wav, text, language, args))
                        clip["short_transcript"] = clip["hyp_chars"] < SHORT_TRANSCRIPT_RATIO * chars
                        clips.append(clip)
                        print(f"{language} {arm} {voice} {index}: {chars} chars {seconds:.1f}s "
                              f"cer={clip['cer']:.3f}", flush=True)
                    per_voice[voice] = {"summary": get_arm_summary(clips), "clips": clips}
                all_clips = [c for v in per_voice.values() for c in v["clips"]]
                per_language["arms"][arm] = {"pooled": get_arm_summary(all_clips),
                                             "voices": per_voice}
        finally:
            del engine
            apply_cuda_release()
        result["languages"][language] = per_language
    result["provenance"] = provenance(__file__, args)
    atomic_json_write(result, args.out)
    for language, data in result["languages"].items():
        for arm, body in data["arms"].items():
            print(language, arm, json.dumps(body["pooled"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
