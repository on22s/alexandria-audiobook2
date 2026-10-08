import os
import copy
import tempfile
import hashlib
import json
import shutil
import subprocess
import threading
import zipfile
import io
import re
import time
import logging
import gc
import uuid
from script_repair import EXPLICIT_SILENCE_MS
from speech_policy import get_spoken_symbol, get_scene_break_text
from verbalization import (SET_APART_HINT, classify,
                           extract_delivery_cues, is_pictographic_kana,
                           split_bracketed_spans)
from utils import (atomic_json_write, safe_load_json, is_oom_failure, is_path_inside,
                   get_app_config_path, is_nonverbal_text, secure_filename, file_lock)
from chunk_status_journal import ChunkStatusJournal, get_chunk_status_journal_path
from config_settings import load_app_config
from audio_validation import publish_audio_output, remove_stale_audio, validate_generated_audio
from tts import (
    TTSEngine,
    ExportCancelled,
    ensure_audio_export_active,
    combine_audio_with_pauses,
    compute_timeline,
    get_pause_duration_ms,
    sanitize_filename,
    voice_category,
    DEFAULT_PAUSE_MS,
    SAME_SPEAKER_PAUSE_MS,
                 voice_config_for_chunk)
from pydub import AudioSegment

MAX_CHUNK_CHARS = 500
NARRATOR_GENERATION_FIELDS = (
    "focus_speaker", "character_focus", "narrator_version", "chapter_narrator_version",
    "narrator_gender", "focus_gender", "narrator_age_group", "focus_age_group")
CHUNK_GENERATION_FIELDS = (
    "text", "speaker", "instruct", *NARRATOR_GENERATION_FIELDS,
    "character_style", "default_style", "style_timeline")
GENERATION_INPUTS_CHANGED = "Chunk removed or generation inputs changed during generation"


def get_index_selection(indices, count):
    """Validate an explicit selection and return unique indices in source order."""
    if not indices:
        raise ValueError("Select at least one index")
    if any(type(index) is not int or index < 0 or index >= count for index in indices):
        raise ValueError(f"Selected indices must be integers between 0 and {count - 1}")
    return sorted(set(indices))


def get_chapter_reuse_duration(plan, old, wanted, changed_only, out_dir):
    """Return the duration of an existing chapter that export will retain."""
    if not old:
        return None
    try:
        duration = int(old["end_ms"]) - int(old["start_ms"])
    except (KeyError, TypeError, ValueError):
        return None
    if duration < 0 or not os.path.isfile(os.path.join(out_dir, old["file"])):
        return None
    if plan["index"] not in wanted or (changed_only and
            old.get("fingerprint") == plan["fingerprint"] and old.get("file") == plan["file"]):
        return duration
    return None


def get_chunk_generation_inputs(chunk):
    """Fields whose changes make a captured render obsolete."""
    return {field: chunk[field] for field in CHUNK_GENERATION_FIELDS if field in chunk}


def get_chunk_generation_requests(chunks, indices):
    """Freeze each requested row once, retaining source indices for engine results."""
    return {idx: copy.deepcopy(chunks[idx]) for idx in indices if 0 <= idx < len(chunks)}


def get_captured_chunk(chunks, index):
    """Read a captured request by index; legacy private callers may pass a list."""
    if isinstance(chunks, dict):
        return chunks.get(index)
    return chunks[index] if 0 <= index < len(chunks) else None


def _new_chunk_uid():
    """Stable per-chunk id for audio filenames.

    Unlike chunk['id'] (which is the list position and gets renumbered on every
    insert/delete), this is assigned once and never changes, so a chunk's audio
    filename can't collide with a neighbour's after the list shifts.
    """
    return uuid.uuid4().hex[:12]




def get_speaker(entry):
    """Get speaker from entry, checking both 'speaker' and 'type' fields."""
    return entry.get("speaker") or entry.get("type") or ""


def _is_structural_text(text):
    """Check if text is a title, chapter heading, dedication, or other structural fragment."""
    stripped = text.strip()
    if not stripped:
        return True
    sentence = stripped.rstrip('\"\'”’»」』)）]】')
    # Very short and not a full sentence (no sentence-ending punctuation)
    if len(stripped) < 80 and (not sentence or sentence[-1] not in '.!?。！？'):
        return True
    return False


def _make_chunk(speaker, text, instruct, pause_after=None, source_indices=None):
    """Build a chunk dict, omitting pause_after when None for clean JSON."""
    chunk = {"speaker": speaker, "text": text, "instruct": instruct}
    if pause_after is not None:
        chunk["pause_after"] = pause_after
    if source_indices is not None:
        chunk["source_entry_indices"] = list(source_indices)
    return chunk


def split_on_unspeakable(entry, scene_break_pause_ms):
    """Split one entry into speakable parts, handling each glyph by class.

    A scene break inside prose becomes a boundary carrying pause_after rather
    than a character the narrator reads aloud. Verbalized symbols become words.
    Delivery cues move into instruct. Anything unmapped is left in place and
    reported, so an unknown glyph stays visible instead of being silently voiced.

    Returns (parts, review_chars). Never mutates the entry it is given.
    """
    # A run of repeated emoji is a section divider, the same role as a row of
    # box-drawing characters, so it is folded to a scene-break glyph before
    # classification. Done first because each emoji is category So on its own
    # and would otherwise be reported for review one character at a time.
    text = get_scene_break_text(str(entry.get("text") or ""))
    review = []
    for position, char in enumerate(text):
        neighbours = text[max(0, position - 2):position] + text[position + 1:position + 3]
        if classify(char) == "review" or is_pictographic_kana(char, neighbours):
            review.append(char)
    segments, current = [], []
    for char in text:
        kind = classify(char)
        if kind == "scene_break":
            segments.append("".join(current))
            current = []
        elif kind == "verbalize":
            current.append(get_spoken_symbol(char))
        else:
            current.append(char)
    segments.append("".join(current))

    parts = []
    for index, segment in enumerate(segments):
        segment_parts = []
        # A bracketed span is delivered differently from the prose around it,
        # so it becomes its own part rather than inheriting the entry's
        # instruct. These boundaries are NOT scene breaks and carry no pause.
        for fragment, set_apart in split_bracketed_spans(segment):
            cleaned, hints = extract_delivery_cues(fragment)
            if set_apart:
                hints = hints + [SET_APART_HINT]
            cleaned = " ".join(cleaned.split()) if cleaned.strip() else ""
            if not cleaned:
                continue
            part = dict(entry)
            part["text"] = cleaned
            if hints:
                existing = str(part.get("instruct") or "").strip()
                part["instruct"] = " ".join(filter(None, [existing] + hints))
            segment_parts.append(part)

        if not segment_parts:
            # A scene break with nothing before it has no audio anchor to
            # attach a pause to, mirroring the leading-nonverbal rule below.
            if parts:
                parts[-1]["pause_after"] = max(
                    int(parts[-1].get("pause_after") or 0), scene_break_pause_ms)
            continue
        # The gap between segments is the scene break, so the pause goes on the
        # last part of every segment except the final one.
        if index < len(segments) - 1:
            segment_parts[-1]["pause_after"] = max(
                int(segment_parts[-1].get("pause_after") or 0),
                scene_break_pause_ms)
        parts.extend(segment_parts)

    return parts, review


def log_review_characters(review):
    """Warn about glyphs no rule could classify, grouped by character.

    split_on_unspeakable leaves these in the text on purpose - guessing at an
    unknown symbol is worse than flagging it - but the report previously went
    nowhere, so "flagged" meant the narrator read it aloud with nobody told.
    """
    if not review:
        return
    counts = {}
    for item in review:
        counts.setdefault(item["character"], []).append(item["entry_index"])
    summary = ", ".join(
        f"{character!r} x{len(indexes)} (first at entry {min(indexes)})"
        for character, indexes in sorted(counts.items()))
    logger.warning(
        "Unmapped symbols left in TTS text and needing review: %s", summary)


def get_speakable_entries(script_entries, review_sink=None):
    """Return copied TTS entries, converting nonverbal marks into a pause.

    Punctuation-only and block-glyph dialogue remains in annotated_script.json
    for fidelity, but sending it to TTS produces noise or failures. A nonverbal
    entry extends the preceding speakable entry's pause without mutating caller
    data. Leading nonverbal marks have no audio anchor and are omitted.
    """
    speakable = []
    for entry_index, source_entry in enumerate(script_entries):
        entry = dict(source_entry)
        if is_nonverbal_text(entry.get("text")):
            if not speakable:
                continue
            previous = speakable[-1]
            previous["pause_after"] = max(
                int(previous.get("pause_after") or 0), DEFAULT_PAUSE_MS)
        else:
            parts, review = split_on_unspeakable(entry, EXPLICIT_SILENCE_MS)
            if review_sink is not None:
                review_sink.extend({"entry_index": entry_index,
                                    "character": character}
                                   for character in review)
            speakable.extend(parts)
    return speakable


