#!/usr/bin/env python3
"""
batch_train_lora.py — Train a LoRA adapter for every narrator zip in a directory.

Reads all .zip files from --zips_dir (defaults to the _deduped folder), extracts
each one, runs train_lora.py with early stopping at --target_loss, saves the
adapter, updates the manifest, then cleans up. Fully resumable: skips any narrator
whose adapter already exists in --models_dir.

Usage:
    python batch_train_lora.py [options]

    # Dry-run — list what would be trained:
    python batch_train_lora.py --dry_run

    # Full run with defaults:
    python batch_train_lora.py

    # Custom zips folder:
    python batch_train_lora.py --zips_dir /path/to/my/zips
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

REPO2_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_DIR = os.path.join(REPO2_DIR, "app")
sys.path.insert(0, APP_DIR)
from adapter_artifacts import AdapterValidationError, validate_adapter_artifacts
from archive_utils import validate_zip_members
from device_utils import normalize_device
from utils import file_lock
from voice_manifest import get_voice_manifest, validate_adapter_registration_id_locked, validate_adapter_training_output, get_resolved_adapter_manifest_rows_locked
from adapter_naming_transaction import lock_adapter_naming
from adapter_publication import get_adapter_publication_recovery_command

# ── Paths ────────────────────────────────────────────────────────────────────

TRAIN_SCRIPT = os.path.join(REPO2_DIR, "app", "train_lora.py")
PYTHON       = os.path.join(REPO2_DIR, "app", "env", "bin", "python")
DATASETS_DIR = os.path.join(REPO2_DIR, "lora_datasets")
MODELS_DIR   = os.path.join(REPO2_DIR, "lora_models")
MANIFEST     = os.path.join(MODELS_DIR, "manifest.json")
DEFAULT_ZIPS = os.path.join(os.environ.get("ALEXANDRIA_ZIPS_DIR",
                                           os.path.join(REPO2_DIR, "zips2")), "_deduped")


# ── Helpers ──────────────────────────────────────────────────────────────────

def sanitize(name: str) -> str:
    """Convert a filename into a safe dataset/adapter id."""
    raw_name = os.path.splitext(os.path.basename(name))[0]
    name = raw_name.lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        digest = hashlib.sha256(raw_name.lower().encode("utf-8")).hexdigest()[:12]
        name = f"dataset_{digest}"
    return name


def get_dataset_id_collisions(zip_paths: list[str]) -> dict[str, list[str]]:
    """Return lossy normalized IDs shared by more than one input ZIP."""
    by_id = {}
    for path in zip_paths:
        by_id.setdefault(sanitize(path), []).append(path)
    return {dataset_id: paths for dataset_id, paths in by_id.items() if len(paths) > 1}


def load_manifest(path: str) -> list:
    if os.path.exists(path):
        return get_voice_manifest(path)
    return []


def save_manifest(path: str, data: list):
    """Write via temp file + os.replace, so a crash mid-write (Ctrl+C, OOM-kill,
    a training subprocess dying) can't truncate/corrupt manifest.json - this
    is checkpointed after every single narrator specifically so an interrupted
    run can resume, and voice_profiler.py/name_voices.py/the web app's LoRA
    listing all depend on this same file staying valid."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def is_completed_adapter(path: str) -> bool:
    """Return whether path contains a complete, structurally valid PEFT artifact."""
    try:
        validate_adapter_artifacts(path, require_training_meta=True)
        return True
    except AdapterValidationError:
        return False


def adapter_exists(models_dir: str, dataset_id: str, manifest: list) -> str | None:
    """Return the existing adapter path if one was already trained for this dataset_id.

    Checks the manifest's own dataset_id field first - this is the only field
    guaranteed to survive name_voices.py's renaming stage (it rewrites id/name
    to a descriptive slug but leaves dataset_id alone), so it's what makes
    resume-skip still work on a narrator whose adapter has already been
    renamed. Falls back to a directory-name-prefix scan for adapters that
    predate manifest tracking or aren't yet registered in it.
    """
    with file_lock(os.path.join(models_dir, "manifest.json")):
        pending = get_adapter_publication_recovery_command(models_dir)
        if pending:
            raise ValueError("Adapter publication recovery is required: " + pending)
        canonical = get_resolved_adapter_manifest_rows_locked(models_dir, manifest)
        for entry in canonical:
            if entry.get("dataset_id") == dataset_id:
                candidate = os.path.join(models_dir, entry.get("id", ""))
                if is_completed_adapter(candidate):
                    return candidate
        for name in os.listdir(models_dir):
            candidate = os.path.join(models_dir, name)
            if (name == dataset_id or name.startswith(dataset_id + "_")) and is_completed_adapter(candidate):
                return candidate
        return None


