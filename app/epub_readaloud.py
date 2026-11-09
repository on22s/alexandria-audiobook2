"""AA2 EPUB 3 read-aloud export (#1779) — source-preserving SMIL overlays.

Retained EPUB bytes (see scripts upload retention) drive spine order,
stable ids, titles and cover. AA2 chunk timelines drive SMIL clip times.
Every step fails closed where synchronization cannot be justified.

Normative basis (W3C EPUB 3.3): package metadata / navigation / Media
Overlays. Manifest XHTML items associate overlays with the
``media-overlay`` *attribute* naming the SMIL manifest id (never a
``properties`` token). ``media:duration`` values are SMIL clock values:
each overlay refines its SMIL id and equals the sum of its active clips
(``clipEnd - clipBegin``); the publication duration is unrefined and
equals the sum of the overlay durations. Exactly one ``dcterms:modified``
UTC timestamp is emitted. SMIL timing is ``<par>`` directly in ``<body>``
(EPUB 3.3 ``<seq>`` would require ``epub:textref``); clips are ordered
and non-overlapping. SMIL clocks are whole-millisecond (``HH:MM:SS.mmm``):
a clip that collapses to zero length or overlaps after 1 ms rounding is
refused explicitly rather than padded/shifted, since inventing 1 ms per
sub-millisecond span would drift reported durations (e.g. ten 1-frame
spans at 24 kHz span ~0.4 ms, not 10 ms). Real TTS clips are orders of
magnitude longer; a row that collapses at millisecond precision must be
repaired upstream first. EPUB 2 NCX-only sources are converted by generating a
real EPUB 3 ``nav`` from the NCX (labels/order/hrefs preserved), dropping
the OPF ``guide``/``toc`` legacy attributes and preserving NCX/resource
bytes; stamping ``version="3.0"`` without a nav is never done.

Source coverage (R6): every ``body`` text node (excluding
``head``/``script``/``style``) is counted once in source order. Blocks
(``p``/``h1``-``h6``/``li``/``blockquote``/...) and containers
(``div``/``section``/...) partition the tree: a container with direct
text coarsens to one segment holding its whole subtree so mixed
``lead <p/> tail`` never duplicates or drops nodes. Mapping requires
exact normalized token coverage (NFKC + casefold + word tokens;
case/punctuation intentionally ignored) over contiguous chunks — many
chunks may form one segment, one chunk spanning multiple segments is
refused explicitly rather than inventing sub-segment timing.
"""
import copy
import datetime
import hashlib
import io
import json
import posixpath
import re
import zipfile
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit
import unicodedata
import xml.etree.ElementTree as ET

import utils  # inserts the repo root on sys.path for alexandria_alignment users
from epub_package import get_epub_package

MAX_PUBLICATION_XML_BYTES = 2 * 1024**2

MAX_EPUB_READALOUD_XML_BYTES = 8 * 1024 * 1024
XHTML_NS = "http://www.w3.org/1999/xhtml"
OPS_NS = "http://www.idpf.org/2007/ops"
SMIL_NS = "http://www.w3.org/ns/SMIL"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
EPUB3_MEDIA_TYPE = "application/oebps-package+xml"
SMIL_MEDIA_TYPE = "application/smil+xml"
XHTML_MEDIA_TYPE = "application/xhtml+xml"
NCX_MEDIA_TYPE = "application/x-dtbncx+xml"

_NOTIFIER = "AA2 read-aloud export (source-preserving; generated audio overlay, never a text-only substitute)"

_WORDS_RE = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)
_CLOCK_RE = re.compile(r"(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?")
_MODIFIED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
          "dt", "dd", "figcaption", "pre", "td", "th"}
_CONTAINER = {"body", "div", "section", "article", "main", "header", "footer",
              "aside", "figure", "ul", "ol", "dl", "table", "thead", "tbody",
              "tfoot", "tr", "fieldset", "nav", "hgroup"}
_EXCLUDE = {"script", "style"}


def _local(tag):
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


# --- URI <-> archive-path boundary -------------------------------------
# ZIP member names are decoded archive paths and stay that way. URIs in
# OPF/SMIL/nav attributes are percent-encoded exactly once from those
# decoded paths (path and fragment encoded separately), and resolved
# back by exactly one strict percent-decode. Existing source hrefs are
# preserved verbatim in the output OPF/nav bytes and are never
# re-encoded here, so an already-encoded ``chap%20one.xhtml`` is not
# double-encoded to ``%2520``; a literal ``%`` in a ZIP name encodes to
# ``%25`` (``a%2Fb.xhtml`` -> ``a%252Fb.xhtml``).
# Raw non-ASCII is a valid IRI (EPUBCheck accepts ``café.xhtml`` raw)
# and is accepted on resolution; raw space / ASCII URI-forbidden
# characters and malformed percent escapes are not (RSC-020) and fail
# closed rather than being silently ``unquote``-tolerated.

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_RAW_URI_FORBIDDEN = frozenset(' "<>\\^`{|}')


def _check_raw_uri(raw, what):
    if not isinstance(raw, str):
        raise ValueError(f"Invalid URI in {what}: {raw!r}")
    for ch in raw:
        code = ord(ch)
        if code < 0x20 or code == 0x7F or ch in _RAW_URI_FORBIDDEN:
            raise ValueError(f"Invalid URI in {what}: {raw!r}")
    i = 0
    while i < len(raw):
        if raw[i] == "%":
            if i + 2 >= len(raw) or raw[i + 1] not in _HEX_DIGITS or raw[i + 2] not in _HEX_DIGITS:
                raise ValueError(f"Invalid percent escape in {what}: {raw!r}")
            i += 3
        else:
            i += 1


def _decode_uri_path(raw, what):
    _check_raw_uri(raw, what)
    try:
        decoded = unquote(raw, errors="strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"Invalid URI in {what}: {raw!r}") from error
    if "\\" in decoded or any(ord(c) < 0x20 or ord(c) == 0x7F for c in decoded):
        raise ValueError(f"Invalid URI in {what}: {raw!r}")
    return decoded


def _decode_uri_fragment(raw, what):
    if isinstance(raw, str) and "#" in raw:
        raise ValueError(f"Invalid URI in {what}: {raw!r}")
    _check_raw_uri(raw, what)
    try:
        decoded = unquote(raw, errors="strict")
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError(f"Invalid URI in {what}: {raw!r}") from error
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in decoded):
        raise ValueError(f"Invalid URI in {what}: {raw!r}")
    return decoded


def _encode_uri_path(decoded_path):
    return quote(decoded_path, safe="/")


def _encode_uri_fragment(decoded_fragment):
    return quote(decoded_fragment, safe="")


def _resolve_uri_path(raw_path, base_dir, what):
    decoded = _decode_uri_path(raw_path, what)
    base = base_dir if base_dir else "."
    joined = posixpath.normpath(posixpath.join(base, decoded))
    if joined == ".." or joined.startswith("../") or joined.startswith("/"):
        raise ValueError(f"URI escapes the archive in {what}: {raw_path!r}")
    return joined


def _namespace(tag):
    if isinstance(tag, str) and tag.startswith("{"):
        return tag[1:].split("}", 1)[0]
    return ""


def _text_node_text(node):
    return "".join(node.itertext())


