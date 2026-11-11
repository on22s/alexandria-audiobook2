#!/usr/bin/env python
"""Harvest every English refusal text into a golden fixture.

WHY THIS FILE EXISTS. Rule 9 makes the English text of a warning a protected
asset. Converting `HTTPException(detail=...)` to `AppError(...)` moves the same
sentence into a different AST shape; a golden corpus that compared source lines
would call that a change even when the bytes are identical. This harvest
normalises both shapes to the same `ast.unparse` text, so a conversion is
invisible and a rewording is not.

What is pinned: every `raise HTTPException(..., detail=<expr>)` and every
`raise AppError(status, <expr>, ...)` in app/*.py and app/routers/*.py, as
rendered source expressions, sorted. Dynamic text is pinned as the f-string
template including its expressions, which is stricter than the string it
happens to produce at one call.

Run from anywhere: python tools/i18n/extract_api_details.py [--check]
"""
import ast
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
OUTPUT = REPO / "app" / "tests" / "fixtures" / "api_detail_golden.json"
SERVER_SOURCES = ("app/*.py", "app/routers/*.py")


def _refusal_value(call):
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    if call.func.id == "HTTPException":
        for keyword in call.keywords:
            if keyword.arg == "detail":
                return keyword.value
        return None
    if call.func.id == "AppError" and len(call.args) >= 2:
        return call.args[1]
    return None


def harvest(root=REPO):
    """Return (sorted refusal expressions, number of remaining detail= sites)."""
    texts = []
    remaining_detail_sites = 0
    for pattern in SERVER_SOURCES:
        for path in sorted(Path(root).glob(pattern)):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Raise):
                    continue
                value = _refusal_value(node.exc)
                if value is None:
                    continue
                texts.append(ast.unparse(value))
                if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "HTTPException":
                    remaining_detail_sites += 1
    return sorted(texts), remaining_detail_sites


def render(texts):
    payload = {
        "_meta": {
            "source": "every raise HTTPException(detail=...) and raise AppError(status, message) "
                      "in app/*.py and app/routers/*.py",
            "regenerate": "python tools/i18n/extract_api_details.py",
        },
        "strings": texts,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def main(argv):
    if argv not in ([], ["--check"]):
        print("usage: extract_api_details.py [--check]", file=sys.stderr)
        return 2
    texts, _ = harvest()
    rendered = render(texts)
    if argv == ["--check"]:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(REPO)} is stale; run: python tools/i18n/extract_api_details.py",
                  file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
