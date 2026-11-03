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

from casting_tags import get_voice_tags
from utils import atomic_json_write, character_voice_seed, file_lock, is_path_inside, safe_load_json

MAX_VOICES = 20000
MAX_META_BYTES = 64 * 1024
STATUSES = ('unreviewed', 'shortlist', 'callback', 'reject')
PRIORITIES = ('none', 'low', 'medium', 'high')
MAX_NOTES = 2000
MAX_TAGS, MAX_TAG_LENGTH = 20, 40
_PERSONA_ID = re.compile(r'^persona/[^/\\]+/[^/\\]+$')
_DESIGNER_ID = re.compile(r'^designer/[A-Za-z0-9_.-]+$')


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


def get_casting_marks(marks_path):
    if not is_path_inside(marks_path, os.path.dirname(os.path.abspath(marks_path))):
        raise ValueError('marks outside data directory')
    data = safe_load_json(marks_path, default={})
    return data if isinstance(data, dict) else {}


def get_validated_mark_patch(patch):
    """-> a clean patch; raises ValueError naming the bad field."""
    if not isinstance(patch, dict) or not patch or set(patch) - {'status', 'priority', 'notes', 'tags'}:
        raise ValueError('A mark may set status, priority, notes and tags only')
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
        tags = patch['tags']
        if (not isinstance(tags, list) or len(tags) > MAX_TAGS
                or any(not isinstance(t, str) or not t.strip() or len(t) > MAX_TAG_LENGTH for t in tags)):
            raise ValueError(f'tags must be up to {MAX_TAGS} non-empty texts of at most {MAX_TAG_LENGTH} characters')
        clean['tags'] = list(dict.fromkeys(t.strip() for t in tags))
    return clean


def save_casting_mark(marks_path, voice_id, patch):
    """Merge a validated patch into one voice's marks; -> the voice's marks."""
    clean = get_validated_mark_patch(patch)
    with file_lock(marks_path):
        marks = get_casting_marks(marks_path)
        mark = {**marks.get(voice_id, {}), **clean}
        marks[voice_id] = mark
        atomic_json_write(marks, marks_path)
    return mark