def get_spine_documents(archive_bytes, *, max_xml_bytes=MAX_EPUB_READALOUD_XML_BYTES):
    """Parse spine, manifest and navigation without extracting to disk."""
    if not isinstance(archive_bytes, bytes) or not archive_bytes:
        raise ValueError("Retained EPUB bytes are missing.")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        opf_path, package = get_epub_package(
            archive, max_xml_bytes=MAX_PUBLICATION_XML_BYTES, require_unique=True)
        namespace = package.tag.split('}')[0] + '}' if '}' in package.tag else ''
        if package.tag != namespace + 'package':
            raise ValueError("Retained EPUB root is not a publication package.")
        if _namespace(package.tag) != OPF_NS:
            raise ValueError("Retained EPUB package is not in the OPF namespace.")
        major = (package.get('version') or '').split('.')[0]
        if major not in ('2', '3'):
            raise ValueError("Retained EPUB package version is unsupported; only EPUB 2 or EPUB 3 sources can be exported.")
        # EPUB3 OUTPUT, not necessarily EPUB3 INPUT: EPUB2 inputs are upgraded
        # on output (version 3.0, manifest media-overlay association,
        # SMIL-clock media:duration, generated nav from NCX). Package/nav
        # metadata is preserved; NCX and resource bytes are retained.
        manifest = {}
        for item in package.findall(namespace + 'manifest/' + namespace + 'item'):
            item_id, href = item.get('id'), item.get('href')
            if not item_id or not href:
                continue
            if item_id in manifest:
                raise ValueError("Retained EPUB has duplicate manifest ids.")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or parsed.query:
                raise ValueError("Retained EPUB has an external manifest reference.")
            if parsed.fragment:
                raise ValueError("Retained EPUB manifest reference must not carry a fragment.")
            path = _resolve_uri_path(parsed.path, str(PurePosixPath(opf_path).parent),
                                     "manifest href")
            manifest[item_id] = {"path": path, "media_type": item.get('media-type', ''),
                                 "properties": item.get('properties', ''),
                                 "media_overlay": item.get('media-overlay'),
                                 "href": href}
        spine = []
        for itemref in package.findall(namespace + 'spine/' + namespace + 'itemref'):
            idref = itemref.get('idref')
            item = manifest.get(idref) if idref else None
            if item is None:
                raise ValueError("Retained EPUB spine references a missing manifest item.")
            spine.append({"idref": idref, "path": item["path"], "media_type": item["media_type"],
                          "media_overlay": itemref.get('media-overlay'),
                          "linear": itemref.get('linear') != 'no'})
        metadata = package.find(namespace + 'metadata')
        title = author = language = None
        if metadata is not None:
            DC = '{http://purl.org/dc/elements/1.1/}'
            titles = metadata.findall(DC + 'title')
            creators = metadata.findall(DC + 'creator')
            langs = metadata.findall(DC + 'language')
            title = "".join(titles[0].itertext()).strip() if titles else None
            author = "".join(creators[0].itertext()).strip() if creators else None
            language = "".join(langs[0].itertext()).strip() if langs else None
            if language is not None and not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]+)*", language):
                raise ValueError("Retained EPUB language tag is invalid.")
        toc = []
        for item in manifest.values():
            if 'nav' in item['properties'].split():
                try:
                    root = ET.fromstring(archive.read(item['path']))
                except (KeyError, ET.ParseError):
                    continue
                for nav in root.iter():
                    if nav.tag.rsplit('}', 1)[-1] != 'nav':
                        continue
                    nav_type = nav.get('{%s}type' % OPS_NS) or nav.get('epub:type') or ''
                    if 'toc' not in nav_type.split():
                        continue
                    for link in nav.iter():
                        if link.tag.rsplit('}', 1)[-1] != 'a':
                            continue
                        label = ' '.join(''.join(link.itertext()).split())
                        href = link.get('href') or ''
                        parsed = urlsplit(href)
                        if not label or parsed.scheme or parsed.netloc or parsed.query:
                            continue
                        base = PurePosixPath(item['path']).parent
                        target = _resolve_uri_path(parsed.path, str(base), "nav href")
                        toc.append({"path": target,
                                    "fragment": _decode_uri_fragment(parsed.fragment, "nav href") or None,
                                    "label": label})
                    return (opf_path, package, namespace, manifest, spine,
                            {"title": title, "author": author, "language": language or "und",
                             "toc": toc, "source_version": major})
        return (opf_path, package, namespace, manifest, spine,
                {"title": title, "author": author, "language": language or "und", "toc": toc,
                 "source_version": major})


def _visible_pieces(node):
    """Yield visible text-node strings of a subtree in order (skip script/style)."""
    if node.text:
        yield node.text
    for child in node:
        if _local(child.tag) in _EXCLUDE:
            if child.tail:
                yield child.tail
            continue
        if _local(child.tag) in _BLOCK or _local(child.tag) in _CONTAINER:
            yield " "
            yield from _visible_pieces(child)
            yield " "
        else:
            yield from _visible_pieces(child)
        if child.tail:
            yield child.tail


def _visible_text(node):
    return " ".join("".join(_visible_pieces(node)).split())


def _direct_pieces(node, is_root=True):
    """Text outside BLOCK/CONTAINER descendants (tails of blocks included)."""
    if not is_root and (_local(node.tag) in _BLOCK or _local(node.tag) in _CONTAINER):
        return ""
    out = []
    if node.text:
        out.append(node.text)
    for child in node:
        if _local(child.tag) in _EXCLUDE:
            if child.tail:
                out.append(child.tail)
            continue
        if _local(child.tag) in _BLOCK or _local(child.tag) in _CONTAINER:
            if child.tail:
                out.append(child.tail)
            continue
        out.append(_direct_pieces(child, is_root=False))
        if child.tail:
            out.append(child.tail)
    return "".join(out)


def _has_direct_text(node):
    return bool(" ".join(_direct_pieces(node).split()))


def _has_block_descendant(node):
    for desc in node.iter():
        if desc is node:
            continue
        if _local(desc.tag) in _BLOCK:
            return True
    return False


def get_xhtml_segments(archive_bytes, path, max_xml_bytes=MAX_EPUB_READALOUD_XML_BYTES):
    """Return (root_element, segments[{fragment, text}]) for one spine file."""
    if path.startswith("../") or "\\" in path:
        raise ValueError("Spine path escapes the archive.")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        try:
            raw = archive.read(path)
        except KeyError as error:
            raise ValueError(f"Spine document is missing: {path}") from error
    if len(raw) > max_xml_bytes:
        raise ValueError("Spine document exceeds the read-aloud limit.")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as error:
        raise ValueError(f"Spine document is malformed: {path}") from error
    if _namespace(root.tag) != XHTML_NS:
        raise ValueError(f"Spine document is not in the XHTML namespace: {path}")
    seen_ids = set()
    for element in root.iter():
        element_id = element.get('id')
        if element_id:
            if element_id in seen_ids:
                # Duplicate stable ids would make SMIL references ambiguous.
                raise ValueError(f"Duplicate element id in spine document: {element_id}")
            seen_ids.add(element_id)
    body = None
    for element in root.iter():
        if _local(element.tag) == 'body':
            body = element
            break
    start = body if body is not None else root
    segments = []

    def _emit(el):
        text = _visible_text(el)
        if not text:
            return
        segments.append({"fragment": el.get('id') or None, "text": text, "element": el})

    def _process(el):
        tag = _local(el.tag)
        if tag in _EXCLUDE or tag == 'head':
            return
        if tag in _BLOCK or tag in _CONTAINER or tag == 'html':
            if _has_direct_text(el):
                _emit(el)
                return
            if tag in _BLOCK and not _has_block_descendant(el):
                _emit(el)
                return
            for child in el:
                _process(child)
            return
        for child in el:
            _process(child)

    try:
        _process(start)
    except RecursionError as error:
        raise ValueError(f"Spine document is too deeply nested to segment: {path}") from error
    full_text = "\n".join(segment["text"] for segment in segments)
    return root, segments, full_text


