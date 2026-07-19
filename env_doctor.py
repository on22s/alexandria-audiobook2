"""Stdlib-only doctor for the two Python interpreters this project depends
on: `app/env` (the FastAPI app's own venv) and `rocm_python` (the sibling
repo's env that Voice Lab's dedup/profiling stages run under - see
CLAUDE.md's "Voice Lab pipeline" section).

Catches env drift before it surfaces mid-run, e.g. a required package
missing from `app/env`, or a `rocm_python` whose torch build is older than
documented. Run directly (`python3 env_doctor.py`) - no probing happens on
import, only inside main()/its helpers.
"""

import json
import os
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

VOICELAB_CONFIG_PATH = os.path.join(
    os.environ.get("ALEXANDRIA_DATA_DIR", "").strip() or ROOT_DIR,
    "voicelab_config.json",
)

# --- Spec as data -----------------------------------------------------

ENV_SPECS = {
    "app_env": {
        "path": os.path.join(ROOT_DIR, "app", "env", "bin", "python"),
        "required": [
            "fastapi", "uvicorn", "pydantic", "soundfile", "numpy",
            "librosa", "transformers", "peft", "mutagen", "torch",
        ],
        "optional": [],
        "version_hint": {"torch": "2.10.0+rocm"},
    },
    "rocm_python": {
        # Resolved at run time from voicelab_config.json's "rocm_python" key
        # (same file/key the app reads via core.py's VOICELAB_DEFAULTS /
        # _load_voicelab_config - reusing the file rather than duplicating
        # that lookup).
        "path": None,
        "required": [
            "torch", "librosa", "peft", "transformers", "speechbrain",
            "scipy", "soundfile", "llama_cpp", "mutagen",
        ],
        "optional": ["matplotlib", "seaborn", "umap", "pyannote.audio"],
        "version_hint": {},
    },
}

# Import name overrides for packages whose distribution name differs from
# the module you `import`.
_IMPORT_NAME_OVERRIDES = {
    "llama_cpp": "llama_cpp",
    "umap": "umap",
    "pyannote.audio": "pyannote.audio",
}

_OPTIONAL_ABSENCE_NOTES = {
    "pyannote.audio": "diarization unavailable - see requirements-diarization.txt",
}

PROBE_SCRIPT = """
import importlib
import importlib.metadata as md
import json
import sys

packages = sys.argv[1:]
out = {}
for pkg in packages:
    version = None
    try:
        version = md.version(pkg)
    except md.PackageNotFoundError:
        try:
            mod = importlib.import_module(pkg)
            version = getattr(mod, "__version__", None)
        except Exception:
            version = None
    out[pkg] = version
print(json.dumps(out))
"""


def resolve_rocm_python_path() -> str:
    """Read voicelab_config.json's "rocm_python" key the same way the app
    does (core.py's VOICELAB_DEFAULTS falls back to the
    ALEXANDRIA_ROCM_PYTHON env var, then ""). Read-only - no validation
    beyond that, matching the plan's "don't duplicate the lookup logic
    beyond a read"."""
    if os.path.exists(VOICELAB_CONFIG_PATH):
        try:
            with open(VOICELAB_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("rocm_python"), str) and data["rocm_python"]:
                return data["rocm_python"]
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return os.environ.get("ALEXANDRIA_ROCM_PYTHON", "")


def probe_interpreter(python_path: str, packages: List[str], timeout: int = 30) -> Optional[Dict[str, Optional[str]]]:
    """Run `python_path -c PROBE_SCRIPT <packages...>` and return the parsed
    {package: version_or_None} dict, or None if the interpreter can't be run
    at all (missing/not executable/timed out/bad output)."""
    if not python_path or not os.path.exists(python_path):
        return None
    try:
        result = subprocess.run(
            [python_path, "-c", PROBE_SCRIPT, *packages],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError, ValueError):
        return None


# --- Pure evaluation logic (unit-tested with fake probe results) ------

