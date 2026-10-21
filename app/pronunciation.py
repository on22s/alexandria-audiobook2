"""A shared pronunciation lexicon for names the TTS cannot guess.

46% of the active book's lines contain a Latinized Japanese proper noun -
`Subaru` alone in 811 of 2,609 - and an English-trained model has no way to
know that it is SU-ba-ru rather than soo-BAR-oo. Observed errors already
include `rom and` heard as `roman` and `rezero` as `risero`.

WHY A LEXICON RATHER THAN BETTER PROMPTING. The pronunciation of a name is a
fact about the book, not about the model or the sentence. It belongs in data
that a person can correct once and have applied everywhere, which is the same
reasoning behind `character_aliases.json` and `voice_config.json`.

ORTHOGRAPHIC, NOT PHONETIC. Qwen3-TTS takes text; there is no phoneme input to
address. So an entry is a RESPELLING - "Subaru" -> "Soo-bah-roo" - which the
model reads as ordinary text. That is a real limitation: respelling is a blunt
instrument compared with IPA, and what respelling actually helps is an
empirical question per name.

WHICH IS WHY THIS SHIPS EMPTY. The mechanism is here; the entries are not
guessed. `proper_noun_pronunciation.py` provides the harness to test candidate
respellings against real lines, and an entry should be added because it was
measured to help, not because it looked right.

A TENSION WORTH STATING BEFORE ANYONE MEASURES THIS. WER is the wrong gate for
a lexicon. Validation scores the transcript against the ORIGINAL text, which is
correct - "Subaru" is what the book says. But if a respelling makes the model
say the name properly and the ASR then transcribes it as "Soo bah roo", WER
gets WORSE while the audio gets BETTER. A lexicon is judged by listening, or by
an ASR comparison that normalises the name on both sides. Optimising it against
raw WER would select for names that are easy to transcribe rather than right.
"""
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
import re
import threading

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(REPO, "pronunciation.json")

_cache = {"path": None, "fingerprint": None, "entries": {}, "pattern": None}
_cache_lock = threading.RLock()
logger = logging.getLogger(__name__)
_audition_matcher = ContextVar("pronunciation_audition_matcher", default=None)


def _compile(entries):
    """One regex over all keys, longest first.

    Longest-first matters: with both "Natsuki" and "Natsuki Subaru" present,
    alternation would otherwise match the shorter key inside the longer name
    and leave a half-substituted phrase.
    """
    keys = sorted((k for k in entries if k), key=len, reverse=True)
    if not keys:
        return None
    return re.compile(r"(?<![\w'])(" + "|".join(re.escape(k) for k in keys)
                      + r")(?![\w'])")


def load_lexicon(path=None, force=False):
    """-> {name: respelling}. Cached on file identity, size and timestamps so edits take effect."""
    with _cache_lock:
        return _load_lexicon(path, force)