def group_into_chunks(script_entries, max_chars=MAX_CHUNK_CHARS,
                      review_sink=None, include_source_indices=False):
    """Group consecutive entries by same speaker into chunks up to max_chars"""
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:
        raise ValueError("max_chars must be a positive integer")
    if include_source_indices:
        script_entries = [{**entry, "_source_entry_indices": [index]}
                          for index, entry in enumerate(script_entries)]
    script_entries = get_speakable_entries(script_entries,
                                           review_sink=review_sink)
    if not script_entries:
        return []

    chunks = []
    current_speaker = get_speaker(script_entries[0])
    current_text = script_entries[0].get("text", "")
    current_instruct = script_entries[0].get("instruct", "")
    current_pause_after = script_entries[0].get("pause_after")
    current_indices = script_entries[0].get("_source_entry_indices") if include_source_indices else None

    for entry in script_entries[1:]:
        speaker = get_speaker(entry)
        text = entry.get("text", "")
        instruct = entry.get("instruct", "")

        # Don't merge structural text (titles, chapter headings, dedications),
        # and never merge past a pending pause: pause_after belongs to the
        # boundary after current_text, so absorbing the next entry moves that
        # silence to the end of the combined text. A scene break inside a
        # paragraph then played its pause after both sentences instead of
        # between them, which is not audible as a break at all.
        if (speaker == current_speaker and instruct == current_instruct
                and current_pause_after is None
                and not _is_structural_text(current_text)
                and not _is_structural_text(text)):
            combined = current_text + " " + text
            if len(combined) <= max_chars:
                current_text = combined
                if include_source_indices:
                    current_indices = sorted(set(current_indices + entry["_source_entry_indices"]))
                # Last merged entry's pause_after wins
                current_pause_after = entry.get("pause_after", current_pause_after)
            else:
                chunks.append(_make_chunk(current_speaker, current_text, current_instruct, current_pause_after, current_indices))
                current_text = text
                current_indices = entry["_source_entry_indices"] if include_source_indices else None
                current_instruct = instruct
                current_pause_after = entry.get("pause_after")
        else:
            chunks.append(_make_chunk(current_speaker, current_text, current_instruct, current_pause_after, current_indices))
            current_speaker = speaker
            current_indices = entry["_source_entry_indices"] if include_source_indices else None
            current_text = text
            current_instruct = instruct
            current_pause_after = entry.get("pause_after")

    # Don't forget the last chunk
    chunks.append(_make_chunk(current_speaker, current_text, current_instruct, current_pause_after, current_indices))

    bounded = []
    for chunk in chunks:
        remaining = chunk["text"]
        while len(remaining) > max_chars:
            boundaries = list(re.finditer(r"\s+|[.!?。！？]", remaining[:max_chars]))
            cut = boundaries[-1].end() if boundaries else max_chars
            if cut < max(1, max_chars // 2):
                cut = max_chars
            piece = {**chunk, "text": remaining[:cut]}
            piece.pop("pause_after", None)
            bounded.append(piece)
            remaining = remaining[cut:]
        bounded.append({**chunk, "text": remaining})
    return bounded

logger = logging.getLogger(__name__)

# Explicit, because pydub/ffmpeg's unspecified default for 24 kHz mono is
# 32 kbps (measured with ffprobe on 2026-09-11): every voiceline and the merged
# audiobook were written at a rate that audibly degrades speech. 128 kbps is
# transparent for mono speech at this sample rate and what listeners expect.
MP3_BITRATE = "128k"


def _load_audio_segment(path, **kwargs):
    """Load audio with an explicitly owned input handle.

    pydub 0.25 leaves the handle it opens for WAV input open on an early
    return path; keeping ownership here makes the lifetime explicit.
    """
    with open(path, "rb") as source:
        return AudioSegment.from_file(source, **kwargs)


def _export_audio_segment(segment, path, format_name, cancel_check=None, **kwargs):
    """Export audio with an explicitly owned output handle."""
    if cancel_check is not None:
        from audio_export import export_audio_segment
        return export_audio_segment(segment, path, format_name, cancel_check, **kwargs)
    with open(path, "wb+") as target:
        segment.export(target, format=format_name, **kwargs)
CHAPTER_EXPORT_DIR = "chapter_exports"
DEFAULT_CHAPTER_TEMPLATE = "{chapter_number} - {chapter_name}"
CHAPTER_TEMPLATE_FIELDS = ("chapter_number", "chapter_name", "book_name",
                           "series_name", "volume_number")


def get_chapter_export_rows(manifest):
    """Return a usable whole chapter row set, or an empty set for old/corrupt data."""
    if not isinstance(manifest, dict):
        return []
    rows = manifest.get("chapters", [])
    if not isinstance(rows, list):
        return []
    indices = set()
    for row in rows:
        if not isinstance(row, dict):
            return []
        index = row.get("index")
        filename = row.get("file")
        if (type(index) is not int or index < 0 or index in indices
                or not isinstance(filename, str) or not filename):
            return []
        indices.add(index)
    return list(rows)


def build_chapter_filename(template, number, title, ext, padding=2, book_name="",
                           series_name="", volume_number=""):
    """One chapter's filename from a template such as
    "{chapter_number} - {chapter_name}". Unknown fields are left as text rather
    than raising, the number is zero-padded to `padding` digits, and the
    result goes through secure_filename so a chapter called "Part 1/2: ?!"
    cannot escape the export directory or fail on the filesystem. The
    extension is added here, never by the template."""
    if not isinstance(ext, str) or not re.fullmatch(r"[A-Za-z0-9]+", ext):
        raise ValueError("Invalid chapter filename extension")
    values = {"chapter_number": str(number).zfill(max(int(padding or 0), 0)),
              "chapter_name": (title or "").strip() or f"chapter {number}",
              "book_name": book_name or "", "series_name": series_name or "",
              "volume_number": str(volume_number or "")}
    name = template or DEFAULT_CHAPTER_TEMPLATE
    for field, value in values.items():
        name = name.replace("{" + field + "}", value)
    name = secure_filename(re.sub(r"\s+", " ", name).strip(" ._-")) or f"chapter_{number}"
    return f"{name}.{ext}"


def build_chapter_filenames(groups, template, ext, padding=2, book_name="",
                            series_name="", volume_number=""):
    """Build one unique filename per chapter group or reject the template.

    A custom template may omit ``{chapter_number}``, and chapter titles may
    repeat. Writing both groups to one path silently replaces the earlier
    chapter, so uniqueness is an export precondition rather than a best-effort
    cleanup after audio has already been rendered.
    """
    filenames = [build_chapter_filename(
        template, index + 1, title, ext, padding=padding,
        book_name=book_name, series_name=series_name,
        volume_number=volume_number)
        for index, (title, _, _) in enumerate(groups)]
    seen, duplicates = set(), set()
    for name in filenames:
        key = name.casefold()
        if key in seen:
            duplicates.add(name)
        seen.add(key)
    duplicates = sorted(duplicates)
    if duplicates:
        raise ValueError(
            "Chapter template produces duplicate filenames: "
            + ", ".join(duplicates)
            + ". Include {chapter_number} to make each chapter unique.")
    return filenames


def _loading_progress(progress_callback):
    """Adapt a message-taking progress callback to the loader's (done, total)."""
    if not progress_callback:
        return None
    return lambda done, total: progress_callback(f"Loading audio {done}/{total}")

class ProjectManager:
    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.script_path = os.path.join(root_dir, "annotated_script.json")
        self.chunks_path = os.path.join(root_dir, "chunks.json")
        self.voicelines_dir = os.path.join(root_dir, "voicelines")
        self.voice_config_path = os.path.join(root_dir, "voice_config.json")
        # config.json placement must match app.py's get_app_config_path exactly:
        # legacy app/config.json when the data dir is the repo root, else
        # <data_dir>/config.json. root_dir here IS the data dir (ProjectManager is
        # constructed with DATA_DIR), so resolve against this file's own repo
        # root/app dir — NOT root_dir/app, which points at <data_dir>/app and
        # silently misses the config when ALEXANDRIA_DATA_DIR relocates data.
        _app_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_path = get_app_config_path(root_dir, os.path.dirname(_app_dir), _app_dir)

        # Ensure voicelines dir exists
        os.makedirs(self.voicelines_dir, exist_ok=True)

        self.engine = None
        self._engine_lock = threading.Lock()
        self._chunks_lock = threading.Lock()  # Thread-safe chunks.json writes
        self._uids_backfilled = False  # see _read_chunks
        self._uids_file_version = None
        self._status_journal = None
        self._active_journal_identity = None
        self._config_cache = None
        self._config_mtime = None
        self._config_lock = threading.Lock()

    def invalidate_config_cache(self):
        """Force the next config read to reload config.json from disk."""
        with self._config_lock:
            self._config_cache = None
            self._config_mtime = None

    def _read_config(self):
        """Load config.json, caching by mtime to avoid repeated disk reads/parses
        across hot-path calls (get_engine, _load_tts_config, ...)."""
        with self._config_lock:
            try:
                mtime = os.path.getmtime(self.config_path)
            except OSError:
                mtime = None
            if self._config_cache is None or mtime != self._config_mtime:
                self._config_cache = load_app_config(self.config_path)
                self._config_mtime = mtime
            return self._config_cache

    def get_engine(self):
        with self._engine_lock:
            if self.engine:
                return self.engine

            config = self._read_config()

            try:
                self.engine = TTSEngine(config)
                print(f"TTS engine initialized (mode={self.engine.mode})")
                return self.engine
            except Exception as e:
                print(f"Failed to initialize TTS engine: {e}")
                return None

    def _load_tts_config(self):
        """Load TTS config section from config.json for pause defaults."""
        return self._read_config().get("tts", {})

    def load_chunks(self):
        """Load chunks from disk, regenerating if missing or corrupted.
        
        Uses _chunks_lock to prevent race conditions with concurrent saves.
        """
        with self._chunks_lock, file_lock(self.chunks_path):
            return self._read_chunks()

    def _get_chunks_file_version(self):
        """Identify the file whose rows have already received stable UIDs."""
        try:
            stat = os.stat(self.chunks_path)
        except OSError:
            return None
        return (stat.st_dev, stat.st_ino, stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns)

    def _save_chunks_locked(self, chunks, uids_backfilled=False):
        """Publish rows and retain UID validation only for known-valid writes."""
        if os.path.exists(get_chunk_status_journal_path(self.chunks_path)):
            self._get_status_journal_locked().save_compacted(
                chunks, save_snapshot=lambda rows: atomic_json_write(rows, self.chunks_path))
            self._status_journal = None
        else:
            atomic_json_write(chunks, self.chunks_path)
        self._uids_backfilled = uids_backfilled
        self._uids_file_version = (self._get_chunks_file_version()
                                   if uids_backfilled else None)

    def _read_chunks(self):
        """Read chunks.json, regenerating from script if missing or corrupted.

        Internal method - callers must hold _chunks_lock. Always returns a
        list (possibly empty), never None.
        """
        if os.path.exists(get_chunk_status_journal_path(self.chunks_path)):
            return self._get_status_journal_locked().get_rows()
        file_version = self._get_chunks_file_version()
        chunks = safe_load_json(self.chunks_path)
        if not isinstance(chunks, list) or not all(isinstance(c, dict) for c in chunks):
            chunks = None
        if chunks is not None:
            # Retain the backfill cache across ordinary status writes, but
            # revalidate a replaced saved book or an externally edited file.
            if (not self._uids_backfilled
                    or file_version != self._uids_file_version
                    or file_version != self._get_chunks_file_version()):
                changed = False
                for chunk in chunks:
                    if not chunk.get("uid"):
                        chunk["uid"] = _new_chunk_uid()
                        changed = True
                if changed:
                    self._save_chunks_locked(chunks, uids_backfilled=True)
                else:
                    after_version = self._get_chunks_file_version()
                    self._uids_backfilled = file_version == after_version
                    self._uids_file_version = (after_version
                                               if self._uids_backfilled else None)
            return chunks
        if os.path.exists(self.chunks_path):
            # Back the corrupted file up rather than deleting it - regenerating
            # resets every chunk to pending/no-audio, so keep a copy in case the
            # user's generation progress can be salvaged from it.
            backup = self.chunks_path + ".corrupt"
            suffix = 0
            while os.path.lexists(backup):
                suffix += 1
                backup = self.chunks_path + f".corrupt.{suffix}"
            logger.warning("chunks.json is corrupted; backing it up to %s and regenerating.", backup)
            try:
                os.replace(self.chunks_path, backup)
            except OSError as error:
                raise OSError(
                    f"Cannot preserve corrupted chunks at {self.chunks_path} "
                    f"in {backup}; refusing regeneration. Preserve the original "
                    "file and resolve the backup failure before retrying."
                ) from error

        # If no chunks (or corrupted), generate from script
        if os.path.exists(self.script_path):
            try:
                with open(self.script_path, "r", encoding="utf-8") as f:
                    script = json.load(f)
                if not isinstance(script, list) or not all(isinstance(entry, dict) for entry in script):
                    raise ValueError("expected an array of objects")
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"annotated_script.json is also corrupted ({e}). Starting with empty chunks.")
                return []

            review = []
            chunks = group_into_chunks(script, review_sink=review)
            log_review_characters(review)

            # Initialize chunk status
            for i, chunk in enumerate(chunks):
                chunk["id"] = i
                chunk["uid"] = _new_chunk_uid()
                chunk["status"] = "pending"  # pending, generating, done, error
                chunk["audio_path"] = None

            self._save_chunks_locked(chunks, uids_backfilled=True)
            return chunks

        return []

    def _get_status_journal_locked(self):
        try:
            with open(os.path.join(self.root_dir, "state.json"), encoding="utf-8") as handle:
                state = json.load(handle)
        except FileNotFoundError:
            state = {}
        if not isinstance(state, dict):
            raise ValueError("Invalid book identity for chunk status journal")
        identity = {key: state.get(key) for key in ("active_book_id", "book_generation")}
        if self._active_journal_identity is not None and identity != self._active_journal_identity:
            raise ValueError(GENERATION_INPUTS_CHANGED)
        if self._status_journal is None or self._status_journal.book_identity != identity:
            self._status_journal = ChunkStatusJournal(self.chunks_path, identity)
        return self._status_journal

    def _resolve_alias(self, speaker, voice_config):
        """Resolve speaker aliases by following `alias_of` chain in voice_config.

        Prevent infinite loops by limiting chain length.
        Returns the canonical speaker name (string).
        """
        if not speaker:
            return speaker
        name = speaker
        seen = set()
        for _ in range(16):  # Increased from 8 to handle longer alias chains
            if name in seen:
                logger.warning(f"Alias cycle detected for speaker '{speaker}': chain visited {seen}")
                break
            seen.add(name)
            entry = voice_config.get(name, {})
            alias = entry.get('alias_of') or entry.get('alias')
            if not alias:
                break
            # If alias is empty or same, stop
            if not isinstance(alias, str) or alias.strip() == '' or alias == name:
                break
            # Continue resolution
            name = alias
        else:
            # Loop completed without break - chain exceeded limit
            logger.warning(f"Alias chain for '{speaker}' exceeded 16 iterations; using last resolved name '{name}'")
        return name

    def save_chunks(self, chunks):
        with self._chunks_lock, file_lock(self.chunks_path):
            self._save_chunks_locked(chunks)

    def _modify_chunk(self, index, mutator):
        """Atomically apply `mutator(chunk)` to a single chunk (thread-safe read-modify-write).

        Unlike load_chunks() + modify + save_chunks(), this holds the lock for the
        entire read-modify-write cycle, preventing concurrent threads from
        overwriting each other's updates.
        """
        with self._chunks_lock, file_lock(self.chunks_path):
            chunks = self._read_chunks()
            if not (0 <= index < len(chunks)):
                return None
            chunk = chunks[index]
            mutator(chunk)
            self._save_chunks_locked(chunks, uids_backfilled=bool(chunk.get("uid")))
            return chunk

    def insert_chunk(self, after_index):
        """Insert an empty chunk after the given index. Returns the new chunk list."""
        with self._chunks_lock, file_lock(self.chunks_path):
            chunks = self._read_chunks()
            if not (0 <= after_index < len(chunks)):
                return None

            # Copy speaker from the row we're splitting from
            source = chunks[after_index]
            new_chunk = {
                "id": after_index + 1,
                "uid": _new_chunk_uid(),
                "speaker": source.get("speaker", "NARRATOR"),
                "text": "",
                "instruct": "",
                "status": "pending",
                "audio_path": None
            }
            chunks.insert(after_index + 1, new_chunk)

            # Re-number all IDs
            for i, chunk in enumerate(chunks):
                chunk["id"] = i

            self._save_chunks_locked(chunks, uids_backfilled=True)
            return chunks

    def delete_chunk(self, index):
        """Delete a chunk at the given index. Returns (deleted_chunk, updated_chunks) or None."""
        with self._chunks_lock, file_lock(self.chunks_path):
            chunks = self._read_chunks()
            if not (0 <= index < len(chunks)):
                return None
            if len(chunks) <= 1:
                return None  # don't allow deleting the last chunk

            deleted = chunks.pop(index)

            # Re-number all IDs
            for i, chunk in enumerate(chunks):
                chunk["id"] = i

            self._save_chunks_locked(chunks, uids_backfilled=True)
            return deleted, chunks

    def restore_chunk(self, at_index, chunk_data):
        """Re-insert a chunk at a specific index. Returns the updated chunk list."""
        if not isinstance(chunk_data, dict):
            return None
        audio_path = chunk_data.get("audio_path")
        if audio_path and not self._is_project_audio_path(audio_path):
            return None
        with self._chunks_lock, file_lock(self.chunks_path):
            chunks = self._read_chunks()

            chunk_data = copy.deepcopy(chunk_data)
            uid = chunk_data.get("uid")
            if uid and any(chunk.get("uid") == uid for chunk in chunks):
                return None
            at_index = max(0, min(at_index, len(chunks)))
            if isinstance(chunk_data, dict):
                chunk_data.setdefault("uid", _new_chunk_uid())
            chunks.insert(at_index, chunk_data)

            # Re-number all IDs
            for i, chunk in enumerate(chunks):
                chunk["id"] = i

            self._save_chunks_locked(chunks, uids_backfilled=bool(chunk_data.get("uid")))
            return chunks

    def _is_project_audio_path(self, path):
        """Whether a persisted chunk audio path remains inside this project."""
        return isinstance(path, str) and is_path_inside(
            os.path.join(self.root_dir, path), self.root_dir)

    def update_chunk(self, index, data):
        """Thread-safe chunk update. See _modify_chunk."""
        def mutator(chunk):
            render_changed = any(field in data and chunk.get(field, "") != data[field]
                                 for field in ("text", "instruct", "speaker"))
            if "text" in data: chunk["text"] = data["text"]
            if "instruct" in data: chunk["instruct"] = data["instruct"]
            if "speaker" in data: chunk["speaker"] = data["speaker"]

            # pause_after: set or clear (None removes the key)
            if "pause_after" in data:
                if data["pause_after"] is not None:
                    chunk["pause_after"] = max(0, int(data["pause_after"]))
                else:
                    chunk.pop("pause_after", None)

            # Detach the previous render when its inputs change; keep the file
            # on disk, but never export it under the edited text or speaker.
            if render_changed:
                chunk["status"] = "pending"
                chunk["audio_path"] = None

        return self._modify_chunk(index, mutator)

    def _update_chunk_fields(self, index, **kwargs):
        """Atomically set arbitrary fields on a chunk (status, audio_path, error, etc.).

        Unlike update_chunk, this applies the given fields directly with no
        special-casing or status side-effects. See _modify_chunk.
        """
        def mutator(chunk):
            chunk.update(kwargs)

        return self._modify_chunk(index, mutator)

    def _update_chunk_fields_by_uid(self, uid, expected_chunk=None, required_status=None,
                                    audio_source=None, **kwargs):
        """Conditionally publish to the live UID under one lock, preserving other rows.

        Generation compares only captured render inputs. An owned converted temp
        file is promoted under this same guard, after conversion outside the lock.
        Existing non-generation callers retain the UID-only update contract.
        """
        if not uid:
            return None
        with self._chunks_lock, file_lock(self.chunks_path):
            use_journal = (self._active_journal_identity is not None
                           or os.path.exists(get_chunk_status_journal_path(self.chunks_path)))
            journal = self._get_status_journal_locked() if use_journal else None
            chunks = None if journal else self._read_chunks()
            chunk = (journal.get_row(uid) if journal
                     else next((item for item in chunks if item.get("uid") == uid), None))
            if chunk is None:
                return None
            if expected_chunk is not None and (
                    get_chunk_generation_inputs(chunk) != get_chunk_generation_inputs(expected_chunk)):
                return None
            if required_status is not None and chunk.get("status") != required_status:
                return None
            if audio_source is not None:
                publish_audio_output(audio_source, self.get_chunk_audio_path(kwargs["audio_path"]))
            if kwargs:
                if journal:
                    chunk = journal.apply_update(uid, kwargs)
                else:
                    chunk.update(kwargs)
                    self._save_chunks_locked(chunks, uids_backfilled=bool(chunk.get("uid")))
            return chunk

    def _publish_generated_chunk_audio(self, chunk, temp_path, speaker):
        """Convert privately, then attach/promote audio only to unchanged inputs."""
        staged_path = None
        try:
            staged_audio = self._export_chunk_audio(temp_path, f".render-{uuid.uuid4().hex}")
            staged_path = self.get_chunk_audio_path(staged_audio)
            extension = os.path.splitext(staged_audio)[1]
            audio_path = f"voicelines/voiceline_{chunk['uid']}_{sanitize_filename(speaker)}{extension}"
            updated = self._update_chunk_fields_by_uid(
                chunk["uid"], expected_chunk=chunk, audio_source=staged_path,
                status="done", audio_path=audio_path, audio_revision=uuid.uuid4().hex, error=None, drift=None)
            if updated is None:
                raise ValueError(GENERATION_INPUTS_CHANGED)
            return audio_path
        finally:
            if staged_path:
                self._remove_temp_file(staged_path)

    def _reset_captured_generations(self, captured, indices, done_indices):
        """Reset generating owners; count unfinished requests and report stale inputs."""
        count, failed = 0, []
        for idx in indices:
            if idx in done_indices:
                continue
            chunk = get_captured_chunk(captured, idx)
            current = (self._update_chunk_fields_by_uid(chunk.get("uid"), expected_chunk=chunk)
                       if chunk else None)
            if current is None:
                failed.append((idx, GENERATION_INPUTS_CHANGED))
            elif current.get("status") == "generating":
                updated = self._update_chunk_fields_by_uid(
                    chunk["uid"], expected_chunk=chunk, required_status="generating", status="pending")
                if updated is None:
                    failed.append((idx, GENERATION_INPUTS_CHANGED))
                else:
                    count += 1
            else:
                # A cancelled queued worker never changed the captured row.
                count += 1
        return count, failed

    def generate_chunk_audio(self, index):
        chunks = self.load_chunks()
        if not (0 <= index < len(chunks)):
            return False, "Invalid chunk index"
        return self._generate_captured_chunk_audio(index, copy.deepcopy(chunks[index]))

    def _generate_captured_chunk_audio(self, index, chunk):
        if not chunk:
            return False, "Invalid chunk index"
        updated = self._update_chunk_fields_by_uid(
            chunk.get("uid"), expected_chunk=chunk, status="generating", error=None)
        if updated is None:
            return False, GENERATION_INPUTS_CHANGED

        temp_path = None
        try:
            engine = self.get_engine()
            if not engine:
                message = "TTS engine not initialized"
                updated = self._update_chunk_fields_by_uid(
                    chunk["uid"], expected_chunk=chunk, status="error", error=message)
                return False, message if updated is not None else GENERATION_INPUTS_CHANGED

            # atomic_json_write's write-temp+rename makes plain reads safe even
            # while app.py's voice_library endpoints hold file_lock(voice_config_path)
            # for a read-modify-write — no extra locking needed here.
            voice_config = safe_load_json(self.voice_config_path, default={})

            speaker = chunk["speaker"]
            # Resolve aliases to canonical speaker used for TTS
            canonical_speaker = self._resolve_alias(speaker, voice_config)
            if canonical_speaker != speaker:
                print(f"Resolving alias: '{speaker}' -> '{canonical_speaker}'")
            speaker_to_use = canonical_speaker
            text = chunk["text"]
            instruct = chunk.get("instruct", "")

            print(f"Generating chunk {index}: speaker={speaker}, instruct='{instruct}', text='{text[:50]}...'")

            # Generate to temp file (unique per chunk for parallel processing)
            import tempfile
            fd, temp_path = tempfile.mkstemp(prefix=f"chunk_{index}_", suffix=".wav", dir=self.root_dir)
            os.close(fd)  # Close the file descriptor, we'll write via TTS engine

            # Pass canonical speaker to the TTS engine so it uses the aliased config;
            # the identity anchor in force at this line (a character can change
            # from a point in the book - tts.active_character_style)
            generation_args = (text, instruct, speaker_to_use,
                voice_config_for_chunk(voice_config, speaker_to_use, index), temp_path)
            failure = None
            if callable(getattr(engine, "generate_voice_result", None)):
                result = engine.generate_voice_result(*generation_args)
                success, failure = result.success, result.failure
            else:
                success = engine.generate_voice(*generation_args)

            if success:
                validate_generated_audio(
                    temp_path, f"chunk {index} ({speaker_to_use})")

                print(f"Generated WAV size: {os.path.getsize(temp_path)} bytes")

                audio_path = self._publish_generated_chunk_audio(
                    chunk, temp_path, speaker_to_use)

                return True, audio_path
            else:
                message = failure.get_message() if failure else "Generation returned False"
                updated = self._update_chunk_fields_by_uid(
                    chunk["uid"], expected_chunk=chunk, status="error", error=message)
                return False, message if updated is not None else GENERATION_INPUTS_CHANGED

        except Exception as e:
            try:
                updated = self._update_chunk_fields_by_uid(
                    chunk["uid"], expected_chunk=chunk, status="error", error=str(e))
                if updated is None:
                    return False, GENERATION_INPUTS_CHANGED
            except Exception as update_err:
                print(f"Warning: Failed to update chunk {index} status to error: {update_err}")
            return False, str(e)
        finally:
            # Cleanup runs on every exit path - success, early validation returns,
            # and exceptions - so the temp wav never leaks.
            if temp_path:
                self._remove_temp_file(temp_path)

    def _load_pause_defaults(self):
        """Return (pause_between_speakers_ms, pause_same_speaker_ms) from config."""
        tts_cfg = self._load_tts_config()
        return (
            tts_cfg.get("pause_between_speakers_ms", DEFAULT_PAUSE_MS),
            tts_cfg.get("pause_same_speaker_ms", SAME_SPEAKER_PAUSE_MS),
        )

    def get_chunk_audio_path(self, path):
        """Resolve persisted audio only within this project's data directory."""
        if not isinstance(path, str) or not path:
            raise ValueError("Invalid chunk audio path")
        full_path = os.path.realpath(os.path.join(self.root_dir, path))
        if not is_path_inside(full_path, self.root_dir):
            raise ValueError("Chunk audio path escapes the project directory")
        return full_path

    def get_chunk_audio_segment(self, chunk):
        """Decode one admitted audio file, or report an unreadable chunk as absent."""
        path = chunk.get("audio_path")
        if not path:
            return None
        try:
            full_path = self.get_chunk_audio_path(path)
            if not os.path.exists(full_path):
                return None
            extension = os.path.splitext(full_path)[1].lstrip(".").lower()
            load_kwargs = {}
            # OGG may contain several codecs; let its decoder inspect the file.
            if extension in ("mp3", "wav", "flac"):
                load_kwargs = {"format": extension}
                if extension != "wav":
                    load_kwargs["codec"] = extension
            return _load_audio_segment(full_path, **load_kwargs)
        except Exception as error:
            print(f"Error loading audio segment {path}: {error}")
            return None

    def _load_chunks_with_audio(self, cancel_check=None, progress_callback=None, chunks=None):
        """Load chunks and pair each with its AudioSegment.

        Returns (result, skipped_count): result is the list of (chunk, segment)
        pairs that loaded successfully; skipped_count is how many chunks were
        dropped (missing audio_path, missing file, or a failed audio load) so
        callers can report a partial export instead of a silent blanket success.
        """
        chunks = self.load_chunks() if chunks is None else chunks
        result = []
        skipped = 0
        for position, chunk in enumerate(chunks):
            if cancel_check and cancel_check():
                raise ExportCancelled()
            if progress_callback and position and position % 50 == 0:
                progress_callback(position, len(chunks))
            segment = self.get_chunk_audio_segment(chunk)
            if segment is not None:
                result.append((chunk, segment))
            else:
                skipped += 1
        return result, skipped

    def merge_audio(self, cancel_check=None, progress_callback=None, chunks=None):
        """progress_callback(message) reports loading progress; cancel_check()
        returning True aborts before the merge is written."""
        try:
            chunks_with_audio, skipped = self._load_chunks_with_audio(
                cancel_check=cancel_check,
                progress_callback=_loading_progress(progress_callback), chunks=chunks)
        except ExportCancelled:
            return False, "Merge cancelled"
        if not chunks_with_audio:
            return False, "No audio segments found"

        pause_ms, same_speaker_pause_ms = self._load_pause_defaults()
        timeline = compute_timeline(chunks_with_audio, pause_ms, same_speaker_pause_ms)

        # Build final audio from timeline
        audio_segments = [seg for _, seg, _ in timeline]
        speakers = [chunk["speaker"] for chunk, _, _ in timeline]
        pause_overrides = [chunk.get("pause_after") for chunk, _, _ in timeline]

        final_audio = combine_audio_with_pauses(
            audio_segments, speakers, pause_ms, same_speaker_pause_ms, pause_overrides
        )
        output_filename = "cloned_audiobook.mp3"
        output_path = os.path.join(self.root_dir, output_filename)
        pending_output = output_path + f".pending.{uuid.uuid4().hex}"
        try:
            _export_audio_segment(final_audio, pending_output, "mp3", bitrate=MP3_BITRATE)
            os.replace(pending_output, output_path)
        finally:
            self._remove_temp_file(pending_output)

        if skipped:
            return True, f"{output_filename} ({skipped} chunk(s) skipped — missing/corrupt audio)"
        return True, output_filename

    def export_audacity(self, progress_callback=None, cancel_check=None):
        """Export project as an Audacity-compatible zip with per-speaker WAV tracks,
        a LOF file for auto-import, and a labels file for chunk annotations."""
        try:
            chunks_with_audio, skipped = self._load_chunks_with_audio(
                cancel_check=cancel_check,
                progress_callback=_loading_progress(progress_callback))
        except ExportCancelled:
            return False, "Export cancelled"
        if not chunks_with_audio:
            return False, "No audio segments found"

        # Phase 1 — Compute timeline
        pause_ms, same_speaker_pause_ms = self._load_pause_defaults()
        try:
            timeline = compute_timeline(chunks_with_audio, pause_ms, same_speaker_pause_ms,
                                        cancel_check=cancel_check)
        except ExportCancelled:
            return False, "Export cancelled"

        if not timeline:
            return False, "No audio segments found"

        # Total duration = last chunk's start + its length
        _, last_seg, last_start = timeline[-1]
        total_duration_ms = last_start + len(last_seg)

        # Phase 2 — Group once, retaining only the already-loaded segments.
        speaker_chunks = {}
        for chunk, segment, start_ms in timeline:
            speaker_chunks.setdefault(chunk["speaker"], []).append((segment, start_ms))
        speakers_ordered = list(speaker_chunks)

        # Phase 3 — Build LOF and labels content
        speaker_filenames = {}
        used_filenames = set()
        for speaker in speakers_ordered:
            base_name = sanitize_filename(speaker)
            safe_name = base_name
            suffix = 2
            while safe_name in used_filenames:
                safe_name = f"{base_name}_{suffix}"
                suffix += 1
            speaker_filenames[speaker] = safe_name
            used_filenames.add(safe_name)

        lof_lines = []
        for speaker in speakers_ordered:
            lof_lines.append(f'file "{speaker_filenames[speaker]}.wav"')
        lof_content = "\n".join(lof_lines) + "\n"

        label_lines = []
        for chunk, segment, start_ms in timeline:
            start_sec = start_ms / 1000.0
            end_sec = (start_ms + len(segment)) / 1000.0
            text_preview = chunk.get("text", "")[:80]
            label = f"[{chunk['speaker']}] {text_preview}"
            # Audacity labels are tab-separated, one per line - a tab or newline
            # inside the text would add a phantom column or split the row.
            label = label.replace("\t", " ").replace("\r", " ").replace("\n", " ")
            label_lines.append(f"{start_sec:.6f}\t{end_sec:.6f}\t{label}")
        labels_content = "\n".join(label_lines) + "\n"

        # Phase 4 — Zip everything to a temp path, then atomically replace, so a
        # failure mid-write can't leave a truncated zip the download route serves
        # as valid (or clobber a previous good export with garbage).
        zip_path = os.path.join(self.root_dir, "audacity_export.zip")
        tmp_zip = zip_path + f".pending.{uuid.uuid4().hex}"
        try:
            ensure_audio_export_active(cancel_check)
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("project.lof", lof_content)
                zf.writestr("labels.txt", labels_content)

                for speaker in speakers_ordered:
                    if progress_callback:
                        progress_callback(f"Writing track: {speaker}")
                    ensure_audio_export_active(cancel_check)
                    track_cursor = 0
                    track = AudioSegment.empty()
                    for segment, start_ms in speaker_chunks[speaker]:
                        ensure_audio_export_active(cancel_check)
                        gap = start_ms - track_cursor
                        if gap > 0:
                            track += AudioSegment.silent(duration=gap)
                        track += segment
                        track_cursor = start_ms + len(segment)
                    remaining = total_duration_ms - track_cursor
                    if remaining > 0:
                        track += AudioSegment.silent(duration=remaining)

                    safe_name = speaker_filenames[speaker]
                    with io.BytesIO() as wav_buffer:
                        track.export(wav_buffer, format="wav")
                        ensure_audio_export_active(cancel_check)
                        zf.writestr(f"{safe_name}.wav", wav_buffer.getvalue())
                    del track
            ensure_audio_export_active(cancel_check)
            os.replace(tmp_zip, zip_path)
        except BaseException as error:
            try:
                if os.path.exists(tmp_zip):
                    os.remove(tmp_zip)
            except OSError:
                pass
            if isinstance(error, ExportCancelled):
                return False, "Export cancelled"
            raise

        if skipped:
            return True, f"{zip_path} ({skipped} chunk(s) skipped — missing/corrupt audio)"
        return True, zip_path

    def merge_m4b(self, per_chunk_chapters=False, metadata=None, cancel_check=None, progress_callback=None):
        """Merge audio chunks into an M4B audiobook with chapter markers.

        Args:
            per_chunk_chapters: If True, each chunk is a chapter. If False,
                detect chapter headings and group chunks into sections.
            metadata: Optional dict with keys: title, author, narrator, year,
                description, cover_path (absolute path to cover image).

        Returns:
            tuple: (success: bool, message: str)
        """
        metadata = metadata or {}
        try:
            chunks_with_audio, skipped = self._load_chunks_with_audio(cancel_check=cancel_check,
                    progress_callback=_loading_progress(progress_callback))
            if not chunks_with_audio:
                return False, "No audio segments found"

            # Phase 1 — Compute timeline
            pause_ms, same_speaker_pause_ms = self._load_pause_defaults()
            timeline = compute_timeline(chunks_with_audio, pause_ms, same_speaker_pause_ms, cancel_check=cancel_check)

            if not timeline:
                return False, "No audio segments found"

            # Phase 2 — Build chapters
            chapters = self._build_m4b_chapters(timeline, per_chunk_chapters)
            print(f"  M4B: {len(chapters)} chapters")

            # Phase 3 — Combine audio and export to temp WAV
            audio_segments = [seg for _, seg, _ in timeline]
            speakers = [chunk["speaker"] for chunk, _, _ in timeline]
            pause_overrides = [chunk.get("pause_after") for chunk, _, _ in timeline]
            final_audio = combine_audio_with_pauses(
                audio_segments, speakers, pause_ms, same_speaker_pause_ms, pause_overrides, cancel_check=cancel_check
            )

        except ExportCancelled:
            return False, "Export cancelled"

        staging_id = uuid.uuid4().hex
        temp_wav = os.path.join(self.root_dir, f".m4b-{staging_id}.wav")
        meta_path = os.path.join(self.root_dir, f".m4b-{staging_id}.txt")
        output_path = os.path.join(self.root_dir, "audiobook.m4b")
        pending_output = output_path + f".pending.{uuid.uuid4().hex}"

        try:
            _export_audio_segment(final_audio, temp_wav, "wav", cancel_check=cancel_check)

            # Phase 4 — Write FFmpeg metadata file with book metadata
            meta_lines = [";FFMETADATA1"]
            meta_lines.append(f"title={self._escape_ffmeta(metadata.get('title') or 'Audiobook')}")
            meta_lines.append(f"artist={self._escape_ffmeta(metadata.get('author') or '')}")
            meta_lines.append(f"album_artist={self._escape_ffmeta(metadata.get('narrator') or '')}")
            meta_lines.append(f"date={self._escape_ffmeta(metadata.get('year') or '')}")
            meta_lines.append(f"comment={self._escape_ffmeta(metadata.get('description') or '')}")
            meta_lines.append("genre=Audiobook")
            meta_lines.append("")
            for title, start_ms, end_ms in chapters:
                safe_title = self._escape_ffmeta(title)
                meta_lines.append("[CHAPTER]")
                meta_lines.append("TIMEBASE=1/1000")
                meta_lines.append(f"START={start_ms}")
                meta_lines.append(f"END={end_ms}")
                meta_lines.append(f"title={safe_title}")
                meta_lines.append("")

            with open(meta_path, "w", encoding="utf-8") as f:
                f.write("\n".join(meta_lines))

            # Phase 5 — FFmpeg: WAV + chapters → M4B (AAC)
            cover_path = metadata.get("cover_path") or ""
            has_cover = cover_path and os.path.exists(cover_path)

            cmd = ["ffmpeg", "-y", "-i", temp_wav]
            if has_cover:
                cmd += ["-i", cover_path]
            cmd += ["-i", meta_path, "-map_metadata", "2" if has_cover else "1"]
            # Map audio stream
            cmd += ["-map", "0:a"]
            if has_cover:
                # Map cover as attached picture
                cmd += ["-map", "1:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
            cmd += [
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                "-f", "mp4",
                pending_output
            ]
            from m4b_encode import encode_m4b
            result, stderr = encode_m4b(cmd, len(final_audio) / 1000, cancel_check, progress_callback)
            if result != 0:
                if progress_callback:
                    progress_callback("FFmpeg stderr: " + stderr[-500:])
                print(f"FFmpeg stderr: {stderr[-500:]}")
                return False, f"FFmpeg failed (exit {result})"
            ensure_audio_export_active(cancel_check)
            os.replace(pending_output, output_path)

        except ExportCancelled:
            return False, "Export cancelled"

        finally:
            # Only the unpublished staging output is ever removed here; a prior
            # complete audiobook remains available when an encode fails.
            cleanup = [temp_wav, meta_path, pending_output]
            for tmp in cleanup:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        if skipped:
            return True, f"audiobook.m4b ({skipped} chunk(s) skipped — missing/corrupt audio)"
        return True, "audiobook.m4b"

    @staticmethod
    def _escape_ffmeta(text):
        """Escape special characters for FFmpeg metadata format."""
        text = text.replace("\\", "\\\\")
        text = text.replace("=", "\\=")
        text = text.replace(";", "\\;")
        text = text.replace("#", "\\#")
        text = text.replace("\n", " ")
        text = text.replace("\r", " ")  # CRLF (e.g. pasted description) would else corrupt the key=value line
        return text

    # Regex for detecting chapter/section headings in chunk text
    _HEADING_RE = re.compile(
        r'^(chapter|part|book|volume|prologue|epilogue|introduction|conclusion|act|section)\b',
        re.IGNORECASE
    )

    def _chapter_groups(self, chunks, per_chunk_chapters):
        """Chapter boundaries from headings and narrator fragments: list of
        (title, first_index, last_index) over `chunks`. The one place chapter
        structure is decided, shared by the M4B export and the per-chapter
        export (and by filename previews, which have no audio to hand).
        """
        if per_chunk_chapters:
            return [(f"[{c['speaker']}] {c.get('text', '')[:80]}", i, i)
                    for i, c in enumerate(chunks)]

        # Smart grouping: detect chapter headings
        heading_indices = []
        for i, chunk in enumerate(chunks):
            text = chunk.get("text", "").strip()
            # Preserve explicit headings and unquoted narrator titles, while
            # keeping short dialogue and complete sentences in their chapter.
            if self._HEADING_RE.match(text):
                heading_indices.append(i)
            elif (get_speaker(chunk).upper() == "NARRATOR" and text
                  and '"' not in text and _is_structural_text(text)):
                heading_indices.append(i)

        # If no headings detected, fall back to per-chunk
        if not heading_indices:
            print("  Chapters: no chapter headings detected, falling back to per-chunk chapters")
            return self._chapter_groups(chunks, per_chunk_chapters=True)

        groups = []
        # Pre-heading chunks → "Introduction"
        if heading_indices[0] > 0:
            groups.append(("Introduction", 0, heading_indices[0] - 1))
        # Each heading starts a chapter that runs until the next heading
        for idx, head_i in enumerate(heading_indices):
            title = chunks[head_i].get("text", "").strip()
            if len(title) > 120:
                title = title[:117] + "..."
            last = (heading_indices[idx + 1] - 1 if idx + 1 < len(heading_indices)
                    else len(chunks) - 1)
            groups.append((title, head_i, last))
        return groups

    def _build_m4b_chapters(self, timeline, per_chunk_chapters):
        """Build chapter list from timeline entries.

        Returns:
            list of (title, start_ms, end_ms) tuples
        """
        chunks = [chunk for chunk, _, _ in timeline]
        chapters = []
        for title, first, last in self._chapter_groups(chunks, per_chunk_chapters):
            start_ms = timeline[first][2]
            end_ms = timeline[last][2] + len(timeline[last][1])
            chapters.append((title, start_ms, end_ms))
        return chapters

    def get_chapter_audio(self, pairs, pause_defaults, cancel_check=None):
        """Render one chapter using the same pauses for full and changed exports."""
        pause_ms, same_speaker_pause_ms = pause_defaults
        return combine_audio_with_pauses(
            [segment for _, segment in pairs],
            [chunk["speaker"] for chunk, _ in pairs],
            pause_ms, same_speaker_pause_ms,
            [chunk.get("pause_after") for chunk, _ in pairs],
            cancel_check=cancel_check)

    def _get_chapter_export_plans(self, chunks, options, pause_defaults):
        """Plan shared names, source ranges and fingerprints without publication."""
        groups = self._chapter_groups(chunks, options["per_chunk_chapters"])
        filenames = build_chapter_filenames(
            groups, options["template"], options["format"], padding=options["padding"],
            book_name=options["book_name"], series_name=options["series_name"],
            volume_number=options["volume_number"])
        return [{"index": index, "number": index + 1, "title": title,
                 "file": filenames[index], "chunks": [first, last],
                 "fingerprint": self._chapter_fingerprint(
                     chunks[first:last + 1], pause_defaults)}
                for index, (title, first, last) in enumerate(groups)]

    def _apply_chapter_export_plans(self, plans, chunks, options, pause_defaults,
                                    previous, chapters, changed_only, get_pairs,
                                    durations=None, progress_callback=None,
                                    cancel_check=None):
        """Stage the selected set, then publish it with rollback on failure."""
        out_dir = os.path.join(self.root_dir, CHAPTER_EXPORT_DIR)
        os.makedirs(out_dir, exist_ok=True)
        with file_lock(os.path.join(self.root_dir, ".chapter-export")):
            staging_dir = tempfile.mkdtemp(prefix=".chapter-export-", dir=out_dir)
            retain_backup = False
            try:
                result = self._stage_chapter_export_plans(
                    plans, chunks, options, pause_defaults, previous, chapters,
                    changed_only, get_pairs, durations, progress_callback,
                    cancel_check, staging_dir)
                if not result or not result[0]:
                    return result
                ensure_audio_export_active(cancel_check)
                backup_dir = os.path.join(staging_dir, "old")
                os.mkdir(backup_dir)
                names = sorted(name for name in os.listdir(staging_dir)
                               if name not in ("old", "manifest.json")) + ["manifest.json"]
                touched = []
                try:
                    for name in names:
                        ensure_audio_export_active(cancel_check)
                        target = os.path.join(out_dir, name)
                        backup = os.path.join(backup_dir, name)
                        existed = os.path.lexists(target)
                        if existed:
                            os.replace(target, backup)
                        touched.append((target, backup, existed))
                        os.replace(os.path.join(staging_dir, name), target)
                except BaseException as error:
                    failures = []
                    for target, backup, existed in reversed(touched):
                        try:
                            if existed:
                                os.replace(backup, target)
                            elif os.path.lexists(target):
                                os.unlink(target)
                        except OSError as rollback_error:
                            failures.append(f"{target}: {rollback_error}")
                    if failures:
                        retain_backup = True
                        raise RuntimeError(
                            f"Chapter export rollback failed; recovery files kept at {staging_dir}: "
                            + "; ".join(failures)) from error
                    if isinstance(error, ExportCancelled):
                        return False, "Export cancelled"
                    raise
                return result
            except ExportCancelled:
                return False, "Export cancelled"
            finally:
                if not retain_backup:
                    shutil.rmtree(staging_dir)

    def _stage_chapter_export_plans(self, plans, chunks, options, pause_defaults,
                                    previous, chapters, changed_only, get_pairs,
                                    durations, progress_callback, cancel_check,
                                    staging_dir):
        """Write selected plans and one manifest with shared absolute offsets."""
        try:
            wanted = set(range(len(plans))) if chapters is None else set(get_index_selection(chapters, len(plans)))
        except ValueError as exc:
            return False, str(exc)
        out_dir = os.path.join(self.root_dir, CHAPTER_EXPORT_DIR)
        rows, written, reused, cursor = [], 0, 0, 0
        for plan in plans:
            if cancel_check and cancel_check():
                return False, "Export cancelled"
            index = plan["index"]
            first, last = plan["chunks"]
            if index:
                previous_last = plans[index - 1]["chunks"][1]
                cursor += get_pause_duration_ms(
                    chunks[previous_last], chunks[first], *pause_defaults)
            old = previous.get(index)
            old_duration = get_chapter_reuse_duration(plan, old, wanted, changed_only, out_dir)
            if old_duration is not None:
                row, duration = dict(old), old_duration
                if index in wanted:
                    reused += 1
            elif index in wanted:
                pairs, skipped = get_pairs(first, last)
                if skipped or not pairs:
                    return None
                try:
                    chapter_timeline = compute_timeline(
                        pairs, *pause_defaults, cancel_check=cancel_check)
                    duration = chapter_timeline[-1][2] + len(chapter_timeline[-1][1])
                    piece = self.get_chapter_audio(pairs, pause_defaults, cancel_check)
                    if progress_callback:
                        progress_callback(
                            f"Writing chapter {index + 1}/{len(plans)}: {plan['title'][:60]}")
                    kwargs = {"bitrate": MP3_BITRATE} if options["format"] == "mp3" else {}
                    _export_audio_segment(
                        piece, os.path.join(staging_dir, plan["file"]), options["format"],
                        cancel_check=cancel_check, **kwargs)
                except ExportCancelled:
                    return False, "Export cancelled"
                del piece
                row = dict(plan)
                written += 1
            else:
                if durations is None:
                    return None
                row, duration = None, durations[index]
            if row is not None:
                row["start_ms"], row["end_ms"] = cursor, cursor + duration
                rows.append(row)
            cursor += duration

        atomic_json_write({**options, "chapters": rows},
                          os.path.join(staging_dir, "manifest.json"))
        note = f"{written} chapter file(s) written"
        if reused:
            note += f", {reused} unchanged and kept"
        return True, note

    def _export_changed_chapters(self, options, chapters,
                                 progress_callback=None, cancel_check=None):
        """Return None for incomplete/old exports, otherwise decode changed groups only."""
        chunks = self.load_chunks()
        if not chunks:
            return None
        for chunk in chunks:
            path = chunk.get("audio_path")
            try:
                full_path = self.get_chunk_audio_path(path) if path else ""
            except ValueError:
                return None
            if not path or not os.path.isfile(full_path):
                return None
        out_dir = os.path.join(self.root_dir, CHAPTER_EXPORT_DIR)
        manifest = safe_load_json(os.path.join(out_dir, "manifest.json"), {})
        if manifest.get("chapter_plan_version") != options["chapter_plan_version"]:
            return None
        previous = {row["index"]: row for row in get_chapter_export_rows(manifest)}
        pause_defaults = self._load_pause_defaults()
        try:
            plans = self._get_chapter_export_plans(chunks, options, pause_defaults)
        except ValueError as exc:
            return False, str(exc)
        for plan in plans:
            old = previous.get(plan["index"])
            if not old or not os.path.isfile(os.path.join(out_dir, old["file"])):
                return None
            try:
                if int(old["start_ms"]) < 0 or int(old["end_ms"]) < int(old["start_ms"]):
                    return None
            except (KeyError, TypeError, ValueError):
                return None

        def get_pairs(first, last):
            return self._load_chunks_with_audio(
                cancel_check=cancel_check, chunks=chunks[first:last + 1])

        return self._apply_chapter_export_plans(
            plans, chunks, options, pause_defaults, previous, chapters, True,
            get_pairs, progress_callback=progress_callback, cancel_check=cancel_check)

    def export_chapters(self, fmt="mp3", per_chunk_chapters=False,
                        template=DEFAULT_CHAPTER_TEMPLATE, padding=2,
                        book_name="", series_name="", volume_number="",
                        chapters=None, changed_only=False,
                        progress_callback=None, cancel_check=None):
        """Write selected chapter files and shared source/timing metadata.

        Changed-only exports decode only changed groups when the prior manifest
        is complete and uses the current chapter render/offset plan.
        """
        if fmt not in ("mp3", "wav"):
            return False, f"Unsupported format: {fmt}"
        options = {"format": fmt, "template": template, "padding": padding,
                   "per_chunk_chapters": per_chunk_chapters, "book_name": book_name,
                   "series_name": series_name, "volume_number": volume_number,
                   "chapter_plan_version": 1}
        if changed_only:
            try:
                incremental = self._export_changed_chapters(
                    options, chapters, progress_callback, cancel_check)
            except ExportCancelled:
                return False, "Export cancelled"
            if incremental is not None:
                return incremental
        try:
            pairs, skipped = self._load_chunks_with_audio(
                cancel_check=cancel_check,
                progress_callback=_loading_progress(progress_callback))
            if not pairs:
                return False, "No audio segments found"
            pause_defaults = self._load_pause_defaults()
            timeline = compute_timeline(pairs, *pause_defaults, cancel_check=cancel_check)
            chunks = [chunk for chunk, _, _ in timeline]
            plans = self._get_chapter_export_plans(chunks, options, pause_defaults)
        except ExportCancelled:
            return False, "Export cancelled"
        except ValueError as exc:
            return False, str(exc)
        out_dir = os.path.join(self.root_dir, CHAPTER_EXPORT_DIR)
        os.makedirs(out_dir, exist_ok=True)
        manifest = safe_load_json(os.path.join(out_dir, "manifest.json"), {})
        previous = {row["index"]: row for row in get_chapter_export_rows(manifest)}
        durations = [timeline[last][2] + len(timeline[last][1]) - timeline[first][2]
                     for first, last in (plan["chunks"] for plan in plans)]
        result = self._apply_chapter_export_plans(
            plans, chunks, options, pause_defaults, previous, chapters,
            changed_only and manifest.get("chapter_plan_version") == 1,
            lambda first, last: (pairs[first:last + 1], 0), durations,
            progress_callback, cancel_check)
        if result and result[0] and skipped:
            return True, result[1] + f" ({skipped} chunk(s) skipped - missing/corrupt audio)"
        return result

    def _chapter_fingerprint(self, chunks, pause_defaults=None):
        """What a chapter's audio is made of: each chunk's audio file and its
        size/mtime, plus its pause defaults and overrides. Same fingerprint, same output,
        which is what changed-only export relies on."""
        if pause_defaults is None:
            pause_defaults = self._load_pause_defaults()
        parts = [json.dumps(pause_defaults)]
        for c in chunks:
            path = c.get("audio_path") or ""
            full = self.get_chunk_audio_path(path) if path else ""
            try:
                st = os.stat(full)
                parts.append(f"{path}|{st.st_size}|{st.st_mtime_ns}|{c.get('pause_after')}")
            except OSError:
                parts.append(f"{path}|missing|{c.get('pause_after')}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]

    def preview_chapter_filenames(self, fmt="mp3", per_chunk_chapters=False,
                                  template=DEFAULT_CHAPTER_TEMPLATE, padding=2,
                                  book_name="", series_name="", volume_number="",
                                  chapters=None, changed_only=False):
        """Preview selected chapter files that export would write, excluding reuse."""
        chunks = [chunk for chunk in self.load_chunks()
                  if self.get_chunk_audio_segment(chunk) is not None]
        options = {"format": fmt, "per_chunk_chapters": per_chunk_chapters,
                   "template": template, "padding": padding, "book_name": book_name,
                   "series_name": series_name, "volume_number": volume_number}
        plans = self._get_chapter_export_plans(chunks, options, self._load_pause_defaults()) if chunks else []
        wanted = set(range(len(plans))) if chapters is None else set(get_index_selection(chapters, len(plans)))
        out_dir = os.path.join(self.root_dir, CHAPTER_EXPORT_DIR)
        manifest = safe_load_json(os.path.join(out_dir, "manifest.json"), {})
        previous = {row["index"]: row for row in get_chapter_export_rows(manifest)}
        changed_only = changed_only and manifest.get("chapter_plan_version") == 1
        return [{"number": plan["number"], "title": plan["title"], "file": plan["file"]}
                for plan in plans if plan["index"] in wanted and
                get_chapter_reuse_duration(plan, previous.get(plan["index"]),
                                           wanted, changed_only, out_dir) is None]

    def generate_chunks_parallel(self, indices, max_workers=2, progress_callback=None,
                                  cancel_check=None):
        """Accumulate durable UID transitions, compact after every worker stops."""
        with self._chunks_lock, file_lock(self.chunks_path):
            if self._active_journal_identity is not None:
                raise ValueError("Parallel chunk generation is already active")
            self._read_chunks()
            journal = self._get_status_journal_locked()
            self._active_journal_identity = copy.deepcopy(journal.book_identity)
        try:
            return self._generate_chunks_parallel_with_status_journal(
                indices, max_workers, progress_callback, cancel_check)
        finally:
            with self._chunks_lock, file_lock(self.chunks_path):
                try:
                    if os.path.exists(get_chunk_status_journal_path(self.chunks_path)):
                        self._save_chunks_locked(self._read_chunks(), uids_backfilled=True)
                finally:
                    self._active_journal_identity = None
                    self._status_journal = None

    def _generate_chunks_parallel_with_status_journal(self, indices, max_workers=2,
                                                     progress_callback=None, cancel_check=None):
        """Generate multiple chunks in parallel using ThreadPoolExecutor.

        Uses individual TTS API calls with per-speaker voice settings.

        Args:
            indices: List of chunk indices to generate
            max_workers: Number of concurrent TTS workers
            progress_callback: Optional callback(completed, failed, total) for progress updates
            cancel_check: Optional callable returning True when cancellation is requested

        Returns:
            dict with 'completed', 'failed', and 'cancelled' keys
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import gc

        results = {"completed": [], "failed": [], "cancelled": 0}
        indices = list(dict.fromkeys(indices))

        # Filter out empty-text chunks
        chunks = self.load_chunks()
        if chunks:
            indices = [i for i in indices if 0 <= i < len(chunks) and chunks[i].get("text", "").strip()]

        captured = get_chunk_generation_requests(chunks, indices)
        total = len(indices)

        if total == 0:
            return results

        all_indices = list(indices)

        def _run_round(round_indices, workers):
            """One pass at `workers` concurrency.

            Returns (completed, oom_failed, hard_failed, cancelled). OOM failures
            are kept separate so the caller can step concurrency down and retry
            just those, while hard failures (bad text, missing engine) are final.
            """
            completed, oom_failed, hard_failed = [], [], []
            was_cancelled = False
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(self._generate_captured_chunk_audio, idx, captured.get(idx)): idx
                           for idx in round_indices}
                for future in as_completed(futures):
                    if cancel_check and cancel_check() and not was_cancelled:
                        was_cancelled = True
                        print("[CANCEL] Cancellation requested — stopping parallel generation")
                        for pending_future in futures:
                            if pending_future is not future:
                                pending_future.cancel()
                    idx = futures[future]
                    if future.cancelled():
                        continue
                    try:
                        success, msg = future.result()
                        if success:
                            completed.append(idx)
                            print(f"Chunk {idx} completed: {msg}")
                        elif is_oom_failure(msg):
                            oom_failed.append((idx, msg))
                            print(f"Chunk {idx} failed (VRAM): {msg}")
                        else:
                            hard_failed.append((idx, msg))
                            print(f"Chunk {idx} failed: {msg}")
                    except Exception as e:
                        (oom_failed if is_oom_failure(e) else hard_failed).append((idx, str(e)))
                        print(f"Chunk {idx} error: {e}")
                    if progress_callback:
                        # oom_failed chunks aren't final yet (they get retried
                        # next round at a lower worker count), but they ARE
                        # accounted for right now - count them alongside
                        # completed/hard_failed so the running total this
                        # round doesn't look like it's stalled or losing track
                        # of in-flight work.
                        progress_callback(len(results["completed"]) + len(completed),
                                          len(results["failed"]) + len(hard_failed) + len(oom_failed), total)
            return completed, oom_failed, hard_failed, was_cancelled

        # Start at the configured worker count and step down one at a time only
        # when a round hits VRAM OOM, retrying just the OOM-failed chunks, until
        # they succeed or we're down to a single worker.
        workers = max(1, max_workers)
        pending = list(all_indices)
        cancelled = False
        print(f"Starting parallel generation of {total} chunks with {workers} workers...")
        while pending:
            completed, oom_failed, hard_failed, was_cancelled = _run_round(pending, workers)
            results["completed"].extend(completed)
            results["failed"].extend(hard_failed)
            if was_cancelled:
                cancelled = True
                break
            if oom_failed and workers > 1:
                workers -= 1
                pending = [idx for idx, _ in oom_failed]
                print(f"[VRAM] Out-of-memory on {len(pending)} chunk(s) — stepping TTS "
                      f"workers down to {workers} and retrying.")
                gc.collect()
                continue
            # workers == 1 (or no OOM left): remaining OOM failures are now final.
            results["failed"].extend(oom_failed)
            break

        # Reset remaining "generating" chunks to "pending" on cancel.
        if cancelled:
            done_indices = set(results["completed"]) | {idx for idx, _ in results["failed"]}
            reset_count, stale_failures = self._reset_captured_generations(
                captured, all_indices, done_indices)
            results["cancelled"] += reset_count
            results["failed"].extend(stale_failures)

        print(f"Parallel generation complete: {len(results['completed'])} succeeded, "
              f"{len(results['failed'])} failed, {results['cancelled']} cancelled")
        return results

    def _group_indices_by_voice_type(self, indices, chunks, voice_config):
        """Reorder indices so chunks with the same voice type are contiguous.

        Grouping key matches how tts.py routes batches:
        - "custom" for custom voices (all batched together)
        - "clone:{speaker}" for clone voices (batched per speaker)
        - "lora:{adapter}" for LoRA voices (batched per adapter)
        - "design" / "ensemble" for their sequential engine paths

        Within each group, original order is preserved.
        """
        from collections import OrderedDict
        groups = OrderedDict()

        for idx in indices:
            if not (0 <= idx < len(chunks)):
                groups.setdefault("custom", []).append(idx)
                continue
            speaker = chunks[idx].get("speaker", "")
            # Resolve alias before grouping so alias groups collate with their canonical speaker
            canonical = self._resolve_alias(speaker, voice_config)
            voice_data = voice_config.get(canonical, {})
            category = voice_category(voice_data)

            if category == "clone":
                key = f"clone:{canonical}"
            elif category == "lora":
                adapter_id = voice_data.get("adapter_id", "")
                key = f"lora:{adapter_id}"
            else:
                key = category

            groups.setdefault(key, []).append(idx)

        reordered = []
        for key, group_indices in groups.items():
            print(f"  Voice group '{key}': {len(group_indices)} chunks")
            reordered.extend(group_indices)

        return reordered

    def _export_chunk_audio(self, temp_path, filename_base):
        """Convert a temp WAV to MP3, falling back to a copied WAV when ffmpeg
        lacks an MP3 encoder. Returns the relative audio_path (under voicelines/).
        Raises ValueError if the source audio has zero duration.
        """
        segment = _load_audio_segment(temp_path)
        if len(segment) == 0:
            raise ValueError("Audio has 0 duration")

        try:
            mp3_filename = f"{filename_base}.mp3"
            mp3_filepath = os.path.join(self.voicelines_dir, mp3_filename)
            fd, staged_mp3 = tempfile.mkstemp(prefix=".chunk-", suffix=".mp3",
                                              dir=self.voicelines_dir)
            os.close(fd)
            try:
                _export_audio_segment(segment, staged_mp3, "mp3", bitrate=MP3_BITRATE)

                # Validate: conda ffmpeg often lacks libmp3lame, producing a tiny
                # (~428 byte) header-only file without raising an error.
                mp3_size = os.path.getsize(staged_mp3)
                if mp3_size < 1024:
                    print(f"MP3 export produced invalid file ({mp3_size} bytes) — ffmpeg likely "
                          f"lacks MP3 encoder (libmp3lame). Falling back to WAV.")
                    raise RuntimeError("MP3 export produced invalid file")
                validate_generated_audio(staged_mp3, "MP3 chunk export")
                publish_audio_output(staged_mp3, mp3_filepath)
            finally:
                if os.path.exists(staged_mp3):
                    os.remove(staged_mp3)
            return f"voicelines/{mp3_filename}"

        except Exception as e:
            if "invalid file" not in str(e).lower():
                print(f"MP3 conversion failed: {e}")
            wav_filename = f"{filename_base}.wav"
            wav_filepath = os.path.join(self.voicelines_dir, wav_filename)
            fd, staged_wav = tempfile.mkstemp(prefix=".chunk-", suffix=".wav",
                                              dir=self.voicelines_dir)
            os.close(fd)
            try:
                shutil.copy2(temp_path, staged_wav)
                publish_audio_output(staged_wav, wav_filepath)
            finally:
                if os.path.exists(staged_wav):
                    os.remove(staged_wav)
            return f"voicelines/{wav_filename}"

    def _remove_temp_file(self, temp_path):
        """Best-effort delete of a temp batch WAV, retrying briefly on transient locks."""
        if not os.path.exists(temp_path):
            return
        for attempt in range(3):
            try:
                os.remove(temp_path)
                return
            except OSError:
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))
                else:
                    print(f"Warning: Could not delete temp file {temp_path}")

    def _finalize_completed_chunk(self, idx, chunks):
        """Finalize from a captured row, conditionally publishing to the live UID.

        Returns (outcome, original_index, audio_path_or_error); never mutates the
        input accumulator or saves a stale list over concurrent editor changes.
        """
        chunk = get_captured_chunk(chunks, idx)
        if chunk is None:
            return "failed", idx, "Index out of range in captured generation"
        temp_path = os.path.join(self.root_dir, f"temp_batch_{idx}.wav")
        try:
            if not os.path.exists(temp_path):
                raise ValueError("Temp audio file not found")
            validate_generated_audio(temp_path, f"batch chunk {idx}")
            audio_path = self._publish_generated_chunk_audio(
                chunk, temp_path, chunk.get("speaker", "unknown"))
            print(f"Chunk {idx} completed: {audio_path}")
            return "completed", idx, audio_path
        except Exception as e:
            print(f"Error processing chunk {idx}: {e}")
            updated = self._update_chunk_fields_by_uid(
                chunk.get("uid"), expected_chunk=chunk, status="error", error=str(e))
            return "failed", idx, str(e) if updated is not None else GENERATION_INPUTS_CHANGED
        finally:
            self._remove_temp_file(temp_path)

    def _record_batch_failures(self, batch_failed, chunks, current_batch_size):
        """Separate retryable OOM failures and conditionally mark live owners."""
        oom_failed, hard_failures = [], []
        for idx, error in batch_failed:
            chunk = get_captured_chunk(chunks, idx)
            current = (self._update_chunk_fields_by_uid(chunk.get("uid"), expected_chunk=chunk)
                       if chunk else None)
            if current is None:
                hard_failures.append((idx, GENERATION_INPUTS_CHANGED))
                continue
            if is_oom_failure(error) and current_batch_size > 1:
                oom_failed.append(idx)
                continue
            updated = self._update_chunk_fields_by_uid(
                chunk["uid"], expected_chunk=chunk, status="error", error=str(error))
            hard_failures.append((idx, error if updated is not None else GENERATION_INPUTS_CHANGED))
        return oom_failed, hard_failures

    def generate_chunks_batch(self, indices, batch_seed=-1, batch_size=4, progress_callback=None,
                               batch_group_by_type=False, cancel_check=None):
        """Generate multiple chunks using batch TTS API with a single seed.

        Args:
            indices: List of chunk indices to generate
            batch_seed: Single seed for all generations (-1 for random)
            batch_size: Number of chunks per batch request
            progress_callback: Optional callback(completed, failed, total) for progress updates
            batch_group_by_type: Group indices by voice type before batching for
                GPU efficiency. When False, indices are batched in sequential order.
            cancel_check: Optional callable returning True when cancellation is requested

        Returns:
            dict with 'completed', 'failed', and 'cancelled' keys
        """
        results = {"completed": [], "failed": [], "cancelled": 0}
        indices = list(dict.fromkeys(indices))

        # Load chunks and voice config
        chunks = self.load_chunks()

        # Filter out empty-text chunks
        if chunks:
            indices = [i for i in indices if 0 <= i < len(chunks) and chunks[i].get("text", "").strip()]

        captured = get_chunk_generation_requests(chunks, indices)
        total = len(indices)

        if total == 0:
            return results

        print(f"Starting batch generation of {total} chunks (batch_size={batch_size}, seed={batch_seed}, "
              f"group_by_type={batch_group_by_type})...")
        
        # atomic_json_write's write-temp+rename makes plain reads safe even
        # while app.py's voice_library endpoints hold file_lock(voice_config_path)
        # for a read-modify-write — no extra locking needed here.
        voice_config = safe_load_json(self.voice_config_path, default={})

        # Get TTS engine
        engine = self.get_engine()
        if not engine:
            for idx in indices:
                results["failed"].append((idx, "TTS engine not initialized"))
            return results

        # Reserve each captured row conditionally; never save the old whole list.
        active_indices = []
        for idx in indices:
            chunk = captured.get(idx)
            if not chunk or self._update_chunk_fields_by_uid(
                    chunk.get("uid"), expected_chunk=chunk, status="generating") is None:
                results["failed"].append((idx, GENERATION_INPUTS_CHANGED))
            else:
                active_indices.append(idx)
        indices = active_indices

        # Optionally reorder indices so same voice-type chunks are contiguous.
        # This produces larger homogeneous batches (e.g. all custom voices
        # together) instead of fragmenting each batch across voice types.
        if batch_group_by_type:
            indices = self._group_indices_by_voice_type(indices, chunks, voice_config)

        # Process indices in batches, starting at the configured size and
        # stepping the size down only when a batch hits VRAM OOM (retrying just
        # the OOM-failed chunks at the smaller size), down to 1.
        pending = list(indices)
        current_batch_size = max(1, batch_size)
        batch_num = 0
        while pending:
            if cancel_check and cancel_check():
                print(f"[CANCEL] Cancellation requested before batch {batch_num + 1}")
                break

            batch_indices = pending[:current_batch_size]
            pending = pending[current_batch_size:]
            batch_num += 1
            print(f"Batch {batch_num} ({len(batch_indices)} chunks, size={current_batch_size}, {len(pending)} queued)")

            # Build batch request data
            batch_chunks = []
            cleanup_failures = []
            for idx in batch_indices:
                chunk = captured.get(idx)
                if chunk:
                    if self._update_chunk_fields_by_uid(
                            chunk["uid"], expected_chunk=chunk, status="generating") is None:
                        cleanup_failures.append((idx, GENERATION_INPUTS_CHANGED))
                        continue
                    temp_path = os.path.join(
                        self.root_dir, f"temp_batch_{idx}.wav")
                    try:
                        remove_stale_audio(temp_path)
                    except OSError as exc:
                        message = f"Could not remove stale temp audio: {exc}"
                        cleanup_failures.append((idx, message))
                        continue
                    # Resolve aliases so batch uses canonical speaker config
                    speaker = chunk.get("speaker", "")
                    canonical = self._resolve_alias(speaker, voice_config)
                    batch_chunk = {
                        "index": idx,
                        "text": chunk.get("text", ""),
                        "instruct": chunk.get("instruct", ""),
                        "speaker": canonical
                    }
                    # Preserve narrator-selection metadata through the
                    # project boundary; the TTS engine uses these fields to
                    # resolve focus/chapter/gender strategies per chunk.
                    for field in NARRATOR_GENERATION_FIELDS:
                        if field in chunk:
                            batch_chunk[field] = chunk[field]
                    batch_chunks.append(batch_chunk)

            # Call batch TTS with single seed. If stale-output cleanup rejected
            # every row, there is nothing safe to dispatch.
            try:
                batch_results = (engine.generate_batch(
                    batch_chunks, voice_config, self.root_dir, batch_seed)
                    if batch_chunks else {"completed": [], "failed": []})
            except Exception:
                self._reset_captured_generations(captured, indices, set())
                raise
            batch_results["failed"].extend(cleanup_failures)

            # Process completed chunks - convert to MP3 and update status
            for idx in batch_results["completed"]:
                outcome, out_idx, payload = self._finalize_completed_chunk(idx, captured)
                if outcome == "completed":
                    results["completed"].append(out_idx)
                else:
                    results["failed"].append((out_idx, payload))

            oom_failed, hard_failures = self._record_batch_failures(
                batch_results["failed"], captured, current_batch_size)
            results["failed"].extend(hard_failures)

            if oom_failed:
                gc.collect()
                current_batch_size = max(1, current_batch_size - 1)
                pending = oom_failed + pending  # retry the OOM chunks first
                print(f"[VRAM] Out-of-memory on {len(oom_failed)} chunk(s) — stepping TTS "
                      f"batch size down to {current_batch_size} and retrying.")

            if progress_callback:
                progress_callback(len(results["completed"]), len(results["failed"]), total)

        # Reset remaining "generating" chunks to "pending" on cancel or completion
        done_indices = set(results["completed"]) | {idx for idx, _ in results["failed"]}
        reset_count, stale_failures = self._reset_captured_generations(
            captured, indices, done_indices)
        results["cancelled"] += reset_count
        results["failed"].extend(stale_failures)

        print(f"Batch generation complete: {len(results['completed'])} succeeded, "
              f"{len(results['failed'])} failed, {results['cancelled']} cancelled")
        return results
