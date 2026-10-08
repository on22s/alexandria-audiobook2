#!/usr/bin/env python3
"""Download whisper-base model for offline use."""

import os
import sys
import shutil
import tempfile

# Try to import with proper error handling
try:
    from transformers import AutoProcessor
    from huggingface_hub import snapshot_download
except ImportError:
    print("ERROR: transformers or huggingface_hub not installed")
    print("Install with: pip install transformers huggingface_hub")
    sys.exit(1)

def download_model():
    """Download and cache the whisper-base model locally.

    Publish the directory only after both save operations complete. A failed
    download or replacement must not damage an existing offline bundle.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    model_path = os.path.join(script_dir, "models", "whisper-base")

    print("=" * 70)
    print("Downloading OpenAI Whisper Base Model")
    print("=" * 70)
    print(f"Destination: {model_path}")
    print()

    staged_path = None
    backup_path = None
    try:
        parent = os.path.dirname(model_path)
        os.makedirs(parent, exist_ok=True)
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
            if os.path.islink(model_path) or not os.path.isdir(model_path):
                raise ValueError("Offline model destination must be a directory, not a link or file")
            backup_path = staged_path + ".previous"
            os.replace(model_path, backup_path)
        try:
            os.replace(staged_path, model_path)
        except BaseException:
            if backup_path is not None:
                os.replace(backup_path, model_path)
                backup_path = None
            raise
        staged_path = None
        if backup_path is not None:
            try:
                shutil.rmtree(backup_path)
                backup_path = None
            except OSError as exc:
                print(f"WARNING: New model is installed, but previous-bundle cleanup failed: {exc}")
                print(f"Previous download retained for recovery or manual cleanup: {backup_path}")

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
