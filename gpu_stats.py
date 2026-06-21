"""Shared rocm-smi JSON-parsing helper.

Lives at the repo root (not inside app/) so both the FastAPI app (via
app/utils.py's re-export) and the standalone root-level scripts
(alexandria_batch_processor.py, alexandria_preparer_rocm_compatible.py)
can import it without either side needing to reach across the app/
package boundary.
"""

import json
import subprocess


def run_rocm_smi_json(args, rocm_smi_path="rocm-smi", timeout=5):
    """Run `<rocm_smi_path> <args> --json` and return the parsed per-card dict, or None.

    Filters stdout down to JSON-looking lines first, since rocm-smi sometimes
    prints warnings to stdout ahead of the JSON payload. Returns None if the
    binary is missing, times out, or produces no JSON.
    """
    try:
        result = subprocess.run(
            [rocm_smi_path] + list(args) + ["--json"],
            capture_output=True, text=True, timeout=timeout
        )
        # rocm-smi sometimes prints warnings to stdout ahead of the JSON, and
        # the JSON payload itself may be pretty-printed across several lines.
        # Parse everything from the first line that opens the JSON object so a
        # multi-line payload isn't truncated to just "{".
        lines = result.stdout.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith('{'):
                return json.loads('\n'.join(lines[i:]))
    except Exception:
        pass
    return None