def _load_lexicon(path=None, force=False):
    """Load the cache while the caller holds its lock."""
    path = path or DEFAULT_PATH
    try:
        stat = os.stat(path)
        fingerprint = (stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError as error:
        if not isinstance(error, FileNotFoundError):
            logger.warning("Pronunciation lexicon unavailable at %s; continuing without substitutions: %s", path, error)
        _cache.update({"path": path, "fingerprint": None, "entries": {},
                       "pattern": None})
        return {}
    if not force and _cache["path"] == path and _cache["fingerprint"] == fingerprint:
        return dict(_cache["entries"])
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (ValueError, OSError) as error:
        # Retain nonblocking generation, but make the failed lexicon visible.
        logger.warning("Pronunciation lexicon invalid at %s; continuing without substitutions: %s", path, error)
        _cache.update({"path": path, "fingerprint": fingerprint, "entries": {},
                       "pattern": None})
        return {}
    if not isinstance(raw, dict):
        logger.warning("Pronunciation lexicon invalid at %s; expected an object, continuing without substitutions", path)
        raw = {}
    elif "names" in raw and not isinstance(raw["names"], dict):
        logger.warning("Pronunciation lexicon has invalid names at %s; skipping non-string entries", path)
    source = raw.get("names") if isinstance(raw.get("names"), dict) else raw
    entries = {str(k): str(v) for k, v in source.items()
               if isinstance(k, str) and isinstance(v, str)
               and k.strip() and v.strip()}
    _cache.update({"path": path, "fingerprint": fingerprint, "entries": entries,
                   "pattern": _compile(entries)})
    return dict(entries)


def apply_pronunciation(text, path=None):
    """-> (text, [{name, spoken}]) with every applied substitution recorded.

    Returns the substitutions rather than mutating anything, so a caller can
    put them in an artifact. A silent respelling would be untraceable in the
    audio - the listener hears one thing and the script says another.
    """
    if not text:
        return text, []
    audition = _audition_matcher.get() if path is None else None
    if audition is not None:
        entries, pattern = audition
        return get_pronunciation_result(text, entries, pattern)
    with _cache_lock:
        load_lexicon(path)
        pattern, entries = _cache["pattern"], _cache["entries"]
    return get_pronunciation_result(text, entries, pattern)


def get_pronunciation_result(text, entries, pattern=None):
    """Apply one simultaneous, case-sensitive map without changing its inputs."""
    active = entries if pattern is not None else {
        key: value for key, value in entries.items() if key.strip() and value.strip()}
    pattern = pattern if pattern is not None else _compile(active)
    if not text or not pattern:
        return text, []
    applied = []

    def swap(match):
        name = match.group(1)
        spoken = active[name]
        applied.append({"name": name, "spoken": spoken})
        return spoken

    return pattern.sub(swap, text), applied


@contextmanager
def ensure_pronunciation_snapshot():
    """Freeze one worker's lexicon through synthesis and speech admission."""
    if _audition_matcher.get() is not None:
        yield
        return
    try:
        entries = load_lexicon()
        matcher = (entries, _compile(entries))
    except Exception as error:  # acquisition remains as fail-soft as speech preparation
        logger.warning("Pronunciation snapshot unavailable; continuing without substitutions: %s", error)
        matcher = ({}, None)
    token = _audition_matcher.set(matcher)
    try:
        yield
    finally:
        _audition_matcher.reset(token)


@contextmanager
def ensure_literal_pronunciation_audition():
    """Read audition spellings literally in this worker; retain normal speech preparation."""
    with ensure_pronunciation_audition({}):
        yield


@contextmanager
def ensure_pronunciation_audition(entries):
    """Use a private draft map at the normal substitution stage in this worker."""
    active = {key: value for key, value in entries.items() if key.strip() and value.strip()}
    token = _audition_matcher.set((active, _compile(active)))
    try:
        yield
    finally:
        _audition_matcher.reset(token)


# ── keeping the name list honest ─────────────────────────────────────────

def character_forms(script_path=None, aliases_path=None, voice_config_path=None):
    """Every written form of every character that ACTUALLY OCCURS in the text.

    WHY DERIVED RATHER THAN LISTED. The first version of this file carried a
    hardcoded list of thirteen names typed from memory. It included Ram and
    Rem, who do not appear in this book at all, and it would silently have no
    entry for a character it forgot or for any other book. The roster already
    exists in character_aliases.json and voice_config.json; a second list
    beside them is the parallel-maintenance problem this repo has been bitten
    by before.

    ALIASES ARE USED FOR DISCOVERY, NEVER FOR SUBSTITUTION. The alias map
    records IDENTITY - 'BETTY' -> 'BEATRICE' is one character - while a
    lexicon records SOUND, and those come apart. Speaking "Betty" as
    "Beatrice" would put a word in the audio that is not in the book, which is
    worse than mispronouncing it. So every written form gets its own entry and
    the canonical name confers nothing on its variants.

    CASE IS LOAD-BEARING. `Felt` is a character here and `felt` is a verb:
    242 and 65 occurrences in the same book. Matching case-insensitively would
    rewrite the verb. Only forms that occur with their own capitalisation are
    returned.
    """
    import json as _json
    from utils import get_runtime_data_dir
    root = get_runtime_data_dir(REPO)
    script_path = script_path or os.path.join(root, "chunks.json")
    aliases_path = aliases_path or os.path.join(root, "character_aliases.json")
    voice_config_path = voice_config_path or os.path.join(root,
                                                          "voice_config.json")

    def _read(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return _json.load(fh)
        except (OSError, ValueError):
            return None

    candidates = set()
    aliases = _read(aliases_path)
    if isinstance(aliases, dict):
        for variant, canonical in aliases.items():
            candidates.add(str(variant))
            candidates.add(str(canonical))
    vc = _read(voice_config_path)
    if isinstance(vc, dict):
        chars = vc.get("characters") if isinstance(vc.get("characters"), dict) else vc
        candidates.update(str(k) for k in chars)

    chunks = _read(script_path)
    if not isinstance(chunks, list):
        return {}
    text = "\n".join(str(c.get("text") or "") for c in chunks
                      if isinstance(c, dict))
    if isinstance(chunks, list):
        candidates.update(str(c.get("speaker")) for c in chunks
                          if isinstance(c, dict) and c.get("speaker"))

    # A speaker label is usually upper case while prose uses natural case, so
    # try the label AND its title-cased form - but count each spelling
    # separately, because that is what the substitution will match.
    token_counts = Counter(re.findall(r"[\w']+", text))

    def count_occurrences(form):
        if re.fullmatch(r"\w+", form):
            return token_counts[form]
        return len(re.findall(r"(?<![\w'])" + re.escape(form) + r"(?![\w'])", text))

    forms = {}
    for name in candidates:
        name = name.strip()
        if len(name) < 2:
            continue
        for form in {name, name.title()}:
            n = count_occurrences(form)
            if not n:
                continue
            # A form whose LOWERCASE also occurs is a name that collides with
            # an ordinary word: Felt/felt (242/65), Man/man (11/95), Cat/cat
            # (1/22). Case-sensitive matching already protects the verb, but
            # an editor filling this file in deserves to be warned before
            # giving "Man" a respelling.
            lower = count_occurrences(form.lower())
            forms[form] = {"occurrences": n,
                           "collides_with_common_word": bool(lower and
                                                             form != form.lower()),
                           "lowercase_occurrences": lower}
    return dict(sorted(forms.items(),
                       key=lambda kv: -kv[1]["occurrences"]))


MAX_OCCURRENCE_RULES = 32
MAX_OCCURRENCE_RULE_BYTES = 65536
OCCURRENCE_COORDINATE_SPACE = 'pre_lexicon_v1'
OCCURRENCE_RENDER_FIELDS = ('coordinate_space', 'offset_unit', 'uid', 'text_sha256', 'prepared_sha256',
                            'start', 'end', 'original', 'replacement', 'phonetic_hint',
                            'fallback', 'restrictions')
OCCURRENCE_RULE_FIELDS = frozenset(('id', 'state', 'note', *OCCURRENCE_RENDER_FIELDS))
PRONUNCIATION_MODEL_CONTEXT = {'engine_mode': 'mode', 'language': 'language'}


class OccurrenceAnchorConflict(ValueError):
    """A reviewed anchor no longer identifies this exact saved occurrence."""


def get_pronunciation_model_context(settings):
    """The existing model settings which can restrict an occurrence rule."""
    return {field: settings.get(key) for field, key in PRONUNCIATION_MODEL_CONTEXT.items()}


def is_pronunciation_settings_dependent(chunk):
    return any(set(rule.get('restrictions', {})) & PRONUNCIATION_MODEL_CONTEXT.keys()
               for rule in get_occurrence_pronunciation_inputs(chunk.get('pronunciation_rules')))


def get_pronunciation_text_digest(text):
    """Bind anchors to exact Unicode codepoints, including reviewed surrogates."""
    import hashlib
    return hashlib.sha256(text.encode('utf-8', errors='surrogatepass')).hexdigest()


def get_occurrence_rule_error(rule):
    """Return a bounded rule's schema error without interpreting its anchor."""
    if (not isinstance(rule, dict) or len(rule) > len(OCCURRENCE_RULE_FIELDS)
            or any(key not in OCCURRENCE_RULE_FIELDS for key in rule)):
        return 'invalid_rule'
    if (not isinstance(rule.get('id'), str) or not 1 <= len(rule['id']) <= 64
            or rule.get('state') not in ('draft', 'approved', 'rejected')
            or rule.get('coordinate_space') != OCCURRENCE_COORDINATE_SPACE
            or rule.get('offset_unit') != 'unicode_codepoint'
            or not isinstance(rule.get('uid'), str) or not 1 <= len(rule['uid']) <= 128):
        return 'invalid_rule'
    if any(not isinstance(rule.get(key), str) or re.fullmatch(r'[0-9a-f]{64}', rule[key]) is None
           for key in ('text_sha256', 'prepared_sha256')):
        return 'invalid_anchor_digest'
    if (type(rule.get('start')) is not int or type(rule.get('end')) is not int
            or rule['start'] < 0 or rule['end'] <= rule['start']
            or not isinstance(rule.get('original'), str) or not 1 <= len(rule['original']) <= 512):
        return 'invalid_anchor_span'
    replacement, hint, fallback = (rule.get(key) for key in ('replacement', 'phonetic_hint', 'fallback'))
    if any(value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 512)
           for value in (replacement, hint, fallback)):
        return 'invalid_replacement'
    if (replacement is None) == (hint is None) or (replacement is not None and fallback is not None):
        return 'choose_respelling_or_phonetic_hint'
    restrictions = rule.get('restrictions', {})
    if (not isinstance(restrictions, dict) or len(restrictions) > 4
            or set(restrictions) - {'speaker', 'voice_sha256', *PRONUNCIATION_MODEL_CONTEXT}
            or any(not isinstance(value, str) or not value.strip() or len(value) > 256
                   for value in restrictions.values())):
        return 'invalid_restrictions'
    voice = restrictions.get('voice_sha256')
    if voice is not None and re.fullmatch(r'[0-9a-f]{64}', voice) is None:
        return 'invalid_voice_identity'
    if restrictions.get('engine_mode') not in (None, 'local', 'external'):
        return 'invalid_engine_mode'
    if not isinstance(rule.get('note', ''), str) or len(rule.get('note', '')) > 512:
        return 'invalid_note'
    return None


def get_occurrence_anchor_error(rule, uid, text_digest, prepared, prepared_digest):
    """Use exact hashes and Unicode-codepoint bounds; never search for reattachment."""
    if rule['uid'] != uid:
        return 'wrong_chunk'
    if rule['text_sha256'] != text_digest:
        return 'stale_chunk_text'
    if rule['prepared_sha256'] != prepared_digest:
        return 'stale_preparation'
    if rule['end'] > len(prepared) or prepared[rule['start']:rule['end']] != rule['original']:
        return 'stale_span'
    return None


def validate_occurrence_pronunciation_rules(rules, uid, text, prepared):
    """Refuse invalid/stale approval data before a bounded chunk document save."""
    if not isinstance(rules, list) or len(rules) > MAX_OCCURRENCE_RULES:
        raise ValueError('At most 32 occurrence rules are allowed per line.')
    text_digest = get_pronunciation_text_digest(text)
    prepared_digest = get_pronunciation_text_digest(prepared)
    seen, approved = set(), []
    for rule in rules:
        error = get_occurrence_rule_error(rule)
        if error:
            raise ValueError('Occurrence rule needs review: ' + error)
        error = get_occurrence_anchor_error(rule, uid, text_digest, prepared, prepared_digest)
        if error:
            raise OccurrenceAnchorConflict('Occurrence rule needs review: ' + error)
        if rule['id'] in seen:
            raise ValueError('Occurrence rule IDs must be unique.')
        seen.add(rule['id'])
        if rule['state'] == 'approved':
            approved.append(rule)
    previous_end = -1
    for rule in sorted(approved, key=lambda row: row['start']):
        if rule['start'] < previous_end:
            raise ValueError('Approved occurrence spans may not overlap.')
        previous_end = rule['end']
    if len(json.dumps(rules, ensure_ascii=False, allow_nan=False).encode('utf-8', errors='surrogatepass')) > MAX_OCCURRENCE_RULE_BYTES:
        raise ValueError('Occurrence rules exceed the 64 KiB line limit.')


def get_occurrence_pronunciation_inputs(rules):
    """Capture approved render data; notes, review drafts and IDs do not alter speech."""
    if not isinstance(rules, list) or len(rules) > MAX_OCCURRENCE_RULES:
        return []
    active = [{key: dict(rule[key]) if key == 'restrictions' else rule[key]
               for key in OCCURRENCE_RENDER_FIELDS if key in rule}
              for rule in rules if get_occurrence_rule_error(rule) is None and rule['state'] == 'approved']
    return sorted(active, key=lambda row: (row['start'], row['end']))


def get_occurrence_pronunciation_resolution(text, entries, pattern=None, *, rules=None,
                                           uid=None, text_digest=None, context=None):
    """Plan scoped and shared replacements against one string, then apply once."""
    if not rules:
        spoken, substitutions = get_pronunciation_result(text, entries, pattern)
        return {'text': spoken, 'substitutions': substitutions, 'decisions': []}
    if not isinstance(rules, list) or len(rules) > MAX_OCCURRENCE_RULES:
        spoken, substitutions = get_pronunciation_result(text, entries, pattern)
        return {'text': spoken, 'substitutions': substitutions,
                'decisions': [{'id': None, 'applied': False, 'reason': 'invalid_rule_document'}]}
    prepared_digest = get_pronunciation_text_digest(text)
    context = context or {}
    candidates, decisions, approved, ids, duplicate_ids = [], [], [], set(), set()
    for rule in rules:
        error = get_occurrence_rule_error(rule)
        rule_id = rule.get('id') if isinstance(rule, dict) else None
        if not isinstance(rule_id, str) or not 1 <= len(rule_id) <= 64:
            rule_id = None
        decision = {'id': rule_id, 'applied': False, 'reason': error}
        decisions.append(decision)
        if error:
            continue
        if rule['id'] in ids:
            decision['reason'] = 'duplicate_rule_id'
            duplicate_ids.add(rule['id'])
            continue
        ids.add(rule['id'])
        if rule['state'] != 'approved':
            decision['reason'] = rule['state']
            continue
        error = get_occurrence_anchor_error(rule, uid, text_digest, text, prepared_digest)
        if error:
            decision['reason'] = error
            continue
        approved.append((rule, decision))
    # Invalid stored overlap is never resolved by list order or active restriction.
    conflicts = set()
    for i, (rule, _decision) in enumerate(approved):
        for other, _ in approved[i + 1:]:
            if rule['start'] < other['end'] and other['start'] < rule['end']:
                conflicts.update((rule['id'], other['id']))
    for rule, decision in approved:
        if rule['id'] in duplicate_ids:
            decision['reason'] = 'duplicate_rule_id'
            continue
        if rule['id'] in conflicts:
            decision['reason'] = 'overlapping_approved_spans'
            continue
        mismatch = next((key for key, value in rule.get('restrictions', {}).items()
                         if context.get(key) != value), None)
        if mismatch:
            decision['reason'] = 'scope_mismatch:' + mismatch
            continue
        replacement = rule.get('replacement')
        reason = 'applied_respelling'
        if replacement is None:
            replacement = rule.get('fallback')
            if replacement is None:
                decision['reason'] = 'unsupported_phonetic_hint'
                continue
            reason = 'applied_phonetic_fallback'
        decision.update(applied=True, reason=reason)
        candidates.append((rule['start'], rule['end'], replacement,
                           {'name': rule['original'], 'spoken': replacement,
                            'rule_id': rule['id'], 'scope': 'occurrence'}))
    if not candidates:
        spoken, substitutions = get_pronunciation_result(text, entries, pattern)
        return {'text': spoken, 'substitutions': substitutions, 'decisions': decisions}
    active = entries if pattern is not None else {
        key: value for key, value in entries.items() if key.strip() and value.strip()}
    pattern = pattern if pattern is not None else _compile(active)
    replacements = list(candidates)
    if pattern is not None:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < stop and begin < end for begin, stop, _replacement, _row in candidates):
                continue
            name = match.group(1)
            replacements.append((start, end, active[name], {'name': name, 'spoken': active[name]}))
    ordered = sorted(replacements, key=lambda row: row[0])
    pieces, cursor = [], len(text)
    for start, end, replacement, _row in reversed(ordered):
        pieces.extend((text[end:cursor], replacement))
        cursor = start
    pieces.append(text[:cursor])
    return {'text': ''.join(reversed(pieces)),
            'substitutions': [row for _start, _end, _replacement, row in ordered],
            'decisions': decisions}


