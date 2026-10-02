from review_report import save_review_report
from book_state_transaction import (ensure_book_state, apply_book_input_selection,
                                    apply_book_state_locked)
import asyncio
import contextlib
import copy
import difflib
import hashlib
import json
import io
import logging
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
import xml.etree.ElementTree as ET
from math import ceil
from typing import Dict, List, Literal, Optional
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from speaker_identity import get_validated_alias_graph
from llm_provider import manual_llm_dir
from generation_checkpoint_deltas import load_generation_delta_checkpoint
from generation_checkpoint_shards import (get_generation_checkpoint_artifacts,
                                           remove_generation_shard_checkpoint)
from config_settings import load_app_config
from generate_script import fix_mojibake
from lmstudio_settings import (ensure_ideal_settings, get_active_llm_config,
                               get_planned_ideal_settings)
from narrator_prompt import get_valid_narrator_name, is_narrator_attested
from script_preflight import audit_unicode_text
from source_normalization import normalize_known_source_corruptions
from three_pass_generate import (build_attribute_request,
                                 build_instruct_request,
                                 build_three_pass_request_preflight,
                                 get_three_pass_preflight_capacity,
                                 get_context_rescue_windows,
                                 apply_segment_gate_controls,
                                 default_instruct,
                                 read_source_text,
                                 resolve_three_pass_generation_settings,
                                 three_pass_checkpoint_path,
                                 three_pass_manifest_path)
from text_diff import word_diff
from default_prompts import (load_segment_prompts, load_attribute_prompts,
                             load_instruct_prompts)
from pass_quality import (split_outer_quote_regions, validate_attribution,
                          validate_instruct, validate_segment_quality)
from speaker_identity import stabilize_speaker_identities
from generate_script import LLMGenParams
from utils import file_lock
from alexandria_alignment import validate_epub_archive

from core import (
    _saved_book_meta_path,
    is_task_running,
    get_active_book_id,
    BASE_DIR,
    _compute_eta,
    CHARACTER_ALIASES_PATH,
    CONFIG_PATH,
    DATA_DIR,
    REPORTS_DIR,
    ROOT_DIR,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    UPLOADS_DIR,
    VOICE_CONFIG_PATH,
    _batch_cancel_helper,
    _cancel_task,
    _combine_pass_stats,
    _combine_pass_totals,
    _extract_diff_highlights,
    _extract_failed_sections,
    _extract_new_aliases,
    _extract_review_stats,
    _format_book_summary,
    _format_pass_summary,
    _init_batch_state,
    _init_task_log,
    _insert_llm_summary, _get_deterministic_review_summary,
    _markdown_aliases_lines,
    _markdown_book_pass_lines,
    _markdown_diff_highlights_lines,
    _markdown_heads_up_lines,
    _markdown_stats_table,
    _new_review_totals,
    _pause_task,
    _require_safe_filename,
    _resume_task,
    _run_claimed_background_task,
    _save_upload_limited,
    _stream_subprocess_to_logs,
    _task_log_path,
    _warn_corrupted_json,
    check_global_gpu_lock,
    claim_gpu_task, schedule_claimed_background_task,
    process_state,
    run_process,
)
from review_script import clear_checkpoint
from utils import atomic_json_write, backup_file_with_timestamp, safe_load_json, secure_filename


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()
MAX_SCRIPT_BATCH_ITEMS = 1000
MAX_MANUAL_REPLY_CHARACTERS = 2 * 1024 * 1024
MAX_UPLOAD_STORAGE_BYTES = 8 * 1024**3
MAX_SCRIPT_UPLOAD_BYTES = 512 * 1024**2
MAX_UPLOAD_HASH_CACHE_ENTRIES = 512
_upload_hash_cache = OrderedDict()
_upload_hash_lock = threading.Lock()
_upload_dedupe_lock = threading.Lock()


class ReviewRequest(BaseModel):
    dedupe_speakers: bool = True
    force_review: bool = False

class ContextualReviewRequest(BaseModel):
    window_size: int = 4
    dedupe_speakers: bool = True
    force_review: bool = False

class BatchReviewRequest(BaseModel):
    script_names: List[str] = Field(max_length=MAX_SCRIPT_BATCH_ITEMS)  # library names without .json
    context_window: int = 0            # >0 enables contextual review
    dedupe_speakers: bool = True       # merge same-character aliases, consistent across the batch
    force_review: bool = False
    find_nicknames: bool = True        # run nickname discovery per book first, into the shared series alias file
    bidirectional: bool = False        # after the forward pass, re-scan in reverse so early books get
                                       # discovery seeded with full-series hindsight (requires find_nicknames)



def get_review_force_args(force_review, backward=False):
    """Intentional backward passes always rerun a completed review."""
    return ["--force-review"] if force_review or backward else []


def get_batch_review_highlights(pool: dict) -> dict:
    """Select stable top-five rewrites and first-five speaker changes."""
    return {
        "text": sorted(pool["text"], key=lambda h: h["magnitude"], reverse=True)[:5],
        "speaker": pool["speaker"][:5],
    }


def get_batch_review_task_snapshot(task, bidirectional):
    """Derive display fields from pass results without changing live task state."""
    snapshot = copy.deepcopy(task)
    passes = ("fwd", "bwd") if bidirectional else ("fwd",)
    if not any(f"{field}_{key}" in snapshot
               for key in ("fwd", "bwd") for field in ("stats", "diffs", "failures")):
        return snapshot  # Retain generic-only historical status records.
    for field in ("stats", "diffs", "failures"):
        snapshot.pop(field, None)
    if any(snapshot.get(f"stats_{key}") for key in passes):
        snapshot["stats"] = _combine_pass_stats(
            *(snapshot.get(f"stats_{key}") for key in passes))
    diffs = [snapshot[f"diffs_{key}"] for key in passes if snapshot.get(f"diffs_{key}")]
    if diffs:
        snapshot["diffs"] = {
            field: [item for diff in diffs for item in diff.get(field, [])]
            for field in ("text_rewrites", "speaker_changes")}
    for key in passes:
        failures = snapshot.get(f"failures_{key}")
        if failures and failures.get("sections"):
            snapshot["failures"] = failures
    return snapshot


