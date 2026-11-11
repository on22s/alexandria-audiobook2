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
  * renderUiSafety(target, 'key', 'English') for the authored safety nodes, and
    formatUiSafetyText('key', 'English') for the same contract in a string sink
    (a confirm body); an inline safety warning is a catalog source exactly like
    a plain message.
  * app-help.js: every entry of its seven help maps becomes
    help.<kind>.<slug>[.what|.recommend|.source], plus the literal t('help.*')
    calls that key getItemHelpText's inline fallbacks;
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
# app-help.js is harvested by harvest_help below: its English lives in the help
# maps (data), not in the second argument of t() at a call site.
JS_EXCLUDED = {"app-i18n.js", "app-help.js"}
HELP_JS = STATIC / "js" / "app-help.js"
HELP_MAP_KINDS = (
    ("FIELD_HELP", "field"),
    ("ACTION_HELP", "action"),
    ("ITEM_HELP", "item"),
    ("NAV_HELP", "nav"),
    ("DYNAMIC_FIELD_HELP", "dynamic"),
    ("CHANGED_FIELD_HELP", "changed"),
)
UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", "'": "'", '"': '"', "\\": "\\"}
MARKUP_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
               "meta", "param", "source", "track", "wbr"}


class HarvestError(Exception):
    """A message whose English source cannot be read from the tree."""


class MarkupHarvester(html.parser.HTMLParser):
    def __init__(self, source, text):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.text = text
        self.line_starts = [0]
        for match in re.finditer("\n", text):
            self.line_starts.append(match.end())
        self.messages = {}
        self.active = None
        self.depth = 0
        self.html_active = None
        self.html_depth = 0

    def _pos(self):
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        keyed = attributes.get("data-i18n")
        html_keyed = attributes.get("data-i18n-html")
        if keyed:
            if self.active is not None or self.html_active is not None:
                raise HarvestError(f"{self.source}: nested data-i18n is not supported")
            self.active = (keyed, [])
            self.depth = 1
        elif html_keyed:
            if self.active is not None or self.html_active is not None:
                raise HarvestError(f"{self.source}: nested data-i18n-html is not supported")
            self.html_active = (html_keyed, self.text.index(">", self._pos()) + 1)
            self.html_depth = 1
        elif self.active is not None:
            self.depth += 1
        elif self.html_active is not None and tag not in MARKUP_VOID:
            self.html_depth += 1
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
        if self.html_active is not None:
            self.html_depth -= 1
            if self.html_depth <= 0:
                key, start = self.html_active
                self.html_active = None
                self._record(key, " ".join(self.text[start:self._pos()].split()),
                             f"{self.source}:<{tag}>")
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


def _call_arguments_after_first(text, start):
    """Read the arguments after one non-literal expression (the render target)."""
    depth = 0
    quote = None
    index = start
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == '\\':
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in '([{':
            depth += 1
        elif char in ')]}':
            if depth == 0:
                return []
            depth -= 1
        elif char == ',' and depth == 0:
            return _call_arguments(text, index + 1)
        index += 1
    return []


def harvest_js(path, messages):
    text = path.read_text(encoding="utf-8")
    name = path.name
    for match in re.finditer(r"\bcreateUiMessageNode\s*\(", text):
        arguments = _call_arguments(text, match.end())
        if len(arguments) < 3 or arguments[1] is None or arguments[2] is None:
            raise HarvestError(f"{name}: createUiMessageNode needs literal key and English text")
        _record(messages, arguments[1], arguments[2], name)
    for match in re.finditer(r"\brenderUiSafety\s*\(", text):
        arguments = _call_arguments_after_first(text, match.end())
        if len(arguments) < 2 or arguments[0] is None or arguments[1] is None:
            raise HarvestError(f"{name}: renderUiSafety needs literal key and English text")
        _record(messages, arguments[0], arguments[1], name)
    for match in re.finditer(r"\bformatUiSafetyText\s*\(", text):
        arguments = _call_arguments(text, match.end())
        if len(arguments) < 2 or arguments[0] is None or arguments[1] is None:
            raise HarvestError(f"{name}: formatUiSafetyText needs literal key and English text")
        _record(messages, arguments[0], arguments[1], name)
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


class JsObjectReader:
    """Read the plain object literals that hold app-help.js's help maps.

    The maps are data: string keys, string values, nested objects and comments.
    Anything else raises instead of guessing, because a silently mis-read map
    would produce a catalog that looks complete and is wrong (Rule 21).
    """

    def __init__(self, text, index):
        self.text = text
        self.index = index

    def _error(self, message):
        line = self.text.count("\n", 0, self.index) + 1
        raise HarvestError(f"app-help.js:{line}: {message}")

    def _skip(self):
        text = self.text
        while self.index < len(text):
            if text[self.index].isspace():
                self.index += 1
            elif text.startswith("//", self.index):
                end = text.find("\n", self.index)
                self.index = len(text) if end < 0 else end + 1
            elif text.startswith("/*", self.index):
                end = text.find("*/", self.index + 2)
                if end < 0:
                    self._error("unterminated comment")
                self.index = end + 2
            else:
                return
        self._error("unexpected end of file")

    def _read_string(self):
        text = self.text
        quote = text[self.index]
        self.index += 1
        chunks = []
        while self.index < len(text):
            char = text[self.index]
            if char == "\\":
                escape = text[self.index + 1:self.index + 2]
                chunks.append(UNESCAPE.get(escape, escape))
                self.index += 2
                continue
            if char == quote:
                self.index += 1
                return "".join(chunks)
            chunks.append(char)
            self.index += 1
        self._error("unterminated string")

    def _read_key(self):
        self._skip()
        if self.text[self.index] in "'\"":
            return self._read_string()
        start = self.index
        while self.index < len(self.text) and re.match(r"[A-Za-z0-9_$]", self.text[self.index]):
            self.index += 1
        if start == self.index:
            self._error(f"expected an object key, found {self.text[self.index:self.index + 20]!r}")
        return self.text[start:self.index]

    def read_object(self):
        self._skip()
        if self.text[self.index] != "{":
            self._error("expected an object literal")
        self.index += 1
        values = {}
        while True:
            self._skip()
            if self.text[self.index] == "}":
                self.index += 1
                return values
            key = self._read_key()
            self._skip()
            if self.text[self.index] != ":":
                self._error(f"expected ':' after key {key!r}")
            self.index += 1
            values[key] = self.read_value()
            self._skip()
            if self.text[self.index] == ",":
                self.index += 1
            elif self.text[self.index] != "}":
                self._error(f"expected ',' or '}}' after key {key!r}")

    def read_value(self):
        self._skip()
        if self.text[self.index] in "'\"":
            return self._read_string()
        if self.text[self.index] == "{":
            return self.read_object()
        self._error(f"unsupported value {self.text[self.index:self.index + 20]!r}")