def get_normalized_words(text):
    """NFKC + casefold tokenization (case/punctuation intentionally ignored)."""
    folded = unicodedata.normalize("NFKC", text or "").casefold()
    folded = folded.replace('\u2019', "'").replace('\u2018', "'")
    return _WORDS_RE.findall(folded)


def get_word_alignment_score(source_words, script_words):
    """Simple ratio via difflib; high only when texts genuinely agree."""
    import difflib
    if not source_words and not script_words:
        return 1.0
    return difflib.SequenceMatcher(None, source_words, script_words, autojunk=False).ratio()


def map_spine_to_chunks(spine_segments, script_entries, *, start=0, threshold=None, require_full_coverage=False):
    """Map each spine segment to a contiguous chunk range or fail closed.

    script_entries: [{index, prepared_text, start_frame, end_frame}]
    Matching is sequential and monotonic with exact normalized token
    coverage: similarity is only a search hint, never authority for
    different actual spoken text. A changed word fails even at high ratio.
    Coverage must be exact and contiguous from ``cursor``: leading,
    interior or trailing dropped chunks (including missing/corrupt clip
    rows, which never reach ``script_entries``) fail closed rather than
    fabricating positions. ``threshold`` is retained for backwards
    compatibility but ignored; exact coverage is the authority.

    Many chunks may form one segment (split). One chunk spanning
    multiple segments has no exact segment boundary and is refused
    explicitly with a spanning error — inventing sub-paragraph timing
    would fabricate positions.

    Returns (mapping, end_cursor), retaining each row's original segment_index.
    """
    if not spine_segments:
        raise ValueError("Spine document has no readable segments.")
    if not script_entries:
        raise ValueError("There are no script entries with audio to synchronize.")
    if not isinstance(start, int) or start < 0 or start > len(script_entries):
        raise ValueError("Spine mapping cursor is invalid.")
    script_words = [get_normalized_words(entry["prepared_text"]) for entry in script_entries]
    mapping = []
    cursor = start
    for segment_index, segment in enumerate(spine_segments):
        source_words = get_normalized_words(segment["text"])
        if not source_words:
            continue
        if cursor >= len(script_entries):
            raise ValueError(
                f"No unambiguous synchronization for spine text: {segment['text'][:80]!r} "
                f"(script exhausted)")
        combined: list = []
        end_candidate = None
        for candidate in range(cursor, len(script_entries)):
            combined.extend(script_words[candidate])
            if len(combined) > len(source_words):
                break
            if combined == source_words:
                end_candidate = candidate + 1
                break
        if end_candidate is None:
            # One chunk spanning multiple segments: the next chunk is
            # longer than this segment and starts with it exactly.
            nxt = script_words[cursor] if cursor < len(script_words) else []
            if len(nxt) > len(source_words) and nxt[:len(source_words)] == source_words:
                raise ValueError(
                    f"One script chunk spans multiple spine segments "
                    f"(first spine text {segment['text'][:80]!r} continues beyond one segment); "
                    f"supported mapping is one segment per contiguous chunk range "
                    f"— re-split the chunk or re-align the script.")
            probe = [w for words in script_words[cursor:cursor + 3] for w in words]
            hint = get_word_alignment_score(source_words, probe) if probe else 0.0
            raise ValueError(
                f"No unambiguous synchronization for spine text: {segment['text'][:80]!r} "
                f"(no exact contiguous normalized coverage; partial hint {hint:.2f})")
        chunk_indexes = list(range(cursor, end_candidate))
        mapping.append({"fragment": segment["fragment"], "text": segment["text"],
                        "segment_index": segment_index,
                        "chunk_indexes": chunk_indexes,
                        "start_frame": script_entries[cursor]["start_frame"],
                        "end_frame": script_entries[end_candidate - 1]["end_frame"]})
        cursor = end_candidate
    if cursor == start:
        raise ValueError("No spine text could be synchronized.")
    if require_full_coverage:
        used = {index for row in mapping for index in row["chunk_indexes"]}
        first, last = min(used), max(used)
        gap = [i for i in range(first, last + 1) if i not in used]
        if gap:
            raise ValueError(f"Script entries {gap} have no unambiguous spine match; failing closed.")
        missing = [entry["index"] for entry in script_entries if entry["index"] not in used]
        if missing:
            raise ValueError(
                f"Script entries {missing} have no unambiguous spine match; failing closed.")
    return mapping, cursor