def _write_batch_review_report(state: dict, names: List[str], bidirectional: bool, discover: bool) -> Optional[str]:
    """Write one plain-language Markdown summary covering an entire batch review run
    (whether it was 1 book or many).

    Returns the path to the written file, or None if it couldn't be written.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(REPORTS_DIR, f"batch_review_{timestamp}.md")

    tasks = [get_batch_review_task_snapshot(task, bidirectional)
             for task in state.get("tasks", [])]
    total_books = len(names)
    if bidirectional:
        # Require both passes' stats and a "done" status; a forward-only
        # result cannot establish completion of the backward pass.
        done = [t for t in tasks if t.get("stats_fwd") and t.get("stats_bwd") and t.get("status") == "done"]
    else:
        done = [t for t in tasks if t.get("stats_fwd") and t.get("status") == "done"]
    incomplete = [t for t in tasks if t.get("status") == "incomplete"]
    failed = [t for t in tasks if t.get("status") == "failed"]
    cancelled = [t for t in tasks if t.get("status") == "cancelled"]

    book_word = "book" if total_books == 1 else "books"
    intro = [
        "# Batch Review Report",
        "",
        f"*Generated {time.strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
        f"The AI reviewer checked **{total_books} {book_word}** for possible mistakes — like "
        "the wrong character speaking a line, awkward wording, or repeated narration — and "
        "recorded its changes.",
    ]

    if bidirectional:
        intro += [
            "",
            "It went through the books twice: once in reading order, then a second "
            '"hindsight" pass from the last book back to the first, so things learned about '
            "characters later in the series could also be applied to earlier books.",
        ]

    if cancelled or state.get("cancel"):
        intro += ["", f"**Note:** this run was stopped early — {len(done)} of {total_books} "
                       f"{book_word} finished before it was cancelled."]
    if failed:
        names_list = ", ".join(f"*{t['name']}*" for t in failed)
        intro += ["", f"**Note:** {len(failed)} {'book' if len(failed) == 1 else 'books'} "
                       f"could not be reviewed (an error occurred): {names_list}"]
    if incomplete:
        names_list = ", ".join(f"*{t['name']}*" for t in incomplete)
        intro += ["", f"**Note:** {len(incomplete)} {'book' if len(incomplete) == 1 else 'books'} "
                       f"{'was' if len(incomplete) == 1 else 'were'} only partially reviewed: "
                       f"{names_list}. See the warnings below for the recorded reason and retry guidance."]

    if bidirectional:
        overall = _combine_pass_totals(state)
    else:
        overall = state["totals_fwd"]

    lines = list(intro)
    lines += ["", "## Overall totals", ""]
    lines += _markdown_stats_table(overall)

    if bidirectional:
        lines += ["", "### First pass (reading order)", ""]
        lines += _markdown_stats_table(state["totals_fwd"])
        lines += ["", "### Second pass (hindsight)", ""]
        lines += _markdown_stats_table(state["totals_bwd"])

    diff_pool = get_batch_review_highlights(state.get("diff_pool", {"text": [], "speaker": []}))
    overall_highlights = {
        "text_rewrites": diff_pool["text"],
        "speaker_changes": diff_pool["speaker"],
    }
    hl_lines = _markdown_diff_highlights_lines(overall_highlights, max_each=5)
    if hl_lines:
        lines += ["", "## Highlights", ""]
        lines += hl_lines

    heads_up = _markdown_heads_up_lines(overall)
    if heads_up:
        lines += ["", "## Things to check", ""]
        lines += heads_up

    if discover:
        aliases_fwd = state.get("aliases_fwd", [])
        aliases_bwd = state.get("aliases_bwd", [])
        lines += ["", "## New character names discovered", ""]
        if not aliases_fwd and not aliases_bwd:
            lines.append("- No new character names were found.")
        elif bidirectional:
            if aliases_fwd:
                lines += _markdown_aliases_lines(aliases_fwd, pass_label=" — first pass")
            if aliases_bwd:
                lines += _markdown_aliases_lines(aliases_bwd, pass_label=" — second/hindsight pass")
        else:
            lines += _markdown_aliases_lines(aliases_fwd)

    # Publish the deterministic summary before the book-by-book breakdown.
    partial = bool(cancelled or failed or incomplete or state.get("cancel") or len(done) < total_books)
    lines = _insert_llm_summary(lines, len(intro), overall, incomplete=partial)

    if total_books > 1:
        lines += ["", "## Book-by-book breakdown", ""]
        for t in tasks:
            name = t.get("name", "?")
            status = t.get("status")
            lines += [f"### {name}", ""]
            if bidirectional:
                stats_fwd = t.get("stats_fwd")
                stats_bwd = t.get("stats_bwd")
                if stats_fwd or stats_bwd:
                    if stats_fwd:
                        lines += ["#### First pass (reading order)", ""]
                        lines += _markdown_book_pass_lines(
                            stats_fwd, t.get("diffs_fwd"), t.get("failures_fwd"), heading="#####")
                    if stats_bwd:
                        if stats_fwd:
                            lines.append("")
                        lines += ["#### Second pass (hindsight)", ""]
                        lines += _markdown_book_pass_lines(
                            stats_bwd, t.get("diffs_bwd"), t.get("failures_bwd"), heading="#####")
                elif status == "cancelled":
                    lines.append("- Not reviewed — the run was cancelled before reaching this book.")
                elif status == "failed":
                    lines.append("- Not reviewed — an error occurred for this book.")
                else:
                    lines.append("- Not reviewed.")
            else:
                stats = t.get("stats_fwd") or t.get("stats")
                if stats:
                    lines += _markdown_book_pass_lines(stats, t.get("diffs"), t.get("failures"))
                elif status == "cancelled":
                    lines.append("- Not reviewed — the run was cancelled before reaching this book.")
                elif status == "failed":
                    lines.append("- Not reviewed — an error occurred for this book.")
                else:
                    lines.append("- Not reviewed.")
            lines.append("")

    try:
        save_review_report(path, "\n".join(lines) + "\n",
                           _get_deterministic_review_summary(overall, partial), partial)
    except OSError:
        return None
    return path


# Characters that are invisible to a reader and fatal to everything that
# matches text. index18 carries 318 zero-width joiners spliced between ellipsis
# characters, and three books carry non-breaking spaces where a space belongs
# (101, 39 and 19 of them). Any code that locates a line in its source - the
# dialogue span map, the lexicon scan, chunk fingerprinting - fails on a
# character it cannot see and cannot explain why.
#
# Deliberately NOT stripped: standalone numeric lines. They look like page
# numbers and are chapter numbers here (1-8 in index18, 001-008 in
# owarimonogatari3), so dropping them would delete the book's structure to
# tidy its whitespace.
_INVISIBLE = str.maketrans({
    "\u200b": "", "\u200c": "", "\u200d": "", "\ufeff": "",   # zero width
    "\u00a0": " ", "\u202f": " ", "\u2009": " ",              # fixed spaces
})


def normalize_extracted_text(text):
    """-> text with invisible characters removed and blank runs collapsed."""
    text = text.translate(_INVISIBLE)
    # Three or more newlines carry no more meaning than two, and uneven runs
    # make paragraph counts depend on the publisher's spacer markup.
    return re.sub(r"\n{3,}", "\n\n", text)


class _HTMLTextExtractor(HTMLParser):
    """Strip HTML tags from EPUB content, preserving block-level structure."""
    BLOCK_TAGS = frozenset({
        'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'li', 'blockquote', 'br', 'hr', 'tr', 'section', 'article',
    })
    SKIP_TAGS = frozenset({'style', 'script', 'title'})

    # A paragraph ends with a BLANK LINE, not a single newline. `br` is a line
    # break inside one paragraph and stays single.
    #
    # WHY IT MATTERS: everything downstream that reasons about structure keys
    # on "\n\n" - the dialogue span map refuses to let a quote cross a
    # paragraph break, and chunking splits on them. With one newline per block
    # this extractor produced 162 paragraph breaks for a book where ebooklib
    # found 3,830, and 25 against 1,445 for another. The words were all there;
    # the shape was gone, and it depended on the publisher: books that leave an
    # empty <p> between paragraphs looked fine, books that do not looked like
    # one enormous paragraph.
    PARAGRAPH_TAGS = frozenset({
        'p', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        'li', 'blockquote', 'hr', 'tr', 'section', 'article',
    })

    def __init__(self, anchor_ids=None):
        super().__init__()
        self.parts = []
        self._pending_newline = 0
        self._skip_depth = 0
        self._anchor_ids = frozenset(anchor_ids or ())

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag in self.BLOCK_TAGS:
            self._pending_newline = max(
                self._pending_newline, 2 if tag in self.PARAGRAPH_TAGS else 1)
        if self._skip_depth == 0 and self._anchor_ids:
            attributes = dict(attrs)
            anchor_id = (attributes.get('id') or attributes.get('xml:id')
                         or (attributes.get('name') if tag == 'a' else None))
            if anchor_id in self._anchor_ids:
                self.parts.append(('anchor', anchor_id))

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        if self._pending_newline and self.parts:
            self.parts.append('\n' * self._pending_newline)
        self._pending_newline = 0
        self.parts.append(data)

    def get_text(self):
        return ''.join(part for part in self.parts if isinstance(part, str))

    def get_text_with_anchor_positions(self):
        """Return extracted text and character offsets for requested anchors."""
        text_parts = []
        positions = {}
        length = 0
        for part in self.parts:
            if isinstance(part, tuple):
                positions.setdefault(part[1], length)
            else:
                text_parts.append(part)
                length += len(part)
        return ''.join(text_parts), positions


def _resolve_epub_href(base_path, href):
    """Resolve an internal EPUB href to a ZIP path and optional fragment."""
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc or (not parsed.path and not parsed.fragment):
        return None, None
    decoded_path = unquote(parsed.path)
    if not decoded_path:
        path = base_path
    elif decoded_path.startswith('/'):
        path = posixpath.normpath(decoded_path.lstrip('/'))
    else:
        path = posixpath.normpath(posixpath.join(
            posixpath.dirname(base_path), decoded_path))
    return path, unquote(parsed.fragment) or None


def _get_epub3_toc_entries(zf, manifest):
    nav_items = [item for item in manifest.values()
                 if 'nav' in item['properties'].split()]
    for nav_item in nav_items:
        try:
            root = ET.fromstring(zf.read(nav_item['path']))
        except (KeyError, ET.ParseError):
            continue
        for nav in root.iter():
            if nav.tag.rsplit('}', 1)[-1] != 'nav':
                continue
            nav_type = (nav.get('{http://www.idpf.org/2007/ops}type')
                        or nav.get('epub:type') or nav.get('type') or '')
            if 'toc' not in nav_type.split():
                continue
            entries = []
            for link in nav.iter():
                if link.tag.rsplit('}', 1)[-1] != 'a' or not link.get('href'):
                    continue
                label = ' '.join(''.join(link.itertext()).split())
                path, fragment = _resolve_epub_href(nav_item['path'], link.get('href'))
                if label and path:
                    entries.append((path, fragment, label))
            if entries:
                return entries
    return []


def _get_epub2_toc_entries(zf, opf, opf_ns, manifest):
    spine = opf.find(f'.//{opf_ns}spine')
    toc_id = spine.get('toc') if spine is not None else None
    ncx_item = manifest.get(toc_id) if toc_id else None
    if ncx_item is None:
        ncx_item = next((item for item in manifest.values()
                         if item['media_type'] == 'application/x-dtbncx+xml'), None)
    if ncx_item is None:
        return []
    try:
        root = ET.fromstring(zf.read(ncx_item['path']))
    except (KeyError, ET.ParseError):
        return []
    entries = []
    for nav_point in root.iter():
        if nav_point.tag.rsplit('}', 1)[-1] != 'navPoint':
            continue
        label = ''
        href = None
        for child in nav_point.iter():
            local_name = child.tag.rsplit('}', 1)[-1]
            if local_name == 'text' and not label:
                label = ' '.join(''.join(child.itertext()).split())
            elif local_name == 'content' and href is None:
                href = child.get('src')
        if label and href:
            path, fragment = _resolve_epub_href(ncx_item['path'], href)
            if path:
                entries.append((path, fragment, label))
    return entries


_TOC_QUOTES_RE = re.compile(r"[‘’“”\"'`]")
_TOC_DASHES_RE = re.compile(r"[‐-―−-]")
# A title is "already present" at 0.75 similarity to some nearby line.
# MEASURED, not guessed. Across the six ReZero EPUBs, 89 TOC entries resolved
# and the four that the exact test called missing were all already in the text,
# differing only in punctuation or prefix; they score 0.759, 0.837, 0.983 and
# 1.000 against the line already there. A genuinely absent title - the
# image-heading case this whole feature exists for - has no such line to match
# and scores far below that against ordinary prose.
_TITLE_PRESENT_RATIO = 0.75


def _normalize_toc_label(value):
    """Fold the differences a book and its own TOC are allowed to have.

    Curly versus straight quotes and the several Unicode dashes are the ones
    that actually bite: a chapter titled `Arc 9, Chapter 47 - "Voice"` in the
    NCX and `Arc 9, Chapter 47 - " "Voice" "` in the page is the same title,
    and comparing them raw inserts a second copy for the narrator to read out.
    """
    folded = unicodedata.normalize("NFKC", value)
    folded = _TOC_QUOTES_RE.sub("", folded)
    folded = _TOC_DASHES_RE.sub("-", folded)
    return " ".join(folded.split()).casefold()


def _toc_title_already_present(label, window):
    """Whether `label` is effectively already written in `window`.

    Exact containment first, because it is cheap and covers the common case.
    Then line by line: a heading occupies its own line, so comparing against
    whole lines keeps the ratio meaningful instead of diluting it across 500
    characters of surrounding prose.
    """
    normalized_label = _normalize_toc_label(label)
    if not normalized_label:
        return True
    if normalized_label in _normalize_toc_label(window):
        return True
    for line in window.splitlines():
        normalized_line = _normalize_toc_label(line)
        if not normalized_line:
            continue
        ratio = difflib.SequenceMatcher(
            None, normalized_label, normalized_line).ratio()
        if ratio >= _TITLE_PRESENT_RATIO:
            return True
    return False


def _insert_epub_toc_titles(text, anchor_positions, toc_targets):
    """Insert missing TOC labels at their resolved positions in one document."""
    insertions = []
    seen = set()
    for fragment, label in toc_targets:
        key = (fragment, label.casefold())
        if key in seen:
            continue
        seen.add(key)
        if fragment is None:
            position = 0
        elif fragment in anchor_positions:
            position = anchor_positions[fragment]
        else:
            continue
        if not _toc_title_already_present(label, text[position:position + 500]):
            insertions.append((position, label))
    # BACK TO FRONT BY POSITION, not by TOC order. Inserting shifts every
    # offset after the insertion point, so each insert must happen at a
    # position no earlier than the ones already applied. `reversed()` alone
    # gives that only when the TOC happens to list anchors in ascending
    # document order - legal EPUBs need not, and a table of contents that
    # names a later anchor first then lands its title short by the length of
    # everything inserted before it, which can be mid-word.
    for position, label in sorted(insertions, key=lambda entry: entry[0],
                                  reverse=True):
        text = text[:position] + label + '\n\n' + text[position:]
    return text


def get_epub_xhtml_text(data: bytes) -> str:
    """Decode XHTML using its Unicode byte order or XML encoding declaration."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif data.startswith(b"<\0"):
        encoding = "utf-16-le"
    elif data.startswith(b"\0<"):
        encoding = "utf-16-be"
    else:
        declaration = re.match(br"\s*<\?xml\b[^>]*\bencoding\s*=\s*['\"]([^'\"]+)['\"]", data[:256])
        encoding = declaration.group(1).decode("ascii") if declaration else "utf-8-sig"
    return data.decode(encoding, errors="replace")


