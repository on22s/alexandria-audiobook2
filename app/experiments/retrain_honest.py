"""Retrain with a genuinely held-out split, and see what changes.

TWO QUESTIONS AT ONCE, both unanswerable before the train/val fix landed.

1. DO THE CLEAN-DATA FAILURES RECOVER? Five adapters scored 0.027-0.404 while
   their training data was verifiably one speaker (0.69-0.81 internal
   consistency, as good as the working ones). Same learning rate, same 200
   samples, same 6 epochs, same final loss ~4.1 as the adapters that worked.
   Nothing in the recorded settings distinguishes them.

   If a retrain with identical settings produces a working adapter, training is
   stochastically unreliable and the library needs a post-training gate. If it
   reproduces the failure, something in that dataset is wrong in a way speaker
   consistency does not capture, and the recipe is not the problem.

2. HOW BIG IS THE CONTAMINATION BOUND? Every existing library score is measured
   on material the adapter trained on - `train_lora.py` read the root
   metadata.jsonl (all 200) rather than train/ (180). Retraining on 180 leaves
   the 20 val clips genuinely unseen, so these are the FIRST honest per-adapter
   numbers in this project.

   Controls are included for exactly this: working adapters retrained the same
   way. The gap between their contaminated and honest scores is the size of the
   bound, and it applies to every number in the library.

WRITES TO A SEPARATE DIRECTORY. The existing lora_models/ library is not
touched - a retrain that turns out worse must not destroy the adapter that
shipped.
"""
import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)

DEFAULT_ZIPS = os.environ.get(
    "ALEXANDRIA_VOICE_ZIPS",
    os.path.join(os.path.expanduser("~"), "Desktop", "zips2",
                 "_deduped_labeled"))


def find_zip(dataset, zip_dir):
    key = "".join(c for c in dataset.lower() if c.isalnum())
    for f in sorted(os.listdir(zip_dir)):
        if not f.endswith(".zip"):
            continue
        if "".join(c for c in f.lower() if c.isalnum()).startswith(key):
            return os.path.join(zip_dir, f)
    return None


def extract(zip_path, dest):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)
    return dest


def dataset_of(adapter, models_dir):
    p = os.path.join(models_dir, adapter, "training_meta.json")
    if not os.path.exists(p):
        return None, {}
    with open(p, encoding="utf-8") as handle:
        meta = json.load(handle)
    return os.path.basename(os.path.dirname(
        str(meta.get("ref_sample_audio") or ""))), meta


def is_completed_retrain_row(row, eval_lines=None):
    """A failed or unmeasured attempt is not a completed adapter measurement."""
    if (not isinstance(row, dict) or not isinstance(row.get("adapter"), str)
            or not row["adapter"] or row.get("error") or row.get("ecapa_error")
            or type(row.get("n")) is not int or row["n"] < 1
            or (eval_lines is not None and row["n"] != eval_lines)):
        return False
    score, duration = row.get("new_ecapa_heldout"), row.get("dur_ratio")
    return (type(score) in (int, float) and math.isfinite(score) and -1 <= score <= 1
            and type(duration) in (int, float) and math.isfinite(duration) and duration > 0)


def get_retrain_completion(results, adapters, controls, eval_lines):
    requested = list(adapters) + list(controls)
    names = [row.get("adapter") for row in results if isinstance(row, dict)]
    if len(requested) != len(set(requested)) or len(names) != len(set(names)):
        raise ValueError("retrain roster and results must name each adapter once")
    completed = {row["adapter"] for row in results
                 if is_completed_retrain_row(row, eval_lines)}
    return {"requested": len(requested), "completed": len(completed & set(requested)),
            "complete": completed.issuperset(requested)}


def get_retrain_settings(args):
    return {key: getattr(args, key) for key in ("epochs", "lora_r", "lora_alpha", "seed",
            "reference_rank", "eval_lines", "use_medoid", "medoid_clips")}


def is_matching_retrain_settings(settings, args):
    expected = get_retrain_settings(args)
    return (isinstance(settings, dict) and settings == expected
            and all(type(settings.get(key)) is type(value) for key, value in expected.items()))


def get_retrain_source_hashes(args, adapter):
    from experiments.provenance import input_sha256

    dataset, _ = dataset_of(adapter, args.models)
    archive = find_zip(dataset, args.zips) if dataset else None
    if not archive:
        raise ValueError(f"source ZIP missing for {adapter}")
    paths = [archive, os.path.join(args.models, adapter, "training_meta.json"),
             os.path.join(APP, "config.json"), __file__, os.path.join(APP, "train_lora.py"),
             os.path.join(APP, "tts.py"), os.path.join(APP, "voice_reference.py"),
             os.path.join(APP, "experiments", "generation.py"),
             os.path.join(APP, "experiments", "library_voice_fidelity.py")]
    return input_sha256(paths)


