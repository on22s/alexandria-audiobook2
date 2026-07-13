"""HuggingFace download utilities for built-in LoRA adapters."""
import json
import logging
import os
import shutil
import time
from utils import atomic_json_write as _atomic_json_write, safe_load_json

logger = logging.getLogger("AlexandriaUI")

BUILTIN_LORA_HF_REPO = "Finrandojin/Alexandria"

REQUIRED_ADAPTER_FILES = [
    "adapter_config.json",
    "adapter_model.safetensors",
    "ref_sample.wav",
    "training_meta.json",
]
OPTIONAL_ADAPTER_FILES = ["preview_sample.wav"]

# In-memory manifest cache
_manifest_cache = None
_manifest_cache_key = None
_manifest_cache_time = 0
_MANIFEST_TTL = 3600  # 1 hour


def _normalize_manifest_entries(entries):
    """Return only entries safe for every backend and frontend consumer."""
    if not isinstance(entries, list):
        raise ValueError(f"manifest.json is not a list (got {type(entries).__name__})")
    normalized = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            logger.warning("Skipping malformed built-in LoRA manifest entry: %r", item)
            continue
        entry = dict(item)
        entry["id"] = entry["id"].strip()
        entry["name"] = str(entry.get("name") or entry["id"])
        for field in ("epochs", "sample_count"):
            if not isinstance(entry.get(field), (int, float)):
                entry[field] = None
        if not isinstance(entry.get("final_loss"), (int, float)):
            entry["final_loss"] = None
        normalized.append(entry)
    return normalized


def builtin_hf_name(adapter_id):
    """Map a local adapter id ('builtin_watson') to its HF subfolder ('watson').

    Single source for this mapping (also used by app.py's manifest lookup) so the
    downloader and the lookup can't drift. Prefix-only, so an id like
    'my_builtin_x' isn't mangled to 'my_x' the way str.replace would.
    """
    prefix = "builtin_"
    return adapter_id[len(prefix):] if adapter_id.startswith(prefix) else adapter_id


def fetch_builtin_manifest(builtin_dir, hf_repo=BUILTIN_LORA_HF_REPO):
    """Fetch manifest.json from HF repo, with local fallback and in-memory caching."""
    global _manifest_cache, _manifest_cache_key, _manifest_cache_time

    now = time.time()
    # Key the cache on the args — a call with a different repo/dir within the TTL
    # must not get the other one's manifest.
    cache_key = (hf_repo, builtin_dir)
    if (_manifest_cache is not None and _manifest_cache_key == cache_key
            and (now - _manifest_cache_time) < _MANIFEST_TTL):
        return _manifest_cache

    # Try remote
    try:
        from huggingface_hub import hf_hub_download
        cached_path = hf_hub_download(repo_id=hf_repo, filename="manifest.json")
        with open(cached_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        entries = _normalize_manifest_entries(entries)
        # Save local copy for offline fallback
        os.makedirs(builtin_dir, exist_ok=True)
        local_path = os.path.join(builtin_dir, "manifest.json")
        _atomic_json_write(entries, local_path)
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as e:
        logger.warning(f"Failed to fetch remote LoRA manifest, using local fallback: {e}")
        local_path = os.path.join(builtin_dir, "manifest.json")
        try:
            entries = _normalize_manifest_entries(safe_load_json(local_path, default=[]))
        except ValueError as validation_error:
            logger.warning("Invalid local LoRA manifest: %s", validation_error)
            entries = []

    _manifest_cache = entries
    _manifest_cache_key = cache_key
    _manifest_cache_time = now
    return entries


def download_builtin_adapter(adapter_id, builtin_dir, hf_repo=BUILTIN_LORA_HF_REPO):
    """Download a built-in LoRA adapter from HF to builtin_dir/adapter_id/.

    Args:
        adapter_id: Local adapter ID (e.g. "builtin_watson")
        builtin_dir: Path to the builtin_lora directory
        hf_repo: HF repo ID

    Returns:
        Path to the adapter directory on disk.

    Raises:
        RuntimeError: If a required file fails to download.
    """
    from huggingface_hub import hf_hub_download

    # Strip builtin_ prefix to get HF subfolder name
    hf_name = builtin_hf_name(adapter_id)
    adapter_dir = os.path.join(builtin_dir, adapter_id)
    os.makedirs(adapter_dir, exist_ok=True)

    for filename in REQUIRED_ADAPTER_FILES + OPTIONAL_ADAPTER_FILES:
        local_path = os.path.join(adapter_dir, filename)
        if os.path.exists(local_path):
            continue
        try:
            cached = hf_hub_download(
                repo_id=hf_repo,
                filename=f"{hf_name}/{filename}",
            )
            shutil.copy2(cached, local_path)
            logger.info(f"Downloaded {hf_name}/{filename} -> {local_path}")
        except Exception as e:
            if filename in REQUIRED_ADAPTER_FILES:
                raise RuntimeError(
                    f"Failed to download {hf_name}/{filename} for {adapter_id}: {e}"
                )
            logger.warning(f"Optional file {hf_name}/{filename} not available: {e}")

    return adapter_dir


def is_adapter_downloaded(adapter_id, builtin_dir):
    """Check if all required files exist for a built-in adapter."""
    adapter_dir = os.path.join(builtin_dir, adapter_id)
    return (
        os.path.isdir(adapter_dir)
        and all(
            os.path.exists(os.path.join(adapter_dir, f))
            for f in REQUIRED_ADAPTER_FILES
        )
    )