def help_slug(identity):
    """The catalog-key segment for one help id or CSS selector.

    Mirrors getHelpCatalogKey() in app-help.js; test_ui_help_i18n.py asserts the
    two derivations agree for every map entry, so they cannot drift silently.
    """
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9_-]+", "-", str(identity).lower()))


def read_help_maps(path=HELP_JS):
    text = path.read_text(encoding="utf-8")
    maps = {}
    for name, _kind in HELP_MAP_KINDS:
        match = re.search(r"\bconst\s+" + name + r"\s*=\s*", text)
        if not match:
            raise HarvestError(f"{path.name}: no {name} declaration")
        maps[name] = JsObjectReader(text, match.end()).read_object()
    return maps


def help_messages(maps):
    """Flatten the seven help maps into help.<kind>.<id>[.field] keys."""
    messages = {}

    def add(key, english, where):
        if not isinstance(english, str) or not english.strip():
            raise HarvestError(f"app-help.js: {key} has no English text")
        _record(messages, key, english, where)

    for name, kind in HELP_MAP_KINDS:
        entries = maps.get(name)
        if not isinstance(entries, dict) or not entries:
            raise HarvestError(f"app-help.js: {name} is not a non-empty object")
        if name == "CHANGED_FIELD_HELP":
            for handler, fields in entries.items():
                if not isinstance(fields, dict) or not fields:
                    raise HarvestError(f"app-help.js: {name}.{handler} is not a non-empty object")
                for field, english in fields.items():
                    add(f"help.changed.{help_slug(handler)}.{help_slug(field)}", english, name)
            continue
        for identity, entry in entries.items():
            base = f"help.{kind}.{help_slug(identity)}"
            if name != "FIELD_HELP":
                add(base, entry, name)
                continue
            if not isinstance(entry, dict):
                raise HarvestError(f"app-help.js: {name}.{identity} is not an object")
            unknown = sorted(set(entry) - {"what", "recommend", "source"})
            if unknown:
                raise HarvestError(f"app-help.js: {name}.{identity} has unknown fields {unknown}")
            add(base + ".what", entry.get("what"), name)
            for field in ("recommend", "source"):
                if field in entry:
                    add(base + "." + field, entry[field], name)
    return messages


def strip_js_comments(text):
    """Blank out // and /* */ comments, keeping strings and byte offsets."""
    out = list(text)
    index = 0
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            quote = char
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if text.startswith("//", index):
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            for position in range(index, end):
                out[position] = " "
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            end = len(text) if end < 0 else end + 2
            for position in range(index, end):
                if out[position] != "\n":
                    out[position] = " "
            index = end
            continue
        index += 1
    return "".join(out)


def harvest_help(path, messages):
    """Harvest the help maps plus the literal help t() calls in app-help.js.

    A t() whose key is computed by getHelpCatalogKey() is the map reader and is
    skipped here because the map harvest above already recorded that key.
    """
    for key, english in help_messages(read_help_maps(path)).items():
        _record(messages, key, english, path.name)
    text = strip_js_comments(path.read_text(encoding="utf-8"))
    for match in re.finditer(r"\bt\s*\(", text):
        arguments = _call_arguments(text, match.end())
        if not arguments:
            raise HarvestError(f"{path.name}: empty t() call")
        if arguments[0] is None:
            window = text[match.end():match.end() + 40]
            # Two non-literal calls are legitimate: the map reader derives its key
            # from getHelpCatalogKey(), and formatHelpText() forwards its caller's
            # key and English to t() after supplying literal-brace values.
            if "getHelpCatalogKey(" in window or window.startswith("key, english, values, locale"):
                continue
            raise HarvestError(
                f"{path.name}: t() must take a literal key or derive it from getHelpCatalogKey()")
        if len(arguments) < 2 or arguments[1] is None:
            raise HarvestError(f"{path.name}: t({arguments[0]!r}) needs literal English text")
        if not arguments[0].startswith("help."):
            raise HarvestError(f"{path.name}: help key {arguments[0]!r} is not under help.")
        _record(messages, arguments[0], arguments[1], path.name)


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
        parser = MarkupHarvester(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        parser.feed(path.read_text(encoding="utf-8"))
        for key, english in parser.messages.items():
            _record(messages, key, english, path.name)
    for path in sorted((root / "app" / "static" / "js").glob("*.js")):
        if path.name in JS_EXCLUDED:
            continue
        harvest_js(path, messages)
    harvest_help(root / "app" / "static" / "js" / "app-help.js", messages)
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
