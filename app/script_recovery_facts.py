"""Shared saved recovery candidates; reading never repairs or resumes a run."""
from pathlib import Path
from utils import safe_load_json


def three_pass_manifest_path(output_path):
    return output_path + ".threepass_manifest.json"


def get_saved_script_recovery_manifest(data_dir, manifest_path):
    """Apply the Script screen's saved candidate policy, without admission.

    A matching input path is a candidate, not a current source/settings proof.
    The caller holds any required book admission; resume validates fingerprints.
    """
    state = safe_load_json(str(Path(data_dir) / "state.json"), {})
    if (not isinstance(state, dict)
            or '_source_scope' in state
            or state.get("script_generation_input_file") != state.get("input_file_path")):
        return None
    try:
        manifest = safe_load_json(manifest_path, {})
    except RecursionError:
        # Optional corrupt diagnostics must not hide an otherwise readable book.
        return None
    if not isinstance(manifest, dict) or manifest.get("status") not in {"failed", "incomplete"}:
        return None
    return manifest