def extract_zip(zip_path: str, dest_dir: str):
    """Extract zip, flattening a single top-level directory if present."""
    if os.path.islink(dest_dir):
        raise ValueError("dataset extraction destination is a symlink")
    for root, directories, files in os.walk(dest_dir):
        for name in directories + files:
            if os.path.islink(os.path.join(root, name)):
                raise ValueError("dataset extraction destination contains a symlink")
    with zipfile.ZipFile(zip_path, "r") as zf:
        validate_zip_members(zf, dest_dir)
        zf.extractall(dest_dir)
    # If metadata.jsonl is not at root, look one level deep and flatten
    if not os.path.exists(os.path.join(dest_dir, "metadata.jsonl")):
        for entry in os.listdir(dest_dir):
            candidate = os.path.join(dest_dir, entry, "metadata.jsonl")
            if os.path.isdir(os.path.join(dest_dir, entry)) and os.path.exists(candidate):
                nested = os.path.join(dest_dir, entry)
                for item in os.listdir(nested):
                    shutil.move(os.path.join(nested, item), os.path.join(dest_dir, item))
                os.rmdir(nested)
                break


def parse_epoch_losses(lines: list[str]) -> dict[int, float]:
    """Extract {epoch: avg_loss} from train_lora.py output lines."""
    losses = {}
    for line in lines:
        if line.startswith("[EPOCH]"):
            # [EPOCH] 3/5 avg_loss=4.3254
            m = re.search(r"\[EPOCH\]\s+(\d+)/\d+\s+avg_loss=([0-9.]+)", line)
            if m:
                losses[int(m.group(1))] = float(m.group(2))
    return losses


# ── Core training loop ───────────────────────────────────────────────────────