def evaluate_env(spec: dict, probe_result: Optional[Dict[str, Optional[str]]]) -> Tuple[List[dict], bool]:
    """Given one env's spec and its probe result (or None if the
    interpreter itself couldn't be run), return (rows, ok).

    Each row is {package, expected, found, status, note} where status is
    one of "OK", "MISSING", "OPTIONAL-MISSING". `ok` is True only when the
    interpreter was probed successfully and every required package is
    present (optional packages never affect it).
    """
    required = spec.get("required", [])
    optional = spec.get("optional", [])
    version_hint = spec.get("version_hint", {})

    if probe_result is None:
        rows = [
            {"package": pkg, "expected": "required", "found": None,
             "status": "MISSING", "note": "interpreter not found"}
            for pkg in required
        ] + [
            {"package": pkg, "expected": "optional", "found": None,
             "status": "OPTIONAL-MISSING", "note": "interpreter not found"}
            for pkg in optional
        ]
        return rows, False

    rows = []
    ok = True
    for pkg in required:
        found = probe_result.get(pkg)
        if found is None:
            rows.append({"package": pkg, "expected": "required", "found": None,
                          "status": "MISSING", "note": ""})
            ok = False
        else:
            note = ""
            hint = version_hint.get(pkg)
            if hint and not str(found).startswith(hint):
                note = f"expected version prefix {hint!r}"
            rows.append({"package": pkg, "expected": "required", "found": found,
                          "status": "OK", "note": note})

    for pkg in optional:
        found = probe_result.get(pkg)
        if found is None:
            note = _OPTIONAL_ABSENCE_NOTES.get(pkg, "")
            rows.append({"package": pkg, "expected": "optional", "found": None,
                          "status": "OPTIONAL-MISSING", "note": note})
        else:
            rows.append({"package": pkg, "expected": "optional", "found": found,
                         "status": "OK", "note": ""})

    return rows, ok


def format_table(env_name: str, path: str, rows: List[dict]) -> str:
    lines = [f"== {env_name} ({path or 'not configured'}) =="]
    if not rows:
        lines.append("  (no packages declared)")
        return "\n".join(lines)
    widths = {
        "package": max(len("package"), *(len(r["package"]) for r in rows)),
        "expected": max(len("expected"), *(len(r["expected"]) for r in rows)),
        "found": max(len("found"), *(len(str(r["found"])) for r in rows)),
        "status": max(len("status"), *(len(r["status"]) for r in rows)),
    }
    header = (f"  {'package'.ljust(widths['package'])}  {'expected'.ljust(widths['expected'])}  "
              f"{'found'.ljust(widths['found'])}  {'status'.ljust(widths['status'])}  note")
    lines.append(header)
    for r in rows:
        note = r.get("note") or ""
        lines.append(
            f"  {r['package'].ljust(widths['package'])}  {r['expected'].ljust(widths['expected'])}  "
            f"{str(r['found']).ljust(widths['found'])}  {r['status'].ljust(widths['status'])}  {note}"
        )
    return "\n".join(lines)


def run_all(specs: dict) -> Tuple[Dict[str, List[dict]], bool]:
    """Resolve paths, probe every env in `specs`, evaluate, and return
    ({env_name: rows}, overall_ok)."""
    all_rows = {}
    overall_ok = True
    for env_name, spec in specs.items():
        path = spec["path"]
        if path is None:
            path = resolve_rocm_python_path()
        packages = list(dict.fromkeys(spec.get("required", []) + spec.get("optional", [])))
        probe_result = probe_interpreter(path, packages) if path else None
        rows, ok = evaluate_env({**spec, "path": path}, probe_result)
        all_rows[env_name] = {"path": path, "rows": rows}
        overall_ok = overall_ok and ok
    return all_rows, overall_ok


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv

    all_rows, overall_ok = run_all(ENV_SPECS)

    if as_json:
        print(json.dumps({
            "envs": {name: data for name, data in all_rows.items()},
            "ok": overall_ok,
        }, indent=2))
    else:
        for env_name, data in all_rows.items():
            print(format_table(env_name, data["path"], data["rows"]))
            print()
        print("OK" if overall_ok else "FAIL - required package(s) missing (see MISSING rows above)")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
