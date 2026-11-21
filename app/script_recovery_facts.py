"""Shared saved recovery candidates; reading never repairs or resumes a run."""
from pathlib import Path
from utils import safe_load_json


def three_pass_manifest_path(output_path):
    return output_path + ".threepass_manifest.json"


def three_pass_checkpoint_path(output_path):
    return output_path + ".threepass_checkpoint.json"


def has_selected_source_state(data_dir):
    """True when state still points the active book at the same selected source."""
    state = safe_load_json(str(Path(data_dir) / "state.json"), {})
    return (isinstance(state, dict)
            and '_source_scope' not in state
            and state.get("script_generation_input_file") == state.get("input_file_path"))


def get_saved_script_recovery_manifest(data_dir, manifest_path):
    """Apply the Script screen's saved candidate policy, without admission.

    A matching input path is a candidate, not a current source/settings proof.
    The caller holds any required book admission; resume validates fingerprints.
    """
    if not has_selected_source_state(data_dir):
        return None
    try:
        manifest = safe_load_json(manifest_path, {})
    except RecursionError:
        # Optional corrupt diagnostics must not hide an otherwise readable book.
        return None
    if not isinstance(manifest, dict) or manifest.get("status") not in {"failed", "incomplete", "publishing"}:
        return None
    return manifest


def is_checkpoint_from_different_source(output_path, selected_source_sha256):
    """True only when a paired checkpoint proves the run used another source.

    An absent, unreadable or unproven checkpoint is not evidence of a different
    source, so legacy runs stay conservative. Reading never repairs or resumes.
    """
    checkpoint_path = Path(three_pass_checkpoint_path(str(output_path)))
    if not checkpoint_path.is_file():
        return False
    try:
        checkpoint = safe_load_json(str(checkpoint_path), {})
    except (OSError, RecursionError, UnicodeError, ValueError):
        return False
    if not isinstance(checkpoint, dict):
        return False
    fingerprint = checkpoint.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return False
    source_sha256 = fingerprint.get("source_sha256")
    if not isinstance(source_sha256, str) or not isinstance(selected_source_sha256, str):
        return False
    return source_sha256 != selected_source_sha256


def has_selected_source_checkpoint(data_dir, output_path, selected_source_sha256=None):
    """True when a loader-relevant active checkpoint may still be resumed.

    The worker saves a fingerprinted checkpoint after its first pass-1 unit,
    before any manifest exists (the manifest is written at publication). Such a
    checkpoint is a reusable recovery candidate on its own. A checkpoint that
    can be read and proves a different source is not a candidate; anything
    unreadable or unproven is treated as a potentially reusable candidate so the
    caller can fence conservatively. Reading never repairs or resumes a run.
    """
    checkpoint_path = Path(three_pass_checkpoint_path(str(output_path)))
    if not checkpoint_path.is_file():
        return False
    try:
        checkpoint = safe_load_json(str(checkpoint_path), {})
    except (OSError, RecursionError, UnicodeError, ValueError):
        return True
    if not isinstance(checkpoint, dict):
        return True
    fingerprint = checkpoint.get("fingerprint")
    if not isinstance(fingerprint, dict):
        return True
    source_sha256 = fingerprint.get("source_sha256")
    if not isinstance(source_sha256, str):
        return True
    if (isinstance(selected_source_sha256, str)
            and source_sha256 != selected_source_sha256):
        return False
    return True