def get_retrain_input_hashes(args, adapter, include_generated=True):
    from experiments.provenance import input_sha256

    base = Path(args.work) / adapter
    paths = [base / "adapter" / name for name in ("adapter_model.safetensors",
             "adapter_config.json", "training_meta.json", "ref_sample.wav")]
    data = base / "data"
    if not (data / "metadata.jsonl").is_file() and not (data / "train/metadata.jsonl").is_file():
        raise ValueError("retrain working dataset has no training metadata")
    for folder in (data, base / "val"):
        paths.extend(path for path in sorted(folder.rglob("*")) if path.is_file()
                     and (path.suffix == ".wav" or path.name in ("metadata.jsonl", "ref_text.txt")))
    if include_generated:
        paths.extend(base / f"gen_{index}.wav" for index in range(args.eval_lines))
    return {**get_retrain_source_hashes(args, adapter), **input_sha256(paths)}


def get_validated_retrain_row(row, args):
    """Verify measured vectors and current source/training/output identities."""
    import soundfile as sf

    if not is_completed_retrain_row(row, args.eval_lines):
        raise ValueError("retrain row is not fully measured")
    dataset, _ = dataset_of(row["adapter"], args.models)
    if row.get("dataset") != dataset:
        raise ValueError("retrain row identifies another source dataset")
    scores, durations, pairs = row.get("ecapa_scores"), row.get("duration_ratios"), row.get("scored_pairs")
    if (not isinstance(scores, list) or not isinstance(durations, list) or not isinstance(pairs, list)
            or any(len(values) != args.eval_lines for values in (scores, durations, pairs))
            or any(type(value) not in (int, float) or not math.isfinite(value) or not -1 <= value <= 1 for value in scores)
            or any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in durations)
            or row["new_ecapa_heldout"] != round(statistics.median(scores), 4)
            or row["dur_ratio"] != round(statistics.median(durations), 3)):
        raise ValueError("retrain row has incomplete or inconsistent measured vectors")
    identity = {"schema_version": 1, "settings": get_retrain_settings(args),
                "inputs": get_retrain_input_hashes(args, row["adapter"])}
    evidence = row.get("measurement")
    if (not isinstance(evidence, dict) or type(evidence.get("schema_version")) is not int
            or evidence != identity or not is_matching_retrain_settings(evidence.get("settings"), args)):
        raise ValueError("retrain inputs or training settings changed")
    base = Path(args.work) / row["adapter"]
    for index, pair in enumerate(pairs):
        if (not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(path, str) for path in pair)
                or Path(pair[0]).parent != base / "val" or Path(pair[1]) != base / f"gen_{index}.wav"):
            raise ValueError("retrain row names another held-out output pair")
        human, generated = sf.info(pair[0]), sf.info(pair[1])
        if human.frames <= 0 or generated.frames <= 0:
            raise ValueError("retrain pair has no measured audio duration")
        ratio = (generated.frames / generated.samplerate) / (human.frames / human.samplerate)
        if durations[index] != ratio:
            raise ValueError("retrain duration disagrees with its actual held-out WAVs")
    return row


def get_completed_retrain_result(path, args):
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if (not isinstance(document, dict) or not is_matching_retrain_settings(document.get("settings"), args)
            or any(type(document.get(key)) is not type(getattr(args, key))
                   or document[key] != getattr(args, key)
                   for key in ("epochs", "lora_r", "seed", "reference_rank", "eval_lines"))
            or document.get("requested_adapters") != list(args.adapters)
            or document.get("requested_controls") != list(args.controls)):
        raise ValueError("retrain artifact declares another request or training settings")
    rows = document.get("results")
    if not isinstance(rows, list) or len(rows) != len(args.adapters + args.controls):
        raise ValueError("retrain result does not cover the full requested roster")
    expected_roles = {**{name: "failure" for name in args.adapters}, **{name: "control" for name in args.controls}}
    if {row.get("adapter") for row in rows if isinstance(row, dict)} != set(expected_roles):
        raise ValueError("retrain result has missing or unexpected adapters")
    for row in rows:
        if row.get("role") != expected_roles[row["adapter"]]:
            raise ValueError("retrain result has another adapter role")
        get_validated_retrain_row(row, args)
    completion = get_retrain_completion(rows, args.adapters, args.controls, args.eval_lines)
    if (not completion["complete"] or type(document.get("complete")) is not bool
            or any(type(document.get(key)) is not int for key in ("requested", "completed"))
            or any(document.get(key) != value for key, value in completion.items())):
        raise ValueError("retrain completion summary disagrees with measured roster")
    return document


