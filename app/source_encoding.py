"""One source decoding policy for single-pass and three-pass generation."""
from pathlib import Path


def get_decoded_source_text(raw):
    """Prefer strict UTF-8, then CP1252; retain explicit damage as replacement."""
    for encoding in ("utf-8", "cp1252"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8/replace"


def read_source_text(path):
    """Read one byte snapshot using the shared source decoding policy."""
    return get_decoded_source_text(Path(path).read_bytes())


def get_normalized_source_newlines(text):
    """Give both generation paths the same LF paragraph boundaries."""
    return text.replace('\r\n', '\n').replace('\r', '\n')
