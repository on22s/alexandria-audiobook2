"""Reject unpinned Whisper source and incomplete or altered model bytes."""
import hashlib
import json
import subprocess
from pathlib import Path


def verify_whisper_assets(root):
    root = Path(root)
    assets = json.loads((root / "whisper_assets.json").read_text(encoding="utf-8"))
    source = root / "whisper.cpp"
    result = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                            check=True, capture_output=True, text=True)
    if result.stdout.strip() != assets["source_commit"]:
        raise ValueError("Whisper source commit differs from its pinned release; reinstall the source.")
    if subprocess.run(["git", "-C", str(source), "diff", "--quiet", "HEAD", "--"],
                      check=False).returncode != 0:
        raise ValueError("Whisper source contains tracked changes; restore it before building.")
    model = root / "models" / "whisper.cpp" / assets["model_filename"]
    digest = hashlib.sha256()
    with model.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != assets["model_sha256"]:
        raise ValueError("Whisper model checksum mismatch; remove the invalid model and rerun Install.")
    return {"source_commit": assets["source_commit"], "model_sha256": digest.hexdigest()}


if __name__ == "__main__":
    print(json.dumps(verify_whisper_assets(Path(__file__).resolve().parents[1])))
