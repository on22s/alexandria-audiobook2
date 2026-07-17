"""Sanitized, bounded diagnostics bundles for support without leaking secrets.

Pure and self-contained (no app imports) so it is fully unit-testable and cannot
mutate app state. The caller (routers/voicelab.py) gathers raw sections and hands
them to :func:`build_diagnostics`, which recursively redacts secrets, collapses
home paths, and enforces per-value / list / total size limits before returning.

Full logs are intentionally NOT embedded — only their identifiers. Point users at
Pinokio's native Get Help / session bundle for complete logs.
"""

import datetime
import json
import re

SCHEMA_VERSION = 1

REDACTED = "[REDACTED]"

# Per-value and structural bounds keep the bundle small, deterministic, and safe
# to copy/paste into an issue. Tunable but must stay conservative.
MAX_STRING_CHARS = 2000
MAX_LIST_ITEMS = 50
MAX_TOTAL_BYTES = 65536
STRING_TRUNCATION_MARKER = "…[truncated]"
LIST_TRUNCATION_MARKER = "…[truncated]"

# A dict value is fully redacted when its key name contains any of these tokens.
# Substring match on the lowercased key so "llm_api_key", "X-Auth-Token",
# "client_secret", "sessionCookie" etc. are all caught.
_SENSITIVE_KEY_TOKENS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth_password", "cookie", "credential", "private_key",
    "access_key", "client_secret", "bearer", "passphrase",
)

# scheme://user:pass@host  ->  scheme://[REDACTED]@host
_URL_CREDENTIALS = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
# key=secret / key: secret in free text (e.g. inside a log line)
_INLINE_CREDENTIAL = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization|bearer)(\s*[=:]\s*)"
    r"([^\s,;]+)")
# Common home-directory shapes across platforms.
_HOME_PATTERNS = (
    re.compile(r"/home/[^/\s]+"),
    re.compile(r"/Users/[^/\s]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+"),
)


def _key_is_sensitive(key):
    lowered = str(key).lower()
    return any(token in lowered for token in _SENSITIVE_KEY_TOKENS)


def redact_text(value, home_dir=None):
    """Scrub URL credentials, inline key=secret pairs, and home paths from a str."""
    if not isinstance(value, str):
        return value
    result = value
    if home_dir:
        # Replace the exact resolved home first, then generic /home/<user> shapes.
        result = result.replace(home_dir, "~")
    for pattern in _HOME_PATTERNS:
        result = pattern.sub("~", result)
    result = _URL_CREDENTIALS.sub(r"\1" + REDACTED + "@", result)
    result = _INLINE_CREDENTIAL.sub(r"\1\2" + REDACTED, result)
    if len(result) > MAX_STRING_CHARS:
        result = result[:MAX_STRING_CHARS] + STRING_TRUNCATION_MARKER
    return result


def redact(obj, home_dir=None):
    """Recursively redact secrets and bound sizes in an arbitrary JSON-like value."""
    if isinstance(obj, dict):
        cleaned = {}
        for key, value in obj.items():
            if _key_is_sensitive(key):
                cleaned[key] = REDACTED
            else:
                cleaned[key] = redact(value, home_dir)
        return cleaned
    if isinstance(obj, (list, tuple)):
        items = [redact(item, home_dir) for item in obj[:MAX_LIST_ITEMS]]
        if len(obj) > MAX_LIST_ITEMS:
            items.append(LIST_TRUNCATION_MARKER)
        return items
    if isinstance(obj, str):
        return redact_text(obj, home_dir)
    # int / float / bool / None pass through unchanged.
    return obj


def _too_big(bundle):
    return len(json.dumps(bundle, ensure_ascii=False).encode("utf-8")) > MAX_TOTAL_BYTES


def build_diagnostics(sections, home_dir=None, schema_version=SCHEMA_VERSION):
    """Assemble a redacted, bounded diagnostics bundle from named raw sections.

    ``sections`` is an ordered dict of section-name -> raw JSON-like value. Each
    is independently redacted. If the whole bundle still exceeds the total byte
    budget, the largest optional sections are dropped (replaced with an explicit
    marker) from largest to smallest until it fits, so the result is always
    bounded and valid.
    """
    bundle = {
        "schema_version": schema_version,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "note": ("Secrets and home paths are redacted; logs are referenced by "
                 "identifier only. For full logs use Pinokio's Get Help / "
                 "session bundle."),
        "sections": {name: redact(value, home_dir) for name, value in sections.items()},
    }

    if _too_big(bundle):
        # Drop largest sections first; keep dropping until within budget.
        sized = sorted(
            bundle["sections"].items(),
            key=lambda kv: len(json.dumps(kv[1], ensure_ascii=False).encode("utf-8")),
            reverse=True,
        )
        for name, _value in sized:
            bundle["sections"][name] = f"[omitted: exceeded {MAX_TOTAL_BYTES}-byte bundle budget]"
            if not _too_big(bundle):
                break
    return bundle
