"""Casting library (#1532): every designed voice across books, with review marks.

Two sources, both already written by AA2:
  persona   designed_voices/persona/<book_key>/<speaker_xxx>/{meta.json, preview.wav}
            (generate_personas._save_generated_preview)
  designer  saved Designer voices in designed_voices/manifest.json
A voice's id is its location under designed_voices, so marks follow the files.
Review marks live in casting_marks.json in the data dir (flat JSON, like the
voice library). Casting a voice uses the existing candidate routes; this module
only builds the clone entry, in one place for both sources.
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

from casting_tags import get_voice_tags
from utils import atomic_json_write, character_voice_seed, file_lock, is_path_inside, safe_load_json

MAX_VOICES = 20000
MAX_META_BYTES = 64 * 1024
STATUSES = ('unreviewed', 'shortlist', 'callback', 'reject')
PRIORITIES = ('none', 'low', 'medium', 'high')
MAX_NOTES = 2000
MAX_TAGS, MAX_TAG_LENGTH = 20, 40
MAX_COLLECTIONS, MAX_COLLECTION_LENGTH = MAX_TAGS, MAX_TAG_LENGTH
COLORS = ('red', 'amber', 'green', 'blue', 'purple')
MARK_FIELDS = ('status', 'priority', 'notes', 'tags', 'tag_colors', 'stars', 'circles', 'collections')
MARK_DEFAULTS = {'status': 'unreviewed', 'priority': 'none', 'notes': '', 'tags': [], 'tag_colors': {},
                 'stars': [], 'circles': [], 'collections': []}
METADATA_KEY = '__studio__'
MAX_ACTIVITY = 100
MAX_ACTION_LENGTH = 100
LIFECYCLES = ('active', 'trashed', 'deleted')
LEGAL_LIFECYCLE_TRANSITIONS = (('active', 'trashed'), ('trashed', 'active'), ('trashed', 'deleted'))
_PERSONA_ID = re.compile(r'^persona/[^/\\]+/[^/\\]+$')
_DESIGNER_ID = re.compile(r'^designer/[A-Za-z0-9_.-]+$')


def get_casting_marks_path(voice_library_path):
    """-> the casting_marks.json path in the same data root as the voice library."""
    return os.path.join(os.path.dirname(os.path.abspath(voice_library_path)), 'casting_marks.json')


def get_persona_clone_entry(preview_path, ref_text, description, speaker):
    """The voice-config fields a persona preview is cast with (shared with persona generation)."""
    return {"type": "clone", "persona_status": "generated", "ref_audio": preview_path, "ref_text": ref_text,
            "description": description, "character_style": description, "seed": character_voice_seed(speaker)}


def _read_meta(path):
    with open(path, 'rb') as handle:
        if os.fstat(handle.fileno()).st_size > MAX_META_BYTES:
            raise ValueError('meta.json is too large')
        meta = json.loads(handle.read().decode('utf-8'))
    if not isinstance(meta, dict):
        raise ValueError('meta.json is not an object')
    return meta


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _voice(voice_id, source, book_id, name, description, ref_text, audio_rel, designed_dir):
    audio = os.path.join(designed_dir, *audio_rel.split('/')[1:]) if audio_rel else ''
    present = bool(audio) and is_path_inside(audio, designed_dir) and os.path.isfile(audio)
    return {'id': voice_id, 'source': source, 'book_id': book_id, 'name': name, 'description': description,
            'ref_text': ref_text, 'preview_url': '/' + audio_rel if present else None, 'audio_present': present,
            'tags': get_voice_tags(description)}


def _persona_voices(designed_dir, skipped):
    root = os.path.join(designed_dir, 'persona')
    if not os.path.isdir(root):
        return []
    voices = []
    for book_key in sorted(os.listdir(root)):
        book_dir = os.path.join(root, book_key)
        if not os.path.isdir(book_dir) or not is_path_inside(book_dir, root):
            continue
        for folder in sorted(os.listdir(book_dir)):
            meta_path = os.path.join(book_dir, folder, 'meta.json')
            if not is_path_inside(meta_path, book_dir):
                skipped.append({'id': f'persona/{book_key}/{folder}', 'reason': 'outside the voice folder'})
                continue
            if not os.path.isfile(meta_path):
                if os.path.isdir(os.path.join(book_dir, folder)):
                    skipped.append({'id': f'persona/{book_key}/{folder}', 'reason': 'missing meta.json'})
                continue
            try:
                meta = _read_meta(meta_path)
            except (OSError, ValueError, UnicodeDecodeError) as error:
                skipped.append({'id': f'persona/{book_key}/{folder}', 'reason': str(error)[:200]})
                continue
            expected = f'designed_voices/persona/{book_key}/{folder}/preview.wav'
            preview = _text(meta.get('preview')).replace('\\', '/')
            voices.append(_voice(f'persona/{book_key}/{folder}', 'persona', _text(meta.get('book_id')) or book_key,
                                 _text(meta.get('speaker')) or folder, _text(meta.get('description')),
                                 _text(meta.get('ref_text')), expected if preview == expected else '', designed_dir))
            if len(voices) >= MAX_VOICES:
                return voices
    return voices


def _designer_voices(designed_dir, skipped):
    manifest_path = os.path.join(designed_dir, 'manifest.json')
    if not is_path_inside(manifest_path, designed_dir):
        skipped.append({'id': 'designer', 'reason': 'manifest outside designed voices'})
        return []
    manifest = safe_load_json(manifest_path, default=None)
    if manifest is None and not os.path.exists(manifest_path):
        return []
    if not isinstance(manifest, list):
        skipped.append({'id': 'designer', 'reason': 'unreadable or malformed Designer manifest'})
        return []
    voices = []
    for entry in manifest if isinstance(manifest, list) else []:
        if not isinstance(entry, dict) or not _DESIGNER_ID.match(f"designer/{entry.get('id', '')}"):
            skipped.append({'id': str(entry.get('id', ''))[:80] if isinstance(entry, dict) else '',
                            'reason': 'malformed Designer entry'})
            continue
        filename = _text(entry.get('filename'))
        safe = filename and '/' not in filename and '\\' not in filename and filename not in ('.', '..')
        voices.append(_voice(f"designer/{entry['id']}", 'designer', 'Designer', _text(entry.get('name')) or entry['id'],
                             _text(entry.get('description')), _text(entry.get('sample_text')),
                             f'designed_voices/{filename}' if safe else '', designed_dir))
    return voices


def get_casting_catalog(designed_dir):
    """-> {'voices': [...], 'skipped': [...]}; unreadable entries are reported, never invented."""
    skipped = []
    voices = _persona_voices(designed_dir, skipped) + _designer_voices(designed_dir, skipped)
    return {'voices': voices[:MAX_VOICES], 'skipped': skipped, 'truncated': len(voices) > MAX_VOICES}


def get_catalog_voice(designed_dir, voice_id):
    if not (_PERSONA_ID.match(voice_id or '') or _DESIGNER_ID.match(voice_id or '')):
        return None
    return next((v for v in get_casting_catalog(designed_dir)['voices'] if v['id'] == voice_id), None)


def get_voice_clone_config(voice, speaker):
    """The clone entry that casts `voice` for `speaker`; None if it has no usable audio."""
    if not voice or not voice['audio_present']:
        return None
    path = voice['preview_url'][1:]
    if voice['source'] == 'persona':
        return get_persona_clone_entry(path, voice['ref_text'], voice['description'], speaker)
    return {"type": "clone", "ref_audio": path, "ref_text": voice['ref_text'],
            **({"description": voice['description']} if voice['description'] else {})}


class CastingMarksCorrupt(ValueError):
    """casting_marks.json exists but cannot be trusted; refuse rather than reset it."""


def _load_marks_document(marks_path):
    """-> the whole casting_marks.json document; malformed data refuses loudly, never resets."""
    if not is_path_inside(marks_path, os.path.dirname(os.path.abspath(marks_path))):
        raise ValueError('marks outside data directory')
    if not os.path.exists(marks_path):
        return {}
    try:
        with open(marks_path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise CastingMarksCorrupt(f'casting marks are unreadable: {error}') from error
    if not isinstance(data, dict):
        raise CastingMarksCorrupt('casting marks are not a JSON object')
    for key, value in data.items():
        if not isinstance(key, str) or (key != METADATA_KEY and not isinstance(value, dict)):
            raise CastingMarksCorrupt('casting marks contain a malformed entry')
        if key != METADATA_KEY and 'lifecycle' in value and value['lifecycle'] not in LIFECYCLES:
            raise CastingMarksCorrupt('stored lifecycle is invalid; repair the marks file before editing')
        if key != METADATA_KEY and 'tag_colors' in value:
            try:
                tags = _validated_text_list(value.get('tags', []), MAX_TAGS, MAX_TAG_LENGTH, 'tags')
                colors = _validated_tag_colors(value['tag_colors'])
                if tags != value.get('tags', []) or colors != value['tag_colors'] or set(colors) - set(tags):
                    raise ValueError('tag colours do not match stored tags')
            except ValueError as error:
                raise CastingMarksCorrupt('stored tag colours are invalid; repair the marks file before editing') from error
    return data


def _activity_events(document):
    metadata = document.get(METADATA_KEY)
    if metadata is None:
        return []
    if not isinstance(metadata, dict):
        raise CastingMarksCorrupt(f'{METADATA_KEY} metadata is not an object')
    events = metadata.get('events', [])
    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
        raise CastingMarksCorrupt(f'{METADATA_KEY} activity is not a list of events')
    return events


def get_casting_marks(marks_path):
    """-> {voice_id: mark}; the reserved metadata entry is never reported as a voice."""
    return {key: value for key, value in _load_marks_document(marks_path).items() if key != METADATA_KEY}


def get_casting_activity(marks_path):
    """-> the newest MAX_ACTIVITY batch-event metadata, separate from voice marks."""
    return list(_activity_events(_load_marks_document(marks_path))[-MAX_ACTIVITY:])


def normalize_casting_mark(mark):
    """-> the mark with canonical defaults on absent fields; the input is not mutated."""
    if mark is not None and not isinstance(mark, dict):
        raise CastingMarksCorrupt('a stored casting mark is not an object')
    if mark is not None and 'lifecycle' in mark and mark['lifecycle'] not in LIFECYCLES:
        raise CastingMarksCorrupt('stored lifecycle is invalid; repair the marks file before editing')
    full = {**MARK_DEFAULTS, **(mark or {})}
    for key, value in full.items():
        if isinstance(value, list):
            full[key] = list(value)
        elif isinstance(value, dict):
            full[key] = dict(value)
    return full


def get_mark_lifecycle(mark):
    """-> the lifecycle of one stored mark; absent means active."""
    if mark is None:
        return 'active'
    if not isinstance(mark, dict):
        raise CastingMarksCorrupt('a stored casting mark is not an object')
    lifecycle = mark.get('lifecycle', 'active')
    if lifecycle not in LIFECYCLES:
        raise CastingMarksCorrupt('stored lifecycle is invalid; repair the marks file before editing')
    return lifecycle


def get_voice_lifecycle(marks, voice_id):
    """-> the lifecycle for voice_id from a marks mapping; absent means active."""
    if not isinstance(marks, dict):
        raise CastingMarksCorrupt('casting marks are not a mapping')
    return get_mark_lifecycle(marks.get(voice_id))


def _validated_lifecycle(value, name):
    if value not in LIFECYCLES:
        raise ValueError(name + ' must be one of: ' + ', '.join(LIFECYCLES))
    return value


def _validated_text_list(values, limit, max_length, name):
    if (not isinstance(values, list) or len(values) > limit
            or any(not isinstance(value, str) or not value.strip() or len(value) > max_length
                   for value in values)):
        raise ValueError(f'{name} must be up to {limit} non-empty texts of at most {max_length} characters')
    return list(dict.fromkeys(value.strip() for value in values))


def _validated_tag_colors(value):
    """-> a tag -> palette-colour map; malformed or unknown colours are refused, never coerced."""
    if not isinstance(value, dict) or len(value) > MAX_TAGS:
        raise ValueError('tag_colors must map up to ' + str(MAX_TAGS) + ' custom tags to colours of: '
                         + ', '.join(COLORS))
    clean = {}
    for tag, color in value.items():
        if not isinstance(tag, str) or not tag.strip() or len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f'tag_colors keys must be non-empty tag names of at most {MAX_TAG_LENGTH} characters')
        if not isinstance(color, str) or color not in COLORS:
            raise ValueError('tag_colors colours must be one of: ' + ', '.join(COLORS))
        clean[tag.strip()] = color
    return clean


def _validated_colors(values, name):
    if (not isinstance(values, list) or len(values) > len(COLORS)
            or any(not isinstance(value, str) or value not in COLORS for value in values)):
        raise ValueError(f'{name} must be up to {len(COLORS)} unique colours of: {", ".join(COLORS)}')
    return list(dict.fromkeys(values))


def get_validated_mark_patch(patch):
    """-> a clean patch; raises ValueError naming the bad field."""
    if not isinstance(patch, dict) or not patch or set(patch) - set(MARK_FIELDS):
        raise ValueError('A mark may set status, priority, notes, tags, tag_colors, stars, circles and collections only')
    clean = {}
    if 'status' in patch:
        if patch['status'] not in STATUSES:
            raise ValueError(f'status must be one of {", ".join(STATUSES)}')
        clean['status'] = patch['status']
    if 'priority' in patch:
        if patch['priority'] not in PRIORITIES:
            raise ValueError(f'priority must be one of {", ".join(PRIORITIES)}')
        clean['priority'] = patch['priority']
    if 'notes' in patch:
        if not isinstance(patch['notes'], str) or len(patch['notes']) > MAX_NOTES:
            raise ValueError(f'notes must be text of at most {MAX_NOTES} characters')
        clean['notes'] = patch['notes']
    if 'tags' in patch:
        clean['tags'] = _validated_text_list(patch['tags'], MAX_TAGS, MAX_TAG_LENGTH, 'tags')
    if 'tag_colors' in patch:
        clean['tag_colors'] = _validated_tag_colors(patch['tag_colors'])
    if 'collections' in patch:
        clean['collections'] = _validated_text_list(
            patch['collections'], MAX_COLLECTIONS, MAX_COLLECTION_LENGTH, 'collections')
    for field in ('stars', 'circles'):
        if field in patch:
            clean[field] = _validated_colors(patch[field], field)
    return clean


def _merge_casting_mark(existing, clean):
    """-> the stored mark after applying a validated patch.

    A patch replaces whole fields, like the other mark arrays. Colours for a
    tag that is not (or is no longer) on the voice are dropped, so a legacy
    tags-only patch cannot leave a stale colour behind. A patch that names a
    colour for an absent tag is refused instead of silently storing it.
    """
    if existing is not None and not isinstance(existing, dict):
        raise CastingMarksCorrupt('the stored mark is not an object')
    mark = {**(existing or {}), **clean}
    tags = mark.get('tags') if isinstance(mark.get('tags'), list) else []
    colors = mark.get('tag_colors')
    if 'tag_colors' in clean:
        unknown = sorted(tag for tag in clean['tag_colors'] if tag not in tags)
        if unknown:
            raise ValueError('tag_colors must name only tags on this voice: ' + ', '.join(unknown))
    if colors is not None and not isinstance(colors, dict):
        raise CastingMarksCorrupt('the stored tag_colors is not an object')
    if isinstance(colors, dict):
        mark['tag_colors'] = {tag: color for tag, color in colors.items() if tag in tags}
    return mark


def save_casting_mark(marks_path, voice_id, patch, expected_lifecycle='active'):
    """Merge a validated patch into one voice's marks; -> the voice's marks."""
    expected_lifecycle = _validated_lifecycle(expected_lifecycle, 'expected_lifecycle')
    clean = get_validated_mark_patch(patch)
    with file_lock(marks_path):
        document = _load_marks_document(marks_path)
        current_lifecycle = get_mark_lifecycle(document.get(voice_id))
        if current_lifecycle == 'deleted':
            raise CastingMarkConflict(f'{voice_id} was permanently deleted; reload the library')
        if current_lifecycle != expected_lifecycle:
            raise CastingMarkConflict(f'{voice_id} lifecycle changed since it was read; reload the library')
        mark = _merge_casting_mark(document.get(voice_id), clean)
        document[voice_id] = mark
        atomic_json_write(document, marks_path)
    return mark


class CastingMarkConflict(ValueError):
    """A batch change was prepared against marks that have since changed."""


class CastingMarkNotFound(ValueError):
    """A batch change names a voice the catalog does not list."""


def _validated_action(action):
    if not isinstance(action, str) or not action.strip() or len(action) > MAX_ACTION_LENGTH:
        raise ValueError(f'action must be non-empty text of at most {MAX_ACTION_LENGTH} characters')
    return action.strip()


def save_casting_mark_batch(marks_path, changes, action, known_voice_ids):
    """All-or-none marks batch; -> ({voice_id: full mark}, newest MAX_ACTIVITY events).

    Validate voice ids and field values first, then compare every expected
    value against canonical current marks under one file_lock before the
    single atomic write.
    """
    action = _validated_action(action)
    if not isinstance(changes, list) or not changes:
        raise ValueError('a batch needs at least one change')
    prepared, seen = [], set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError('every change must be an object')
        voice_id = change.get('voice_id')
        if not isinstance(voice_id, str) or not voice_id:
            raise ValueError('every change needs a voice id')
        if voice_id in seen:
            raise ValueError(f'duplicate voice id in one batch: {voice_id}')
        if voice_id not in known_voice_ids:
            raise CastingMarkNotFound(f'Voice not found in the casting library: {voice_id}; reload it')
        seen.add(voice_id)
        expected_lifecycle = _validated_lifecycle(change.get('expected_lifecycle', 'active'), 'expected_lifecycle')
        prepared.append((voice_id, get_validated_mark_patch(change.get('patch')),
                        get_validated_mark_patch(change.get('expected')), expected_lifecycle))
    with file_lock(marks_path):
        document = _load_marks_document(marks_path)
        events = list(_activity_events(document))
        planned = {}
        for voice_id, clean, expected, expected_lifecycle in prepared:
            if set(expected) != set(clean):
                raise ValueError('expected must contain exactly the keys being changed')
            current_lifecycle = get_mark_lifecycle(document.get(voice_id))
            if current_lifecycle == 'deleted':
                raise CastingMarkConflict(f'{voice_id} was permanently deleted; reload the library')
            if current_lifecycle != expected_lifecycle:
                raise CastingMarkConflict(f'{voice_id} lifecycle changed since it was read; reload the library')
            current = normalize_casting_mark(document.get(voice_id))
            for field, value in expected.items():
                if current[field] != value:
                    raise CastingMarkConflict(f'{voice_id} changed since it was read ({field}); reload the library')
            planned[voice_id] = _merge_casting_mark(current, clean)
        document.update(planned)
        event = {'id': uuid.uuid4().hex, 'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                 'action': action, 'count': len(planned)}
        document[METADATA_KEY] = {**(document.get(METADATA_KEY) or {}),
                                 'events': (events + [event])[-MAX_ACTIVITY:]}
        atomic_json_write(document, marks_path)
        activity = list(document[METADATA_KEY]['events'])
    return planned, activity


def save_casting_lifecycle_batch(marks_path, changes, action, known_voice_ids):
    """All-or-none lifecycle batch; -> ({voice_id: full mark or tombstone}, activity)."""
    action = _validated_action(action)
    if not isinstance(changes, list) or not changes or len(changes) > 1000:
        raise ValueError('a lifecycle batch needs between 1 and 1000 changes')
    prepared, seen, targets = [], set(), set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError('every change must be an object')
        voice_id = change.get('voice_id')
        if not isinstance(voice_id, str) or not voice_id:
            raise ValueError('every change needs a voice id')
        if voice_id in seen:
            raise ValueError(f'duplicate voice id in one batch: {voice_id}')
        if voice_id not in known_voice_ids:
            raise CastingMarkNotFound(f'Voice not found in the casting library: {voice_id}; reload it')
        seen.add(voice_id)
        target = _validated_lifecycle(change.get('to'), 'to')
        expected_lifecycle = _validated_lifecycle(change.get('expected_lifecycle'), 'expected_lifecycle')
        targets.add(target)
        if (expected_lifecycle, target) not in LEGAL_LIFECYCLE_TRANSITIONS:
            raise CastingMarkConflict(f'illegal lifecycle transition for {voice_id}: {expected_lifecycle} to {target}')
        prepared.append((voice_id, target, expected_lifecycle))
    if len(targets) != 1:
        raise ValueError('every change in a lifecycle batch must have the same target lifecycle')
    with file_lock(marks_path):
        document = _load_marks_document(marks_path)
        events = list(_activity_events(document))
        planned = {}
        for voice_id, target, expected_lifecycle in prepared:
            current_lifecycle = get_mark_lifecycle(document.get(voice_id))
            if current_lifecycle == 'deleted':
                raise CastingMarkConflict(f'{voice_id} was permanently deleted; reload the library')
            if current_lifecycle != expected_lifecycle:
                raise CastingMarkConflict(f'{voice_id} lifecycle changed since it was read; reload the library')
            if target == 'deleted':
                planned[voice_id] = {'lifecycle': 'deleted'}
            else:
                existing = document.get(voice_id)
                if existing is not None and not isinstance(existing, dict):
                    raise CastingMarksCorrupt('the stored mark is not an object')
                planned[voice_id] = {**(existing or {}), 'lifecycle': target}
        document.update(planned)
        event = {'id': uuid.uuid4().hex, 'at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                  'action': action, 'count': len(planned)}
        document[METADATA_KEY] = {**(document.get(METADATA_KEY) or {}),
                                  'events': (events + [event])[-MAX_ACTIVITY:]}
        atomic_json_write(document, marks_path)
        activity = list(document[METADATA_KEY]['events'])
    return planned, activity
