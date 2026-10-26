"""Does a scene description in the delivery instruction make a SCENE sound better?

Where this comes from. DRAMA-Eval (arXiv 2610.31796) rated whole multi-speaker
scenes and found two things that bear on this app. Systems that render each
line separately and concatenate - Qwen3-TTS among them - score well line by
line and badly as a scene (Chinese C2/C3 28.2/27.7 against 73.2/75.7 for a
whole-scene model). And adding a scene description raised a whole-scene model's
scene scores by 5-10 points (their Table 5). That second number was measured on
SeedAudio, not on a per-line engine, so it says nothing yet about this app,
which sends each line its own `instruct` and has no scene field at all.

THREE ARMS, same lines, same two voices, same seed:

    line         the per-line instruction alone             (production shape)
    scene        scene description + per-line instruction
    wrong_scene  ANOTHER scene's description + per-line instruction

THE CONTROL IS THE POINT, as in `joint_scene`. `scene` beating `line` alone
proves little: it changes two things at once - the instruction is longer, AND
it is about this scene. `wrong_scene` holds the first constant and destroys the
second approximately: the descriptions are not exactly length-matched per scene,
so this is an exploratory relevance control rather than an isolated causal test.
Each scene borrows the description of a scene from a different book
(a fixed rotation, never itself), so it is fluent, plausible and wrong.

Each arm's scene is joined with the production joiner
(`tts.combine_audio_with_pauses`), so the joins a listener hears are the joins
an export would have. Ratings come from `blinded_listening.py --scene-context`,
which shuffles the arms, conceals the key and keeps the positive controls.

WER IS A GATE, NOT THE ANSWER. `instruct_value` found per-line, per-character
and no instructions within 0.6% WER of each other, so a transcript cannot see
delivery. Here it catches only the failure a longer instruction can cause: the
scene text being spoken, or a line breaking. An arm more than `WER_MARGIN`
worse than `line` on a scene marks that scene unpublishable.

Readings, fixed before running (blinded preference):

  scene > line AND scene > wrong_scene   relevant scene context is audible; an
                                         annotator-written scene field is worth
                                         building
  scene ~ wrong_scene, both > line       a longer instruction helps, relevance
                                         does not; do not build a scene feature
  all three ~                            nothing audible through `instruct`; the
                                         paper's gain does not transfer to a
                                         per-line engine - look at the joins
  wrong_scene > scene                    suspect the harness before believing it
  positive controls fail                 the package cannot be read at all

The fixture is public domain (PDNC, Austen). Line text is read from the
checked-in gold fixtures by row id; `_` italics markup is dropped because it
is typography, not speech. Clone voices are refused: the clone path ignores
`instruct`, so all three arms would be the same request.

Rendering requires fresh output paths inside this checkout, matching the
listening packager's confined source contract. Interrupted runs keep an
incomplete manifest. The GPU queue lease covers TTS and model cleanup; Whisper
runs on CPU with a private cache. A complete manifest is published only after
cleanup and a recheck of the captured input identities.
"""
import argparse
import json
import math
from pathlib import Path
import os
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

ARMS = ("line", "scene", "wrong_scene")
FIXTURE = os.path.join(APP, "experiments", "fixtures", "scene_context_scenes.json")
VOICE_ROLES = ("female", "male")
# Absolute WER, mean over a scene's lines. Production arms sit within 0.6% of
# each other (`instruct_value`), so five points is a broken line, not noise.
WER_MARGIN = 0.05


class SceneFixtureError(ValueError):
    """The scene fixture cannot support a fair comparison."""


def get_spoken_text(line):
    """PDNC line text as it should be spoken: no italics markup or wrapping."""
    return re.sub(r"\s+", " ", line.replace("_", "")).strip()


def _capture_json(path, limit=1024 * 1024):
    from project_scope_files import capture_scope_file
    try:
        path = Path(os.path.abspath(path))
        facts, raw = capture_scope_file(path.parent, path.name, limit=limit,
                                        include_bytes=True)
        if raw is None:
            raise ValueError("experiment JSON is missing")
        return json.loads(raw), facts["sha256"]
    except (OSError, ValueError, RecursionError) as exc:
        raise SceneFixtureError("cannot read bounded experiment JSON") from exc