def get_occurrence_pronunciation_candidates(prepared, uid, text, query):
    """Offer bounded exact prepared-text spans; browser offsets are never input."""
    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError('Choose an exact spelling of 1–128 characters.')
    if len(text) > 131072:
        raise ValueError('This line exceeds the occurrence editor text limit.')
    pattern = _compile({query: query})
    anchors = {'coordinate_space': OCCURRENCE_COORDINATE_SPACE,
               'offset_unit': 'unicode_codepoint', 'uid': uid,
               'text_sha256': get_pronunciation_text_digest(text),
               'prepared_sha256': get_pronunciation_text_digest(prepared)}
    candidates = []
    for match in pattern.finditer(prepared):
        if len(candidates) == 64:
            return {'candidates': candidates, 'truncated': True}
        start, end = match.span()
        candidates.append({**anchors, 'start': start, 'end': end, 'original': match.group(1),
                           'before': prepared[max(0, start - 40):start],
                           'after': prepared[end:end + 40]})
    return {'candidates': candidates, 'truncated': False}


_occurrence_context = ContextVar('pronunciation_occurrence_context', default=None)


@contextmanager
def ensure_occurrence_pronunciation_context(chunk, context=None):
    """Carry a bounded immutable render snapshot through existing prepared dispatch."""
    rules = chunk.get('pronunciation_rules')
    scoped = None
    if rules:
        if not isinstance(rules, list) or len(rules) > MAX_OCCURRENCE_RULES:
            snapshot = 'invalid_rule_document'
        else:
            snapshot = []
            for rule in rules:
                if get_occurrence_rule_error(rule) is not None:
                    snapshot.append({'state': 'invalid'})
                    continue
                snapshot.append({key: dict(rule[key]) if key == 'restrictions' else rule[key]
                                 for key in ('id', 'state', *OCCURRENCE_RENDER_FIELDS) if key in rule})
        scoped = {'uid': chunk.get('uid'),
                  'text_digest': get_pronunciation_text_digest(chunk.get('text', '')),
                  'rules': snapshot, 'context': dict(context or {})}
    token = _occurrence_context.set(scoped)
    try:
        yield
    finally:
        _occurrence_context.reset(token)


def ensure_speech_pronunciation_result(text):
    """Use the canonical pre-lexicon string and this render's two snapshots."""
    scoped = _occurrence_context.get()
    if scoped is None:
        spoken, substitutions = apply_pronunciation(text)
        return {'text': spoken, 'substitutions': substitutions, 'decisions': []}
    with ensure_pronunciation_snapshot():
        entries, pattern = _audition_matcher.get()
        return get_occurrence_pronunciation_resolution(
            text, entries, pattern, rules=scoped['rules'], uid=scoped['uid'],
            text_digest=scoped['text_digest'], context=scoped['context'])