def load_resumed_results(path, resume, seed, reference_rank, eval_lines=None, args=None):
    if not resume or not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            prior = json.load(handle)
    except json.JSONDecodeError:
        return []
    if not isinstance(prior, dict):
        raise ValueError("resume artifact must be an object")
    if (type(prior.get("seed")) is not int or prior["seed"] != seed
            or type(prior.get("reference_rank", 0)) is not int
            or prior.get("reference_rank", 0) != reference_rank):
        raise ValueError("resume artifact settings do not match this run")
    rows = prior.get("results") or []
    if not isinstance(rows, list):
        raise ValueError("resume artifact results must be a list")
    measured = [row for row in rows if is_completed_retrain_row(row, eval_lines)]
    names = [row["adapter"] for row in measured]
    if len(names) != len(set(names)):
        raise ValueError("resume artifact repeats a completed adapter")
    if args is not None:
        current = []
        for row in measured:
            if row["adapter"] not in args.adapters + args.controls:
                continue
            try:
                get_validated_retrain_row(row, args)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                continue
            expected_role = "control" if row["adapter"] in args.controls else "failure"
            if row.get("role") == expected_role:
                current.append(row)
        measured = current
    return measured


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--adapters", nargs="+", required=True)
    ap.add_argument("--controls", nargs="*", default=[],
                    help="working adapters, retrained to size the "
                         "contamination bound")
    ap.add_argument("--models", default=os.path.join(REPO, "lora_models"))
    ap.add_argument("--zips", default=DEFAULT_ZIPS)
    ap.add_argument("--work", default=os.path.join(
        REPO, "ab_test_runtime", "retrain_honest"))
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--lora-r", type=int, default=64)
    ap.add_argument("--lora-alpha", type=int, default=128)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--use-medoid", action="store_true",
                    help="write a medoid ref.wav before training instead of "
                         "letting train_lora fall back to the first training "
                         "clip. The fallback is a LOTTERY - it recovered "
                         "husky_baritone_20s_m_anime (0.004 -> 0.691) and did "
                         "nothing for husky_baritone_40s_m_military "
                         "(0.141 -> 0.149), which is the difference this flag "
                         "removes.")
    ap.add_argument("--medoid-clips", type=int, default=14)
    ap.add_argument("--reference-rank", type=int, default=0,
                    help="0=best medoid; 1=second-ranked reference candidate")
    ap.add_argument("--eval-lines", type=int, default=8)
    ap.add_argument("--out", default=os.path.join(
        REPO, "ab_test_runtime", "experiments", "retrain_honest.json"))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--check-artifact", help="validate a completed retrain without inference")
    args = ap.parse_args()
    if args.eval_lines < 1:
        ap.error("eval-lines must be positive")
    if len(args.adapters + args.controls) != len(set(args.adapters + args.controls)):
        ap.error("adapters and controls must name each adapter once")

    if args.check_artifact:
        try:
            get_completed_retrain_result(args.check_artifact, args)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            ap.exit(1, f"REFUSING incomplete or stale retrain result: {error}\n")
        return
    py = os.path.join(APP, "env", "bin", "python")
    os.makedirs(args.work, exist_ok=True)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vcv", os.path.join(APP, "experiments", "voice_compare_view.py"))
    vcv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vcv)
    sys.path.insert(0, os.path.join(APP, "experiments"))
    from library_voice_fidelity import ecapa_pairs, extract_val

    jobs = [(a, "failure") for a in args.adapters] + \
           [(a, "control") for a in args.controls]
    results = load_resumed_results(
        args.out, args.resume, args.seed, args.reference_rank, args.eval_lines, args)
    completed = {row.get("adapter") for row in results}

    def build_doc():
        """The artifact's shape, in one place.

        SAY WHAT WAS ASKED FOR, NOT JUST WHAT FINISHED. The queue seeds the
        21-adapter artifact by copying the 5-adapter pilot into it, and each
        completed adapter is written back as it lands so an interrupt can
        resume. Both are deliberate, but together they mean a file named
        reference_rank1_all21.json legitimately holds 5 results for hours
        with nothing in it saying so.

        This shape existed in two places - the incremental checkpoint and
        the final write - and only the final one was taught to record the
        request. That put the completeness fields everywhere except the
        artifact that needs them: the partial one sitting on disk while the
        run is still going, which is the only one anybody reads mid-flight.
        One builder, so the checkpoint cannot drift from the result again.
        """
        return {"settings": get_retrain_settings(args), "epochs": args.epochs, "lora_r": args.lora_r,
                "seed": args.seed,
                "reference_rank": args.reference_rank,
                "eval_lines": args.eval_lines,
                "requested_adapters": list(args.adapters),
                "requested_controls": list(args.controls),
                **get_retrain_completion(results, args.adapters, args.controls, args.eval_lines),
                "results": results}

    def save_results():
        from utils import atomic_json_write
        atomic_json_write(build_doc(), args.out)
    print(f"{len(jobs)} retrains ({len(args.adapters)} failures, "
          f"{len(args.controls)} controls)\n")

    for adapter, role in jobs:
        if adapter in completed:
            print(f"  {adapter[:34]:36} RESUMED")
            continue
        dataset, old_meta = dataset_of(adapter, args.models)
        zp = find_zip(dataset, args.zips) if dataset else None
        if not zp:
            results.append({"adapter": adapter, "role": role,
                            "error": "source zip not found"})
            save_results()
            print(f"  {adapter[:34]:36} NO ZIP")
            continue
        source_hashes = get_retrain_source_hashes(args, adapter)
        ddir = os.path.join(args.work, adapter, "data")
        odir = os.path.join(args.work, adapter, "adapter")
        if not os.path.exists(os.path.join(ddir, "metadata.jsonl")):
            extract(zp, ddir)

        # CHOOSE THE REFERENCE DELIBERATELY when asked. Without this,
        # train_lora falls back to the first training clip - which is how the
        # earlier retrains "recovered": the fallback happened to be a better
        # reference than the shipped one. That makes recovery a property of
        # clip ordering rather than of anything chosen, and it explains why one
        # adapter jumped 0.687 and another moved 0.009 under identical
        # treatment.
        ref_note = None
        if args.use_medoid:
            from voice_reference import select_reference_sample
            meta_rel = os.path.join(ddir, "train", "metadata.jsonl")
            if not os.path.exists(meta_rel):
                meta_rel = os.path.join(ddir, "metadata.jsonl")
            drows = [json.loads(l) for l in open(meta_rel, encoding="utf-8")
                     if l.strip()][:args.medoid_clips]
            candidate_rows = [(os.path.join(ddir, r["audio_filepath"]), r)
                              for r in drows
                              if os.path.exists(os.path.join(
                                  ddir, r["audio_filepath"]))]
            cand = [path for path, _row in candidate_rows]
            pick, score = select_reference_sample(
                cand, max_clips=args.medoid_clips,
                reference_rank=args.reference_rank, dataset_root=ddir)
            if pick is not None:
                import shutil as _sh
                _sh.copy2(cand[pick], os.path.join(ddir, "ref.wav"))
                ref_text = str(candidate_rows[pick][1].get("text") or "").strip()
                if not ref_text:
                    raise ValueError("selected medoid has no transcript")
                with open(os.path.join(ddir, "ref_text.txt"), "w",
                          encoding="utf-8") as handle:
                    handle.write(ref_text)
                ref_note = {"clip": os.path.basename(cand[pick]),
                            "similarity": score,
                            "rank": args.reference_rank,
                            "text": ref_text}
                print(f"    reference: {ref_note['clip']} "
                      f"(similarity {score})")
            else:
                print("    reference: could not choose a medoid; "
                      "train_lora will fall back to the first clip")

        # The trainer now prefers train/metadata.jsonl when it exists, so the
        # 20 val clips stay unseen. That is the whole point of this run.
        cmd = [py, "-u", os.path.join(APP, "train_lora.py"),
               "--data_dir", ddir, "--output_dir", odir,
               "--epochs", str(args.epochs), "--lora_r", str(args.lora_r),
               "--lora_alpha", str(args.lora_alpha), "--seed", str(args.seed)]
        log = os.path.join(REPO, "ab_test_runtime", "logs",
                           f"retrain_{adapter}.log")
        with open(log, "w", encoding="utf-8") as fh:
            rc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                timeout=7200).returncode
        if rc != 0 or not os.path.exists(
                os.path.join(odir, "adapter_model.safetensors")):
            results.append({"adapter": adapter, "role": role,
                            "error": f"train rc={rc}"})
            save_results()
            print(f"  {adapter[:34]:36} TRAIN FAILED rc={rc}")
            continue
        trained_on = None
        with open(log, encoding="utf-8") as fh:
            for line in fh:
                if "[DATA] Found" in line:
                    trained_on = line.strip()[:90]
                    break

        # Score on the held-out val clips - unseen for the first time.
        clips = extract_val(zp, os.path.join(args.work, adapter, "val"),
                            args.eval_lines)
        from tts import TTSEngine
        from experiments.generation import render, GenerationFailed
        with open(os.path.join(APP, "config.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        engine = TTSEngine(config)
        entry = {"type": "lora",
                 "adapter_path": os.path.relpath(odir, REPO),
                 "seed": str(args.seed)}
        generation_inputs = get_retrain_input_hashes(args, adapter, include_generated=False)
        pairs, durs = [], []
        for i, (human_wav, text) in enumerate(clips):
            gen = os.path.join(args.work, adapter, f"gen_{i}.wav")
            try:
                render(engine, text, "", "SPEAKER", {"SPEAKER": entry},
                       entry, gen)
            except GenerationFailed:
                continue
            import soundfile as sf
            durs.append((sf.info(gen).frames / sf.info(gen).samplerate) /
                        max(sf.info(human_wav).frames /
                            sf.info(human_wav).samplerate, 1e-6))
            pairs.append([human_wav, gen])
        cos, err = ecapa_pairs(pairs, os.environ.get(
            "ALEXANDRIA_SIBLING_PYTHON",
            os.path.join(os.path.dirname(REPO), "alexandria-audiobook.git",
                         "app", "env", "bin", "python")))
        vals = [c for c in (cos or []) if c is not None]
        rec = {"adapter": adapter, "role": role, "dataset": dataset,
               "trained_on": trained_on, "medoid_reference": ref_note,
               "old_ecapa_contaminated": None,
               "new_ecapa_heldout": round(statistics.median(vals), 4)
               if vals else None,
               "dur_ratio": round(statistics.median(durs), 3) if durs else None,
               "n": len(vals), "ecapa_error": err,
               "ecapa_scores": vals, "duration_ratios": durs, "scored_pairs": pairs}
        if len(clips) != args.eval_lines or len(pairs) != args.eval_lines or len(cos or []) != args.eval_lines:
            rec["error"] = "not every requested held-out clip was measured"
        elif (source_hashes != get_retrain_source_hashes(args, adapter)
                or generation_inputs != get_retrain_input_hashes(args, adapter, include_generated=False)):
            rec["error"] = "retrain inputs changed during measurement"
        elif is_completed_retrain_row(rec, args.eval_lines):
            rec["measurement"] = {"schema_version": 1, "settings": get_retrain_settings(args),
                                  "inputs": get_retrain_input_hashes(args, adapter)}
            try:
                get_validated_retrain_row(rec, args)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
                rec["error"] = str(error)
        results.append(rec)
        save_results()
        print(f"  {adapter[:34]:36} {role:8} held-out ecapa "
              f"{rec['new_ecapa_heldout']}  dur {rec['dur_ratio']}")

    # Attach the old contaminated score for comparison.
    fid = os.path.join(REPO, "ab_test_runtime", "experiments",
                       "library_voice_fidelity.json")
    if os.path.exists(fid):
        old = {r["adapter"]: r.get("ecapa")
               for r in json.load(open(fid, encoding="utf-8"))["results"]}
        for r in results:
            r["old_ecapa_contaminated"] = old.get(r["adapter"])

    print(f"\n  {'adapter':34}{'role':9}{'was':>8}{'now':>8}{'delta':>8}")
    for r in results:
        if r.get("new_ecapa_heldout") is None:
            continue
        o = r.get("old_ecapa_contaminated")
        d = (r["new_ecapa_heldout"] - o) if o is not None else float("nan")
        print(f"  {r['adapter'][:33]:34}{r['role']:9}"
              f"{o if o is not None else float('nan'):8.3f}"
              f"{r['new_ecapa_heldout']:8.3f}{d:+8.3f}")
    print("\n  'was' is contaminated (trained on its own eval clips);")
    print("  'now' is honest. Controls show how much of any drop is the")
    print("  contamination rather than the retrain.")

    doc = build_doc()
    try:
        from experiments.provenance import provenance
        doc["provenance"] = provenance(__file__, args)
    except Exception as exc:                                # noqa: BLE001
        doc["provenance"] = {"error": str(exc)[:120]}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    from utils import atomic_json_write
    atomic_json_write(doc, args.out)
    print(f"\nwrote {args.out}")
    if not doc["complete"]:
        sys.exit(3)


if __name__ == "__main__":
    main()
