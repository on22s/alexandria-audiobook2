"""Shared rocm-smi JSON-parsing helper.

Lives at the repo root (not inside app/) so both the FastAPI app (via
app/utils.py's re-export) and the standalone root-level scripts
(alexandria_batch_processor.py, alexandria_preparer_rocm_compatible.py)
can import it without either side needing to reach across the app/
package boundary.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def run_rocm_smi_json(args, rocm_smi_path="rocm-smi", timeout=5):
    """Run `<rocm_smi_path> <args> --json` and return the parsed per-card dict, or None.

    Filters stdout down to JSON-looking lines first, since rocm-smi sometimes
    prints warnings to stdout ahead of the JSON payload. Returns None if the
    binary is missing, times out, exits non-zero, or produces no JSON.
    """
    try:
        result = subprocess.run(
            [rocm_smi_path] + list(args) + ["--json"],
            capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        logger.debug(f"{rocm_smi_path} not found")
        return None
    except subprocess.TimeoutExpired:
        logger.debug(f"{rocm_smi_path} timed out after {timeout}s")
        return None
    except Exception as e:
        logger.debug(f"{rocm_smi_path} unexpected error: {e}")
        return None

    if result.returncode != 0:
        logger.debug(f"{rocm_smi_path} returned error: {result.returncode}, stderr: {result.stderr}")
        return None

    # rocm-smi sometimes prints warnings to stdout ahead of the JSON, and
    # the JSON payload itself may be pretty-printed across several lines.
    # Parse everything from the first line that opens the JSON object so a
    # multi-line payload isn't truncated to just "{".
    lines = result.stdout.split('\n')
    for i, line in enumerate(lines):
        if line.strip().startswith('{'):
            try:
                return json.loads('\n'.join(lines[i:]))
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"{rocm_smi_path} JSON parse error: {e}")
                return None
    return None
