"""Render the #1547 listening set with LoRA voices.

Uses the same public-domain Alice fixture, but every character speaks through a
trained LoRA adapter instead of a built-in custom voice. Renders the 30 lines,
the scene lines, and three adapter-swapped lines for the voice-fault arm.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from tts import TTSEngine

ADAPTER_ROOT = Path(os.environ.get("AA2_LORA_ROOT", APP_DIR.parent / "lora_models"))
CAST = {
    "NARRATOR": "husky_tenor_30s_m_literary",
    "ALICE": "husky_soprano_20s_f",
    "HATTER": "breathy_tenor_20s_m_fantasy",
    "QUEEN": "crisp_mezzo_30s_f",
    "CHESHIRE CAT": "silky_alto_40s_f_literary_1",
    "MOCK TURTLE": "husky_baritone_50s_m_gothic",
    "DUCHESS": "breathy_alto_50s_f_fantasy",
    "WHITE RABBIT": "crisp_tenor_20s_m",
    "GRYPHON": "gravelly_baritone_50s_m_scifi",
    "CATERPILLAR": "husky_baritone_35s_m",
    "KING": "breathy_baritone_40s_m_literary",
    "MARCH HARE": "crisp_tenor_30s_m_fantasy",
}
SWAPS = (
    ("swap_scene_03_0", "scene_03", 0, "KING"),
    ("swap_scene_05_0", "scene_05", 0, "QUEEN"),
    ("swap_scene_02_1", "scene_02", 1, "HATTER"),
)
DEFAULT_INSTRUCT = {
    "NARRATOR": "Neutral, even narration with a calm and steady tone.",
}


def get_voice_data(speaker, override=None):
    adapter_id = override or CAST[speaker]
    path = ADAPTER_ROOT / adapter_id
    if not (path / "adapter_model.safetensors").is_file():
        raise FileNotFoundError(f"Missing adapter weights: {path}")
    return {"type": "lora", "voice": adapter_id, "adapter_id": adapter_id,
            "adapter_path": str(path), "seed": 42, "character_style": "", "default_style": ""}


def build_instruct_map(chunks_path):
    """Look up the original instruct for each scene line."""
    rows = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
    index = {}
    for row in rows:
        index.setdefault((row.get("speaker"), row.get("text")), row.get("instruct") or "")
    return index


def build_jobs(fixture, instruct_map):
    jobs = []
    for line in fixture["lines"]:
        jobs.append({"job_id": line["id"], "speaker": line["speaker"],
                     "text": line["text"], "instruct": line.get("instruct") or DEFAULT_INSTRUCT.get(line["speaker"], "")})
    for scene in fixture["scenes"]:
        for index, line in enumerate(scene["lines"]):
            jobs.append({"job_id": f"{scene['id']}_{index}", "speaker": line["speaker"],
                         "text": line["text"],
                         "instruct": instruct_map.get((line["speaker"], line["text"]),
                                                      DEFAULT_INSTRUCT.get(line["speaker"], ""))})
    scenes = {scene["id"]: scene for scene in fixture["scenes"]}
    for job_id, scene_id, index, speaker in SWAPS:
        line = scenes[scene_id]["lines"][index]
        jobs.append({"job_id": job_id, "speaker": speaker, "text": line["text"],
                     "instruct": instruct_map.get((line["speaker"], line["text"]),
                                                  DEFAULT_INSTRUCT.get(line["speaker"], "")),
                     "swap_for": scenes[scene_id]["lines"][index]})
    return jobs


def load_engine(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    config.setdefault("tts", {})
    config["tts"]["mode"] = "local"
    return TTSEngine(config)


def render_jobs(jobs, out_dir, config_path, limit=None, force=False):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    engine = load_engine(config_path)
    ordered = sorted(jobs, key=lambda job: (CAST.get(job["speaker"]), job["job_id"]))
    if limit is not None:
        ordered = ordered[:limit]
    manifest = []
    started = time.time()
    for index, job in enumerate(ordered, 1):
        output = out_dir / f"{job['job_id']}.wav"
        if output.is_file() and not force:
            data = output.read_bytes()
            manifest.append({**job, "output": str(output), "bytes": len(data),
                             "sha256": hashlib.sha256(data).hexdigest(), "skipped": True})
            print(f"[{index}/{len(ordered)}] skip {job['job_id']}", file=sys.stderr)
            continue
        t0 = time.time()
        ok = engine.generate_lora_voice(job["text"], job["instruct"],
                                        get_voice_data(job["speaker"]), str(output))
        if not ok or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"Render failed for {job['job_id']}")
        data = output.read_bytes()
        elapsed = time.time() - t0
        manifest.append({**job, "output": str(output), "bytes": len(data),
                         "sha256": hashlib.sha256(data).hexdigest(),
                         "elapsed_seconds": round(elapsed, 2), "skipped": False})
        done = index
        rate = (time.time() - started) / done
        print(f"[{index}/{len(ordered)}] {job['job_id']} {job['speaker']} "
              f"{elapsed:.1f}s eta {(len(ordered) - index) * rate / 60:.1f} min", file=sys.stderr)
    (out_dir / "render_manifest.json").write_text(
        json.dumps({"started_at": started, "config": config_path,
                    "elapsed_seconds": round(time.time() - started, 2),
                    "jobs": manifest}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    parser.add_argument("--chunks", required=True)
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--out-dir", default=str(APP_DIR.parent / "ab_test_runtime" / "listening_critic_1547_lora" / "rendered"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    jobs = build_jobs(fixture, build_instruct_map(args.chunks))
    manifest = render_jobs(jobs, args.out_dir, args.config, args.limit, args.force)
    print(json.dumps({"jobs": len(manifest), "out_dir": args.out_dir,
                      "rendered": sum(not item["skipped"] for item in manifest)}, indent=2))


if __name__ == "__main__":
    main()
