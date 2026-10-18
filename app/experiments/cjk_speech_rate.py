"""Measure how long a character of Chinese/Japanese takes to speak, versus English.

Issue #1332 step 1. The CJK chunk limit was a guess. This renders a fixed number
of sentences per language with the app's own TTS, measures only the speech part
of each WAV, and reports characters per second. The factor the chunk limit is
divided by is `ratio_vs_en` in the output JSON.

    python app/experiments/cjk_speech_rate.py --lines 20 --voices Ryan,Vivian

Inputs are public-domain corpora. Rendering resumes: a WAV already in --work is
measured instead of re-rendered, so a run that died at sentence 19 of 20 does
not redo the first 18 - and does not silently score different audio.

`get_clean_text`, `pick_sentences`, `get_speech_seconds` and `get_rate_summary`
are pure and unit-tested without a GPU. Only `main` loads TTS.
"""
import argparse
import copy
import gc
import json
import os
import re
import statistics
import sys

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

LANGUAGES = ("zh", "ja", "en")
LANGUAGE_TTS = {"zh": "Chinese", "ja": "Japanese", "en": "English"}

# Aozora ruby (｜漢字《かんじ》) and inline notes (［＃改ページ］).
RUBY_PATTERN = re.compile(r"\u300a[^\u300b]*\u300b")
NOTATION_PATTERN = re.compile(r"\uff3b\uff03[^\uff3d]*\uff3d")
RUBY_MARK = "\uff5c"