def _read_json(path, limit=1024 * 1024):
    return _capture_json(path, limit)[0]


def load_scenes(fixture_path=FIXTURE):
    """-> scenes with each turn's speaker and text resolved from PDNC."""
    return _resolve_scenes(_read_json(fixture_path))


def _resolve_scenes(fixture):
    if (not isinstance(fixture, dict)
            or not isinstance(fixture.get("source_fixture"), str)
            or not isinstance(fixture.get("scenes"), list)
            or not 2 <= len(fixture["scenes"]) <= 32):
        raise SceneFixtureError("fixture needs at least two scenes and at most 32")
    pattern = fixture["source_fixture"]
    if (len(pattern) > 4096 or pattern.count("{book}") != 1
            or "{" in pattern.replace("{book}", "")
            or "}" in pattern.replace("{book}", "")):
        raise SceneFixtureError("source_fixture must contain only one literal {book} placeholder")
    scenes, seen, books, book_hashes = [], set(), {}, {}
    for scene in fixture.get("scenes") or []:
        if not isinstance(scene, dict):
            raise SceneFixtureError("scene must be an object")
        scene_id = scene.get("id")
        if (not isinstance(scene_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", scene_id) is None
                or scene_id.casefold() in seen):
            raise SceneFixtureError(f"missing or duplicate scene id {scene_id!r}")
        seen.add(scene_id.casefold())
        if (not isinstance(scene.get("scene"), str)
                or not 1 <= len(scene["scene"].strip()) <= 4096):
            raise SceneFixtureError(f"{scene_id} has no scene description")
        voices = scene.get("voices") or {}
        if (not isinstance(voices, dict) or len(voices) != 2
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in voices.items())
                or sorted(voices.values()) != sorted(VOICE_ROLES)):
            raise SceneFixtureError(
                f"{scene_id} must cast exactly one female and one male speaker")
        book = scene.get("book")
        if not isinstance(book, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,80}", book) is None:
            raise SceneFixtureError(f"{scene_id}: invalid book")
        if book not in books:
            try:
                path = str((Path(REPO) / pattern.replace("{book}", book)).resolve())
                if not Path(path).is_relative_to(Path(REPO).resolve()):
                    raise ValueError("PDNC source must be inside the repository")
                source, source_hash = _capture_json(path, 8 * 1024 * 1024)
                book_hashes[book] = (os.path.relpath(path, REPO), source_hash)
                rows = source.get("entries") if isinstance(source, dict) else None
                if (not isinstance(rows, list) or len(rows) > 10000
                        or any(not isinstance(row, dict)
                               or any(not isinstance(row.get(key), str)
                                      for key in ("id", "line", "expected_speaker"))
                               or not 1 <= len(row["line"]) <= 16384 for row in rows)):
                    raise ValueError("invalid PDNC source rows")
                books[book] = {row["id"]: row for row in rows}
                if len(books[book]) != len(rows):
                    raise ValueError("duplicate PDNC source row")
            except (OSError, ValueError, KeyError, IndexError) as exc:
                raise SceneFixtureError(f"{scene_id}: cannot read PDNC source: {exc}") from exc
        turns = []
        if not isinstance(scene.get("turns"), list) or not 2 <= len(scene["turns"]) <= 64:
            raise SceneFixtureError(f"{scene_id} must be a bounded two-speaker exchange")
        for turn in scene["turns"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                raise SceneFixtureError(f"{scene_id}: invalid turn")
            row = books[book].get(turn.get("id"))
            if row is None:
                raise SceneFixtureError(f"{scene_id}: no PDNC row {turn.get('id')!r}")
            speaker = row.get("expected_speaker")
            if speaker not in voices:
                raise SceneFixtureError(
                    f"{scene_id}: {turn['id']} speaker {speaker!r} is not cast")
            if not isinstance(turn.get("instruct"), str) or len(turn["instruct"]) > 4096:
                raise SceneFixtureError(f"{scene_id}: invalid instruct")
            instruct = turn["instruct"].strip()
            if not instruct:
                raise SceneFixtureError(f"{scene_id}: {turn['id']} has no instruct")
            turns.append({"id": turn["id"], "speaker": speaker,
                          "role": voices[speaker],
                          "text": get_spoken_text(row["line"]),
                          "instruct": instruct})
        if len(turns) < 2 or len({t["speaker"] for t in turns}) != 2:
            raise SceneFixtureError(f"{scene_id} must be a two-speaker exchange")
        scenes.append({"id": scene_id, "book": book,
                       "source_fixture": book_hashes[book][0],
                       "source_sha256": book_hashes[book][1],
                       "scene": scene["scene"].strip(), "turns": turns})
    if len(scenes) < 2:
        raise SceneFixtureError("wrong_scene needs at least two scenes")
    books_by_scene = {scene["id"]: scene["book"] for scene in scenes}
    wrong = get_wrong_scene_map([scene["id"] for scene in scenes])
    if any(books_by_scene[own] == books_by_scene[borrowed] for own, borrowed in wrong.items()):
        raise SceneFixtureError("wrong_scene must borrow from a different book")
    return scenes


def get_wrong_scene_map(scene_ids):
    """A fixed rotation by half the list: never a scene's own description."""
    count = len(scene_ids)
    shift = max(1, count // 2)
    return {scene_id: scene_ids[(index + shift) % count]
            for index, scene_id in enumerate(scene_ids)}


def get_arm_instructions(scenes):
    """-> {scene_id: {arm: [instruction per turn]}}, exactly what is sent."""
    descriptions = {scene["id"]: scene["scene"] for scene in scenes}
    wrong = get_wrong_scene_map([scene["id"] for scene in scenes])
    out = {}
    for scene in scenes:
        lines = [turn["instruct"] for turn in scene["turns"]]
        out[scene["id"]] = {
            "line": lines,
            "scene": [f"{scene['scene']} {line}" for line in lines],
            "wrong_scene": [f"{descriptions[wrong[scene['id']]]} {line}"
                            for line in lines]}
    return out


def get_content_regressions(wer_by_arm, margin=WER_MARGIN):
    """Arms whose scene WER is worse than `line` by more than `margin`."""
    if (not isinstance(wer_by_arm, dict) or set(wer_by_arm) != set(ARMS)
            or any(wer is not None and (type(wer) not in (int, float) or not math.isfinite(wer) or wer < 0)
                   for wer in wer_by_arm.values())):
        return sorted(ARMS)
    baseline = wer_by_arm.get("line")
    if baseline is None:
        return sorted(wer_by_arm)
    return sorted(arm for arm, wer in wer_by_arm.items()
                  if arm != "line" and (wer is None or wer - baseline > margin))


def get_arm_voices(voice_config, voice_names, seed):
    """-> ({role: entry}, voice config) with inherited styles removed.

    The custom path looks the speaker up in the voice config itself, so the
    unstyled entry has to replace the configured one there as well as being
    passed directly; otherwise its `character_style` comes back.
    """
    from experiments.instruct_listening import get_unstyled_entry
    from tts import voice_category
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise SceneFixtureError("seed must be an integer between 0 and 2147483647")
    if set(voice_names) != set(VOICE_ROLES) or len(set(voice_names.values())) != 2:
        raise SceneFixtureError("cast needs two distinct voices")
    entries, arm_config = {}, dict(voice_config)
    for role, name in voice_names.items():
        if name not in voice_config:
            raise SceneFixtureError(f"voice {name!r} is not in the voice config")
        if not isinstance(voice_config[name], dict):
            raise SceneFixtureError("voice entry must be an object")
        entry = get_unstyled_entry(voice_config[name])
        if voice_category(entry) == "clone":
            raise SceneFixtureError(
                f"voice {name!r} is a clone voice; clones ignore instruct, so "
                "every arm would be the same request")
        entry["seed"] = str(seed)
        entries[role] = entry
        arm_config[name] = entry
    return entries, arm_config


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fixture", default=FIXTURE)
    ap.add_argument("--voice-config", default=os.path.join(REPO, "voice_config.json"))
    ap.add_argument("--config", default=os.path.join(APP, "config.json"))
    ap.add_argument("--female-voice", default="",
                    help="voice_config.json entry that plays every female part")
    ap.add_argument("--male-voice", default="",
                    help="voice_config.json entry that plays every male part")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--asr-model", default=None,
                    help="transformers Whisper id; default whisper-base.en")
    ap.add_argument("--out-dir", default=os.path.join(
        REPO, "ab_test_runtime", "scene_context_listening"))
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "scene_context_listening.json"))
    ap.add_argument("--dry-run", action="store_true",
                    help="print every instruction each arm would send, render nothing")
    args = ap.parse_args()

    fixture, fixture_hash = _capture_json(args.fixture)
    scenes = _resolve_scenes(fixture)
    instructions = get_arm_instructions(scenes)
    if args.dry_run:
        for scene in scenes:
            print(f"\n== {scene['id']}")
            for arm in ARMS:
                print(f"  [{arm}]")
                for turn, text in zip(scene["turns"], instructions[scene["id"]][arm]):
                    print(f"    {turn['speaker']}: {text}")
        return
    if not args.female_voice or not args.male_voice:
        ap.error("--female-voice and --male-voice are required to render")

    raw_vc, voices_hash = _capture_json(args.voice_config)
    if not isinstance(raw_vc, dict):
        raise SceneFixtureError("voice config must be an object")
    config, config_hash = _capture_json(args.config)
    if not isinstance(config, dict):
        raise SceneFixtureError("config must be an object")
    vc = (raw_vc.get("characters")
          if isinstance(raw_vc.get("characters"), dict) else raw_vc)
    names = {"female": args.female_voice, "male": args.male_voice}
    entries, arm_config = get_arm_voices(vc, names, args.seed)
    repo = Path(REPO).resolve()
    if any(not Path(os.path.realpath(path)).is_relative_to(repo)
           for path in (args.out_dir, args.out)):
        ap.error("--out-dir and --out must stay inside this checkout for listening packaging")
    input_hashes = {os.path.relpath(path, REPO): digest for path, digest in (
        (args.fixture, fixture_hash), (args.voice_config, voices_hash), (args.config, config_hash))}
    input_hashes.update({scene["source_fixture"]: scene["source_sha256"] for scene in scenes})
    output_paths = [os.path.normcase(os.path.realpath(os.path.join(args.out_dir, f"{scene['id']}_{arm}{suffix}.wav")))
               for scene in scenes for arm in ARMS
               for suffix in [""] + [f"_t{index}" for index in range(len(scene["turns"]))]]
    outputs = set(output_paths)
    if len(outputs) != len(output_paths):
        ap.error("scene IDs produce colliding audio filenames")
    if os.path.normcase(os.path.realpath(args.out)) in outputs:
        ap.error("--out must not collide with rendered audio")
    if os.path.lexists(args.out):
        ap.error("--out must be a new manifest path")
    os.makedirs(args.out_dir, mode=0o700, exist_ok=False)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "x", encoding="utf-8") as handle:
        json.dump({"status": "incomplete", "published": False}, handle)

    from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
    lease = acquire_gpu_lock()
    engine = None
    def set_engine(value):
        nonlocal engine
        engine = value
    try:
        document = _render_scenes(args, scenes, instructions, names, entries, arm_config,
                       config, input_hashes, set_engine)
    finally:
        try:
            if engine is not None and not engine.ensure_idle_local_models_released():
                raise RuntimeError("Experiment model cleanup did not finish")
        except BaseException:
            # Disposable native CLI: never release its lease with live models.
            os._exit(70)
        release_gpu_lock(lease)
    from utils import atomic_json_write
    _validate_input_hashes(input_hashes)
    atomic_json_write(document, args.out)
    print(f"\nwrote {args.out}" + ("" if document["published"] else
                                   "  (NOT publishable: see failures above)"))


