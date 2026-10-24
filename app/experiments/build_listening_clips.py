"""Build the #1547 listener clips from a local Alice render.

The committed fixture carries only public-domain text. This script resolves it
against one local run's chunks.json, writes WAV clips, and records the source
uids and hashes in an ignored audio manifest.
"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from pydub import AudioSegment
from tts import combine_audio_with_pauses

LICENSE = "public-domain"
DEFAULT_PAUSE_MS = 500
DEFAULT_SAME_SPEAKER_PAUSE_MS = 250


def validate_fixture(fixture):
    """Refuse an incomplete or non-public-domain fixture."""
    if fixture.get("license") != LICENSE:
        raise ValueError(f"Fixture must declare license {LICENSE!r}")
    lines = fixture.get("lines") or []
    scenes = fixture.get("scenes") or []
    if len(lines) != 30 or len(scenes) != 5:
        raise ValueError(f"Fixture needs 30 lines and 5 scenes, got {len(lines)} and {len(scenes)}")
    ids = [item["id"] for item in lines] + [item["id"] for item in scenes]
    if len(ids) != len(set(ids)):
        raise ValueError("Fixture item ids must be unique")
    for scene in scenes:
        if len(scene.get("lines") or []) < 2:
            raise ValueError(f"Scene {scene.get('id')} needs at least two lines")


def index_chunks(rows):
    """Index chunks by (speaker, exact text)."""
    index = defaultdict(list)
    for row in rows:
        index[(row.get("speaker"), row.get("text"))].append(row)
    return index


def find_unique(index, speaker, text):
    """Return the one chunk for this exact speaker/text pair."""
    matches = index.get((speaker, text)) or []
    if len(matches) != 1:
        raise ValueError(f"Expected one chunk for {speaker!r} / {text[:60]!r}, found {len(matches)}")
    return matches[0]


def find_line(index, item):
    return find_unique(index, item["speaker"], item["text"])


def find_scene_in_rows(rows, scene):
    """Return the one contiguous chunk sequence for one scene."""
    wanted = [(line["speaker"], line["text"]) for line in scene["lines"]]
    matches = []
    for start in range(0, len(rows) - len(wanted) + 1):
        if [(row.get("speaker"), row.get("text")) for row in rows[start:start + len(wanted)]] == wanted:
            matches.append(rows[start:start + len(wanted)])
    if len(matches) != 1:
        raise ValueError(f"Expected one contiguous match for {scene['id']}, found {len(matches)}")
    return matches[0]


def get_pause_defaults(config_path, pause_ms=None, same_speaker_pause_ms=None):
    """Read the run's pause defaults, with explicit overrides."""
    if pause_ms is not None and same_speaker_pause_ms is not None:
        return pause_ms, same_speaker_pause_ms
    config = {}
    if config_path and Path(config_path).is_file():
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    tts = config.get("tts") or {}
    return (pause_ms if pause_ms is not None else int(tts.get("pause_between_speakers_ms", DEFAULT_PAUSE_MS)),
            same_speaker_pause_ms if same_speaker_pause_ms is not None
            else int(tts.get("pause_same_speaker_ms", DEFAULT_SAME_SPEAKER_PAUSE_MS)))


def write_wav(segment, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    segment.export(str(path), format="wav")
    data = path.read_bytes()
    return len(data), hashlib.sha256(data).hexdigest()


def build_line(row, audio_root, out_path):
    source = Path(audio_root) / row["audio_path"]
    if not source.is_file():
        raise FileNotFoundError(f"Line audio not found: {source}")
    segment = AudioSegment.from_file(source)
    size, digest = write_wav(segment, out_path)
    return {"kind": "line", "source_uid": row.get("uid"), "source": str(source),
            "bytes": size, "sha256": digest, "duration_ms": len(segment)}


def build_scene(rows, audio_root, out_path, pause_ms, same_speaker_pause_ms):
    segments = []
    for row in rows:
        source = Path(audio_root) / row["audio_path"]
        if not source.is_file():
            raise FileNotFoundError(f"Scene audio not found: {source}")
        segments.append(AudioSegment.from_file(source))
    combined = combine_audio_with_pauses(
        segments, [row["speaker"] for row in rows], pause_ms, same_speaker_pause_ms,
        [row.get("pause_after") for row in rows])
    size, digest = write_wav(combined, out_path)
    return {"kind": "scene", "source_uids": [row.get("uid") for row in rows],
            "sources": [str(Path(audio_root) / row["audio_path"]) for row in rows],
            "bytes": size, "sha256": digest, "duration_ms": len(combined)}


def build(fixture_path, chunks_path, audio_root, out_dir, manifest_path,
          pause_ms=None, same_speaker_pause_ms=None):
    fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    validate_fixture(fixture)
    rows = json.loads(Path(chunks_path).read_text(encoding="utf-8"))
    index = index_chunks(rows)
    pause_ms, same_speaker_pause_ms = get_pause_defaults(
        Path(chunks_path).with_name("config.json"), pause_ms, same_speaker_pause_ms)
    out_dir = Path(out_dir)
    manifest = {"fixture": str(Path(fixture_path).resolve()),
                "pause_ms": pause_ms, "same_speaker_pause_ms": same_speaker_pause_ms,
                "clips": {}}
    for item in fixture["lines"]:
        row = find_line(index, item)
        clip = build_line(row, audio_root, out_dir / f"{item['id']}.wav")
        manifest["clips"][item["id"]] = {**clip, "speaker": item["speaker"],
                                         "text": item["text"], "instruct": item.get("instruct", "")}
    for scene in fixture["scenes"]:
        scene_rows = find_scene_in_rows(rows, scene)
        clip = build_scene(scene_rows, audio_root, out_dir / f"{scene['id']}.wav",
                           pause_ms, same_speaker_pause_ms)
        manifest["clips"][scene["id"]] = {**clip, "title": scene.get("title", ""),
                                          "lines": scene["lines"]}
    manifest_path = Path(manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=str(APP_DIR / "fixtures" / "alice_pd_listening.json"))
    parser.add_argument("--chunks", required=True, help="Local run chunks.json")
    parser.add_argument("--audio-root", required=True, help="Directory containing the run's audio_path files")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--manifest-out", default=None)
    parser.add_argument("--pause-ms", type=int, default=None)
    parser.add_argument("--same-speaker-pause-ms", type=int, default=None)
    args = parser.parse_args(argv)
    manifest_path = args.manifest_out or str(Path(args.out_dir) / "audio_manifest.json")
    manifest = build(args.fixture, args.chunks, args.audio_root, args.out_dir, manifest_path,
                     args.pause_ms, args.same_speaker_pause_ms)
    print(json.dumps({"clips": len(manifest["clips"]), "manifest": manifest_path,
                      "pause_ms": manifest["pause_ms"],
                      "same_speaker_pause_ms": manifest["same_speaker_pause_ms"]}, indent=2))


if __name__ == "__main__":
    main()
