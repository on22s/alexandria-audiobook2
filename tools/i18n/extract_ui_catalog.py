#!/usr/bin/env python
"""Harvest every keyed UI message into app/static/i18n/ui.en.json.

WHY A GENERATED ENGLISH CATALOG. The English text is authored where it is used -
in index.html and in the second argument of each t() call - because that is the
synchronous fallback a partial catalog needs. The JSON file is not a runtime
catalog; it is the translator hand-off and the parity reference that
test_ui_catalog_parity.py compares the translated locales against. Generating it
from the source is what makes "the key set is exactly what the source keys" a
fact rather than a hand-maintained list.

SOURCES.
  * markup: data-i18n="key" (text content) and
    data-i18n-attr="attribute:key,..." (that attribute's authored value);
  * app-settings.js and later-phase files: t('key', 'English', ...) calls whose
    first two arguments are string literals;
  * createUiMessageNode('tag', 'key', 'English') for nodes built in JS;
  * server refusals: raise AppError(status, 'English', key='...') and, for a
    dynamic message, an f-string whose interpolations map one-for-one onto the
    literal keys of values={...} in the same order. Plain HTTPException sites
    are not keyed and are not harvested.

A t() call whose first two arguments are not literals fails the harvest loudly:
that pattern would put the fallback outside the source this file can see, and
the English text must stay reviewable at the call site (design section 3.4). A
dynamic AppError message fails the same way unless its values map makes the
English template recoverable, because the client must check a translation's
placeholders against that template rather than against the rendered sentence.

Run from anywhere: python tools/i18n/extract_ui_catalog.py [--check]
"""
import ast
import html.parser
import json
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[2]
STATIC = REPO / "app" / "static"
OUTPUT = STATIC / "i18n" / "ui.en.json"
HTML_SOURCES = (STATIC / "index.html", STATIC / "readalong-player.html")
SERVER_SOURCES = ("app/*.py", "app/routers/*.py")
# app-i18n.js owns t()/createUiMessageNode themselves and keys nothing.
JS_EXCLUDED = {"app-i18n.js"}
UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\"}


class HarvestError(Exception):
    """A message whose English source cannot be read from the tree."""


class MarkupHarvester(html.parser.HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.messages = {}
        self.active = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        keyed = attributes.get("data-i18n")
        if keyed:
            if self.active is not None:
                raise HarvestError(f"{self.source}: nested data-i18n is not supported")
            self.active = (keyed, [])
            self.depth = 1
        elif self.active is not None:
            self.depth += 1
        for entry in (attributes.get("data-i18n-attr") or "").split(","):
            attribute, _, key = entry.partition(":")
            attribute, key = attribute.strip(), key.strip()
            if attribute and key:
                self._record(key, attributes.get(attribute, ""), f"{self.source}:{tag}[{attribute}]")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self.active is not None:
            self.active[1].append(data)

    def handle_endtag(self, tag):
        if self.active is None:
            return
        self.depth -= 1
        if self.depth <= 0:
            key, chunks = self.active
            self.active = None
            self._record(key, "".join(chunks), f"{self.source}:<{tag}>")

    def _record(self, key, english, where):
        english = " ".join(english.split())
        if not english:
            raise HarvestError(f"{where}: keyed message {key!r} has no English text")
        previous = self.messages.get(key)
        if previous is not None and previous != english:
            raise HarvestError(f"{where}: {key!r} is already {previous!r}, now {english!r}")
        self.messages[key] = english


def _read_argument(text, index):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text) or text[index] not in "'\"":
        return None, index
    quote = text[index]
    index += 1
    chunks = []
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            chunks.append(UNESCAPE.get(text[index + 1], text[index + 1]))
            index += 2
            continue
        if char == quote:
            return "".join(chunks), index + 1
        chunks.append(char)
        index += 1
    return None, index


def _call_arguments(text, start):
    """Read (arg1, arg2, arg3) after the opening paren at `start`."""
    arguments = []
    index = start
    for _ in range(3):
        value, index = _read_argument(text, index)
        arguments.append(value)
        while index < len(text) and text[index].isspace():
            index += 1
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        break
    return arguments