def _validate_input_hashes(input_hashes):
    for relative, expected in input_hashes.items():
        _, current = _capture_json(os.path.join(REPO, relative), 8 * 1024 * 1024)
        if current != expected:
            raise SceneFixtureError("Experiment input changed during rendering")


def _render_scenes(args, scenes, instructions, names, entries, arm_config, config, input_hashes, set_engine):
    from pydub import AudioSegment
    from tts import TTSEngine, combine_audio_with_pauses
    from experiments.asr_backends import run_transformers_whisper, word_error_rate
    from experiments.generation import render, GenerationFailed
    from experiments.instruct_listening import identical_arms
    from experiments.provenance import provenance, file_sha256
    engine = TTSEngine(config)
    set_engine(engine)
    asr_cache = {}

    manifest, published, started = [], True, time.time()
    for number, scene in enumerate(scenes, 1):
        arm_files, wer_by_arm, failed = {}, {}, None
        for arm in ARMS:
            segments, speakers, errors = [], [], []
            for index, (turn, instruct) in enumerate(
                    zip(scene["turns"], instructions[scene["id"]][arm])):
                wav = os.path.join(args.out_dir, f"{scene['id']}_{arm}_t{index}.wav")
                try:
                    render(engine, turn["text"], instruct, names[turn["role"]],
                           arm_config, entries[turn["role"]], wav)
                except GenerationFailed as exc:
                    failed = f"{arm} turn {index}: {str(exc)[:80]}"
                    break
                segments.append(AudioSegment.from_wav(wav))
                speakers.append(turn["speaker"])
                hypothesis, _ = run_transformers_whisper(wav, args.asr_model, _cache=asr_cache, device=-1)
                errors.append(word_error_rate(turn["text"], hypothesis))
            if failed:
                break
            joined = os.path.join(args.out_dir, f"{scene['id']}_{arm}.wav")
            combine_audio_with_pauses(segments, speakers).export(joined, format="wav")
            arm_files[arm] = os.path.relpath(joined, REPO)
            wer_by_arm[arm] = (None if None in errors
                               else sum(errors) / len(errors))
        if failed:
            print(f"  {scene['id']}: FAILED {failed}")
            published = False
            continue
        hollow = identical_arms({arm: os.path.join(REPO, path)
                                 for arm, path in arm_files.items()})
        regressions = get_content_regressions(wer_by_arm)
        if hollow or regressions:
            published = False
        manifest.append({"scene_id": scene["id"], "book": scene["book"],
                         "turn_ids": [turn["id"] for turn in scene["turns"]],
                         "instructions": instructions[scene["id"]],
                         "arm_files": arm_files,
                         "arm_sha256": {arm: file_sha256(os.path.join(REPO, path))
                                        for arm, path in arm_files.items()},
                         "wer": wer_by_arm,
                         "identical_arms": hollow,
                         "content_regressions": regressions})
        elapsed = time.time() - started
        remaining = elapsed / number * (len(scenes) - number)
        print(f"  {scene['id']}: WER " + ", ".join(
            f"{arm} {wer_by_arm[arm]:.3f}" if wer_by_arm[arm] is not None
            else f"{arm} unscored" for arm in ARMS)
            + (f"  HOLLOW {hollow}" if hollow else "")
            + (f"  CONTENT REGRESSION {regressions}" if regressions else "")
            + f"  [{number}/{len(scenes)}, ~{remaining / 60:.1f} min left]")

    if not manifest:
        sys.exit("nothing to listen to")
    _validate_input_hashes(input_hashes)
    return {"status": "complete",
                       "provenance": provenance(
                           __file__, args,
                           input_sha256=input_hashes),
                       "seed": args.seed, "arms": list(ARMS),
                       "wer_margin": WER_MARGIN,
                       "wrong_scene_map": get_wrong_scene_map(
                           [scene["id"] for scene in scenes]),
                       "published": published and len(manifest) == len(scenes),
                       "comparisons": manifest}


if __name__ == "__main__":
    main()
