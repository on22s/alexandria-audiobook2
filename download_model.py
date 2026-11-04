#!/usr/bin/env python3
"""Download whisper-base model for offline use."""

import os
import sys
import shutil
import tempfile
import json
import stat

# Try to import with proper error handling
try:
    from transformers import AutoProcessor
    from huggingface_hub import snapshot_download
except ImportError:
    print("ERROR: transformers or huggingface_hub not installed")
    print("Install with: pip install transformers huggingface_hub")
    sys.exit(1)

def get_recovery_identity(path, directory=True):
    """Identify a local entry without following links (including Windows junctions)."""
    info = os.lstat(path)
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError(f"Recovery entry must be a plain {'directory' if directory else 'file'}: {path}")
    if not info.st_ino:
        raise ValueError(f"Recovery identity unavailable: {path}")
    return [info.st_dev, info.st_ino]


def recover_previous_model(model_path, backup_path, receipt_path):
    """Restore only the exact prior directory recorded before its rename."""
    if not os.path.lexists(receipt_path):
        if os.path.lexists(backup_path):
            raise ValueError(f"Unidentified backup; inspect and recover manually: {backup_path}")
        return
    try:
        get_recovery_identity(receipt_path, directory=False)
        with open(receipt_path, encoding='utf-8') as stream:
            identity = json.load(stream)
        if (not isinstance(identity, list) or len(identity) != 2
                or any(type(value) is not int or value < 0 for value in identity)
                or not identity[1]):
            raise ValueError('Invalid prior-directory identity')
        if os.path.lexists(model_path):
            if os.path.lexists(backup_path) or get_recovery_identity(model_path) != identity:
                raise ValueError('Installed directory and pending backup need manual reconciliation')
        else:
            if get_recovery_identity(backup_path) != identity:
                raise ValueError('Prior-directory identity does not match')
            os.replace(backup_path, model_path)
            print(f"Restored previous offline directory: {model_path}")
        os.unlink(receipt_path)
    except Exception as exc:
        raise ValueError(f"Recovery blocked; keep all remnants and inspect {receipt_path} "
                         f"and {backup_path} before retrying: {exc}") from exc


def download_model():
    """Download and cache the whisper-base model locally.

    Publish the directory only after both save operations complete. A failed
    download or replacement must not damage an existing offline bundle.
    """
    script_dir = os.path.dirname(os.path.realpath(__file__))
    model_path = os.path.join(script_dir, "models", "whisper-base")

    print("=" * 70)
    print("Downloading OpenAI Whisper Base Model")
    print("=" * 70)
    print(f"Destination: {model_path}")
    print()

    staged_path = None
    backup_path = None
    receipt_path = os.path.join(os.path.dirname(model_path), '.whisper-base-replacement.json')
    previous_path = os.path.join(os.path.dirname(model_path), '.whisper-base.previous')
    try:
        parent = os.path.dirname(model_path)
        os.makedirs(parent, exist_ok=True)
        if os.path.normcase(os.path.realpath(parent)) != os.path.normcase(os.path.abspath(parent)):
            raise ValueError(f"Offline model parent must not contain links: {parent}")
        remnants = [name for name in os.listdir(parent)
                    if name.startswith('.whisper-base-download-') and name.endswith('.previous')]
        if remnants:
            raise ValueError(f"Unidentified previous downloads; keep and inspect manually in {parent}: {remnants}")
        recover_previous_model(model_path, previous_path, receipt_path)
        staged_path = tempfile.mkdtemp(prefix=".whisper-base-download-", dir=parent)

        # Stream the weight file to disk without constructing a model object.
        print("Downloading model and processor files...")
        snapshot_download(
            repo_id="openai/whisper-base", local_dir=staged_path,
            allow_patterns=["*.json", "*.txt", "model.safetensors"],
        )
        for name in ("config.json", "model.safetensors"):
            path = os.path.join(staged_path, name)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise ValueError(f"Offline snapshot is missing a non-empty {name}")
        print("✓ Model weights downloaded")

        # Validate/save only the small processor, strictly from the staged files.
        print("Validating processor...")
        processor = AutoProcessor.from_pretrained(staged_path, local_files_only=True)
        processor.save_pretrained(staged_path)
        print("✓ Processor saved")

        # Check size (stdlib walk — portable, no shell/du dependency)
        total_bytes = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, files in os.walk(staged_path) for f in files
        )
        print(f"✓ Total size: {total_bytes / (1024 ** 2):.1f} MB")

        if os.path.lexists(model_path):
            identity = get_recovery_identity(model_path)
            with open(receipt_path, 'x', encoding='utf-8') as stream:
                json.dump(identity, stream)
                stream.flush()
                os.fsync(stream.fileno())
            backup_path = previous_path
            os.replace(model_path, backup_path)
        try:
            os.replace(staged_path, model_path)
        except BaseException:
            if backup_path is not None:
                os.replace(backup_path, model_path)
                backup_path = None
                os.unlink(receipt_path)
            raise
        staged_path = None
        if backup_path is not None:
            try:
                shutil.rmtree(backup_path)
                backup_path = None
                os.unlink(receipt_path)
            except OSError as exc:
                print(f"WARNING: New model is installed, but previous-bundle cleanup failed: {exc}")
                print(f"Inspect recovery paths before manual cleanup: {previous_path}, {receipt_path}")

        print()
        print("=" * 70)
        print("✓ SUCCESS: Model downloaded and ready for offline use!")
        print("=" * 70)

        return 0

    except Exception as e:
        print(f"✗ ERROR: {e}")
        if backup_path is not None:
            print(f"Previous download retained for recovery: {backup_path}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        if staged_path is not None and os.path.isdir(staged_path):
            shutil.rmtree(staged_path)

if __name__ == "__main__":
    sys.exit(download_model())