def _frame_to_ms(frame, sample_rate):
    return (frame * 1000 + sample_rate // 2) // sample_rate


def _format_clock_ms(ms):
    hours, rem = divmod(int(ms), 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _parse_clock_ms(value):
    m = re.fullmatch(r"(\d+):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", (value or "").strip())
    if not m:
        raise ValueError(f"Invalid SMIL clock value: {value!r}")
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if minutes > 59 or seconds > 59:
        raise ValueError(f"Invalid SMIL clock value: {value!r}")
    frac = (m.group(4) or "0").ljust(3, "0")
    return (hours * 3600 + minutes * 60 + seconds) * 1000 + int(frac)


def get_smil_time(frame, sample_rate):
    if not isinstance(frame, int) or isinstance(frame, bool) \
            or not isinstance(sample_rate, int) or isinstance(sample_rate, bool) \
            or sample_rate <= 0 or frame < 0:
        raise ValueError("SMIL time is invalid.")
    return _format_clock_ms(_frame_to_ms(frame, sample_rate))


def _clip_bounds_ms(mapping, sample_rate):
    """Ordered, non-overlapping (begin_ms, end_ms) at 1 ms SMIL quantum.

    A clip that collapses to zero or overlaps after rounding is
    refused: padding or shifting it would invent drifted durations.
    """
    bounds = []
    prev_end = None
    for row in mapping:
        begin = _frame_to_ms(int(row["start_frame"]), int(sample_rate))
        end = _frame_to_ms(int(row["end_frame"]), int(sample_rate))
        if prev_end is not None and begin < prev_end:
            raise ValueError(
                "SMIL clips overlap after 1 ms rounding (begin "
                f"{begin} ms is before previous end {prev_end} ms); refusing "
                "to shift clips and invent timing.")
        if end <= begin:
            raise ValueError(
                "SMIL clip is below the 1 ms clock quantum "
                f"({begin}-{end} ms); refusing to invent a 1 ms span.")
        bounds.append((begin, end))
        prev_end = end
    return bounds


def _overlay_duration_ms(mapping, sample_rate):
    return sum(end - begin for begin, end in _clip_bounds_ms(mapping, sample_rate))


def build_smil_document(mapping, audio_filename, sample_rate, *, existing_ids,
                        spine_relpath=None, smil_relpath=None):
    """Build one SMIL file for one spine document.

    ``spine_relpath`` is the XHTML path and ``smil_relpath`` the SMIL path
    (both archive-relative). ``text src`` resolves to the SPECIFIC citing
    XHTML file (e.g. ``ch1.xhtml#frag`` relative to the SMIL).
    """
    if not mapping:
        raise ValueError("SMIL document would be empty.")
    if not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("SMIL sample rate is invalid.")
    bounds = _clip_bounds_ms(mapping, sample_rate)
    ET.register_namespace('', SMIL_NS)
    ET.register_namespace('epub', OPS_NS)
    root = ET.Element(f"{{{SMIL_NS}}}smil", version="3.0")
    body = ET.SubElement(root, f"{{{SMIL_NS}}}body")
    used = set(existing_ids or set())
    for position, (row, (begin_ms, end_ms)) in enumerate(zip(mapping, bounds), 1):
        fragment = row.get("fragment")
        if fragment is None:
            n = position
            while True:
                cand = f"aa2-p-{n:04d}"
                if cand not in used:
                    fragment = cand
                    break
                n += 1
        used.add(fragment)
        par = ET.SubElement(body, f"{{{SMIL_NS}}}par", id=f"aa2-par-{position:04d}")
        if spine_relpath is not None and smil_relpath is not None:
            smil_dir = str(PurePosixPath(smil_relpath).parent)
            spine_rel = (posixpath.relpath(spine_relpath, smil_dir)
                         if smil_dir != '.' else spine_relpath)
            text_src = f"{_encode_uri_path(spine_rel)}#{_encode_uri_fragment(fragment)}"
        else:
            text_src = f"#{_encode_uri_fragment(fragment)}"
        ET.SubElement(par, f"{{{SMIL_NS}}}text", src=text_src)
        ET.SubElement(par, f"{{{SMIL_NS}}}audio", src=_encode_uri_path(audio_filename),
                      clipBegin=_format_clock_ms(begin_ms),
                      clipEnd=_format_clock_ms(end_ms))
    return ET.tostring(root, encoding="unicode")


def validate_epub_readaloud(archive_bytes):
    """Re-parse a built EPUB and validate SMIL/manifest/XHTML bindings.

    Bounded EPUB 3.3 checks (not a full EPUBCheck clone): mimetype and
    manifest/ref resolution, ``media-overlay`` attribute association with
    no ``properties`` token, exactly one ``nav``, one ``dcterms:modified``,
    SMIL-clock durations equal to active-clip sums (one unrefined
    publication sum, one refined value per SMIL), SMIL structure and
    exact XHTML/audio associations, unique ids, and global clip ordering.
    """
    if not isinstance(archive_bytes, bytes) or not archive_bytes:
        raise ValueError("Built EPUB is empty.")
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = archive.namelist()
        if not names or names[0] != 'mimetype':
            raise ValueError("Built EPUB mimetype must be first.")
        if len(names) != len(set(names)):
            raise ValueError("Built EPUB has duplicate members.")
        opf_path, package = get_epub_package(
            archive, max_xml_bytes=MAX_PUBLICATION_XML_BYTES, require_unique=True)
        namespace = package.tag.split('}')[0] + '}' if '}' in package.tag else ''
        if package.tag != namespace + 'package' or _namespace(package.tag) != OPF_NS:
            raise ValueError("Built EPUB package is not in the OPF namespace.")
        if (package.get('version') or '').split('.')[0] != '3':
            raise ValueError("Built EPUB must be EPUB 3 output.")
        manifest = {}
        for item in package.findall(namespace + 'manifest/' + namespace + 'item'):
            item_id = item.get('id')
            href = item.get('href') or ''
            if not item_id or not href:
                raise ValueError("Built EPUB manifest item is missing id or href.")
            if item_id in manifest:
                raise ValueError("Built EPUB has a duplicate manifest id.")
            if item.get('fallback') is not None:
                raise ValueError("Built EPUB manifest must not use fallback chains.")
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError("Built EPUB manifest has an external/fragment reference.")
            path = _resolve_uri_path(parsed.path, str(PurePosixPath(opf_path).parent),
                                     "manifest href")
            if path not in names:
                raise ValueError("Built EPUB manifest references a missing file.")
            if 'media-overlay' in (item.get('properties') or '').split():
                raise ValueError(
                    "Built EPUB uses media-overlay as a properties token; "
                    "EPUB 3 requires the media-overlay attribute.")
            if item.get('media-overlay') is not None and item.get('media-type') != XHTML_MEDIA_TYPE:
                raise ValueError("Built EPUB media-overlay attribute is only valid on XHTML items.")
            manifest[item_id] = (path, item.get('media-type', ''), item.get('properties', ''),
                                 item.get('media-overlay'), href)
        if not manifest:
            raise ValueError("Built EPUB manifest is empty.")
        nav_ids = [i for i, (_p, _m, props, _o, _h) in manifest.items()
                   if 'nav' in (props or '').split()]
        if len(nav_ids) != 1:
            raise ValueError("Built EPUB must declare exactly one nav item.")
        spine_refs = []
        for itemref in package.findall(namespace + 'spine/' + namespace + 'itemref'):
            if itemref.get('media-overlay') is not None:
                raise ValueError("EPUB3 spine must not carry media-overlay; use manifest association.")
            idref = itemref.get('idref')
            if idref not in manifest:
                raise ValueError("Built EPUB spine references a missing manifest item.")
            spine_refs.append(idref)
        if len(spine_refs) != len(set(spine_refs)):
            raise ValueError("Built EPUB spine has duplicate itemrefs.")
        xhtml_to_smil = {}
        smil_id_to_path = {}
        for item_id, (path, media, _props, overlay, _href) in manifest.items():
            if overlay is not None:
                smil_id = overlay
                if smil_id not in manifest:
                    raise ValueError("Built EPUB media-overlay names a missing SMIL.")
                if manifest[smil_id][1] != SMIL_MEDIA_TYPE:
                    raise ValueError("Built EPUB media-overlay does not name a SMIL item.")
                if path in xhtml_to_smil and xhtml_to_smil[path] != smil_id:
                    raise ValueError("Built EPUB XHTML names two overlays.")
                xhtml_to_smil[path] = smil_id
                smil_id_to_path[smil_id] = path
        smil_paths = [path for path, media, _p, _o, _h in manifest.values() if media == SMIL_MEDIA_TYPE]
        if not smil_paths:
            raise ValueError("Built EPUB has no SMIL media overlays.")
        associated = set(xhtml_to_smil.values())
        for item_id, (path, media, _props, _o, _h) in manifest.items():
            if media == SMIL_MEDIA_TYPE and item_id not in associated:
                raise ValueError("Built EPUB SMIL is not associated with any XHTML.")
        for member in names:
            if member.endswith('.smil') and member not in smil_paths:
                raise ValueError(f"Built EPUB contains an undeclared SMIL file: {member}")
        xhtml_ids = {}
        xhtml_roots = {}
        for path, media, _props, _o, _h in manifest.values():
            if media == XHTML_MEDIA_TYPE and path:
                raw = archive.read(path)
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError as error:
                    raise ValueError("Built EPUB XHTML is malformed.") from error
                ids = set()
                for el in root.iter():
                    el_id = el.get('id')
                    if el_id:
                        if el_id in ids:
                            raise ValueError("Built EPUB XHTML has a duplicate id.")
                        ids.add(el_id)
                xhtml_ids[path] = ids
                xhtml_roots[path] = root
        nav_path = manifest[nav_ids[0]][0]
        nav_root = xhtml_roots.get(nav_path)
        if nav_root is None:
            raise ValueError("Built EPUB nav document is not an XHTML manifest item.")
        toc_links = 0
        toc_found = False
        for el in nav_root.iter():
            if _local(el.tag) == 'nav' and (
                    el.get('{%s}type' % OPS_NS) == 'toc'):
                toc_found = True
                for a in el.iter():
                    if _local(a.tag) == 'a' and (a.get('href') or ''):
                        href = a.get('href') or ''
                        parsed = urlsplit(href)
                        if parsed.scheme or parsed.netloc or parsed.query:
                            raise ValueError("Built EPUB nav link is external.")
                        target = _resolve_uri_path(parsed.path,
                                                   str(PurePosixPath(nav_path).parent),
                                                   "nav href")
                        if target not in names:
                            raise ValueError(f"Built EPUB nav link is unresolved: {href}")
                        toc_links += 1
        if not toc_found:
            raise ValueError("Built EPUB nav document has no epub:type toc nav element.")
        if not toc_links:
            raise ValueError("Built EPUB nav document has no toc links.")
        metadata = package.find(namespace + 'metadata')
        if metadata is None:
            raise ValueError("Built EPUB lacks package metadata.")
        mods = [m for m in metadata.findall(namespace + 'meta')
                if m.get('property') == 'dcterms:modified']
        if len(mods) != 1:
            raise ValueError("Built EPUB must have exactly one dcterms:modified.")
        if not _MODIFIED_RE.match((mods[0].text or '').strip()):
            raise ValueError("Built EPUB dcterms:modified must be a UTC timestamp.")
        durations = [m for m in metadata.findall(namespace + 'meta')
                     if m.get('property') == 'media:duration']
        unrefined = [m for m in durations if (m.get('refines') or '') == '']
        refined = [m for m in durations if (m.get('refines') or '') != '']
        smil_ids = {i for i, (_p, media, _pr, _o, _h) in manifest.items() if media == SMIL_MEDIA_TYPE}
        if len(unrefined) != 1:
            raise ValueError("Built EPUB must have exactly one unrefined media:duration.")
        if len(refined) != len(smil_ids):
            raise ValueError("Built EPUB must refine one media:duration per SMIL.")
        seen_refines = set()
        for m in refined:
            ref = (m.get('refines') or '')
            if not ref.startswith('#') or ref[1:] not in smil_ids or ref[1:] in seen_refines:
                raise ValueError("Built EPUB media:duration refines must name each SMIL once.")
            seen_refines.add(ref[1:])
        audio_paths = {path for path, media, _p, _o, _h in manifest.values()
                       if (media or '').startswith('audio/')}
        if not audio_paths:
            raise ValueError("Built EPUB declares no audio in the manifest.")
        spine_order = {manifest[ref][0]: idx for idx, ref in enumerate(spine_refs)}
        clip_events = []  # (spine_index, begin_ms, end_ms) in overlap order
        per_smil_ms = {}
        for smil_id, xhtml_path in smil_id_to_path.items():
            smil_path = manifest[smil_id][0]
            raw = archive.read(smil_path)
            try:
                smil_root = ET.fromstring(raw)
            except ET.ParseError as error:
                raise ValueError("Built EPUB SMIL is malformed.") from error
            if not smil_root.get('version'):
                raise ValueError("Built EPUB SMIL lacks a version.")
            if smil_root.tag != f"{{{SMIL_NS}}}smil":
                raise ValueError("Built EPUB SMIL root is invalid.")
            if smil_root.get('{http://www.w3.org/XML/1998/namespace}lang') is not None:
                raise ValueError("Built EPUB SMIL must not carry xml:lang.")
            ids = set()
            for el in smil_root.iter():
                el_id = el.get('id')
                if el_id:
                    if el_id in ids:
                        raise ValueError("Built EPUB SMIL has a duplicate id.")
                    ids.add(el_id)
                if _local(el.tag) == 'seq' and el.get('{%s}textref' % OPS_NS) is None:
                    raise ValueError("Built EPUB SMIL <seq> requires epub:textref.")
            total_ms = 0
            prev_begin = None
            prev_end = None
            pars = list(smil_root.iter(f"{{{SMIL_NS}}}par"))
            if not pars:
                raise ValueError("Built EPUB SMIL has no timing groups.")
            for par in pars:
                if par.get('text') is not None:
                    raise ValueError("Built EPUB SMIL has a stray par text attribute.")
                texts = [c for c in par if c.tag == f"{{{SMIL_NS}}}text"]
                audios = [c for c in par if c.tag == f"{{{SMIL_NS}}}audio"]
                if len(texts) != 1 or len(audios) != 1:
                    raise ValueError("Built EPUB SMIL <par> must hold one text and one audio.")
                text = texts[0]
                audio = audios[0]
                src = text.get('src') or ''
                if '#' not in src or '://' in src:
                    raise ValueError("Built EPUB SMIL text reference is invalid.")
                file_part, fragment = src.split('#', 1)
                if not file_part or not fragment:
                    raise ValueError("Built EPUB SMIL text reference is invalid.")
                if '?' in file_part:
                    raise ValueError("Built EPUB SMIL text reference is invalid.")
                cited = _resolve_uri_path(file_part,
                                          str(PurePosixPath(smil_path).parent),
                                          "SMIL text href")
                if cited != xhtml_path:
                    raise ValueError(
                        f"Built EPUB SMIL cites the wrong XHTML file: {cited} != {xhtml_path}")
                fragment = _decode_uri_fragment(fragment, "SMIL text href")
                if fragment not in xhtml_ids[cited]:
                    raise ValueError(f"Built EPUB SMIL fragment is unresolved: {fragment} in {cited}")
                asrc = audio.get('src') or ''
                if not asrc or asrc.startswith('/'):
                    raise ValueError("Built EPUB SMIL audio reference is invalid.")
                audio_parsed = urlsplit(asrc)
                if audio_parsed.scheme or audio_parsed.netloc or audio_parsed.query or audio_parsed.fragment:
                    raise ValueError("Built EPUB SMIL audio reference is invalid.")
                audio_path = _resolve_uri_path(audio_parsed.path,
                                               str(PurePosixPath(smil_path).parent),
                                               "SMIL audio href")
                if audio_path not in audio_paths:
                    raise ValueError(f"Built EPUB audio is not declared in the manifest: {audio_path}")
                if audio_path not in names:
                    raise ValueError(f"Built EPUB audio is missing: {audio_path}")
                if not (audio.get('clipBegin') and audio.get('clipEnd')):
                    raise ValueError("Built EPUB SMIL clip lacks begin or end.")
                begin_ms = _parse_clock_ms(audio.get('clipBegin'))
                end_ms = _parse_clock_ms(audio.get('clipEnd'))
                if not (end_ms > begin_ms):
                    raise ValueError("Built EPUB SMIL clip is not ordered.")
                if prev_begin is not None and begin_ms < prev_begin:
                    raise ValueError("Built EPUB SMIL clips are not ordered.")
                if prev_end is not None and begin_ms < prev_end:
                    raise ValueError("Built EPUB SMIL clips overlap.")
                prev_begin = begin_ms
                prev_end = end_ms
                total_ms += end_ms - begin_ms
                clip_events.append((spine_order.get(xhtml_path, -1), begin_ms, end_ms))
            per_smil_ms[smil_id] = total_ms
        for m in refined:
            ref = (m.get('refines') or '')[1:]
            if _parse_clock_ms(m.text or '') != per_smil_ms[ref]:
                raise ValueError("Built EPUB overlay duration must equal active-clip sum.")
        if _parse_clock_ms(unrefined[0].text or '') != sum(per_smil_ms.values()):
            raise ValueError("Built EPUB publication duration must equal overlay sum.")
        clip_events.sort(key=lambda row: (row[0], row[1], row[2]))
        for prev, cur in zip(clip_events, clip_events[1:]):
            if cur[0] == prev[0]:
                continue
            if cur[1] < prev[2]:
                raise ValueError("Built EPUB SMIL clips overlap across documents.")
        return {"smil_files": len(smil_paths), "files": len(names)}


def _fresh_id(by_id, base):
    n = 1
    while True:
        cand = f"{base}-{n:03d}"
        if cand not in by_id:
            return cand
        n += 1


def _stepped_path(path, taken):
    """Return `path`, or a `_N` variant absent from `taken`."""
    if path not in taken:
        return path
    base = PurePosixPath(path)
    for n in range(2, 1000):
        cand = PurePosixPath(base.parent, f"{base.stem}_{n}{base.suffix}").as_posix()
        if cand not in taken:
            return cand
    raise ValueError("Too many filename collisions.")


def _relative_to_opf(target, base_dir):
    """Percent-encoded href for a decoded archive path, relative to the OPF."""
    rel = posixpath.relpath(target, base_dir) if base_dir else target
    return _encode_uri_path(rel)


def _ensure_html5_doctype(data):
    """Ensure an XHTML byte string carries the HTML 5 doctype (idempotent)."""
    if b'<!DOCTYPE html>' in data:
        return data
    decl_end = data.find(b'?>')
    if decl_end != -1:
        rest = data[decl_end + 2:]
        stripped = rest.lstrip()
        if stripped.startswith(b'<!DOCTYPE'):
            gt = stripped.find(b'>')
            if gt != -1:
                return data[:decl_end + 2] + b'\n<!DOCTYPE html>' + stripped[gt + 1:]
        return data[:decl_end + 2] + b'\n<!DOCTYPE html>' + rest
    if data.startswith(b'<!DOCTYPE'):
        gt = data.find(b'>')
        if gt != -1:
            return data[:gt + 1] + data[gt + 1:]
    return b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n' + data


def _opf_dir(opf_path):
    parent = str(PurePosixPath(opf_path).parent)
    return '' if parent == '.' else parent


def get_ncx_points(archive_bytes, manifest, opf_path):
    """Return nested NCX toc as [{label, href(path+fragment), children}] or []."""
    ncx_path = None
    for item in manifest.values():
        if item.get('media_type') == NCX_MEDIA_TYPE:
            ncx_path = item['path']
            break
    if ncx_path is None:
        return []
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        try:
            raw = archive.read(ncx_path)
        except KeyError:
            return []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    nav_map = root.find('.//{%s}navMap' % 'http://www.daisy.org/z3986/2005/ncx/')
    if nav_map is None:
        # Namespace-less or alternate NCX namespace: fall back to local name.
        for el in root.iter():
            if _local(el.tag) == 'navMap':
                nav_map = el
                break
    if nav_map is None:
        return []
    ncx_dir = str(PurePosixPath(ncx_path).parent)

    def _parse_point(point):
        label = ""
        href = ""
        children = []
        for child in point:
            name = _local(child.tag)
            if name == 'navLabel':
                for sub in child:
                    if _local(sub.tag) == 'text':
                        label = " ".join("".join(sub.itertext()).split())
                        break
            elif name == 'content':
                href = child.get('src') or ''
            elif name == 'navPoint':
                nested = _parse_point(child)
                if nested:
                    children.append(nested)
        if not href:
            return {"label": label, "href": href, "full": "",
                    "fragment": None, "children": children}
        parsed_href = urlsplit(href)
        if parsed_href.scheme or parsed_href.netloc or parsed_href.query:
            raise ValueError("Retained EPUB NCX has an external NCX reference.")
        full = _resolve_uri_path(parsed_href.path, ncx_dir, "NCX href")
        return {"label": label, "href": href, "full": full,
                "fragment": _decode_uri_fragment(parsed_href.fragment, "NCX href") or None,
                "children": children}

    points = []
    for child in nav_map:
        if _local(child.tag) == 'navPoint':
            parsed = _parse_point(child)
            if parsed['label'] and parsed['href']:
                points.append(parsed)
    return points


def _nav_links_href(full, fragment, nav_dir):
    rel = posixpath.relpath(full, nav_dir) if nav_dir not in ('', '.') else full
    encoded = _encode_uri_path(rel)
    return f"{encoded}#{_encode_uri_fragment(fragment)}" if fragment else encoded


def build_epub_readaloud(*, retained_epub_bytes, audio_bytes, audio_filename, script_entries,
                         cancel_check=None):
    """Build a source-preserving EPUB 3 read-aloud archive.

    retained_epub_bytes: exact bytes from the upload retention.
    audio_bytes: exported MP3 bytes bound to the sidecar timeline.
    script_entries: [{index, prepared_text, start_frame, end_frame}]
    Returns (epub_bytes, report).
    """
    if not script_entries:
        raise ValueError("There are no script entries with audio to synchronize.")
    for entry in script_entries:
        if not isinstance(entry.get("prepared_text"), str) or not entry["prepared_text"].strip():
            raise ValueError(f"Script entry {entry.get('index')} has no spoken text to synchronize.")
        if not isinstance(entry.get("start_frame"), int) or isinstance(entry.get("start_frame"), bool) \
                or not isinstance(entry.get("end_frame"), int) or isinstance(entry.get("end_frame"), bool) \
                or entry["end_frame"] <= entry["start_frame"]:
            raise ValueError(f"Script entry {entry.get('index')} has invalid frame bounds.")
    sample_rate = script_entries[0].get("sample_rate") if script_entries else None
    if not isinstance(sample_rate, int) or isinstance(sample_rate, bool) or sample_rate <= 0:
        raise ValueError("Script entries lack their PCM sample rate.")
    for entry in script_entries:
        rate = entry.get("sample_rate")
        if rate is not None and rate != sample_rate:
            raise ValueError("Script entries must share one PCM sample rate.")
    (opf_path, package, namespace, manifest, spine, meta) = get_spine_documents(retained_epub_bytes)
    if not spine:
        raise ValueError("Retained EPUB has no spine documents.")
    for item in package.findall(namespace + 'manifest/' + namespace + 'item'):
        if item.get('fallback') is not None:
            raise ValueError("Retained EPUB manifest fallback chains are unsupported; failing closed.")
    output_members = {}
    smil_records = []
    modified_xhtml = {}
    cursor = 0
    ET.register_namespace('', XHTML_NS)
    ET.register_namespace('epub', OPS_NS)
    for spine_item in spine:
        if spine_item.get("linear") is False:
            continue
        mtype = spine_item["media_type"] or ''
        manifest_item = manifest.get(spine_item["idref"], {})
        if 'nav' in (manifest_item.get("properties") or '').split():
            continue
        if mtype == 'image/svg+xml':
            with zipfile.ZipFile(io.BytesIO(retained_epub_bytes)) as _src:
                try:
                    _svg = ET.fromstring(_src.read(spine_item["path"]))
                except (KeyError, ET.ParseError):
                    continue
            if any(_local(el.tag) == 'text' and (el.text or '').strip()
                   for el in _svg.iter()):
                raise ValueError(
                    "SVG spine documents with text are unsupported; failing closed.")
            continue
        if mtype != XHTML_MEDIA_TYPE and 'html' not in mtype:
            if not mtype:
                raise ValueError(f"Retained EPUB spine item has no media type: {spine_item['path']}")
            # Cover / non-content spine items keep no overlay.
            continue
        root, segments, _full = get_xhtml_segments(retained_epub_bytes, spine_item["path"])
        if not segments:
            # Image-only / empty XHTML (cover, title page): no narration
            # required and no chunks consumed; real visible text still maps.
            continue
        mapping, cursor = map_spine_to_chunks(segments, script_entries, start=cursor)
        existing_ids = {el.get('id') for el in root.iter() if el.get('id')}
        assigned = set(existing_ids)
        for position, row in enumerate(mapping, 1):
            segment = segments[row["segment_index"]]
            frag = segment["fragment"]
            if frag is None:
                n = position
                while True:
                    cand = f"aa2-p-{n:04d}"
                    if cand not in assigned:
                        frag = cand
                        break
                    n += 1
                segment["element"].set('id', frag)
            assigned.add(frag)
            row["fragment"] = frag
        xml_body = ET.tostring(root, encoding="unicode")
        xhtml_bytes = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n' + xml_body).encode("utf-8")
        modified_xhtml[spine_item["path"]] = xhtml_bytes
        smil_name = PurePosixPath(spine_item["path"]).with_suffix('.smil').as_posix()
        smil_records.append({"smil_path": smil_name, "spine_path": spine_item["path"],
                             "mapping": mapping, "existing_ids": existing_ids})
    if not smil_records:
        raise ValueError("No spine document could be synchronized; refusing to substitute a text-only EPUB.")
    if cursor != len(script_entries):
        raise ValueError(
            f"Script entries outside spine coverage ([0..{cursor-1}] mapped of {len(script_entries)}); failing closed.")
    base_dir = _opf_dir(opf_path)
    with zipfile.ZipFile(io.BytesIO(retained_epub_bytes)) as _src:
        taken = {info.filename for info in _src.infolist()}
    old_smil_paths = set()
    for item in package.findall(namespace + 'manifest/' + namespace + 'item'):
        if item.get('media-type') == SMIL_MEDIA_TYPE:
            href = item.get('href') or ''
            parsed = urlsplit(href)
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError("Retained EPUB has an external/fragment SMIL reference.")
            old_smil_paths.add(_resolve_uri_path(parsed.path, base_dir, "SMIL href"))
    for record in smil_records:
        if record["smil_path"] in taken and record["smil_path"] not in old_smil_paths:
            record["smil_path"] = _stepped_path(record["smil_path"], taken)
        taken.add(record["smil_path"])
    # Audio sits beside the OPF; SMIL src is relative to each SMIL directory.
    audio_child = audio_filename if "/" in audio_filename else "audio/" + audio_filename
    audio_path_in_epub = audio_child if base_dir == '' else f"{base_dir}/{audio_child}"
    if audio_path_in_epub in taken:
        audio_path_in_epub = _stepped_path(audio_path_in_epub, taken)
    taken.add(audio_path_in_epub)
    ET.register_namespace('', SMIL_NS)
    ET.register_namespace('epub', OPS_NS)
    for record in smil_records:
        smil_dir = str(PurePosixPath(record["smil_path"]).parent)
        audio_src = (posixpath.relpath(audio_path_in_epub, smil_dir)
                     if smil_dir != "." else audio_path_in_epub)
        smil_text = build_smil_document(record["mapping"], audio_src, sample_rate,
                                        existing_ids=record["existing_ids"],
                                        spine_relpath=record["spine_path"],
                                        smil_relpath=record["smil_path"])
        output_members[record["smil_path"]] = ('<?xml version="1.0" encoding="UTF-8"?>\n' + smil_text).encode("utf-8")
    output_members[audio_path_in_epub] = audio_bytes
    output_members.update(modified_xhtml)
    # OPF updates: manifest items + spine media-overlay + metadata.
    ET.register_namespace('opf', OPF_NS)
    ET.register_namespace('dc', DC_NS)
    manifest_el = package.find(namespace + 'manifest')
    spine_el = package.find(namespace + 'spine')
    if manifest_el is None or spine_el is None:
        raise ValueError("Retained EPUB package lacks manifest or spine.")
    guide_el = package.find(namespace + 'guide')
    if guide_el is not None:
        package.remove(guide_el)
    ncx_id = next((item.get('id') for item in manifest_el.findall(namespace + 'item')
                   if item.get('media-type') == NCX_MEDIA_TYPE), None)
    if ncx_id is not None:
        spine_el.set('toc', ncx_id)
    elif spine_el.get('toc') is not None:
        del spine_el.attrib['toc']
    metadata_el = package.find(namespace + 'metadata')
    if metadata_el is None:
        raise ValueError("Retained EPUB package lacks metadata.")
    # Strip stale overlay state so regeneration never duplicates or orphans.
    for item in list(manifest_el.findall(namespace + 'item')):
        if item.get('media-type') == SMIL_MEDIA_TYPE:
            manifest_el.remove(item)
    for item in manifest_el.findall(namespace + 'item'):
        if item.get('media-overlay') is not None:
            del item.attrib['media-overlay']
        tokens = [t for t in (item.get('properties') or '').split() if t != 'media-overlay']
        if tokens:
            item.set('properties', ' '.join(tokens))
        elif item.get('properties') is not None:
            del item.attrib['properties']
    for meta_el in list(metadata_el.findall(namespace + 'meta')):
        if meta_el.get('property') in ('media:duration', 'dcterms:modified') \
                or meta_el.get('name') == 'media:duration':
            metadata_el.remove(meta_el)
    existing_ids = {item.get('id') for item in manifest_el.findall(namespace + 'item')
                    if item.get('id')}
    # Nav: preserve an existing EPUB 3 nav; otherwise generate from NCX.
    nav_items = [item for item in manifest_el.findall(namespace + 'item')
                 if 'nav' in (item.get('properties') or '').split()]
    generated_toc_count = None
    if len(nav_items) == 0:
        points = get_ncx_points(retained_epub_bytes, manifest, opf_path)
        if not points:
            raise ValueError(
                "Retained EPUB has no navigation document and no NCX to convert; "
                "cannot produce a valid EPUB 3 package.")
        nav_path = (f"{base_dir}/nav.xhtml" if base_dir else "nav.xhtml")
        nav_path = _stepped_path(nav_path, taken)
        taken.add(nav_path)
        nav_dir = str(PurePosixPath(nav_path).parent)
        nav_dir = '' if nav_dir == '.' else nav_dir
        language = meta.get("language") or "en"
        title = (meta.get("title") or "Table of Contents")
        ET.register_namespace('', XHTML_NS)
        ET.register_namespace('epub', OPS_NS)
        html = ET.Element(f"{{{XHTML_NS}}}html", lang=language)
        html.set('{http://www.w3.org/XML/1998/namespace}lang', language)
        head = ET.SubElement(html, f"{{{XHTML_NS}}}head")
        ET.SubElement(head, f"{{{XHTML_NS}}}title").text = f"{title} – Table of Contents" if title else "Table of Contents"
        body = ET.SubElement(html, f"{{{XHTML_NS}}}body")
        nav = ET.SubElement(body, f"{{{XHTML_NS}}}nav",
                            {'{%s}type' % OPS_NS: 'toc'})
        ol = ET.SubElement(nav, f"{{{XHTML_NS}}}ol")

        def _add_points(parent_ol, pts):
            for pt in pts:
                li = ET.SubElement(parent_ol, f"{{{XHTML_NS}}}li")
                a = ET.SubElement(li, f"{{{XHTML_NS}}}a",
                                  href=_nav_links_href(pt["full"], pt["fragment"], nav_dir))
                a.text = pt["label"]
                if pt["children"]:
                    sub = ET.SubElement(li, f"{{{XHTML_NS}}}ol")
                    _add_points(sub, pt["children"])

        _add_points(ol, points)
        nav_xml = ET.tostring(html, encoding="unicode")
        nav_bytes = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html>\n' + nav_xml).encode("utf-8")
        output_members[nav_path] = nav_bytes
        nav_id = _fresh_id(existing_ids, 'aa2-nav')
        existing_ids.add(nav_id)
        ET.SubElement(manifest_el, namespace + 'item',
                      {'id': nav_id, 'href': _relative_to_opf(nav_path, base_dir),
                       'media-type': XHTML_MEDIA_TYPE, 'properties': 'nav'})
        generated_toc_count = sum(1 for _ in html.iter()
                                  if _local(_.tag) == 'a')
    elif len(nav_items) != 1:
        raise ValueError("Retained EPUB must declare exactly one nav item.")
    for record in smil_records:
        href = _relative_to_opf(record["smil_path"], base_dir)
        sid = _fresh_id(existing_ids, 'aa2-smil')
        existing_ids.add(sid)
        ET.SubElement(manifest_el, namespace + 'item',
                      {'id': sid, 'href': href, 'media-type': SMIL_MEDIA_TYPE})
        record["smil_id"] = sid
    audio_href = _relative_to_opf(audio_path_in_epub, base_dir)
    audio_id = _fresh_id(existing_ids, 'aa2-audio')
    existing_ids.add(audio_id)
    ET.SubElement(manifest_el, namespace + 'item',
                  {'id': audio_id, 'href': audio_href, 'media-type': "audio/mpeg"})
    # Metadata: one fresh modified + SMIL-clock durations.
    package.set('version', '3.0')
    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_ms = 0
    for record in smil_records:
        overlay_ms = _overlay_duration_ms(record["mapping"], sample_rate)
        total_ms += overlay_ms
        meta_el = ET.SubElement(metadata_el, namespace + 'meta', property="media:duration")
        meta_el.set('refines', f"#{record['smil_id']}")
        meta_el.text = _format_clock_ms(overlay_ms)
    pub_meta = ET.SubElement(metadata_el, namespace + 'meta', property="media:duration")
    pub_meta.text = _format_clock_ms(total_ms)
    mod_meta = ET.SubElement(metadata_el, namespace + 'meta', property="dcterms:modified")
    mod_meta.text = modified
    manifest_by_href = {}
    for item in manifest_el.findall(namespace + 'item'):
        manifest_by_href[item.get('href')] = item
    for record in smil_records:
        xhtml_href = _relative_to_opf(record["spine_path"], base_dir)
        target = manifest_by_href.get(xhtml_href)
        if target is None:
            if record["spine_path"] in old_smil_paths:
                raise ValueError("Retained EPUB spine document has no manifest item.")
            for itemref in spine_el.findall(namespace + 'itemref'):
                path_item = manifest.get(itemref.get('idref'))
                if path_item and path_item["path"] == record["spine_path"]:
                    for item in manifest_el.findall(namespace + 'item'):
                        if item.get('id') == itemref.get('idref'):
                            target = item
                            break
                    break
        if target is None:
            raise ValueError("Retained EPUB spine document has no manifest item.")
        target.set('media-overlay', record["smil_id"])
    for itemref in spine_el.findall(namespace + 'itemref'):
        if 'media-overlay' in itemref.attrib:
            del itemref.attrib['media-overlay']
    ET.register_namespace('opf', OPF_NS)
    ET.register_namespace('dc', DC_NS)
    new_opf = ET.tostring(package, encoding="utf-8", xml_declaration=True)
    output_members[opf_path] = new_opf
    # Container: ensure the EPUB 3 rootfile media type.
    with zipfile.ZipFile(io.BytesIO(retained_epub_bytes)) as source:
        try:
            container_raw = source.read('META-INF/container.xml')
        except KeyError as error:
            raise ValueError("Retained EPUB lacks META-INF/container.xml.") from error
    try:
        container = ET.fromstring(container_raw)
    except ET.ParseError as error:
        raise ValueError("Retained EPUB container is malformed.") from error
    container_ns = container.tag.split('}')[0] + '}' if '}' in container.tag else ''
    if container_ns != '{%s}' % CONTAINER_NS:
        raise ValueError("Retained EPUB container is not in the OCF container namespace.")
    for rootfile in container.findall(container_ns + 'rootfiles/' + container_ns + 'rootfile'):
        rootfile.set('media-type', EPUB3_MEDIA_TYPE)
    ET.register_namespace('', CONTAINER_NS)
    output_members['META-INF/container.xml'] = ET.tostring(
        container, encoding="utf-8", xml_declaration=True)
    with zipfile.ZipFile(io.BytesIO(retained_epub_bytes)) as source:
        for info in source.infolist():
            if info.filename in output_members or info.filename == opf_path:
                continue
            if info.filename in old_smil_paths:
                continue
            if info.is_dir():
                continue
            output_members[info.filename] = source.read(info.filename)
    for item in manifest.values():
        if item.get('media_type') == XHTML_MEDIA_TYPE \
                and item["path"] in output_members \
                and item["path"] not in modified_xhtml:
            output_members[item["path"]] = _ensure_html5_doctype(output_members[item["path"]])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as out:
        out.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        for name, data in output_members.items():
            if name == 'mimetype':
                continue
            out.writestr(name, data)
    epub_bytes = buffer.getvalue()
    validation = validate_epub_readaloud(epub_bytes)
    toc_entries = generated_toc_count if generated_toc_count is not None else len(meta["toc"])
    report = {"source_sha256": hashlib.sha256(retained_epub_bytes).hexdigest(),
              "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
              "audio_filename": audio_path_in_epub,
              "spine_documents": len(smil_records),
              "smil_files": validation["smil_files"],
              "title": meta["title"], "author": meta["author"], "language": meta["language"],
              "toc_entries": toc_entries,
              "bytes": len(epub_bytes)}
    return epub_bytes, report


# Attribution: packaging/reference-validation ideas adapted from
# ReadAloudMaker (MIT, 2026 ReadAloudMaker contributors, pinned commit
# 5928795e9188222f75ea4776feabc6e6445d6379). Bundled-copy ideas (aeneas
# AGPLv3, syncabook) were not used; SMIL is driven by AA2 frame maps.