CJK_PATTERN = re.compile(r"[\u3005\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_PATTERN = re.compile(r"[A-Za-z]")
SPACE_PATTERN = re.compile(r"\s+")
CJK_MARK_PATTERN = re.compile(r"(?<=[\u3002\uff01\uff1f])")
ENGLISH_MARK_PATTERN = re.compile(r"(?<=[.!?])\s+")

FRAME_SECONDS = 0.025
SILENCE_FLOOR_DB = 40.0


def is_cjk_language(language):
    """-> True when `language` is written without spaces between words."""
    return language in ("zh", "ja")


def get_char_range(language):
    """-> (min_chars, max_chars) for a usable sentence in `language`."""
    if is_cjk_language(language):
        return 12, 60
    return 40, 180


def get_char_count(text):
    """-> number of non-space characters in `text`."""
    return len(SPACE_PATTERN.sub("", text))


def get_clean_text(text, language):
    """-> `text` with Aozora ruby/notation gone (ja) and whitespace collapsed.

    Ruby removal is Japanese-only: 《》 is an ordinary mark in Chinese, and
    deleting it there would corrupt the sample rather than clean it.
    """
    cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if language == "ja":
        # Aozora layout: a title/author line first, the 底本 colophon last.
        cleaned = cleaned.split("底本：", 1)[0]
        first_sentence = cleaned.find("。")
        if first_sentence >= 0:
            cleaned = cleaned[cleaned.rfind("\n", 0, first_sentence) + 1:]
        cleaned = NOTATION_PATTERN.sub("", cleaned)
        cleaned = RUBY_PATTERN.sub("", cleaned)
        cleaned = cleaned.replace(RUBY_MARK, "")
    return SPACE_PATTERN.sub(" ", cleaned).strip()


def get_sentences(text, language):
    """-> sentences in reading order, each keeping its terminal mark."""
    if not text:
        return []
    if is_cjk_language(language):
        parts = CJK_MARK_PATTERN.split(text)
    else:
        parts = ENGLISH_MARK_PATTERN.split(text)
    return [part.strip() for part in parts if part.strip()]


def get_even_indices(total, count):
    """-> `count` indices spread evenly over range(total), first and last kept."""
    if count <= 0 or total <= 0:
        return []
    if count >= total:
        return list(range(total))
    if count == 1:
        return [0]
    indices = []
    for step in range(count):
        if step == count - 1:
            indices.append(total - 1)
        else:
            indices.append(int(step * (total - 1) / (count - 1)))
    return indices


def pick_sentences(text, language, count, min_chars=None, max_chars=None):
    """-> `count` usable sentences, evenly spaced, deterministically.

    A usable sentence counts non-space characters inside [min_chars, max_chars]
    and is written in the script of `language`: a Latin-script sentence in a
    Chinese/Japanese sample would be measured as if its letters were hanzi, and
a hanzi sentence in the English sample the same way in reverse.
    """
    if min_chars is None or max_chars is None:
        default_min, default_max = get_char_range(language)
        if min_chars is None:
            min_chars = default_min
        if max_chars is None:
            max_chars = default_max
    if count <= 0:
        return []
    candidates = []
    for sentence in get_sentences(text, language):
        length = get_char_count(sentence)
        if length < min_chars or length > max_chars:
            continue
        if is_cjk_language(language):
            if LATIN_PATTERN.search(sentence):
                continue
        else:
            if CJK_PATTERN.search(sentence):
                continue
        candidates.append(sentence)
    return [candidates[index]
            for index in get_even_indices(len(candidates), count)]


def get_speech_seconds(samples, sample_rate):
    """-> seconds of speech in `samples`, leading/trailing silence excluded.

    A 25 ms frame counts as speech when its RMS is within 40 dB (a factor of
    100 in amplitude) of the loudest frame. The span runs from the first such
    frame to the end of the last, so a breath or a room tone cannot stretch the
    measurement. None when nothing is loud enough to be speech.
    """
    if samples is None or sample_rate is None or sample_rate <= 0:
        return None
    data = np.asarray(samples, dtype=np.float64).reshape(-1)
    if data.size == 0:
        return None
    frame_length = max(1, int(round(float(sample_rate) * FRAME_SECONDS)))
    frame_count = int(np.ceil(data.size / float(frame_length)))
    padded = np.zeros(frame_count * frame_length, dtype=np.float64)
    padded[:data.size] = data
    frames = padded.reshape(frame_count, frame_length)
    loudness = np.sqrt(np.mean(frames * frames, axis=1))
    loudest = float(loudness.max())
    if loudest <= 0.0:
        return None
    threshold = loudest * (10.0 ** (-SILENCE_FLOOR_DB / 20.0))
    audible = np.nonzero(loudness >= threshold)[0]
    if audible.size == 0:
        return None
    first = int(audible[0]) * frame_length
    last = min((int(audible[-1]) + 1) * frame_length, int(data.size))
    return (last - first) / float(sample_rate)


def get_rate_stats(rates):
    """-> n and medians for a list of (chars_per_second, seconds_per_char)."""
    return {"n": len(rates),
            "chars_per_second": statistics.median(
                [rate[0] for rate in rates]),
            "seconds_per_char": statistics.median(
                [rate[1] for rate in rates])}


def get_rate_summary(rows):
    """-> medians per (language, voice) and per language, plus ratio_vs_en.

    `ratio_vs_en` is English median characters per second divided by the
    language's own median: the factor the CJK chunk limit is divided by. It is
    reported per voice wherever both sides have data, and pooled, so one voice
    that reads one language unusually fast cannot silently set the limit.
    """
    by_voice = {}
    by_language = {}
    for row in rows:
        chars = float(row["chars"])
        seconds = float(row["seconds"])
        if chars <= 0.0 or seconds <= 0.0:
            continue
        language = row["language"]
        voice = row["voice"]
        rates = (chars / seconds, seconds / chars)
        by_language.setdefault(language, []).append(rates)
        by_voice.setdefault(language, {}).setdefault(voice, []).append(rates)

    summary = {"languages": {},
               "ratio_vs_en": {"by_voice": {}, "pooled": {}}}
    for language in sorted(by_language):
        entry = get_rate_stats(by_language[language])
        entry["voices"] = {}
        for voice in sorted(by_voice[language]):
            entry["voices"][voice] = get_rate_stats(by_voice[language][voice])
        summary["languages"][language] = entry

    english = summary["languages"].get("en")
    if english is not None:
        for language in sorted(summary["languages"]):
            if language == "en":
                continue
            own = summary["languages"][language]
            per_voice = {}
            for voice, stats in own["voices"].items():
                english_stats = english["voices"].get(voice)
                if english_stats is None:
                    continue
                per_voice[voice] = (english_stats["chars_per_second"]
                                    / stats["chars_per_second"])
            summary["ratio_vs_en"]["by_voice"][language] = per_voice
            summary["ratio_vs_en"]["pooled"][language] = (
                english["chars_per_second"] / own["chars_per_second"])
    return summary


def get_input_path(value):
    """-> absolute path for a CLI input.

    Defaults are written two ways in the spec ('corpora/...' under
    ab_test_runtime, 'ab_test_runtime/...' under the repo), so a relative path
    is tried against REPO and then against REPO/ab_test_runtime.
    """
    path = str(value)
    if os.path.isabs(path):
        return path
    candidate = os.path.join(REPO, path)
    if os.path.exists(candidate):
        return candidate
    runtime = os.path.join(REPO, "ab_test_runtime", path)
    if os.path.exists(runtime):
        return runtime
    return candidate


def get_file_text(path):
    """-> text of `path`, UTF-8 first and CP932 for older Aozora files."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="cp932", errors="replace") as handle:
            return handle.read()


def get_config(path):
    """-> the config dict at `path`, read fresh and shared with nobody."""
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def get_voices(value):
    """-> voice names from a comma separated CLI value."""
    return [name.strip() for name in str(value).split(",") if name.strip()]


def get_audio_seconds(path):
    """-> speech seconds in the WAV at `path`; channels are averaged to mono."""
    import soundfile

    samples, sample_rate = soundfile.read(path, always_2d=True)
    return get_speech_seconds(samples.mean(axis=1), sample_rate)


def ensure_engine(config, language):
    """-> a TTSEngine over a private copy of `config` speaking `language`.

    The engine reads tts.language at construction, so the copy happens per
    call: mutating the shared config would hand the previous language to the
    next engine and silently measure the wrong rate.
    """
    from tts import TTSEngine

    engine_config = copy.deepcopy(config)
    engine_config["tts"]["language"] = language
    return TTSEngine(engine_config)


def apply_cuda_release():
    """Drop cached GPU memory when torch is importable. Never raises."""
    try:
        import torch
    except ImportError:
        return
    torch.cuda.empty_cache()


def apply_voice_renders(engine, sentences, language, voice, entry, work_dir):
    """-> (rows, failures) for one (language, voice), reusing existing WAVs.

    render() raises rather than returning a flag, so a failed generation cannot
    be measured as if it were stale-but-fresh audio.
    """
    from experiments.generation import GenerationFailed, render

    rows = []
    failures = []
    voice_config = {voice: entry}
    for index, sentence in enumerate(sentences):
        wav_path = os.path.join(work_dir,
                                "%s_%s_%03d.wav" % (language, voice, index))
        if not os.path.exists(wav_path):
            try:
                render(engine, sentence, "", voice, voice_config, entry,
                       wav_path)
            except GenerationFailed as exc:
                failures.append({"language": language, "voice": voice,
                                 "index": index, "error": str(exc)[:200]})
                continue
        try:
            seconds = get_audio_seconds(wav_path)
        except Exception as exc:                            # noqa: BLE001
            failures.append({"language": language, "voice": voice,
                             "index": index,
                             "error": ("unreadable wav: %s" % exc)[:200]})
            continue
        if seconds is None:
            failures.append({"language": language, "voice": voice,
                             "index": index, "error": "no speech detected"})
            continue
        rows.append({"language": language, "voice": voice,
                     "chars": get_char_count(sentence), "seconds": seconds})
    return rows, failures


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Measure Chinese/Japanese speech rate against English.")
    parser.add_argument("--zh", default=os.path.join(
        "corpora", "zh_pd", "luxun_pd.txt"))
    parser.add_argument("--ja", default=os.path.join(
        "corpora", "aozora", "rashomon.txt"))
    parser.add_argument("--en", default=os.path.join(
        "ab_test_runtime", "pdnc", "data", "PrideAndPrejudice",
        "novel_text.txt"))
    parser.add_argument("--lines", type=int, default=20,
                        help="sentences per language")
    parser.add_argument("--voices", default="Ryan,Vivian")
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--config", default=os.path.join("app", "config.json"))
    parser.add_argument("--work", default=os.path.join(
        "ab_test_runtime", "cjk_speech_rate_work"))
    parser.add_argument("--out", default=os.path.join(
        "ab_test_runtime", "cjk_speech_rate.json"))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    from experiments.provenance import provenance
    from utils import atomic_json_write

    voices = get_voices(args.voices)
    if not voices:
        raise SystemExit("--voices must name at least one voice")
    work_dir = os.path.abspath(args.work)
    os.makedirs(work_dir, exist_ok=True)
    sources = {"zh": get_input_path(args.zh),
               "ja": get_input_path(args.ja),
               "en": get_input_path(args.en)}
    config = get_config(get_input_path(args.config))

    rows = []
    failures = []
    for language in LANGUAGES:
        text = get_clean_text(get_file_text(sources[language]), language)
        sentences = pick_sentences(text, language, args.lines)
        print("%s: %d sentence(s) from %s"
              % (language, len(sentences), sources[language]))
        if not sentences:
            failures.append({"language": language, "reason": "no sentences"})
            continue
        engine = ensure_engine(config, LANGUAGE_TTS[language])
        try:
            for voice in voices:
                entry = {"type": "custom", "voice": voice,
                         "seed": str(args.seed)}
                voice_rows, voice_failures = apply_voice_renders(
                    engine, sentences, language, voice, entry, work_dir)
                rows.extend(voice_rows)
                failures.extend(voice_failures)
        finally:
            del engine
            gc.collect()
            apply_cuda_release()

    summary = get_rate_summary(rows)
    result = {"rows": rows,
              "summary": summary,
              "failures": failures,
              "provenance": provenance(__file__, args)}
    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    atomic_json_write(result, out_path)

    for language, ratio in sorted(summary["ratio_vs_en"]["pooled"].items()):
        print("%s: %.2fx English characters/second" % (language, ratio))
    print("wrote %s: %d row(s), %d failure(s)"
          % (out_path, len(rows), len(failures)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