def extract_epub_text(epub_path: str, *, archive_bytes: bytes | None = None) -> str:
    """Extract plain text from an EPUB file, ordered by spine (reading order).

    Parses the EPUB ZIP structure directly using stdlib only:
    META-INF/container.xml -> .opf manifest+spine -> XHTML content files.
    """
    if archive_bytes is None:
        validate_epub_archive(epub_path)
    else:
        validate_epub_archive(epub_path, archive_bytes=archive_bytes)
    source = io.BytesIO(archive_bytes) if archive_bytes is not None else epub_path
    with zipfile.ZipFile(source, 'r') as zf:
        # 1. Find the OPF file path from container.xml
        container_xml = zf.read('META-INF/container.xml')
        container = ET.fromstring(container_xml)
        ns = {'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}
        rootfile_el = container.find('.//c:rootfile', ns)
        if rootfile_el is None:
            raise ValueError("Invalid EPUB: no rootfile found in container.xml")
        opf_path = rootfile_el.get('full-path')

        # 2. Parse the OPF to get manifest (id->href) and spine (reading order)
        opf_xml = zf.read(opf_path)
        try:
            opf = ET.fromstring(opf_xml)
        except ET.ParseError:
            # Two verified Sigil-generated books contain this exact duplicated,
            # malformed metadata attribute. It is unrelated to the manifest or
            # spine; remove only that known fragment and otherwise fail closed.
            repaired_opf = opf_xml.replace(b' refines">"#pub-i"', b'')
            if repaired_opf == opf_xml:
                raise
            opf = ET.fromstring(repaired_opf)
        # Detect OPF namespace (varies between EPUB 2 and 3)
        opf_ns = opf.tag.split('}')[0] + '}' if '}' in opf.tag else ''

        # Build manifest, resolving hrefs relative to the OPF file.
        manifest = {}
        for item in opf.findall(f'.//{opf_ns}item'):
            item_id = item.get('id')
            href = item.get('href')
            if item_id and href:
                path, _ = _resolve_epub_href(opf_path, href)
                if path:
                    manifest[item_id] = {
                        'path': path,
                        'media_type': item.get('media-type', ''),
                        'properties': item.get('properties', ''),
                    }

        # Get spine order
        spine_ids = []
        for itemref in opf.findall(f'.//{opf_ns}itemref'):
            idref = itemref.get('idref')
            if idref:
                spine_ids.append(idref)

        # 3. Read EPUB3 navigation, falling back to EPUB2 NCX.
        toc_entries = _get_epub3_toc_entries(zf, manifest)
        if not toc_entries:
            toc_entries = _get_epub2_toc_entries(zf, opf, opf_ns, manifest)
        toc_by_path = {}
        for path, fragment, label in toc_entries:
            toc_by_path.setdefault(path, []).append((fragment, label))

        # 4. Extract text from each spine item in order.
        chapters = []
        for item_id in spine_ids:
            item = manifest.get(item_id)
            if item is None or 'html' not in item['media_type']:
                continue
            href = item['path']
            try:
                html_bytes = zf.read(href)
            except KeyError:
                continue
            html_content = get_epub_xhtml_text(html_bytes)
            targets = toc_by_path.get(href, [])
            anchor_ids = {fragment for fragment, _ in targets if fragment}
            extractor = _HTMLTextExtractor(anchor_ids)
            extractor.feed(html_content)
            text, anchor_positions = extractor.get_text_with_anchor_positions()
            text = _insert_epub_toc_titles(text, anchor_positions, targets).strip()
            if text:
                chapters.append(text)

    return normalize_extracted_text('\n\n'.join(chapters))


def _claim_unique_path(directory: str, filename: str) -> str:
    """Atomically reserve a unique path in directory for filename, returning the
    path to a newly-created empty file the caller should now write/truncate into.

    A directory scan picks a good starting candidate (avoiding O(n) O_EXCL
    failures when the directory is large), then os.O_EXCL claims it -
    closing the TOCTOU race a scan-then-write approach has under concurrent
    uploads of the same filename. Caps at 1000 attempts to prevent a DoS
    from a maliciously pre-populated directory.
    """
    existing = {e.name for e in os.scandir(directory) if e.is_file()}
    base, ext = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while True:
        if candidate not in existing:
            path = os.path.join(directory, candidate)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.close(fd)
                return path
            except FileExistsError:
                pass  # lost the race - fall through and try the next candidate
        if counter > 1000:
            raise RuntimeError(f"Too many collisions for filename: {filename}")
        counter += 1
        candidate = f"{base}_{counter}{ext}"


def _get_upload_hash(path: str) -> str:
    """Return SHA-256 from a bounded cache of current upload versions."""
    stat = os.stat(path)
    version = (stat.st_size, stat.st_mtime_ns)
    with _upload_hash_lock:
        cached = _upload_hash_cache.get(path)
        if cached is not None and cached[0] == version:
            _upload_hash_cache.move_to_end(path)
            return cached[1]
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    with _upload_hash_lock:
        _upload_hash_cache[path] = (version, value)
        _upload_hash_cache.move_to_end(path)
        while len(_upload_hash_cache) > MAX_UPLOAD_HASH_CACHE_ENTRIES:
            _upload_hash_cache.popitem(last=False)
    return value


def _reuse_duplicate_upload(path: str) -> tuple[str, bool]:
    """Remove path and return an existing identical text upload when present."""
    # Serialize the scan-and-remove decision. Without this, two identical
    # concurrent uploads can each select the other as canonical and remove
    # both files, or one scan can stat a path the other just removed.
    with _upload_dedupe_lock:
        size = os.path.getsize(path)
        digest = _get_upload_hash(path)
        for entry in sorted(os.scandir(UPLOADS_DIR), key=lambda item: item.name):
            try:
                if (not entry.is_file() or entry.path == path or
                        os.path.splitext(entry.name)[1].lower() not in {".txt", ".md"} or
                        entry.stat().st_size != size):
                    continue
                if _get_upload_hash(entry.path) == digest:
                    os.remove(path)
                    return entry.path, True
            except FileNotFoundError:
                continue
    return path, False


def _get_reusable_uploads() -> List[dict]:
    uploads = []
    for entry in sorted(os.scandir(UPLOADS_DIR), key=lambda item: item.name.casefold()):
        if not entry.is_file() or os.path.splitext(entry.name)[1].lower() not in {".txt", ".md"}:
            continue
        stat = entry.stat()
        uploads.append({
            "filename": entry.name, "size": stat.st_size, "modified": stat.st_mtime,
            "sha256": _get_upload_hash(entry.path),
        })
    return uploads


def _select_upload(filename: str) -> str:
    safe_name = _require_safe_filename(filename, "Invalid upload filename")
    path = os.path.join(UPLOADS_DIR, safe_name)
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in {".txt", ".md"}:
        raise HTTPException(status_code=404, detail=f"Reusable upload '{filename}' not found.")
    apply_book_input_selection(DATA_DIR, path, secure_filename(os.path.splitext(safe_name)[0]))
    return path


@router.get("/api/uploads")
async def list_reusable_uploads():
    return await asyncio.to_thread(_get_reusable_uploads)


class ExistingUploadRequest(BaseModel):
    filename: str


@router.post("/api/uploads/select")
async def select_existing_upload(request: ExistingUploadRequest):
    path = await asyncio.to_thread(_select_upload, request.filename)
    return {"status": "selected", "stored_filename": os.path.basename(path), "path": path}










def get_upload_storage_bytes():
    """Count regular files retained in the flat source-upload directory."""
    with os.scandir(UPLOADS_DIR) as entries:
        return sum(entry.stat(follow_symlinks=False).st_size for entry in entries
                   if entry.is_file(follow_symlinks=False))


async def await_upload_operation(operation):
    """Finish admitted I/O before reporting cancellation to its owner."""
    task = asyncio.create_task(operation)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = error
        except Exception:
            break
    try:
        result = task.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled
        raise
    return result, cancelled


@router.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    safe_name = _require_safe_filename(file.filename or "", "Invalid or empty filename")
    if os.path.splitext(safe_name)[1].lower() not in {".txt", ".md", ".epub"}:
        raise HTTPException(status_code=400, detail="Supported source formats are TXT, Markdown and EPUB.")
    await asyncio.to_thread(os.makedirs, UPLOADS_DIR, exist_ok=True)
    # The lease is outside uploads so it is neither listed nor quota-counted.
    # Acquire in a worker; waiting for another upload must not block the loop.
    storage_lock = file_lock(UPLOADS_DIR + ".quota")
    try:
        _, cancelled = await await_upload_operation(asyncio.to_thread(storage_lock.__enter__))
    except TimeoutError as error:
        raise HTTPException(status_code=503, detail="Upload storage is busy; retry shortly.") from error
    if cancelled is not None:
        storage_lock.__exit__(None, None, None)
        raise cancelled
    owned_paths = set()
    try:
        available = MAX_UPLOAD_STORAGE_BYTES - await asyncio.to_thread(get_upload_storage_bytes)
        if available <= 0:
            raise HTTPException(status_code=413, detail="Source upload storage limit reached.")
        file_path, cancelled = await await_upload_operation(
            asyncio.to_thread(_claim_unique_path, UPLOADS_DIR, safe_name))
        owned_paths.add(file_path)
        if cancelled is not None:
            raise cancelled
        _, cancelled = await await_upload_operation(
            _save_upload_limited(file, file_path, min(MAX_SCRIPT_UPLOAD_BYTES, available)))
        if cancelled is not None:
            raise cancelled
        if file_path.lower().endswith('.epub'):
            try:
                text, cancelled = await await_upload_operation(asyncio.to_thread(extract_epub_text, file_path))
                if cancelled is not None:
                    raise cancelled
            except Exception as error:
                raise HTTPException(status_code=400, detail=f"Failed to process EPUB: {error}") from error
            if not text.strip():
                raise HTTPException(status_code=400, detail="No readable text content found in EPUB.")
            text_bytes = text.encode('utf-8')
            available = MAX_UPLOAD_STORAGE_BYTES - await asyncio.to_thread(get_upload_storage_bytes)
            if len(text_bytes) > min(MAX_SCRIPT_UPLOAD_BYTES, available):
                raise HTTPException(status_code=413, detail="Extracted EPUB text exceeds source upload storage limits.")
            txt_name = os.path.basename(file_path).rsplit('.', 1)[0] + '.txt'
            txt_path, cancelled = await await_upload_operation(
                asyncio.to_thread(_claim_unique_path, UPLOADS_DIR, txt_name))
            owned_paths.add(txt_path)
            if cancelled is not None:
                raise cancelled
            _, cancelled = await await_upload_operation(asyncio.to_thread(Path(txt_path).write_bytes, text_bytes))
            if cancelled is not None:
                raise cancelled
            os.remove(file_path)
            file_path = txt_path
        result, cancelled = await await_upload_operation(asyncio.to_thread(_reuse_duplicate_upload, file_path))
        file_path, reused = result
        if cancelled is not None:
            raise cancelled
        apply_book_input_selection(DATA_DIR, file_path,
                                  secure_filename(os.path.splitext(os.path.basename(file_path))[0]))
        return {"filename": file.filename, "stored_filename": os.path.basename(file_path),
                "path": file_path, "reused": reused}
    except BaseException:
        for path in owned_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        raise
    finally:
        storage_lock.__exit__(None, None, None)

class GenerateScriptRequest(BaseModel):
    strip_front_matter: bool = True
    first_person_narrator: Optional[str] = None
    # Discard the saved progress for this book and begin at chunk 1. Without
    # it a run on the same text with the same settings resumes where it
    # stopped - by design (a crash or Cancel loses nothing), but there was no
    # way to ask for a fresh run short of changing a setting (#597).
    start_over: bool = False


def get_active_reasoning_effort() -> Optional[str]:
    """The active LLM profile's reasoning_effort, or None."""
    return get_active_llm_config(load_app_config(CONFIG_PATH)).get("reasoning_effort") or None


def build_generate_script_command(input_file: str, output_path: Optional[str] = None,
                                  strip_front_matter: bool = True,
                                  first_person_narrator: Optional[str] = None,
                                  reasoning_effort: Optional[str] = None) -> List[str]:
    """Build the one production command used by single and batch generation.

    `reasoning_effort` is passed explicitly so three_pass_generate records it
    as `thinking_mode` in the run manifest; the request itself would carry it
    anyway through the profile's request body."""
    command = [sys.executable, "-u", os.path.join(BASE_DIR, "three_pass_generate.py"),
               input_file, "--pass2-on-exhaustion", "keep"]
    if output_path is not None:
        command.extend(["--output", output_path])
    if reasoning_effort:
        command.extend(["--reasoning-effort", reasoning_effort])
    if not strip_front_matter:
        command.append("--no-strip-front-matter")
    narrator = get_valid_narrator_name(first_person_narrator)
    if narrator:
        command.extend(["--first-person-narrator", narrator])
    return command


def get_script_recovery_manifest() -> Optional[dict]:
    """Return the active run's resumable manifest, if it is incomplete.

    A three-pass run checkpoints accepted work after each unit.  Restarting the
    same command resumes that checkpoint, but only a failed or diagnostic run
    is a recovery candidate.  A completed manifest must never surface a
    misleading Retry action.
    """
    state = safe_load_json(os.path.join(DATA_DIR, "state.json"), {})
    if (not isinstance(state, dict)
            or state.get("script_generation_input_file") != state.get("input_file_path")):
        return None
    manifest = safe_load_json(three_pass_manifest_path(SCRIPT_PATH), {})
    if not isinstance(manifest, dict) or manifest.get("status") not in {
            "failed", "incomplete"}:
        return None
    return manifest


def ensure_script_recovery_manifest():
    """Recover interrupted book publication before reading recovery metadata."""
    with ensure_book_state(DATA_DIR):
        return get_script_recovery_manifest()


def completed_script_prefix(checkpoint):
    """The entries a running three-pass has fully finished (all three passes),
    in order, stopping at the first one that has not - the snapshot #600
    asks for. Pass 3 fills `annotated` window by window in source order, so
    the finished part is a prefix; narration filled deterministically ahead
    of its window is skipped past rather than counted, so the snapshot never
    contains a line whose neighbours are still unwritten."""
    annotated = checkpoint.get("annotated") or []
    named = checkpoint.get("named") or []
    prefix = []
    for index, entry in enumerate(annotated):
        if not isinstance(entry, dict) or index >= len(named) or named[index] is None:
            break
        prefix.append({key: value for key, value in entry.items()})
    return prefix


class SnapshotRequest(BaseModel):
    name: str


def get_current_generation_checkpoint(*, locked=False):
    """Read the committed checkpoint prefix, including durable indexed changes."""
    try:
        return load_generation_delta_checkpoint(three_pass_checkpoint_path(SCRIPT_PATH),
                                                locked=locked)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=409,
                            detail=f"Cannot read generation checkpoint: {exc}") from exc


