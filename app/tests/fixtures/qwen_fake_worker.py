"""Fake qwen worker fixture (#1844, TEST ONLY — never user-selectable).

Speaks the qwen_align_worker.py argv protocol: argv[1]=job.json,
argv[2]=result.json. Emits deterministic proportional tokens for each
row so worker/parent plumbing tests can run without torch or a model.
Tokens are shaped like the real aligner (text/start_time/end_time) and
cover the input text exactly (words by whitespace, else per character),
which is what the parent validates.
"""
import json
import sys
import wave


def _clean(word):
    kept = "".join(c for c in word if c.isalnum() or c == "'")
    return kept


def _units(text):
    words = [_clean(w) for w in text.split()]
    words = [w for w in words if w]
    if words:
        return words
    return [_clean(c) for c in text if not c.isspace() and _clean(c)]


def main(argv):
    with open(argv[1], encoding="utf-8") as handle:
        job = json.load(handle)
    tokens = {}
    for row in job["rows"]:
        with wave.open(row["wav_path"], "rb") as stream:
            duration = stream.getnframes() / float(stream.getframerate())
        words = _units(row["text"])
        count = max(len(words), 1)
        step = duration / (count + 1)
        tokens[row["id"]] = [
            {"text": word, "start_time": round((i + 1) * step, 3),
             "end_time": round((i + 2) * step, 3)}
            for i, word in enumerate(words)
        ]
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump({"ok": True, "tokens": tokens,
                   "provider": {"qwen_asr_version": "test-fixture", "device": job.get("device")}},
                  handle, ensure_ascii=False)


if __name__ == "__main__":
    main(sys.argv)
