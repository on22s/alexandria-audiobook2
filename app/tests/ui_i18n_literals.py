"""One scanner for display English handed to the UI's message sinks.

WHY THIS EXISTS. tools/i18n/extract_ui_catalog.py only sees the English argument
of t('key', 'English'), so a call site that passes a bare literal to showToast,
showActionError, getActionErrorMessage, confirmIfRemote or setUiMessage - or that
assigns one to .textContent / .innerText - ships English into every locale and
no catalog test notices: the catalogs stay perfectly consistent with a source
that never mentions them. #2019 found eighteen such sites in app-training.js,
#2017 has the same bug in app-core.js and #2018 in app-voicelab.js.

Rule 15 is why the rule is written here once. Each per-file guard
(test_ui_i18n_app_training_literals.py today) scans its file through
bare_message_literals() instead of carrying its own copy, so a later guard for
app-core.js or app-voicelab.js points at this module too.

WHAT COUNTS AS DISPLAY TEXT is test_ui_markup_gate.is_display_text, the same
predicate the untranslated-literal ratchet counts with, so a guard and the
ratchet cannot disagree about whether a string is UI copy. That module is
retired when its numbers reach zero; the predicate then moves here.

Rule 21 applies to this scanner as much as to a metric: the guard that uses it
carries fixtures whose answer is already known, including a literal injected
back into the real file, so a scanner that silently stopped seeing anything
fails instead of passing everything.
"""
from collections import namedtuple
import re

from tests.test_ui_markup_gate import STRING, is_display_text

# sink -> zero-based argument positions that carry display text, with the
# signature that makes the positions reviewable. A new sink is one line here.
MESSAGE_SINKS = {
    'showToast': (0,),                 # showToast(message, type, duration)
    'showActionError': (0, 2),         # showActionError(action, error, recovery, type)
    'getActionErrorMessage': (0, 2),   # getActionErrorMessage(action, error, recovery)
    'confirmIfRemote': (0,),           # confirmIfRemote(taskLabel, failoverOnly)
    'setUiMessage': (2,),              # setUiMessage(node, key, english, values)
}
# A display node written directly. Only these two sinks are text-only; innerHTML
# carries template markup, and the keyed t() calls inside it are found by the
# sink scan above anyway.
MESSAGE_ASSIGNMENT = re.compile(r'\.(?:textContent|innerText)\s*=\s*')
# Calls that turn a literal into a catalog entry: their key and English
# arguments are keyed, not bare.
KEYING_CALL = re.compile(
    r'\b(?:t|tSafety|formatUiSafetyText|translateUiMessage|uiMessage|uiMessageMarkup'
    r'|createUiMessageNode)\s*\(')
# The ratchet's identifier prefixes: paths, URLs, selectors and attribute
# fragments are wiring, never copy.
IDENTIFIER_PREFIXES = ('/', 'http://', 'https://', '.', '#', '[')

Finding = namedtuple('Finding', 'line sink literal')


def _quoted_end(text, index):
    quote = text[index]
    index += 1
    while index < len(text):
        if text[index] == '\\':
            index += 2
            continue
        if text[index] == quote:
            return index + 1
        index += 1
    return index


def arguments(text, open_paren):
    """Top-level (start, end) spans of the arguments after `open_paren`."""
    spans = []
    depth = 0
    start = open_paren
    index = open_paren
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            index = _quoted_end(text, index)
            continue
        if char in '([{':
            depth += 1
        elif char in ')]}':
            if depth == 0:
                spans.append((start, index))
                return spans
            depth -= 1
        elif char == ',' and depth == 0:
            spans.append((start, index))
            start = index + 1
        index += 1
    spans.append((start, len(text)))
    return spans


def keyed_spans(text):
    """Spans of the key and English arguments of every keying call.

    These are expression spans, so they keep the whitespace around the literals
    they cover; _is_keyed tests containment rather than equality.
    """
    spans = set()
    for match in KEYING_CALL.finditer(text):
        for span in arguments(text, match.end())[:3]:
            spans.add(span)
    return spans


def _statement_span(text, start):
    """The span of the statement that begins at `start`."""
    index = start
    depth = 0
    while index < len(text):
        char = text[index]
        if char in "'\"`":
            index = _quoted_end(text, index)
            continue
        if char in '([{':
            depth += 1
        elif char in ')]}':
            depth = max(0, depth - 1)
        elif char == ';' and depth == 0:
            return (start, index)
        index += 1
    return (start, len(text))


def _is_display(body):
    # Runtime interpolations are values, not copy: `${epoch}/${maxEpoch}` is a
    # progress ratio. What remains around them is checked like any literal.
    without_values = re.sub(r'\$\{[^}]*\}', ' ', body)
    if not is_display_text(without_values):
        return False
    return not without_values.strip().startswith(IDENTIFIER_PREFIXES)


def _is_keyed(match, keyed):
    return any(start <= match.start() and match.end() <= end for start, end in keyed)


def _findings(source, span, keyed, sink):
    start, end = span
    findings = []
    for match in STRING.finditer(source, start, end):
        if _is_keyed(match, keyed):
            continue
        body = match.group(0)[1:-1]
        if _is_display(body):
            findings.append(Finding(source.count('\n', 0, match.start()) + 1, sink, body))
    return findings


def bare_message_literals(source):
    """Every display literal still handed straight to a message sink."""
    keyed = keyed_spans(source)
    findings = []
    for name, positions in MESSAGE_SINKS.items():
        pattern = re.compile(r'(?<![\w$])' + re.escape(name) + r'\s*\(')
        for match in pattern.finditer(source):
            spans = arguments(source, match.end())
            for position in positions:
                if position < len(spans):
                    findings.extend(_findings(source, spans[position], keyed, name))
    for match in MESSAGE_ASSIGNMENT.finditer(source):
        findings.extend(_findings(source, _statement_span(source, match.end()), keyed,
                                  'textContent'))
    return sorted(set(findings), key=lambda finding: (finding.line, finding.sink))