@router.post("/api/generate_script/snapshot")
async def snapshot_script(request: SnapshotRequest):
    """Save the finished part of the running generation to the library
    without stopping it (#600). Reads the run's checkpoint - the pipeline
    writes it atomically after every unit - and stores the completed prefix
    as a saved script, with the current voice config as its companion, the
    same shape /api/scripts/save produces. The run keeps going; when it
    finishes it becomes the active script as usual, and the snapshot stays
    in the library."""
    if not process_state["script"].get("running"):
        raise HTTPException(status_code=409, detail="No script generation is running; use Save script instead.")
    checkpoint = get_current_generation_checkpoint()
    if not isinstance(checkpoint, dict):
        raise HTTPException(status_code=409, detail="The run has not written a checkpoint yet.")
    entries = completed_script_prefix(checkpoint)
    if not entries:
        raise HTTPException(status_code=409, detail="Nothing is fully finished yet - Step 3 has not completed a window.")
    safe_name = _require_safe_filename(request.name, "Invalid snapshot name.")
    dest = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
    os.makedirs(SCRIPTS_DIR, exist_ok=True)
    companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
    metadata = _saved_book_meta_path(safe_name)
    with file_lock(dest):
        if any(os.path.exists(path) for path in (dest, companion, metadata)):
            raise HTTPException(status_code=409,
                                detail="A saved script already uses that snapshot name.")
        atomic_json_write(entries, dest)
        if os.path.exists(VOICE_CONFIG_PATH):
            shutil.copy2(VOICE_CONFIG_PATH, companion)
        atomic_json_write({"book_id": get_active_book_id() or safe_name,
                           "snapshot": {"entries": len(entries),
                                        "segmented": len(checkpoint.get("segmented") or []),
                                        "chunks_done": checkpoint.get("chunks_done"),
                                        "stage": checkpoint.get("stage"), "taken": time.time()}},
                          metadata)
    return {"status": "saved", "name": safe_name, "entries": len(entries),
            "segmented": len(checkpoint.get("segmented") or []),
            "chunks_done": checkpoint.get("chunks_done")}


def discard_script_progress(*, locked=False):
    """Remove the single-book run's checkpoint and manifest so the next run
    starts at chunk 1. -> the paths that existed."""
    removed = []
    checkpoint_path = three_pass_checkpoint_path(SCRIPT_PATH)
    if os.path.exists(checkpoint_path):
        removed.append(checkpoint_path)
    remove_generation_shard_checkpoint(checkpoint_path, locked=locked)
    for path in (three_pass_manifest_path(SCRIPT_PATH),):
        try:
            os.remove(path)
            removed.append(path)
        except FileNotFoundError:
            pass
    return removed


