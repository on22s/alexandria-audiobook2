"""Assemble the LoRA-voice main and supplement listener pages."""

import argparse
import hashlib
import json
import random
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from pydub import AudioSegment
from tts import combine_audio_with_pauses
from experiments.listening_critic_harness import (
    MIN_REPEAT_GAP, build_page, write_page,
)
from experiments.render_lora_listening_set import CAST, SWAPS

PAUSE_MS = 500
SAME_PAUSE_MS = 250
SUPPLEMENT_REPEATS = ("clean_01", "voice_02")
SUPPLEMENT_SPECS = (
    ("clean_01", "scene_01", "Clean scene 1", "clean", None, None),
    ("clean_02", "scene_02", "Clean scene 2", "clean", None, None),
    ("clean_03", "scene_04", "Clean scene 3", "clean", None, None),
    ("voice_01", "scene_03", "Voice change 1", "voice", None, "swap_scene_03_0"),
    ("voice_02", "scene_05", "Voice change 2", "voice", None, "swap_scene_05_0"),
    ("voice_03", "scene_02", "Voice change 3", "voice", None, "swap_scene_02_1"),
    ("pause_long_01", "scene_01", "Long pause 1", "pause_long", {0: 3000}, None),
    ("pause_long_02", "scene_05", "Long pause 2", "pause_long", {2: 3000}, None),
    ("pause_short_01", "scene_02", "Short pause 1", "pause_short", {1: 0}, None),
    ("pause_short_02", "scene_04", "Short pause 2", "pause_short", {1: 0}, None),
)


def scene_segments(rendered, scene):
    paths = [Path(rendered) / f"{scene['id']}_{index}.wav" for index in range(len(scene["lines"]))]
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(f"Missing rendered scene line: {path}")
    return [AudioSegment.from_file(path) for path in paths], paths


def combine_scene(rendered, scene, overrides=None, swap_index=None, swap_job=None):
    segments, paths = scene_segments(rendered, scene)
    if swap_index is not None:
        if not swap_job:
            raise ValueError("A swap job is required for a swapped scene")
        swapped = Path(rendered) / f"{swap_job}.wav"
        if not swapped.is_file():
            raise FileNotFoundError(f"Missing swap render: {swapped}")
        segments[swap_index] = AudioSegment.from_file(swapped)
    combined = combine_audio_with_pauses(
        segments, [line["speaker"] for line in scene["lines"]],
        PAUSE_MS, SAME_PAUSE_MS, overrides)
    return combined, paths


def write_clip(segment, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    segment.export(str(path), format="wav")
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "duration_ms": len(segment)}


def build_main(fixture_path, fixture, rendered, clips_dir, out_html, key_out, seed=1547):
    clips_dir = Path(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"pause_ms": PAUSE_MS, "same_speaker_pause_ms": SAME_PAUSE_MS, "clips": {}}
    for line in fixture["lines"]:
        source = Path(rendered) / f"{line['id']}.wav"
        if not source.is_file():
            raise FileNotFoundError(f"Missing rendered line: {source}")
        target = clips_dir / f"{line['id']}.wav"
        shutil.copyfile(source, target)
        data = target.read_bytes()
        manifest["clips"][line["id"]] = {"kind": "line", "bytes": len(data),
                                         "sha256": hashlib.sha256(data).hexdigest(),
                                         "duration_ms": len(AudioSegment.from_file(target)),
                                         "speaker": line["speaker"], "text": line["text"]}
    for scene in fixture["scenes"]:
        combined, paths = combine_scene(rendered, scene)
        target = clips_dir / f"{scene['id']}.wav"
        clip = write_clip(combined, target)
        manifest["clips"][scene["id"]] = {**clip, "kind": "scene", "title": scene.get("title", ""),
                                          "sources": [str(path) for path in paths]}
    (clips_dir / "audio_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return build_page(str(fixture_path), clips_dir, out_html, key_out, seed)


def build_supplement(fixture, rendered, out_dir, out_html, key_out, labels_out, seed=1550):
    scenes = {scene["id"]: scene for scene in fixture["scenes"]}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    items = []
    labels = {}
    for item_id, scene_id, title, expect, override_map, swap_job in SUPPLEMENT_SPECS:
        scene = scenes[scene_id]
        display_title = scene.get("title") or "Scene"
        overrides = None
        swap_index = None
        if override_map is not None:
            overrides = [None] * len(scene["lines"])
            for index, value in override_map.items():
                overrides[index] = value
        if swap_job:
            # Find the line the swap was rendered from.
            swap_index = next(index for index, line in enumerate(scene["lines"])
                              if any(job[0] == swap_job and job[1] == scene_id and job[2] == index
                                     for job in SWAPS))
        combined, paths = combine_scene(rendered, scene, overrides, swap_index, swap_job)
        target = out_dir / f"{item_id}.wav"
        clip = write_clip(combined, target)
        items.append({"item_id": item_id, "kind": "clip", "title": display_title,
                      "lines": scene["lines"], "path": target})
        labels[item_id] = {"expect": "clean" if expect == "clean" else "defect",
                           "fault": None if expect == "clean" else expect,
                           "scene": scene_id, "title": title, "swap": swap_job, **clip}
    schedule = [{"item_id": item["item_id"], "repeat": False} for item in items]
    schedule += [{"item_id": item_id, "repeat": True} for item_id in SUPPLEMENT_REPEATS]
    rng = random.Random(seed)
    for _ in range(1000):
        rng.shuffle(schedule)
        positions = {entry["item_id"]: index for index, entry in enumerate(schedule) if not entry["repeat"]}
        if all(not entry["repeat"] or abs(index - positions[entry["item_id"]]) >= MIN_REPEAT_GAP
               for index, entry in enumerate(schedule)):
            break
    else:
        raise RuntimeError("Could not place supplement repeats")
    trials = []
    for index, entry in enumerate(schedule):
        item = next(item for item in items if item["item_id"] == entry["item_id"])
        trials.append({"id": f"s{index + 1:02d}",
                       "clip": hashlib.sha256(item["item_id"].encode()).hexdigest()[:12],
                       "kind": "clip", "title": item["title"], "scene": item["lines"],
                       "_item_id": entry["item_id"], "_repeat": entry["repeat"]})
    result = write_page(items, trials, seed, out_html, key_out, SUPPLEMENT_REPEATS)
    Path(labels_out).write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**result, "labels": str(labels_out)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    parser.add_argument("--rendered", required=True)
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--seed", type=int, default=1547)
    args = parser.parse_args(argv)
    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    root = Path(args.out_root)
    main_result = build_main(args.fixture, fixture, args.rendered, root / "main_clips",
                             root / "listening_check_lora.html", root / "listening_key_lora.json", args.seed)
    supplement_result = build_supplement(fixture, args.rendered, root / "supplement_clips",
                                         root / "supplement_check_lora.html",
                                         root / "supplement_key_lora.json",
                                         root / "supplement_labels_lora.json", args.seed + 3)
    cast = {"cast": CAST, "swaps": SWAPS,
            "cast_path": str(root / "lora_cast.json")}
    (root / "lora_cast.json").write_text(json.dumps(cast, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"main": main_result, "supplement": supplement_result}, indent=2))


if __name__ == "__main__":
    main()