def train_one(zip_path: str, dataset_id: str, adapter_id: str, args) -> dict | None:
    """Extract, train, register. Returns the training_meta dict or None on failure."""
    dataset_dir = os.path.join(args.datasets_dir, dataset_id)
    output_dir  = os.path.join(args.models_dir, adapter_id)
    output_existed = os.path.exists(output_dir)
    manifest_path = getattr(args, 'manifest', os.path.join(args.models_dir, 'manifest.json'))
    os.makedirs(args.models_dir, exist_ok=True)
    if output_existed:
        try:
            validate_adapter_training_output(output_dir)
        except (OSError, ValueError) as error:
            print(f'  ERROR admitting existing adapter output: {error}', flush=True)
            return None
    if not output_existed:
        try:
            with lock_adapter_naming(args.models_dir, manifest_path):
                pending = get_adapter_publication_recovery_command(args.models_dir)
                if pending:
                    raise ValueError('Adapter publication recovery is required: ' + pending)
                validate_adapter_registration_id_locked(args.models_dir, adapter_id, load_manifest(manifest_path))
                os.mkdir(output_dir)
        except (OSError, ValueError) as error:
            print(f'  ERROR reserving adapter output: {error}', flush=True)
            return None

    def fail(message: str) -> None:
        print(f"  ERROR {message}", flush=True)
        shutil.rmtree(dataset_dir, ignore_errors=True)
        if not output_existed:
            shutil.rmtree(output_dir, ignore_errors=True)

    # Extract zip
    print(f"  Extracting...", flush=True)
    os.makedirs(dataset_dir, exist_ok=True)
    try:
        extract_zip(zip_path, dataset_dir)
    except Exception as e:
        fail(f"extracting: {e}")
        return None

    # Verify metadata
    meta_path = os.path.join(dataset_dir, "metadata.jsonl")
    if not os.path.exists(meta_path):
        fail("no metadata.jsonl after extraction")
        return None

    try:
        with open(meta_path, encoding="utf-8") as metadata_file:
            sample_count = sum(1 for line in metadata_file if line.strip())
    except (OSError, UnicodeError) as exc:
        fail(f"reading metadata: {exc}")
        return None

    # Build command
    command = [
        args.python, "-u", args.train_script,
        "--data_dir",    dataset_dir,
        "--output_dir",  output_dir,
        "--epochs",      str(args.max_epochs),
        "--lr",          str(args.lr),
        "--batch_size",  "1",
        "--lora_r",      str(args.lora_r),
        "--lora_alpha",  str(args.lora_alpha),
        "--gradient_accumulation_steps", str(args.grad_accum),
        "--language",    args.language,
        "--target_loss", str(args.target_loss),
        "--candidate_checkpoints", str(getattr(args, "candidate_checkpoints", 0)),
    ]
    if getattr(args, "device", None):
        command += ["--device", args.device]

    print(f"  Training: max_epochs={args.max_epochs} lr={args.lr} "
          f"r={args.lora_r} alpha={args.lora_alpha} target_loss={args.target_loss}", flush=True)

    t0 = time.time()
    log_lines = []
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                line = line.rstrip()
                log_lines.append(line)
                # Print summary lines to console, skip per-step noise
                if any(line.startswith(tag) for tag in
                       ("[EPOCH]", "[DONE]", "[ERROR]", "[TRAIN] Early", "[TRAIN] Safe",
                        "[DATA] Found", "[DATA] Prepared", "[DATA] Duration",
                        "[DATA] Using reference", "[TRAIN] ===", "[TRAIN]   ")):
                    print(f"  {line}", flush=True)
            proc.wait()
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            finally:
                proc.stdout.close()
        elapsed = time.time() - t0

        if proc.returncode != 0:
            fail(f"train_lora.py exited {proc.returncode}")
            return None

    except Exception as e:
        fail(f"running training: {e}")
        return None

    # Load training_meta.json written by train_lora.py
    meta_file = os.path.join(output_dir, "training_meta.json")
    if not is_completed_adapter(output_dir):
        fail("adapter output is incomplete")
        return None

    try:
        with open(meta_file, encoding="utf-8") as f:
            training_meta = json.load(f)
        epoch_losses = parse_epoch_losses(log_lines)
        final_loss = training_meta.get("final_loss")
        best_loss = training_meta.get("best_loss", final_loss)
        if not isinstance(best_loss, (int, float)):
            raise ValueError("training metadata has no numeric best_loss/final_loss")
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        fail(f"invalid training metadata: {e}")
        return None

    print(f"  Epoch losses: {epoch_losses}", flush=True)
    if not epoch_losses:
        print("  Epoch count unavailable: no parseable epoch summaries.", flush=True)
    print(f"  Adapter saved — best_loss={best_loss:.4f}  time={elapsed:.0f}s", flush=True)

    # Cleanup extracted dataset
    if not args.keep_datasets:
        shutil.rmtree(dataset_dir, ignore_errors=True)
        print(f"  Cleaned up dataset dir", flush=True)

    return {
        "id":           adapter_id,
        "name":         dataset_id,
        "dataset_id":   dataset_id,
        "zip_source":   zip_path,
        "epochs_run":   max(epoch_losses.keys()) if epoch_losses else None,
        "epoch_losses": epoch_losses,
        "final_loss":   final_loss,
        "best_loss":    best_loss,
        "sample_count": sample_count,
        "lora_r":       args.lora_r,
        "lr":           args.lr,
        "target_loss":  args.target_loss,
        "checkpoint_sha256": training_meta.get("checkpoint_sha256"),
        "evaluation_candidates": training_meta.get("evaluation_candidates", []),
        "evaluation_candidate_skips": training_meta.get("evaluation_candidate_skips", []),
        "created":      time.time(),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Batch LoRA training for all narrator zips")
    parser.add_argument("--zips_dir",    default=DEFAULT_ZIPS,
                        help=f"Directory of narrator zips (default: {DEFAULT_ZIPS})")
    parser.add_argument("--datasets_dir", default=DATASETS_DIR,
                        help="Where to extract datasets before training")
    parser.add_argument("--models_dir",  default=MODELS_DIR,
                        help="Where to save trained adapters")
    parser.add_argument("--manifest",    default=MANIFEST,
                        help="manifest.json to update with completed adapters")
    parser.add_argument("--train_script", default=TRAIN_SCRIPT,
                        help=f"Path to train_lora.py (default: {TRAIN_SCRIPT})")
    parser.add_argument("--python",      default=PYTHON,
                        help=f"Python interpreter to run train_script with (default: {PYTHON})")
    parser.add_argument("--target_loss", type=float, default=4.15,
                        help="Early-stop target loss (default: 4.15)")
    parser.add_argument("--max_epochs",  type=int,   default=6,
                        help="Max epochs per narrator if target not reached (default: 6)")
    parser.add_argument("--lr",          type=float, default=1e-6,  help="Learning rate")
    parser.add_argument("--lora_r",      type=int,   default=64,    help="LoRA rank")
    parser.add_argument("--lora_alpha",  type=int,   default=128,   help="LoRA alpha")
    parser.add_argument("--grad_accum",  type=int,   default=4,     help="Gradient accumulation steps")
    parser.add_argument("--language",    default="english",          help="Language")
    parser.add_argument("--keep_datasets", action="store_true",
                        help="Don't delete extracted datasets after training")
    parser.add_argument("--candidate_checkpoints", type=int, default=0, choices=range(0, 3),
                        help="Temporary evaluation candidates per adapter (0-2)")
    parser.add_argument("--device", type=normalize_device, default="auto",
                        help="Device: auto, cpu, cuda, cuda:N, mps, rocm, or hip")
    parser.add_argument("--dry_run",     action="store_true",
                        help="List what would be trained without running")
    args = parser.parse_args()

    # Find zips
    if not os.path.isdir(args.zips_dir):
        print(f"ERROR: --zips_dir not found: {args.zips_dir}")
        return 1

    zips = sorted(
        os.path.join(args.zips_dir, f)
        for f in os.listdir(args.zips_dir)
        if not f.startswith("_") and f.lower().endswith(".zip")
    )

    if not zips:
        print(f"No .zip files found in {args.zips_dir}")
        return 1

    collisions = get_dataset_id_collisions(zips)
    if collisions:
        for dataset_id, paths in sorted(collisions.items()):
            names = ", ".join(os.path.basename(path) for path in paths)
            print(f"ERROR: ZIP names normalize to the same dataset id '{dataset_id}': {names}")
        return 1

    os.makedirs(args.models_dir, exist_ok=True)
    os.makedirs(args.datasets_dir, exist_ok=True)

    try:
        manifest = load_manifest(args.manifest)
    except (OSError, ValueError) as e:
        print(f"ERROR: manifest unreadable: {e}", flush=True)
        return 1

    print(f"Found {len(zips)} zip(s) in {args.zips_dir}")
    print(f"Target loss: {args.target_loss}  Max epochs: {args.max_epochs}  LR: {args.lr}")
    print(f"LoRA r={args.lora_r} alpha={args.lora_alpha}  Grad accum: {args.grad_accum}")
    print(f"Models dir: {args.models_dir}")
    if args.dry_run:
        print("[dry-run] No training will run\n")
    print()

    done = skip = err = 0
    start_all = time.time()

    for i, zip_path in enumerate(zips, 1):
        dataset_id = sanitize(zip_path)
        adapter_id = f"{dataset_id}_{int(time.time())}"

        # Skip if already trained
        existing = adapter_exists(args.models_dir, dataset_id, manifest)
        if existing:
            print(f"[{i:3d}/{len(zips)}] SKIP  {os.path.basename(zip_path)}")
            print(f"          (adapter exists: {os.path.basename(existing)})")
            skip += 1
            continue

        print(f"[{i:3d}/{len(zips)}] TRAIN {os.path.basename(zip_path)}", flush=True)

        if args.dry_run:
            continue

        result = train_one(zip_path, dataset_id, adapter_id, args)

        if result is None:
            err += 1
            print(f"  FAILED\n", flush=True)
            continue

        # Register in manifest
        try:
            with lock_adapter_naming(args.models_dir, args.manifest):
                pending = get_adapter_publication_recovery_command(args.models_dir)
                if pending:
                    raise ValueError('Adapter publication recovery is required: ' + pending)
                manifest = load_manifest(args.manifest)
                validate_adapter_registration_id_locked(args.models_dir, result['id'], manifest)
                manifest.append(result)
                save_manifest(args.manifest, manifest)
        except (OSError, ValueError) as e:
            err += 1
            print(f"  ERROR: unable to register adapter in manifest: {e}\n", flush=True)
            continue
        done += 1

        elapsed_all = time.time() - start_all
        try:
            remaining = sum(1 for path in zips[i:]
                            if not adapter_exists(args.models_dir, sanitize(path), manifest))
        except (OSError, ValueError) as e:
            print(f"  Progress: {done} done, {skip} skipped, {err} errors — "
                  f"ETA unavailable: {e}\n", flush=True)
            continue
        avg_per = elapsed_all / (done + err)
        eta_s = remaining * avg_per
        eta_min = eta_s / 60
        print(f"  Progress: {done} done, {skip} skipped, {err} errors — "
              f"ETA: {eta_min:.0f} min for {remaining} remaining\n", flush=True)

    total = time.time() - start_all
    print(f"\n{'='*60}")
    print(f"Done: {done} trained, {skip} skipped, {err} errors")
    print(f"Total time: {total/60:.1f} min")
    if done:
        print(f"Adapters in: {args.models_dir}")
    return 1 if err else 0


if __name__ == "__main__":
    sys.exit(main())