def start_script_generation(background_tasks: BackgroundTasks, input_file: str,
                            request: Optional[GenerateScriptRequest],
                            require_recovery: bool = False):
    """Queue the one production generation command, optionally from a checkpoint."""
    if require_recovery and ensure_script_recovery_manifest() is None:
        raise HTTPException(
            status_code=409,
            detail="No failed or incomplete three-pass generation is available to resume.")
    check_global_gpu_lock("script")
    try:
        options = {
            "strip_front_matter": request is None or request.strip_front_matter,
            "first_person_narrator": get_valid_narrator_name(
                request.first_person_narrator if request is not None else None),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if require_recovery and options["first_person_narrator"]:
        try:
            _read_and_validate_batch_script_source({
                "filename": os.path.basename(input_file), "input_path": input_file,
                "first_person_narrator": options["first_person_narrator"]})
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not require_recovery:
        try:
            refusal = three_pass_refusal([{
                "filename": os.path.basename(input_file), "input_path": input_file,
                "first_person_narrator": options["first_person_narrator"]}])
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if refusal:
            raise HTTPException(status_code=400, detail=refusal)
    try:
        command = build_generate_script_command(
            input_file,
            strip_front_matter=options["strip_front_matter"],
            first_person_narrator=options["first_person_narrator"],
            reasoning_effort=get_active_reasoning_effort(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with ensure_book_state(DATA_DIR), file_lock(three_pass_checkpoint_path(SCRIPT_PATH)):
        if require_recovery and get_script_recovery_manifest() is None:
            raise HTTPException(status_code=409, detail="Recovery checkpoint changed before start.")
        if request is not None and request.start_over and not require_recovery:
            discard_script_progress(locked=True)
        state_path = os.path.join(DATA_DIR, "state.json")
        state = safe_load_json(state_path, {})
        if isinstance(state, dict):
            # Retry reads these instead of the current form. Otherwise a
            # changed checkbox would invalidate the resume fingerprint.
            state["script_generation_options"] = options
            state["script_generation_input_file"] = input_file
            atomic_json_write(state, state_path)
        schedule_claimed_background_task(background_tasks, "script", run_process, command, "script")
    return {"status": "resuming" if require_recovery else "started"}


@router.post("/api/generate_script")
async def generate_script(background_tasks: BackgroundTasks,
                           request: Optional[GenerateScriptRequest] = None):
    # Get input file from state.json
    state_path = os.path.join(DATA_DIR, "state.json")
    if not os.path.exists(state_path):
        raise HTTPException(status_code=400, detail="No input file selected")

    state = safe_load_json(state_path, default={})
    input_file = state.get("input_file_path")

    if not input_file:
         raise HTTPException(status_code=400, detail="No input file found in state")

    return await asyncio.to_thread(start_script_generation, background_tasks, input_file, request)


def ensure_script_recovery_response(include_detail=False):
    """Read metadata and optional failed-unit detail under one book guard."""
    with ensure_book_state(DATA_DIR):
        manifest = get_script_recovery_manifest()
        if manifest is None:
            return {"recoverable": False}
        failures = manifest.get("diagnostic_failures") or []
        response = {
            "recoverable": True,
            "status": manifest["status"],
            "failed_pass": manifest.get("failed_pass"),
            "failed_chunk": manifest.get("failed_chunk"),
            "failure_count": len(failures),
        }
        if include_detail:
            checkpoint = _load_failed_checkpoint()
            response["detail"] = ({"recoverable": True, **build_recovery_detail(checkpoint)}
                                  if checkpoint is not None else None)
        return response


@router.get("/api/generate_script/recovery")
async def generate_script_recovery(include_detail: bool = False):
    """Return metadata by default; opt in to the validated failed-unit detail."""
    return await asyncio.to_thread(ensure_script_recovery_response, include_detail)


def _load_failed_checkpoint(*, locked=False):
    """The checkpoint of a pass-1 fail-fast, or None. The `failed` block is
    written only on that path (three_pass_generate._save_three_pass_checkpoint)."""
    if get_script_recovery_manifest() is None:
        return None
    checkpoint = get_current_generation_checkpoint(locked=locked)
    if not isinstance(checkpoint, dict) or checkpoint.get("stage") not in (
            "segment_failed", "attribute_failed", "instruct_failed"):
        return None
    failed = checkpoint.get("failed")
    if not isinstance(failed, dict) or not (failed.get("source") or failed.get("entries")):
        return None
    return checkpoint


def ensure_failed_checkpoint():
    """Read a failed unit only after recovering its complete artifact pair."""
    with ensure_book_state(DATA_DIR):
        return _load_failed_checkpoint()


def get_script_recovery_token(checkpoint):
    """Bind a decision to the checkpoint that its validation actually read."""
    return hashlib.sha256(json.dumps(checkpoint, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def build_recovery_detail(checkpoint):
    """What the recovery panel shows for the failed chunk (issue #522 s23):
    where it failed, every attempt with its HTTP status and category, the
    chunk's source, and the exact pass-1 prompt so it can be copied."""
    failed = checkpoint["failed"]
    attempts = failed.get("attempts") or []
    last = attempts[-1] if attempts else {}
    llm = get_active_llm_config(load_app_config(CONFIG_PATH))
    if failed.get("pass") in ("attribute", "instruct"):
        entries = failed.get("entries") or []
        if failed["pass"] == "attribute":
            sys_prompt, user_prompt = build_attribute_request(
                entries, LLMGenParams(), failed.get("roster") or [])
            label = lambda e: e.get("type")
        else:
            sys_prompt, user_prompt = build_instruct_request(entries, LLMGenParams())
            label = lambda e: e.get("speaker")
        unit = {"failed_chunk": None, "chunks_total": None,
                "batch_indices": failed.get("indices") or [],
                "batch_entries": entries, "roster": failed.get("roster") or [],
                "source": "\n".join(f'[{i}] {label(e)}: {e.get("text")}'
                                    for i, e in enumerate(entries))}
    else:
        sys_prompt, usr_template = load_segment_prompts()
        gate_params = LLMGenParams(
            quoted_must_be_spoken=failed.get("quoted_must_be_spoken", True),
            unquoted_must_be_narrator=failed.get("unquoted_must_be_narrator", True))
        sys_prompt = apply_segment_gate_controls(sys_prompt, gate_params)
        user_prompt = usr_template.format(chunk=failed["source"])
        unit = {"failed_chunk": failed.get("chunk"), "chunks_total": failed.get("chunks_total"),
                "source": failed["source"]}
    return {
        "stage": checkpoint.get("stage"),
        "failed_pass": failed.get("pass"),
        **unit,
        "chunks_done": checkpoint.get("chunks_done"),
        "failure_codes": failed.get("failure_codes") or [],
        "reason": failed.get("reason"),
        "last_error": {"category": last.get("error_category"),
                       "http_status": last.get("http_status"),
                       "outcome": last.get("outcome"),
                       "error": last.get("error")},
        "attempts": [{k: a.get(k) for k in (
            "attempt", "outcome", "error_category", "http_status", "error",
            "failure_codes", "finish_reason", "completion_tokens",
            "effective_max_tokens", "next_retry_seconds", "elapsed_seconds")}
            for a in attempts],
        "prompt": {"system": sys_prompt, "user": user_prompt},
        "retry_profile": {
            "api_retry_limit": llm.get("api_retry_limit"),
            "retry_initial_delay_seconds": llm.get("retry_initial_delay_seconds", 1),
            "retry_multiplier": llm.get("retry_multiplier", 2),
            "retry_max_delay_seconds": llm.get("retry_max_delay_seconds", 30),
            "retry_jitter": llm.get("retry_jitter", 0.2),
            "on_api_exhaustion": llm.get("on_api_exhaustion", "fail")},
    }


def apply_manual_recovery(entries, resolution, expected_checkpoint_token=None):
    """Accept hand-supplied output for the failed unit through the SAME gate
    the pass uses - pass 1: a [{type, text}] segmentation of the chunk;
    pass 2: [{n, speaker}] for the batch - and advance the checkpoint past it
    so Retry resumes at the next unit. Returns the gate report on refusal
    (caller -> 422)."""
    with ensure_book_state(DATA_DIR), file_lock(three_pass_checkpoint_path(SCRIPT_PATH)):
        if is_task_running("script", process_state):
            raise HTTPException(status_code=409, detail="Script generation is running.")
        checkpoint = _load_failed_checkpoint(locked=True)
        if checkpoint is None:
            raise HTTPException(status_code=409,
                                detail="No failed generation unit is waiting for recovery.")
        if (expected_checkpoint_token is not None
                and get_script_recovery_token(checkpoint) != expected_checkpoint_token):
            raise HTTPException(status_code=409,
                                detail="The failed generation unit changed; reload recovery before applying a decision.")
        failed = checkpoint["failed"]
        if failed.get("pass") == "attribute":
            frozen = failed.get("entries") or []
            report = validate_attribution(frozen, entries, None)
            if not report.get("passed"):
                return {"accepted": False, "report": report}
            by_n = {int(e["n"]): e for e in entries}
            bound = [{k: v for k, v in f.items() if k != "type"} | {"speaker": by_n[i]["speaker"]}
                     for i, f in enumerate(frozen)]
            bound = stabilize_speaker_identities(bound, established_speakers=failed.get("roster") or [])["entries"]
            named = list(checkpoint.get("named") or [])
            for index, entry in zip(failed.get("indices") or [], bound):
                while len(named) <= index:
                    named.append(None)
                named[index] = entry
            checkpoint["named"] = named
            checkpoint["stage"] = "attribute"
            unit = {"batch_indices": failed.get("indices") or []}
        elif failed.get("pass") == "instruct":
            frozen = failed.get("entries") or []
            report = validate_instruct(frozen, entries)
            if not report.get("passed"):
                return {"accepted": False, "report": report}
            by_n = {int(e["n"]): e for e in entries}
            annotated = list(checkpoint.get("annotated") or [])
            named = checkpoint.get("named") or []
            for position, index in enumerate(failed.get("indices") or []):
                while len(annotated) <= index:
                    annotated.append(None)
                base = named[index] if index < len(named) and isinstance(named[index], dict) else frozen[position]
                annotated[index] = {**base, "instruct": by_n[position]["instruct"]}
            checkpoint["annotated"] = annotated
            checkpoint["stage"] = "instruct"
            unit = {"batch_indices": failed.get("indices") or []}
        else:
            report = validate_segment_quality(
                failed["source"], entries,
                quoted_must_be_spoken=failed.get("quoted_must_be_spoken", True),
                unquoted_must_be_narrator=failed.get("unquoted_must_be_narrator", True))
            if not report.get("passed"):
                return {"accepted": False, "report": report}
            checkpoint["segmented"] = list(checkpoint.get("segmented") or []) + [
                {"type": e["type"], "text": e["text"]} for e in entries]
            checkpoint["chunks_done"] = int(failed["chunk"])
            resolutions = list(checkpoint.get("resolutions") or [])
            if len(resolutions) >= failed["chunk"]:
                resolutions[failed["chunk"] - 1] = resolution
            else:
                resolutions.append(resolution)
            checkpoint["resolutions"] = resolutions
            checkpoint["stage"] = "segment"
            unit = {"chunk": failed["chunk"]}
        checkpoint["failed"] = None
        manifest_path = three_pass_manifest_path(SCRIPT_PATH)
        manifest = get_script_recovery_manifest()
        if manifest is None:
            raise HTTPException(status_code=409, detail="Recovery manifest changed; reload recovery.")
        manifest["status"] = "incomplete"
        manifest.pop("failed_chunk", None)
        manifest["recovered_units"] = (manifest.get("recovered_units") or []) + [
            {**unit, "pass": failed.get("pass"), "resolution": resolution}]
        replacements = {
            os.path.relpath(path, DATA_DIR): json.dumps(value, ensure_ascii=False,
                                                       indent=2, allow_nan=False).encode("utf-8")
            for path, value in ((three_pass_checkpoint_path(SCRIPT_PATH), checkpoint),
                                (manifest_path, manifest))}
        removals = [os.path.relpath(path, DATA_DIR) for path in
                    get_generation_checkpoint_artifacts(three_pass_checkpoint_path(SCRIPT_PATH))[1:]]
        apply_book_state_locked(DATA_DIR, replacements, removals)
    return {"accepted": True, **unit, "chunks_done": checkpoint["chunks_done"],
            "resolution": resolution}


class InjectSegmentationRequest(BaseModel):
    # pass 1: `chunk` + entries [{type, text}]; pass 2: entries [{n, speaker}]
    chunk: Optional[int] = None
    entries: List[Dict]


class SkipChunkRequest(BaseModel):
    chunk: Optional[int] = None


def _require_failed_unit(chunk):
    checkpoint = ensure_failed_checkpoint()
    if checkpoint is None:
        raise HTTPException(status_code=409,
                            detail="No failed generation unit is waiting for recovery.")
    failed = checkpoint["failed"]
    if failed.get("pass") == "segment" and int(failed["chunk"]) != int(chunk or -1):
        raise HTTPException(status_code=409,
                            detail=f"The failed chunk is {failed['chunk']}, not {chunk}.")
    return checkpoint


@router.get("/api/generate_script/recovery/detail")
async def generate_script_recovery_detail():
    """The failed chunk in full: attempts, source, prompt, retry profile."""
    checkpoint = await asyncio.to_thread(ensure_failed_checkpoint)
    if checkpoint is None:
        return {"recoverable": False}
    return {"recoverable": True, **build_recovery_detail(checkpoint)}


@router.post("/api/generate_script/inject")
async def generate_script_inject(request: InjectSegmentationRequest):
    """Manual output injection (#522 s4.4): a pasted [{type, text}]
    segmentation for a failed pass-1 chunk, or [{n, speaker}] for a failed
    pass-2 batch, validated by the pass's own gate."""
    checkpoint = await asyncio.to_thread(_require_failed_unit, request.chunk)
    entries = []
    failed_pass = checkpoint["failed"].get("pass")
    if failed_pass in ("attribute", "instruct"):
        field = "speaker" if failed_pass == "attribute" else "instruct"
        for i, e in enumerate(request.entries):
            value = (str(e.get(field) or "")).strip()
            if not isinstance(e.get("n"), int) or not value:
                raise HTTPException(status_code=422,
                                    detail=f"Entry {i + 1}: needs an integer n and a non-empty {field}.")
            entries.append({"n": e["n"], field: value})
    else:
        for i, e in enumerate(request.entries):
            kind = (str(e.get("type") or "")).strip().upper()
            text = (str(e.get("text") or "")).strip()
            if kind not in ("NARRATOR", "SPOKEN") or not text:
                raise HTTPException(status_code=422,
                                    detail=f"Entry {i + 1}: type must be NARRATOR or SPOKEN and text non-empty.")
            entries.append({"type": kind, "text": text})
    if not entries:
        raise HTTPException(status_code=422, detail="No entries supplied.")
    result = await asyncio.to_thread(apply_manual_recovery, entries, "manual",
                                     get_script_recovery_token(checkpoint))
    if not result["accepted"]:
        raise HTTPException(status_code=422, detail={
            "message": "The segmentation does not pass the fidelity gate.",
            "findings": result["report"].get("findings", []),
            "metrics": result["report"].get("metrics", {})})
    return result


@router.post("/api/generate_script/skip")
async def generate_script_skip(request: SkipChunkRequest):
    """'Skip' that loses nothing. Pass 1: the chunk goes in split only at its
    outer quote marks (the deterministic splitter pass 1's gate itself uses),
    so every word stays and quoted lines are still SPOKEN. Pass 2: the batch's
    spoken lines are labelled UNKNOWN - the fallback the run refused to apply
    on its own because the LLM was unavailable, now applied by the user."""
    checkpoint = await asyncio.to_thread(_require_failed_unit, request.chunk)
    failed = checkpoint["failed"]
    if failed.get("pass") == "attribute":
        entries = [{"n": i, "speaker": "NARRATOR" if e.get("type") == "NARRATOR" else "UNKNOWN"}
                   for i, e in enumerate(failed.get("entries") or [])]
    elif failed.get("pass") == "instruct":
        # The pass's own fallback: the neutral default direction per entry.
        entries = [{"n": i, "instruct": default_instruct(e)}
                   for i, e in enumerate(failed.get("entries") or [])]
    else:
        entries = [{"type": r["type"], "text": r["text"].strip()}
                   for r in split_outer_quote_regions(failed["source"])
                   if r.get("text", "").strip()]
    result = await asyncio.to_thread(apply_manual_recovery, entries, "narrated_as_is",
                                     get_script_recovery_token(checkpoint))
    if not result["accepted"]:
        raise HTTPException(status_code=422, detail={
            "message": "Even the deterministic split does not pass the fidelity gate; "
                       "paste a segmentation by hand.",
            "findings": result["report"].get("findings", [])})
    return result


@router.post("/api/generate_script/retry")
async def retry_generate_script(background_tasks: BackgroundTasks,
                                request: Optional[GenerateScriptRequest] = None):
    """Resume the current failed three-pass run from its durable checkpoint."""
    state = safe_load_json(os.path.join(DATA_DIR, "state.json"), {})
    input_file = state.get("input_file_path") if isinstance(state, dict) else None
    if not input_file:
        raise HTTPException(status_code=400, detail="No input file found for recovery")
    stored_options = state.get("script_generation_options") if isinstance(state, dict) else None
    if isinstance(stored_options, dict):
        try:
            request = GenerateScriptRequest(**stored_options)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=409,
                                detail="Saved recovery settings are invalid; start a new run.") from exc
    return await asyncio.to_thread(start_script_generation, background_tasks, input_file, request,
                                   require_recovery=True)

@router.post("/api/generate_script/cancel")
async def generate_script_cancel():
    return _cancel_task("script", "No script generation is currently running.", "Script generation process already exited.")



@router.post("/api/generate_script/pause")
async def generate_script_pause():
    return _pause_task("script", "No script generation is currently running.",
                        "Script generation is starting up, retry in a moment.",
                        "Script generation")

@router.post("/api/generate_script/resume")
async def generate_script_resume():
    return _resume_task("script", "No script generation is currently running.",
                         "Script generation")


@router.post("/api/review_script")
async def review_script(background_tasks: BackgroundTasks, request: Optional[ReviewRequest] = None):
    """Review the current annotated script. Accepts empty POST or JSON body."""
    if request is None:
        request = ReviewRequest()  # Use defaults
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No annotated script found. Generate a script first.")

    check_global_gpu_lock("review")

    cmd = [sys.executable, "-u", "review_script.py"]
    cmd += get_review_force_args(request.force_review)
    if request.dedupe_speakers:
        cmd += ["--dedupe-speakers", "--remap-voice-config", VOICE_CONFIG_PATH,
                "--alias-registry", CHARACTER_ALIASES_PATH]
    schedule_claimed_background_task(background_tasks, "review", run_process, cmd, "review")
    return {"status": "started", "dedupe_speakers": request.dedupe_speakers}

@router.post("/api/review_script_contextual")
async def review_script_contextual(request: ContextualReviewRequest, background_tasks: BackgroundTasks):
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No annotated script found. Generate a script first.")

    check_global_gpu_lock("review")

    window_size = max(1, min(int(request.window_size or 4), 12))
    total_entries = 0
    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            total_entries = len(json.load(f))
    except (json.JSONDecodeError, ValueError, OSError) as e:
        _warn_corrupted_json("script", SCRIPT_PATH, "estimated_calls will read 0", e)
        total_entries = 0

    review_batch_size = 25
    try:
        cfg = load_app_config(CONFIG_PATH)
        generation = cfg.get("generation") or {}
        review_batch_size = max(1, int(generation.get("review_batch_size", 25)))
    except (ValueError, TypeError) as e:
        _warn_corrupted_json("config", CONFIG_PATH, "using default review_batch_size", e)

    estimated_calls = ceil(total_entries / review_batch_size) if total_entries else 0
    cmd = [sys.executable, "-u", "review_script.py", "--context-window", str(window_size)]
    cmd += get_review_force_args(request.force_review)
    if request.dedupe_speakers:
        cmd += ["--dedupe-speakers", "--remap-voice-config", VOICE_CONFIG_PATH,
                "--alias-registry", CHARACTER_ALIASES_PATH]
    schedule_claimed_background_task(background_tasks, "review",
        run_process,
        cmd,
        "review"
    )
    return {
        "status": "started",
        "mode": "contextual",
        "window_size": window_size,
        "batch_size": review_batch_size,
        "total_entries": total_entries,
        "estimated_calls": estimated_calls,
        "dedupe_speakers": request.dedupe_speakers,
    }


@router.post("/api/review_script/cancel")
async def review_script_cancel():
    return _cancel_task("review", "No script review is currently running.", "Script review process already exited.")


@router.post("/api/review_script/pause")
async def review_script_pause():
    return _pause_task("review", "No script review is currently running.",
                        "Script review is starting up, retry in a moment.",
                        "Script review")


@router.post("/api/review_script/resume")
async def review_script_resume():
    return _resume_task("review", "No script review is currently running.",
                         "Script review")


@router.post("/api/find_nicknames")
async def find_nicknames_endpoint(background_tasks: BackgroundTasks):
    """Scan the working script for character nicknames/aliases and write character_aliases.json."""
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=400, detail="No annotated script found. Generate a script first.")
    # nicknames runs the LLM, so it must claim the GPU lock (this also guards
    # against a duplicate start, replacing the old running-flag check).
    cmd = [sys.executable, "-u", "find_nicknames.py",
           "--aliases-file", CHARACTER_ALIASES_PATH, "--append"]
    schedule_claimed_background_task(background_tasks, "nicknames", run_process, cmd, "nicknames")
    return {"status": "started"}


@router.post("/api/find_nicknames/cancel")
async def find_nicknames_cancel():
    return _cancel_task("nicknames", "No nickname discovery is currently running.", "Nickname discovery already exited.")


@router.post("/api/find_nicknames/pause")
async def find_nicknames_pause():
    return _pause_task("nicknames", "No nickname discovery is currently running.",
                        "Nickname discovery is starting up, retry in a moment.",
                        "Nickname discovery")


@router.post("/api/find_nicknames/resume")
async def find_nicknames_resume():
    return _resume_task("nicknames", "No nickname discovery is currently running.",
                         "Nickname discovery")


@router.get("/api/character_aliases")
async def get_character_aliases():
    """Return the current alias map { alias: canonical }."""
    aliases = safe_load_json(CHARACTER_ALIASES_PATH, default={})
    if not isinstance(aliases, dict):
        return {}
    # Hide identity rows (NAME -> NAME) — they're inert and only clutter the editor.
    # Exact-match only, so a legitimate case-fix alias (kenji -> KENJI) stays visible.
    return {k: v for k, v in aliases.items()
            if isinstance(k, str) and isinstance(v, str) and k.strip() != v.strip()}


@router.post("/api/character_aliases")
async def save_character_aliases(aliases: Dict[str, str]):
    """Overwrite the alias map (lets the user correct discovered nicknames before review)."""
    cleaned = {k: v for k, v in aliases.items() if k.strip() and v.strip()}
    try:
        cleaned = get_validated_alias_graph(cleaned)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    with file_lock(CHARACTER_ALIASES_PATH):
        atomic_json_write(cleaned, CHARACTER_ALIASES_PATH)
    return {"status": "saved", "count": len(cleaned)}


@router.post("/api/review_script/batch/start")
async def review_script_batch_start(request: BatchReviewRequest, background_tasks: BackgroundTasks):
    """Review multiple saved scripts from the Scripts library, in place.
    A shared alias registry keeps merged character names consistent across the batch."""
    check_global_gpu_lock("batch_review")
    if not request.script_names:
        raise HTTPException(status_code=400, detail="No scripts selected.")

    window = max(0, min(int(request.context_window or 0), 12))
    dedupe = bool(request.dedupe_speakers)
    discover = bool(request.find_nicknames) and dedupe
    # A backward pass only adds value when discovery is on (it re-scans early books with the
    # now-complete registry as hindsight context). With discovery off it would be a pure re-apply.
    bidirectional = bool(request.bidirectional) and discover

    names = request.script_names
    total = len(names)

    def _run():
        with ensure_book_state(SCRIPTS_DIR):
            pass
        state = process_state["batch_review"]
        prefix = "bidirectional " if bidirectional else ""
        _init_batch_state(state,
                          [f"Starting {prefix}batch review of {total} script(s)..."],
                          [{"name": n, "status": "pending"} for n in names])
        state["bidirectional"] = bidirectional
        state["totals_fwd"] = _new_review_totals()
        state["totals_bwd"] = _new_review_totals()
        state["aliases_fwd"] = []
        state["aliases_bwd"] = []
        state["diff_pool"] = {"text": [], "speaker": []}

        # One full on-disk log for the whole batch (in-memory list is a capped tail)
        log_path = _init_task_log("batch_review")

        # One shared registry for the whole batch so canonical names align across books
        registry_path = CHARACTER_ALIASES_PATH if dedupe else None
        source_paths = []

        def _process_book(i: int, name: str, tag: str = "") -> bool:
            """Discover + review one book in place. Returns False to stop the batch (cancel)."""
            state["current_task_idx"] = i
            orig_status = state["tasks"][i].get("status")
            state["tasks"][i]["status"] = "running"

            safe_name = secure_filename(name)
            if not safe_name:
                state["logs"].append(f"--- [{i+1}/{total}]{tag} Skipping — invalid name: {name} ---")
                state["tasks"][i]["status"] = "failed"
                return True
            script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
            if not os.path.exists(script_path):
                state["logs"].append(f"--- [{i+1}/{total}]{tag} Skipping — not found: {name} ---")
                state["tasks"][i]["status"] = "failed"
                return True

            state["logs"].append(f"--- [{i+1}/{total}]{tag} Reviewing '{name}' ---")
            if script_path not in source_paths:
                source_paths.append(script_path)

            # Optional nickname discovery first, accumulating into the shared series registry
            if discover and registry_path:
                state["logs"].append(f"[{i+1}]{tag} Discovering nicknames...")
                nick_cmd = [
                    sys.executable, "-u",
                    os.path.join(BASE_DIR, "find_nicknames.py"),
                    "--input", script_path,
                    "--aliases-file", registry_path,
                    "--append",
                ]
                nick_rc, nick_lines = _stream_subprocess_to_logs(nick_cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ", log_file=log_path)
                if state.get("cancel"):
                    state["tasks"][i]["status"] = "cancelled"
                    return False
                if nick_rc != 0:
                    state["tasks"][i]["status"] = "failed"
                    state["logs"].append(f"[{i+1}]{tag} Nickname discovery failed (exit {nick_rc}): {name}")
                    return True
                new_aliases = _extract_new_aliases(nick_lines)
                if new_aliases:
                    state["tasks"][i]["aliases_found"] = new_aliases
                    bucket = state["aliases_bwd"] if tag == " [bwd]" else state["aliases_fwd"]
                    for a in new_aliases:
                        bucket.append({**a, "book": name})
                    state["logs"].append(
                        f"[{i+1}]{tag} New alias(es): " +
                        ", ".join(f"'{a['variant']}' -> '{a['canonical']}'" for a in new_aliases)
                    )

            # Only clear checkpoint at the start of the first pass (forward).
            # For bidirectional reviews, preserve the forward pass checkpoint
            # so if the backward pass crashes, we can resume from where forward left off.
            should_clear = True
            if state.get("bidirectional") and state.get("current_pass") == "bwd":
                # Don't clear checkpoint during backward pass - preserve forward progress
                should_clear = False
                if orig_status == "incomplete":
                    # The forward pass on this book was VRAM-aborted and left behind
                    # its own partial checkpoint (forward-pass progress/aliases). That
                    # checkpoint isn't valid for the backward pass - reusing it would
                    # silently splice forward-pass output into the backward result.
                    should_clear = True

            if should_clear:
                clear_checkpoint(script_path)

            cmd = [
                sys.executable, "-u",
                os.path.join(BASE_DIR, "review_script.py"),
                "--input", script_path,
                "--output", script_path,
            ]
            if window > 0:
                cmd += ["--context-window", str(window)]
            cmd += get_review_force_args(request.force_review, backward=tag == " [bwd]")
            if dedupe:
                cmd += ["--dedupe-speakers", "--alias-registry", registry_path]
                companion = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
                if os.path.exists(companion):
                    cmd += ["--remap-voice-config", companion]

            rc, own_lines = _stream_subprocess_to_logs(cmd, BASE_DIR, state, log_prefix=f"[{i+1}] ", log_file=log_path)

            if state.get("cancel"):
                state["tasks"][i]["status"] = "cancelled"
                return False
            elif rc == 0:
                # Bidirectional runs review each book twice (forward, then backward); keep
                # each pass's stats/diffs separate so the per-book breakdown doesn't lose
                # the first pass's results when the second pass overwrites them.
                pass_key = "bwd" if tag == " [bwd]" else "fwd"
                stats = _extract_review_stats(own_lines)
                if stats is None:
                    # rc == 0 but the summary line is missing/malformed - we
                    # genuinely don't know whether this book finished cleanly
                    # or hit a VRAM abort with no recorded summary. Treat as
                    # incomplete rather than silently calling it "done".
                    state["tasks"][i]["status"] = "incomplete"
                elif (stats.get("batches_failed", 0) > 0 or
                      stats.get("batches_skipped_vram", 0) > 0):
                    # The reviewer bailed out early to avoid an OOM; entries past the
                    # abort point or failed batches were left unreviewed, and a checkpoint
                    # may remain on disk for a future resume. Don't report this book as done.
                    state["tasks"][i]["status"] = "incomplete"
                elif tag == " [bwd]" and orig_status != "done":
                    state["tasks"][i]["status"] = orig_status
                else:
                    state["tasks"][i]["status"] = "done"
                if stats:
                    state["tasks"][i][f"stats_{pass_key}"] = stats
                    totals = state["totals_bwd"] if tag == " [bwd]" else state["totals_fwd"]
                    for key in totals:
                        if key != "books_done":
                            totals[key] += stats[key]
                    totals["books_done"] += 1
                    state["logs"].append(_format_book_summary(i, total, tag, name, stats))
                else:
                    # The subprocess exited 0 but its "Review complete: X -> Y
                    # entries" summary line wasn't found - surface this rather
                    # than silently recording no stats for an otherwise "done" book.
                    state["logs"].append(
                        f"[{i+1}]{tag} Warning: '{name}' finished but no summary "
                        "stats were found in its output."
                    )
                highlights = _extract_diff_highlights(own_lines)
                failures = _extract_failed_sections(own_lines)
                if failures["sections"]:
                    state["tasks"][i][f"failures_{pass_key}"] = failures
                if highlights["text_rewrites"] or highlights["speaker_changes"]:
                    state["tasks"][i][f"diffs_{pass_key}"] = highlights
                    state["diff_pool"] = get_batch_review_highlights({
                        "text": state["diff_pool"]["text"] + [
                            {**item, "book": name} for item in highlights["text_rewrites"]],
                        "speaker": state["diff_pool"]["speaker"] + [
                            {**item, "book": name} for item in highlights["speaker_changes"]],
                    })
            else:
                state["tasks"][i]["status"] = "failed"
                state["logs"].append(f"[{i+1}]{tag} Failed (exit {rc}): {name}")
            return True

        # Forward pass (reading order)
        state["current_pass"] = "fwd"
        if bidirectional:
            state["logs"].append("=== Forward pass (reading order) ===")
        for i, name in enumerate(names):
            if state["cancel"]:
                state["logs"].append("Batch review cancelled.")
                break
            if not _process_book(i, name, tag=" [fwd]" if bidirectional else ""):
                break

        state["logs"].append(_format_pass_summary(
            "Forward pass" if bidirectional else "Batch review",
            state["totals_fwd"], state["aliases_fwd"], show_aliases=discover))

        # Backward pass — re-scan from the end so early books get discovery seeded with the
        # now-complete series registry (catches references that only resolve later in the series).
        if bidirectional and not state["cancel"]:
            state["logs"].append("=== Backward pass (hindsight: re-scanning from the end) ===")
            state["current_pass"] = "bwd"
            for i in range(total - 1, -1, -1):
                if state["cancel"]:
                    state["logs"].append("Batch review cancelled.")
                    break
                if not _process_book(i, names[i], tag=" [bwd]"):
                    break

            state["logs"].append(_format_pass_summary(
                "Backward pass (hindsight)", state["totals_bwd"], state["aliases_bwd"], show_aliases=discover))

            overall_totals = _combine_pass_totals(state)
            overall_aliases = state["aliases_fwd"] + state["aliases_bwd"]
            state["logs"].append(_format_pass_summary("Overall", overall_totals, overall_aliases, show_aliases=discover))

        report_path = _write_batch_review_report(state, names, bidirectional, discover)
        if report_path:
            state["artifacts"].append({
                "artifact_path": report_path,
                "kind": "batch_review_report",
                "source_paths": source_paths,
                "config_path": CONFIG_PATH,
            })
            state["logs"].append(f"Wrote batch review report: {os.path.relpath(report_path, ROOT_DIR)}")

        state["running"] = False
        state["logs"].append("Batch review finished.")

    schedule_claimed_background_task(background_tasks, "batch_review", _run_claimed_background_task, "batch_review", _run)
    return {"status": "started", "task_count": total, "bidirectional": bidirectional}




@router.post("/api/review_script/batch/cancel")
async def review_script_batch_cancel():
    return _batch_cancel_helper("batch_review")


@router.post("/api/review_script/batch/pause")
async def review_script_batch_pause():
    return _pause_task("batch_review", "No batch review is currently running.",
                        "Batch review is starting up, retry in a moment.",
                        "Batch review")


@router.post("/api/review_script/batch/resume")
async def review_script_batch_resume():
    return _resume_task("batch_review", "No batch review is currently running.",
                         "Batch review")


class BatchScriptTask(BaseModel):
    filename: str  # filename inside uploads/
    first_person_narrator: Optional[str] = None

class BatchScriptRequest(BaseModel):
    tasks: List[BatchScriptTask] = Field(max_length=MAX_SCRIPT_BATCH_ITEMS)
    collision_policy: Literal["cancel", "version", "replace"] = "cancel"
    strip_front_matter: bool = True


def _get_versioned_script_path(path: str) -> str:
    base, ext = os.path.splitext(path)
    counter = 2
    candidate = path
    while os.path.exists(candidate):
        candidate = f"{base}_{counter}{ext}"
        counter += 1
    return candidate


def _resolve_batch_output_path(output_path, policy, reserved_outputs):
    """Decide what happens to a batch job's output path given a collision.

    Returns (path, action) where action is one of:
      - "ok": no collision; use path as-is.
      - "skip": collision policy is "cancel"; caller should skip the job.
      - "version": path was suffixed to a versioned, non-colliding path.
      - "backup": path is unchanged but the existing on-disk file should be
        backed up before being overwritten.

    A collision with a *reserved* (in-batch) output is never resolved by
    "replace" -- replacing a file this same batch is about to produce is
    never what "replace" means, so it is versioned instead, exactly like
    the explicit "version" policy.
    """
    is_reserved = output_path in reserved_outputs
    exists_on_disk = os.path.lexists(output_path)
    if not is_reserved and not exists_on_disk:
        return output_path, "ok"
    if policy == "cancel":
        return output_path, "skip"
    if policy == "version" or is_reserved:
        candidate = output_path
        base, extension = os.path.splitext(output_path)
        counter = 2
        while candidate in reserved_outputs or os.path.lexists(candidate):
            candidate = f"{base}_{counter}{extension}"
            counter += 1
        return candidate, "version"
    # policy == "replace" and the collision is disk-only (not reserved)
    return output_path, "backup"


def _resolve_batch_script_input(filename: str) -> str:
    safe_filename = secure_filename(filename)
    if not safe_filename or safe_filename != filename:
        raise ValueError(f"Invalid filename: {filename}")
    input_path = os.path.join(UPLOADS_DIR, safe_filename)
    if not os.path.exists(input_path) and os.path.splitext(safe_filename)[1].lower() == ".epub":
        input_path = os.path.join(UPLOADS_DIR, os.path.splitext(safe_filename)[0] + ".txt")
    if not os.path.isfile(input_path):
        raise ValueError(f"Source is not a regular file: {filename}")
    return input_path


def _read_and_validate_batch_script_source(job):
    """Read one batch source and reject unattested narrator metadata."""
    with open(job["input_path"], encoding="utf-8") as source:
        text = fix_mojibake(source.read())
    text, normalization_count = normalize_known_source_corruptions(text)
    narrator = job.get("first_person_narrator")
    if narrator and not is_narrator_attested(narrator, text):
        raise ValueError(
            f"{job['filename']}: first-person narrator must appear by name "
            "at least three times in the source")
    return text, normalization_count


def get_batch_script_source_identity(path):
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def get_validated_batch_script_source(job):
    """Reuse this request's validated source only while file identity matches."""
    cached = job.get("prepared_source")
    if cached and get_batch_script_source_identity(job["input_path"]) == cached["identity"]:
        return cached["text"], cached["normalizations"]
    return _read_and_validate_batch_script_source(job)


def get_batch_script_sizing_identity(text, settings, context_windows):
    """Identify all request-shape inputs; server capacity is applied separately."""
    inputs = {"source": hashlib.sha256(text.encode("utf-8")).hexdigest(),
              "settings": settings,
              "context_windows": get_context_rescue_windows(context_windows),
              "prompts": [load_segment_prompts(), load_attribute_prompts(),
                          load_instruct_prompts()]}
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def get_batch_script_source_preflight(job, text, settings, context, context_windows):
    """Reuse this request's sizing summary only while its inputs still match."""
    receipt = (job.get("prepared_source") or {}).get("preflight")
    if receipt and receipt["identity"] == get_batch_script_sizing_identity(
            text, settings, context_windows):
        return get_three_pass_preflight_capacity(receipt["report"], context, 1)
    return build_three_pass_request_preflight(
        text, settings, context, 1, context_windows=context_windows)


def build_batch_script_preflight(jobs):
    """Build the shared read-only sizing report used by the UI and dispatcher."""
    config = load_app_config(CONFIG_PATH)
    llm = get_active_llm_config(config)
    status = get_planned_ideal_settings(
        config.get("llm_mode", "local"), llm.get("base_url", ""),
        llm.get("model_name", ""), config.get("llm_remote_ssh"), api_key=llm.get("api_key"))
    parallel = max(1, int(status.get("parallel") or 1))
    context = int(status.get("context_length") or 0)
    generation = config.get("generation") or {}
    settings = resolve_three_pass_generation_settings(config)
    context_windows = generation.get("context_rescue_windows")
    books = []
    for job in jobs:
        text, normalization_count = get_validated_batch_script_source(job)
        report = get_batch_script_source_preflight(
            job, text, settings, context, context_windows)
        unicode_report = audit_unicode_text(text)
        books.append({
            "filename": job["filename"],
            "chunk_count": report["chunk_count"],
            "worst_predicted_tokens": report["worst_predicted_tokens"],
            "p95_predicted_tokens": report["p95_predicted_tokens"],
            "average_predicted_tokens": report["average_predicted_tokens"],
            "scripts": unicode_report["scripts"],
            "is_nfc": unicode_report["is_nfc"],
            "known_normalizations": normalization_count,
            "largest_predicted_completion": report.get("largest_predicted_completion", 0),
            "output_ceiling": report.get("output_ceiling", 0),
            "exceeds_output_ceiling": bool(report.get("exceeds_output_ceiling")),
            "suggested_chunk_size": report.get("suggested_chunk_size"),
        })
    worst = max((book["worst_predicted_tokens"] for book in books), default=0)
    safe = min(parallel, len(jobs))
    while safe > 1 and worst * safe > context:
        safe -= 1
    safe = max(1, safe)
    per_slot = context // safe if context else 0
    for book in books:
        book["fits_selected_slot"] = bool(per_slot and book["worst_predicted_tokens"] <= per_slot)
    fallback = None
    if safe < min(parallel, len(jobs)):
        fallback = (f"Reduced concurrency from {min(parallel, len(jobs))} to {safe} because "
                    f"the largest predicted request needs {worst} tokens.")
    return {"book_count": len(books), "workers": safe, "loaded_parallel": parallel,
            "context_length": context, "per_slot_context": per_slot,
            "worst_request_tokens": worst, "fallback_reason": fallback, "books": books,
            "chunk_size": settings["chunk_size"]}


# Above this share of chunks with no quote mark at all, a book does not mark
# its dialogue with quotes. The worst quote-marked PDNC novel (Mansfield Park)
# is 0.15; a book with em-dash dialogue is 1.0.
UNQUOTED_BOOK_THRESHOLD = 0.5


def three_pass_refusal(jobs):
    """-> the 400 message that stops a single or batch run before it starts,
    or None. One helper for both routes so they cannot drift (Rule 15):
    first the output-ceiling check, then the quotes-only check. Needs no
    server status - only the configured settings and the source text."""
    config = load_app_config(CONFIG_PATH)
    settings = resolve_three_pass_generation_settings(config)
    context_windows = (config.get("generation") or {}).get("context_rescue_windows")
    reports = []
    for job in jobs:
        text, _ = get_validated_batch_script_source(job)
        reports.append((job["filename"], get_batch_script_source_preflight(
            job, text, settings, 0, context_windows)))
    return (output_ceiling_refusal(settings, reports)
            or unquoted_book_refusal(settings, reports))


def unquoted_book_refusal(settings, reports):
    """Dialogue detection "Quote marks only" on a book that does not use quote
    marks would silently narrate the whole book - the plausible-looking
    non-result Rule 21 warns about - so it is refused with the count."""
    if settings["segmentation"] != "quotes":
        return None
    for name, report in reports:
        seg = report["segmentation"]
        if seg["chunks"] and seg["chunks_without_quote_marks"] / seg["chunks"] > UNQUOTED_BOOK_THRESHOLD:
            return (f"Dialogue detection is set to \"Quote marks only\", but "
                    f"{seg['chunks_without_quote_marks']} of {seg['chunks']} pieces of {name} "
                    "contain no quote marks - this book does not seem to mark its dialogue "
                    "with quotes. Switch Dialogue detection to Auto (or Model only) for it.")
    return None


def output_ceiling_refusal(settings, reports):
    """-> the 400 message when some job's largest chunk cannot be re-emitted
    within the run's output ceiling, or None when every chunk fits. Pass 1
    returns the chunk verbatim, so a chunk that needs more output tokens than
    the model may ever be asked for fails on every retry; refusing before the
    run starts is the cheaper failure."""
    over = [(name, report) for name, report in reports if report["exceeds_output_ceiling"]]
    if not over:
        return None
    name, worst = max(over, key=lambda item: item[1]["largest_predicted_completion"])
    suggested = min(report["suggested_chunk_size"] for _, report in over)
    return (f"\"Step 1: text per request\" is set to {settings['chunk_size']} characters, which is "
            f"more than this model can write back in one reply: a piece of {name} would need "
            f"about {worst['largest_predicted_completion']} tokens and the most it can reply is "
            f"{worst['output_ceiling']} (Baseline Response Tokens in Setup, or 16384 if that is lower). "
            f"Set it to {suggested} or below, or raise Baseline Response Tokens.")


def _get_batch_script_workers(jobs):
    report = build_batch_script_preflight(jobs)
    return report["workers"], report["worst_request_tokens"], report["context_length"]


@router.post("/api/generate_script/batch/preflight")
async def generate_script_batch_preflight(request: BatchScriptRequest):
    if not request.tasks:
        raise HTTPException(status_code=400, detail="No files provided.")
    try:
        jobs = [{"filename": task.filename,
                 "input_path": _resolve_batch_script_input(task.filename),
                 "first_person_narrator": get_valid_narrator_name(
                     task.first_person_narrator)}
                for task in request.tasks]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await asyncio.to_thread(build_batch_script_preflight, jobs)


@contextlib.contextmanager
def ensure_batch_script_output(job, state):
    """Recheck collision policy while holding the library writer's claim."""
    candidate = job["output_path"]
    while True:
        with file_lock(candidate):
            if state.get("cancel"):
                yield None
                return
            resolved, action = _resolve_batch_output_path(
                candidate, job.get("collision_policy", "cancel"),
                job.get("other_outputs", ()))
            if action == "skip":
                state["logs"].append(
                    f"[{job['index'] + 1}] Skipping — saved script "
                    f"'{os.path.basename(candidate)}' now exists.")
                yield None
                return
            if resolved == candidate:
                if action == "backup":
                    backup = backup_file_with_timestamp(candidate)
                    state["logs"].append(
                        f"[{job['index'] + 1}] Backed up existing script as "
                        f"'{os.path.basename(backup)}'.")
                yield {**job, "output_path": candidate,
                       "safe_stem": os.path.splitext(os.path.basename(candidate))[0]}
                return
        # Acquire the new name's own lock before trusting its availability.
        candidate = resolved


def _run_batch_script_job(job, state, log_path, total):
    with ensure_batch_script_output(job, state) as prepared:
        if prepared is None:
            state["tasks"][job["index"]]["status"] = (
                "cancelled" if state.get("cancel") else "failed")
            return
        _run_claimed_batch_script_job(prepared, state, log_path, total)


def _run_claimed_batch_script_job(job, state, log_path, total):
    index = job["index"]
    if state.get("cancel"):
        state["tasks"][index]["status"] = "cancelled"
        return
    state["tasks"][index]["status"] = "running"
    state["logs"].append(f"--- [{index + 1}/{total}] {job['filename']} ---")
    env = os.environ.copy()
    if state.get("run_id"):
        env["ALEXANDRIA_RUN_ID"] = state["run_id"]
    command = build_generate_script_command(
        job["input_path"], output_path=job["output_path"],
        strip_front_matter=job.get("strip_front_matter", True),
        first_person_narrator=job.get("first_person_narrator"),
        reasoning_effort=get_active_reasoning_effort(),
    )
    rc, _ = _stream_subprocess_to_logs(
        command, BASE_DIR, state, log_prefix=f"[{index + 1}] ",
        log_file=log_path, env=env)
    if state.get("cancel"):
        state["tasks"][index]["status"] = "cancelled"
    elif rc == 0:
        state["tasks"][index].update({"status": "done", "saved_as": job["safe_stem"]})
        state["logs"].append(f"[{index + 1}] Saved as '{job['safe_stem']}' in Scripts library.")
    else:
        state["tasks"][index]["status"] = "failed"
        state["logs"].append(f"[{index + 1}] Failed (exit {rc}): {job['filename']}")


def get_prepared_batch_script_jobs(request):
    """Validate source identities, narrator evidence and sizing off the event loop."""
    if not request.tasks:
        raise HTTPException(status_code=400, detail="No files provided.")
    try:
        narrators = [get_valid_narrator_name(task.first_person_narrator)
                     for task in request.tasks]
        jobs = [{"filename": task.filename,
                 "input_path": _resolve_batch_script_input(task.filename),
                 "first_person_narrator": narrator}
                for task, narrator in zip(request.tasks, narrators)]
        config = load_app_config(CONFIG_PATH)
        settings = resolve_three_pass_generation_settings(config)
        context_windows = (config.get("generation") or {}).get("context_rescue_windows")
        prepared = []
        for job in jobs:
            identity = get_batch_script_source_identity(job["input_path"])
            text, normalizations = _read_and_validate_batch_script_source(job)
            if get_batch_script_source_identity(job["input_path"]) != identity:
                raise ValueError(f"Source changed while preparing: {job['filename']}")
            sizing_identity = get_batch_script_sizing_identity(text, settings, context_windows)
            report = build_three_pass_request_preflight(
                text, settings, 0, 1, context_windows=context_windows)
            prepared.append({**job, "prepared_source": {
                "identity": identity, "text": text, "normalizations": normalizations,
                "preflight": {
                    "identity": sizing_identity,
                    "report": {key: value for key, value in report.items() if key != "requests"}}}})
        refusal = three_pass_refusal(prepared)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if refusal:
        raise HTTPException(status_code=400, detail=refusal)
    return prepared


def get_validated_batch_script_narrators(request):
    return [job["first_person_narrator"] for job in get_prepared_batch_script_jobs(request)]


@router.post("/api/generate_script/batch/start")
async def generate_script_batch_start(request: BatchScriptRequest, background_tasks: BackgroundTasks):
    """Process multiple text/EPUB files through three_pass_generate.py - the
    same command single-book generation runs (build_generate_script_command)."""
    check_global_gpu_lock("batch_script")
    prepared_jobs = await asyncio.to_thread(get_prepared_batch_script_jobs, request)

    def _run():
        with ensure_book_state(SCRIPTS_DIR):
            pass
        state = process_state["batch_script"]
        _init_batch_state(state,
                          [f"Starting batch of {len(request.tasks)} file(s)..."],
                          [{"filename": t.filename, "status": "pending"} for t in request.tasks])

        # One full on-disk log for the whole batch (in-memory list is a capped tail)
        log_path = _init_task_log("batch_script")

        jobs = []
        reserved_outputs = set()
        for i, task in enumerate(request.tasks):
            if state["cancel"]:
                state["logs"].append("Batch cancelled.")
                break
            try:
                input_path = _resolve_batch_script_input(task.filename)
            except ValueError as exc:
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — {exc}")
                state["tasks"][i]["status"] = "failed"
                continue

            stem = os.path.splitext(os.path.basename(input_path))[0]
            safe_stem = secure_filename(stem) or f"batch_{i+1}"
            output_path = os.path.join(SCRIPTS_DIR, f"{safe_stem}.json")
            was_reserved = output_path in reserved_outputs
            output_path, action = _resolve_batch_output_path(
                output_path, request.collision_policy, reserved_outputs)
            if action == "skip":
                state["logs"].append(
                    f"[{i+1}] Skipping — saved script '{safe_stem}' already exists. "
                    "Choose version or replace explicitly.")
                state["tasks"][i]["status"] = "failed"
                continue
            if action == "version":
                if was_reserved and request.collision_policy == "replace":
                    state["logs"].append(
                        f"[{i+1}] '{safe_stem}.json' collides with an output already "
                        "produced by this batch — writing "
                        f"'{os.path.basename(output_path)}' instead of replacing.")
                safe_stem = os.path.splitext(os.path.basename(output_path))[0]

            reserved_outputs.add(output_path)
            jobs.append({"index": i, "filename": task.filename, "input_path": input_path,
                         "output_path": output_path, "safe_stem": safe_stem,
                         "strip_front_matter": request.strip_front_matter,
                         "first_person_narrator": prepared_jobs[i]["first_person_narrator"],
                         "prepared_source": prepared_jobs[i]["prepared_source"],
                         "collision_policy": "version" if was_reserved else request.collision_policy})

        jobs = [{**job, "other_outputs": tuple(
                    path for path in reserved_outputs if path != job["output_path"])}
                for job in jobs]
        if jobs and not state.get("cancel"):
            config = load_app_config(CONFIG_PATH)
            llm = get_active_llm_config(config)
            _, _, settings_message = ensure_ideal_settings(
                config.get("llm_mode", "local"), llm.get("base_url", ""),
                llm.get("model_name", ""), config.get("llm_remote_ssh"), api_key=llm.get("api_key"))
            state["logs"].append(f"Batch LM Studio preflight: {settings_message}")
            workers, worst, context = _get_batch_script_workers(jobs)
            state["workers"] = workers
            state["logs"].append(
                f"Batch preflight: workers={workers}, worst_request={worst}, context={context}")
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(_run_batch_script_job, job, state, log_path,
                                           len(request.tasks)) for job in jobs]
                for future in futures:
                    future.result()

        state["running"] = False
        state["logs"].append("Batch script generation finished.")

    schedule_claimed_background_task(background_tasks, "batch_script", _run_claimed_background_task, "batch_script", _run)
    return {"status": "started", "task_count": len(request.tasks)}


@router.post("/api/generate_script/batch/cancel")
async def generate_script_batch_cancel():
    return _batch_cancel_helper("batch_script")


@router.post("/api/generate_script/batch/pause")
async def generate_script_batch_pause():
    return _pause_task("batch_script", "No batch script generation is currently running.",
                        "Batch script generation is starting up, retry in a moment.",
                        "Batch script generation")


@router.post("/api/generate_script/batch/resume")
async def generate_script_batch_resume():
    return _resume_task("batch_script", "No batch script generation is currently running.",
                         "Batch script generation")


@router.get("/api/annotated_script")
async def get_annotated_script():
    """Return the current working annotated_script.json.

    No SPA caller - intentionally kept as a programmatic/curl-accessible
    read endpoint (exercised by test_api.py's test_get_annotated_script).
    """
    with ensure_book_state(DATA_DIR):
        if not os.path.exists(SCRIPT_PATH):
            raise HTTPException(status_code=404, detail="No annotated script found")
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

@router.get("/api/annotated_script/diff")
async def get_annotated_script_diff():
    """Word-level differences between the saved source and the active script
    (issue #522 s7.4/7.5), one hunk per divergence with the chunk it sits in
    and the script entry to jump to."""
    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=404, detail="No annotated script found")
    state = safe_load_json(os.path.join(DATA_DIR, "state.json"), {})
    input_file = state.get("input_file_path") if isinstance(state, dict) else None
    if not input_file or not os.path.exists(input_file):
        raise HTTPException(status_code=404, detail="No source text is on record for this script")
    with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list):
        raise HTTPException(status_code=400, detail="Script is not a list of entries")
    source, _encoding = read_source_text(input_file)
    return await asyncio.to_thread(word_diff, source, entries)


@router.get("/api/status")
async def get_task_statuses():
    """Discover registered tasks without serializing logs or live process handles."""
    return {name: {"running": bool(state.get("running"))}
            for name, state in process_state.items()}


@router.get("/api/status/{task_name}")
async def get_status(task_name: str, include_health: bool = False):
    if task_name not in process_state:
        raise HTTPException(status_code=404, detail="Task not found")
    state = dict(process_state[task_name])
    state.pop("process", None)
    state.pop("processes", None)
    if task_name == "batch_review":
        state["tasks"] = [get_batch_review_task_snapshot(task, bool(state.get("bidirectional")))
                          for task in state.get("tasks", [])]
    if include_health and task_name == "voicelab":
        state = copy.deepcopy(state)
    # the same estimate /api/status/eta serves, so the polling page needs no
    # second request to show "about 4m left"
    state["eta"] = _compute_eta(state) if state.get("running") else None
    # a manual-transport run waiting on the user: id + where it is, so the
    # page fetches the full prompt only when the request changes
    pending = read_manual_pending() if state.get("running") else None
    state["manual_request"] = ({"id": pending["id"], "sequence": pending["sequence"],
                                "stage_hint": _stage_hint(state.get("logs") or [])}
                               if pending else None)
    if include_health and task_name == "voicelab":
        from routers.voicelab import _build_voicelab_health
        state["health"] = await asyncio.to_thread(_build_voicelab_health, state=state)
    return state


_STAGE_HINT_RE = re.compile(r"^(Step \d \([a-z]+\): .*|Retrying\.\.\..*|Warning: .*)$")


def _stage_hint(logs):
    """The last log lines that say where the run is (#592's markers) and, if
    the previous paste was rejected, why - so the manual panel can say
    'Step 2 (speakers): window 2 of 4 · Retrying... (attempt 2 of 4)'."""
    hints = []
    for line in reversed(logs[-40:]):
        if _STAGE_HINT_RE.match(line.strip()):
            hints.append(line.strip())
            if line.strip().startswith("Step"):
                break
    return " · ".join(reversed(hints))


def read_manual_pending():
    """The request a manual-transport run is waiting on, or None."""
    path = os.path.join(manual_llm_dir(DATA_DIR), "pending.json")
    data = safe_load_json(path, None)
    return data if isinstance(data, dict) and data.get("id") else None


class ManualReply(BaseModel):
    id: str
    content: str = Field(max_length=MAX_MANUAL_REPLY_CHARACTERS)


@router.get("/api/manual_llm/pending")
async def manual_llm_pending():
    """The prompt to copy for the request the run is waiting on (issue #593).
    The messages are the exact request the model would have received."""
    pending = read_manual_pending()
    if not pending:
        return {"pending": None}
    running = [name for name, state in process_state.items() if state.get("running")]
    hint = _stage_hint(process_state[running[0]]["logs"]) if running else ""
    return {"pending": {**pending, "stage_hint": hint, "task": running[0] if running else None}}


@router.post("/api/manual_llm/response")
async def manual_llm_response(reply: ManualReply):
    """The user's (or their script's) answer to the pending request. Whether
    the content is acceptable is the pipeline's call - a rejected paste comes
    back as the next pending request, carrying the retry prompt."""
    pending = read_manual_pending()
    if not pending:
        raise HTTPException(status_code=409, detail="No request is waiting for a reply.")
    if reply.id != pending["id"]:
        raise HTTPException(status_code=409, detail="That request is no longer the one waiting; reload the prompt.")
    atomic_json_write({"id": reply.id, "content": reply.content, "received": time.time()},
                      os.path.join(manual_llm_dir(DATA_DIR), "response.json"))
    return {"accepted": True, "sequence": pending["sequence"]}


@router.get("/api/logs/{task_name}")
async def get_task_log(task_name: str, download: bool = False):
    """Serve the complete on-disk log for a task (the in-memory status only keeps a
    capped tail). Use ?download=true to download the file."""
    if task_name not in process_state:
        raise HTTPException(status_code=404, detail="Task not found")
    log_path = _task_log_path(task_name)
    if not os.path.exists(log_path):
        raise HTTPException(status_code=404, detail="No log file for this task yet.")
    filename = f"{task_name}.log"
    return FileResponse(
        log_path,
        media_type="text/plain",
        filename=filename if download else None,
    )
