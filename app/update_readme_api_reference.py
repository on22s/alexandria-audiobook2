"""Regenerate the README HTTP route reference from the checked-in API contract.

The route list comes from ``app/api_contract/routes.json`` (the same ordered
snapshot the release verifier checks) and the one-line description of each
operation comes from its route function's docstring in ``app/routers/``.
Nothing is invented here: a route that is not in the contract is not listed,
and a router with routes but no table is a drift the unit tests report.
"""

import argparse
import ast
import json
from pathlib import Path
import sys

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
README = ROOT_DIR / "README.md"
ROUTES_SNAPSHOT = APP_DIR / "api_contract" / "routes.json"
ROUTERS_DIR = APP_DIR / "routers"

START = "<!-- api-reference-start -->"
END = "<!-- api-reference-end -->"

SECTION_TITLES = {
    "benchmark": "Benchmark",
    "casting": "Casting",
    "character_roster": "Character roster",
    "dataset_builder": "Dataset Builder",
    "editor": "Chunks, rendering, merge and export",
    "lora": "LoRA training and adapters",
    "preparer": "Preparer",
    "project_scope": "Project scope",
    "pronunciation_panel": "Pronunciation panel",
    "script": "Script generation and review",
    "scripts_library": "Saved scripts",
    "sound_design": "Sound design",
    "speaker_review": "Speaker review",
    "system": "System, config and status",
    "translated_edition": "Translated edition",
    "voice_design": "Voice Designer and clone references",
    "voice_library": "Voice library and casts",
    "voicelab": "Voice Lab",
    "voices": "Voices, personas, aliases",
}


def get_operation_definitions(routers_dir=ROUTERS_DIR):
    """Map every router operation id to its module and one-line description."""
    definitions = {}
    for path in sorted(Path(routers_dir).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node) or ""
            description = docstring.split("\n", 1)[0].strip()
            if not description:
                description = node.name.replace("_", " ").lower()
            description = " ".join(description.split()).replace("|", "\\|")
            definitions.setdefault(node.name, []).append({
                "module": path.stem,
                "description": description,
            })
    return definitions


def _definition_for(route, definitions):
    """Pick the owning module, disambiguating duplicate function names by path."""
    candidates = definitions.get(route["name"], [])
    if not candidates:
        raise ValueError(f"No router function found for operation {route['name']!r}")
    if len(candidates) == 1:
        return candidates[0]
    for candidate in candidates:
        if candidate["module"] in route["path"]:
            return candidate
    raise ValueError(f"Ambiguous operation {route['name']!r}: {[c['module'] for c in candidates]}")


def build_reference(contract=None, definitions=None):
    """Render the complete generated block, including its markers."""
    if contract is None:
        contract = json.loads(ROUTES_SNAPSHOT.read_text(encoding="utf-8"))
    if definitions is None:
        definitions = get_operation_definitions()
    grouped = {}
    for route in contract:
        if route["type"] != "APIRoute":
            continue
        definition = _definition_for(route, definitions)
        grouped.setdefault(definition["module"], []).append(
            (route, definition["description"]))
    count = sum(len(routes) for routes in grouped.values())
    lines = [
        START,
        "### Routes",
        "",
        f"{count} API operations across {len(grouped)} router modules. Generated from "
        "`app/api_contract/routes.json` and the route docstrings in `app/routers/` by "
        "`app/update_readme_api_reference.py`; regenerate after changing a route. "
        "Framework routes (`/openapi.json`, `/docs`, `/redoc`) and static mounts are "
        "in the contract but are not application endpoints.",
        "",
    ]
    for module, routes in grouped.items():
        lines.extend([
            f"#### {SECTION_TITLES.get(module, module.replace('_', ' ').title())} "
            f"(`app/routers/{module}.py`)",
            "",
            "| method | path | what it does |",
            "|---|---|---|",
        ])
        for route, description in routes:
            methods = " / ".join(route["methods"])
            lines.append(f"| `{methods}` | `{route['path']}` | {description} |")
        lines.append("")
    lines.extend([END, ""])
    return "\n".join(lines)


def replace_reference(readme_text, reference):
    """Replace the marked block in README, failing loudly if the markers moved."""
    if START not in readme_text or END not in readme_text:
        raise ValueError(f"README must contain both {START} and {END}")
    start = readme_text.index(START)
    end = readme_text.index(END, start) + len(END)
    return readme_text[:start] + reference.rstrip("\n") + readme_text[end:]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="report drift without writing, as the unit tests do")
    args = parser.parse_args(argv)
    original = README.read_text(encoding="utf-8")
    expected = replace_reference(original, build_reference())
    if args.check:
        if expected != original:
            print("README API reference is stale; run app/update_readme_api_reference.py",
                  file=sys.stderr)
            return 1
        return 0
    if expected != original:
        README.write_text(expected, encoding="utf-8")
        print("README API reference regenerated")
    else:
        print("README API reference already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