def harvest_js(path, messages):
    text = path.read_text(encoding="utf-8")
    name = path.name
    for match in re.finditer(r"\bcreateUiMessageNode\s*\(", text):
        arguments = _call_arguments(text, match.end())
        if len(arguments) < 3 or arguments[1] is None or arguments[2] is None:
            raise HarvestError(f"{name}: createUiMessageNode needs literal key and English text")
        _record(messages, arguments[1], arguments[2], name)
    for match in re.finditer(r"\bt\s*\(", text):
        arguments = _call_arguments(text, match.end())
        if not arguments or arguments[0] is None or len(arguments) < 2 or arguments[1] is None:
            raise HarvestError(
                f"{name}: t() must take literal key and English text "
                f"({text[max(0, match.start() - 40):match.end() + 40]!r})")
        _record(messages, arguments[0], arguments[1], name)


def _record(messages, key, english, where):
    if messages.get(key, english) != english:
        raise HarvestError(f"{where}: {key!r} is already {messages[key]!r}, now {english!r}")
    messages[key] = english


def _literal_string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _app_error_template(message, values):
    """The English template an AppError message exposes to translators.

    A plain string is already the template. An f-string is allowed only when
    values={...} is a literal dict whose keys, in order, name its
    interpolations; the result replaces each expression with ``{name}``.
    """
    if isinstance(message, ast.Constant) and isinstance(message.value, str):
        return message.value
    if not isinstance(message, ast.JoinedStr) or not isinstance(values, ast.Dict):
        return None
    names = []
    for key in values.keys:
        name = _literal_string(key) if key is not None else None
        if not name:
            return None
        names.append(name)
    formatted = [part for part in message.values if isinstance(part, ast.FormattedValue)]
    if len(formatted) != len(names):
        return None
    replacements = iter(names)
    chunks = []
    for part in message.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            chunks.append(part.value)
        elif isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None:
            chunks.append("{" + next(replacements) + "}")
        else:
            return None
    return "".join(chunks)


def harvest_server(path, messages):
    """Record every raise AppError(status, message, key=...) in one module."""
    source = path.read_text(encoding="utf-8")
    name = path.name
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        func = node.exc.func
        if not (isinstance(func, ast.Name) and func.id == "AppError"):
            continue
        if len(node.exc.args) < 2:
            raise HarvestError(f"{name}:{node.lineno}: AppError needs a status and an English message")
        keywords = {kw.arg: kw.value for kw in node.exc.keywords if kw.arg}
        key = _literal_string(keywords.get("key"))
        if not key:
            raise HarvestError(f"{name}:{node.lineno}: AppError needs a literal key")
        template = _app_error_template(node.exc.args[1], keywords.get("values"))
        if template is None:
            raise HarvestError(
                f"{name}:{node.lineno}: AppError message {key!r} must be a literal, or an "
                f"f-string whose values={{...}} keys match its interpolations in order")
        _record(messages, key, template, name)


def harvest(root=REPO):
    messages = {}
    for path in HTML_SOURCES:
        if not path.exists():
            continue
        parser = MarkupHarvester(path.relative_to(root).as_posix())
        parser.feed(path.read_text(encoding="utf-8"))
        for key, english in parser.messages.items():
            _record(messages, key, english, path.name)
    for path in sorted((root / "app" / "static" / "js").glob("*.js")):
        if path.name in JS_EXCLUDED:
            continue
        harvest_js(path, messages)
    for pattern in SERVER_SOURCES:
        for path in sorted(root.glob(pattern)):
            harvest_server(path, messages)
    return messages


def render(messages):
    payload = {"_meta": {"locale": "en", "source": "harvested"}}
    payload.update(messages)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv):
    if argv not in ([], ["--check"]):
        print("usage: extract_ui_catalog.py [--check]", file=sys.stderr)
        return 2
    rendered = render(harvest())
    if argv == ["--check"]:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(REPO)} is stale; run: python tools/i18n/extract_ui_catalog.py",
                  file=sys.stderr)
            return 1
        return 0
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except HarvestError as error:
        print(f"extract_ui_catalog: {error}", file=sys.stderr)
        raise SystemExit(1)
