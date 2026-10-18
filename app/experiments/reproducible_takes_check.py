"""Live check for #1329/#1343: single-chunk regenerate is repeatable when
`tts.reproducible_takes` is on, and "Different take" changes it.

    python app/experiments/reproducible_takes_check.py --work DIR --output OUT.json

Runs the app's own path (ProjectManager.generate_chunk_audio, and update_chunk
for the take bump, as the Editor does) in a private data folder with built-in
voices and public-domain lines. Per chunk:

- setting on, take 0, rendered twice     -> expected identical
- setting on, take 1 (Different take)    -> expected different from take 0
- setting on, take 1 rendered again      -> expected identical to the first take 1
- setting off, rendered twice (control)  -> expected different; if these match
  too, the check cannot tell a seeded render from an unseeded one.

Audio is compared by decoded samples, so WAV header bytes cannot decide it.
The output holds counts and digests only.
"""
import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.provenance import provenance
from utils import atomic_json_write

# Pride and Prejudice (1813), public domain.
LINES = [
    ("NARRATOR", "It is a truth universally acknowledged, that a single man in possession "
                 "of a good fortune, must be in want of a wife."),
    ("ELIZABETH", "I could easily forgive his pride, if he had not mortified mine."),
    ("NARRATOR", "Mr. Bennet was so odd a mixture of quick parts, sarcastic humour, "
                 "reserve, and caprice, that the experience of three and twenty years "
                 "had been insufficient to make his wife understand his character."),
]
VOICES = {"NARRATOR": "Ryan", "ELIZABETH": "Vivian"}


def get_sample_digest(path):
    """-> (sha256 of decoded samples, sample count, file sha256)."""
    import numpy as np
    import soundfile
    samples, rate = soundfile.read(path, dtype="float32", always_2d=True)
    data = np.ascontiguousarray(samples)
    digest = hashlib.sha256(data.tobytes() + str(rate).encode()).hexdigest()
    with open(path, "rb") as handle:
        file_digest = hashlib.sha256(handle.read()).hexdigest()
    return digest, int(data.shape[0]), file_digest


def get_verdicts(renders):
    """-> per-check booleans from the four render digests of one chunk."""
    on0a, on0b, on1a, on1b, off_a, off_b = (renders[k] for k in (
        "on_take0_a", "on_take0_b", "on_take1_a", "on_take1_b", "off_a", "off_b"))
    return {"repeat_identical": on0a == on0b,
            "different_take_differs": on1a != on0a,
            "take1_repeat_identical": on1a == on1b,
            "control_off_differs": off_a != off_b}


def write_project(root, reproducible, base_config):
    os.makedirs(root, exist_ok=True)
    config = copy.deepcopy(base_config)
    config.setdefault("tts", {})["reproducible_takes"] = reproducible
    atomic_json_write(config, os.path.join(root, "config.json"))
    atomic_json_write({name: {"type": "custom", "voice": voice, "seed": "-1"}
                       for name, voice in VOICES.items()},
                      os.path.join(root, "voice_config.json"))
    atomic_json_write([{"id": i, "speaker": s, "text": t, "instruct": "", "take": 0,
                        "status": "pending"} for i, (s, t) in enumerate(LINES)],
                      os.path.join(root, "chunks.json"))


def render(manager, index):
    ok, result = manager.generate_chunk_audio(index)
    if not ok:
        raise RuntimeError(f"chunk {index} failed: {result}")
    return get_sample_digest(manager.get_chunk_audio_path(result))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.json"))
    args = parser.parse_args(argv)
    from project import ProjectManager

    with open(args.config, encoding="utf-8") as handle:
        base_config = json.load(handle)
    on_root, off_root = os.path.join(args.work, "on"), os.path.join(args.work, "off")
    write_project(on_root, True, base_config)
    write_project(off_root, False, base_config)
    on, off = ProjectManager(on_root), ProjectManager(off_root)
    # One loaded model for both folders: the engine never reads reproducible_takes
    # (ProjectManager does, per render), so sharing it changes nothing but VRAM.
    if on.get_engine() is None:
        raise RuntimeError("TTS engine did not load")
    off.engine = on.engine
    chunks = []
    for index in range(len(LINES)):
        renders, samples = {}, {}
        for key, manager, take in (("on_take0_a", on, None), ("on_take0_b", on, None),
                                   ("on_take1_a", on, 1), ("on_take1_b", on, None),
                                   ("off_a", off, None), ("off_b", off, None)):
            if take is not None:
                manager.update_chunk(index, {"take": take})
            digest, count, _ = render(manager, index)
            renders[key], samples[key] = digest, count
            print(f"chunk {index} {key}: {count} samples {digest[:12]}", flush=True)
        chunks.append({"index": index, "speaker": LINES[index][0],
                       "verdicts": get_verdicts(renders), "sample_counts": samples,
                       "digests": {k: v[:16] for k, v in renders.items()}})
    names = ("repeat_identical", "different_take_differs",
             "take1_repeat_identical", "control_off_differs")
    summary = {name: sum(c["verdicts"][name] for c in chunks) for name in names}
    result = {"chunks": len(chunks), "passed": summary, "per_chunk": chunks,
              "verdict": "pass" if all(v == len(chunks) for v in summary.values()) else "fail",
              "provenance": provenance(__file__, args)}
    atomic_json_write(result, args.output)
    print(json.dumps({"verdict": result["verdict"], "passed": summary, "of": len(chunks)}))
    return 0 if result["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
