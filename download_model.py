#!/usr/bin/env python3
"""Download whisper-base model for offline use."""

import os
import sys

# Try to import with proper error handling
try:
    from transformers import AutoProcessor, AutoModelForSpeechSeq2Seq
except ImportError:
    print("ERROR: transformers not installed")
    print("Install with: pip install transformers")
    sys.exit(1)

def download_model():
    """Download and cache the whisper-base model locally."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    model_path = os.path.join(script_dir, "models", "whisper-base")

    print("=" * 70)
    print("Downloading OpenAI Whisper Base Model")
    print("=" * 70)
    print(f"Destination: {model_path}")
    print()

    try:
        # Create directory if needed
        os.makedirs(model_path, exist_ok=True)

        # Download model
        print("Downloading model weights...")
        model = AutoModelForSpeechSeq2Seq.from_pretrained(
            "openai/whisper-base",
            torch_dtype="auto",
            use_safetensors=True
        )
        model.save_pretrained(model_path)
        print(f"✓ Model saved: {len(list(model.parameters())):,} parameters")

        # Download processor
        print("Downloading processor...")
        processor = AutoProcessor.from_pretrained("openai/whisper-base")
        processor.save_pretrained(model_path)
        print("✓ Processor saved")

        # Check size (stdlib walk — portable, no shell/du dependency)
        total_bytes = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, files in os.walk(model_path) for f in files
        )
        print(f"✓ Total size: {total_bytes / (1024 ** 2):.1f} MB")

        print()
        print("=" * 70)
        print("✓ SUCCESS: Model downloaded and ready for offline use!")
        print("=" * 70)

        return 0

    except Exception as e:
        print(f"✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(download_model())
