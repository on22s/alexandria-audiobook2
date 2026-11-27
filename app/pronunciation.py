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

MATCHING LIMIT IN UNSPACED SCRIPTS. A key written in a script that does not
separate words with spaces (Han, kana, Thai, Lao, Khmer, Myanmar, Tibetan) has
no letter boundary to consult, so a MULTI-CHARACTER key can still fire inside
a longer, unrelated word: a two-character Katakana key hits the first two
characters of a longer Katakana name. Protect the word by adding it as its own
identity entry (the word on both sides of the map). Keys are tried longest
first, so the identity entry absorbs the match instead of the shorter key.
SINGLE-character keys are stricter: one fires only when neither neighbour is a
character of the same script class, because one character carries no evidence
that it is a whole word. Every identity entry is a deliberate exception, so
adding one is always a decision about this book's text.

Script classification is read from the running interpreter's Unicode database
(`unicodedata`), so it depends on the Unicode version that ships with Python.
Characters unassigned in that database classify as ordinary (spaced) text.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import hashlib
import io
import json
import logging
import os
import re
import threading
import unicodedata

from silent_word_characters import remove_silent_word_characters

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(REPO, "pronunciation.json")


def get_pronunciation_path(path=None):
    """The one call-time resolver for the shared lexicon's location.

    An explicit caller path always wins. Otherwise an isolated
    ALEXANDRIA_DATA_DIR owns the file, while an unconfigured runtime keeps the
    legacy install-root default, including standalone imports through an
    alias. Admit configured data roots before using them and keep DEFAULT_PATH
    when the admitted spelling is the install directory. A missing configured
    root keeps its admitted spelling.
    """
    if path:
        return path
    if not os.environ.get("ALEXANDRIA_DATA_DIR", "").strip():
        return DEFAULT_PATH
    from direct_path_admission import get_direct_directory_root, is_plain_path
    from utils import get_runtime_data_dir
    root = get_runtime_data_dir(REPO)
    if not os.path.exists(root):
        if not is_plain_path(root):
            raise ValueError('Pronunciation data root must be a direct directory')
        return os.path.join(root, "pronunciation.json")
    canonical, _identity = get_direct_directory_root(root)
    if os.path.normcase(canonical) == os.path.normcase(REPO):
        return DEFAULT_PATH
    return os.path.join(str(canonical), "pronunciation.json")


_cache = {"path": None, "fingerprint": None, "entries": {}, "pattern": None}
_cache_lock = threading.RLock()
logger = logging.getLogger(__name__)
_audition_matcher = ContextVar("pronunciation_audition_matcher", default=None)


_HAN = frozenset(("Han",))
_HIRAGANA = frozenset(("Hiragana",))
_KATAKANA = frozenset(("Katakana",))
_KANA = frozenset(("Hiragana", "Katakana"))
_THAI = frozenset(("Thai",))
_LAO = frozenset(("Lao",))
_KHMER = frozenset(("Khmer",))
_MYANMAR = frozenset(("Myanmar",))
_TIBETAN = frozenset(("Tibetan",))

# Unicode scripts that do not separate words with spaces. Classification reads
# the character name, which is the only script-property data `unicodedata`
# exposes; the value is the script class used for the single-character rule.
_UNSPACED_SCRIPT_NAME_PREFIXES = (
    ("CJK UNIFIED IDEOGRAPH", _HAN),
    ("CJK COMPATIBILITY IDEOGRAPH", _HAN),
    ("HIRAGANA", _HIRAGANA),
    ("HALFWIDTH KATAKANA", _KATAKANA),
    ("KATAKANA", _KATAKANA),
    ("THAI", _THAI),
    ("LAO", _LAO),
    ("KHMER", _KHMER),
    ("MYANMAR", _MYANMAR),
    ("TIBETAN", _TIBETAN),
)

# Characters whose Script property is Han/Common or Common in a CJK run and
# whose English name does not name that script. Checked against the Unicode
# script property, not just the name: 々, 〆, 〇, 〼, 𖿣 and 〻 are Han; the
# vertical kana repeat marks and spacing voiced marks occur in both kana runs;
# ー is Script=Common but prolongs the syllable in both kana. They occur inside
# the same unspaced run, so a single-character key must not fire next to them.
# Separators (CJK punctuation, ・, ゠) are deliberately absent: they end a word,
# so a single-character key may match beside them. Kangxi/CJK radicals
# (U+2E80–U+2FDF) are symbols, not Han: they are deliberately absent, so a
# single-character key may match beside them.
_CJK_SCRIPT_OVERRIDES = {
    "\u3005": _HAN,        # 々 IDEOGRAPHIC ITERATION MARK (Script=Han)
    "\u3006": _HAN,        # 〆 IDEOGRAPHIC CLOSING MARK (Script=Han)
    "\u3007": _HAN,        # 〇 IDEOGRAPHIC NUMBER ZERO (Script=Han, category Nl)
    "\u303b": _HAN,        # 〻 VERTICAL IDEOGRAPHIC ITERATION MARK (Script=Han)
    "\u303c": _HAN,        # 〼 MASU MARK (Script=Han)
    "\U00016fe3": _HAN,    # 𖿣 OLD CHINESE ITERATION MARK (Script=Han)
    "\u3031": _KANA,       # 〱 VERTICAL KANA REPEAT MARK (Script=Hiragana; runs)
    "\u3032": _KANA,       # 〲 VERTICAL KANA REPEAT MARK WITH VOICED SOUND
    "\u3033": _KANA,       # 〳 VERTICAL KANA REPEAT MARK UPPER HALF
    "\u3034": _KANA,       # 〴 ... WITH VOICED SOUND
    "\u3035": _KANA,       # 〵 VERTICAL KANA REPEAT MARK LOWER HALF
    "\u309b": _KANA,       # ゛ KATAKANA-HIRAGANA VOICED SOUND MARK (Common)
    "\u309c": _KANA,       # ゜ KATAKANA-HIRAGANA SEMI-VOICED SOUND MARK (Common)
    "\u30fc": _KANA,       # ー KATAKANA-HIRAGANA PROLONGED SOUND MARK (Common)
}

_STRAIGHT_AND_TYPOGRAPHIC_APOSTROPHES = "'\u2019\u02bc"
_LEADING_APOSTROPHES = _STRAIGHT_AND_TYPOGRAPHIC_APOSTROPHES + "\u2018"
_COMBINING_CATEGORIES = frozenset(("Mn", "Mc", "Me"))
_VARIATION_SELECTORS = frozenset(
    tuple(chr(codepoint) for codepoint in range(0xFE00, 0xFE10))
    + tuple(chr(codepoint) for codepoint in range(0xE0100, 0xE01F0))
)


@lru_cache(maxsize=4096)
def _unspaced_script_classes(char):
    """-> frozenset of unspaced script classes `char` belongs to (empty if none).

    Pure, so it is memoised per character. `ー` and the CJK marks above belong
    to more than one class; an empty result means an ordinary (spaced) script.
    """
    override = _CJK_SCRIPT_OVERRIDES.get(char)
    if override is not None:
        return override
    category = unicodedata.category(char)
    if not (category.startswith("L") or category.startswith("M")
            or category in ("Nd", "Nl")):
        return frozenset()   # punctuation/separators end a word; not a script neighbour
    name = unicodedata.name(char, "")
    for prefix, classes in _UNSPACED_SCRIPT_NAME_PREFIXES:
        if name.startswith(prefix):
            return classes
    return frozenset()


@lru_cache(maxsize=4096)
def is_unspaced_key(key):
    """Whether a key is written in a script that does not separate words with spaces.

    Decided from the Unicode script class of the key's LETTERS (see
    _unspaced_script_classes); digits and punctuation are neutral, so `3月`
    is unspaced while `3rd` is not. Letter Numbers count as letters here, so
    one `〇` (category Nl) is unspaced on its own. A key with any spaced-script
    letter, no letters at all (a digit-only key), or whitespace keeps the
    normal word bounds. The input is a string key, so the result is memoised.
    """
    letters = [char for char in key
               if unicodedata.category(char).startswith("L")
               or unicodedata.category(char) == "Nl"]
    return (bool(letters) and not any(char.isspace() for char in key)
            and all(_unspaced_script_classes(char) for char in letters))


def _blocks_following(following, *, combining_only=False):
    """Whether `following` belongs to the key rather than the next word.

    A variation selector never blocks: it modifies the preceding character
    (葛 + IVS is still 葛), so it must not make an otherwise valid key miss.
    """
    if not following or following in _VARIATION_SELECTORS:
        return False
    if unicodedata.category(following) in _COMBINING_CATEGORIES:
        return True
    return not combining_only and (following.isalnum() or following == "_")


def is_key_match(text, start, end, key):
    """THE ONE DECISION: may `key` match this exact span of `text`?

    Every caller reaches this through _compile, so no second copy of the
    boundary rules can drift from this one.

    A leading apostrophe always blocks (a name inside O'Brien is not the name;
    U+2018 is treated as a quote used as one). A trailing apostrophe never
    blocks (Mara's is the same name), and a following combining mark blocks
    (राम is not the word रामा) unless it is a variation selector.

    An unspaced key drops the word bound. A SINGLE-character unspaced key is
    stricter: it also refuses a neighbour in its own script class, so 中 does
    not fire in 中国 and ロ does not fire in ローマ. A multi-character key may
    still fire inside a longer word; add that word as an identity entry to
    protect it (longest-first). A key that is not unspaced keeps normal bounds,
    and so does every key with any spaced-script letter.
    """
    previous = text[start - 1] if start else ""
    following = text[end] if end < len(text) else ""
    if previous and previous in _LEADING_APOSTROPHES:
        return False
    if is_unspaced_key(key):
        if len(key) == 1:
            classes = _unspaced_script_classes(key)
            if classes and previous and (_unspaced_script_classes(previous) & classes):
                return False
            if classes and following and (_unspaced_script_classes(following) & classes):
                return False
        return not _blocks_following(following, combining_only=True)
    if previous and (previous.isalnum() or previous == "_"):
        return False
    # Before the combining-mark test on purpose: a possessive apostrophe is not
    # part of the following word, so it is allowed even if a mark follows it
    # (Mara'́). Tightening that rule later means moving this check.
    if following and following in _STRAIGHT_AND_TYPOGRAPHIC_APOSTROPHES:
        return True
    return not _blocks_following(following)


class _PronunciationMatcher:
    """One alternation scan whose every candidate is adjudicated by is_key_match."""

    def __init__(self, keys, ignore_case=False):
        self._regex = re.compile("(" + "|".join(re.escape(key) for key in keys) + ")",
                                 re.IGNORECASE if ignore_case else 0)

    def finditer(self, text):
        position = 0
        while position < len(text):
            match = self._regex.search(text, position)
            if match is None:
                return
            start, end = match.span()
            if is_key_match(text, start, end, match.group(0)):
                yield match
                position = end
            else:
                position = start + 1

    def sub(self, replacement, text):
        pieces, last = [], 0
        for match in self.finditer(text):
            pieces.append(text[last:match.start()])
            pieces.append(replacement(match))
            last = match.end()
        pieces.append(text[last:])
        return "".join(pieces)


def _compile(entries, ignore_case=False):
    """One matcher over all keys, longest first.

    Longest-first matters: with both "Natsuki" and "Natsuki Subaru" present,
    alternation would otherwise match the shorter key inside the longer name
    and leave a half-substituted phrase. Sorting the alternation by length is
    also what keeps a shorter key from firing inside a longer key THAT IS ALSO
    AN ENTRY in a script without word spaces; a longer word with no entry of
    its own is not protected (see the module docstring's matching limit).
    """
    keys = sorted((k for k in entries if k), key=lambda key: (-len(key), key))
    if not keys:
        return None
    return _PronunciationMatcher(keys, ignore_case=ignore_case)


def load_lexicon(path=None, force=False):
    """-> {name: respelling}. Cache parsed entries by file identity and content."""
    with _cache_lock:
        return _load_lexicon(path, force)


def _load_lexicon(path=None, force=False):
    """Load the cache while the caller holds its lock."""
    path = get_pronunciation_path(path)
    try:
        # Windows st_ctime is creation time. Hash the bytes we parse so an
        # in-place edit with the same size and restored mtime cannot stay cached.
        with open(path, "rb") as fh:
            stat = os.fstat(fh.fileno())
            content = fh.read()
        fingerprint = (stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns, stat.st_ctime_ns,
                       hashlib.sha256(content).digest())
    except OSError as error:
        if not isinstance(error, FileNotFoundError):
            logger.warning("Pronunciation lexicon unavailable at %s; continuing without substitutions: %s", path, error)
        _cache.update({"path": path, "fingerprint": None, "entries": {},
                       "pattern": None})
        return {}
    if not force and _cache["path"] == path and _cache["fingerprint"] == fingerprint:
        return dict(_cache["entries"])
    try:
        raw = json.load(io.StringIO(content.decode("utf-8")))
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
    # A key is a written name (#2316): the editor preview reads raw chunk text,
    # so fold the invisible characters before matching, as rendering already
    # does in speech preparation. Text without them is unchanged.
    text = remove_silent_word_characters(text)
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


MAX_OCCURRENCE_RULES = 32
MAX_OCCURRENCE_CANDIDATES = 64
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
    if restrictions.get('engine_mode') not in (None, 'local', 'external', 'mlx'):
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
        accepted_voices = (context.get('voice_sha256'), context.get('voice_sha256_legacy'))
        mismatch = next((key for key, value in rule.get('restrictions', {}).items()
                         if context.get(key) != value
                         and not (key == 'voice_sha256' and value in accepted_voices)), None)
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


HOMOGRAPH_MEANINGS = {
    'lead': ('guide — I lead the team.', 'metal — The pipe contains lead.'),
    'read': ('present tense — I read every day.', 'past tense — Yesterday I read the book.'),
    'wind': ('moving air — The wind blew.', 'turn or coil — Wind the clock.'),
}


def get_occurrence_candidate_anchor(candidate):
    return {key: value for key, value in candidate.items() if key not in ('before', 'after')}


def get_homograph_candidates(prepared, uid, text, language):
    result = {'analysis_status': 'unavailable', 'word_list': list(HOMOGRAPH_MEANINGS),
              'candidate_limit': MAX_OCCURRENCE_CANDIDATES, 'candidates': [], 'truncated': False}
    if not isinstance(language, str) or language.strip().lower() in ('', 'auto', 'unknown'):
        return result
    if language.strip().lower().replace('_', '-').split('-')[0] not in ('en', 'english'):
        result['analysis_status'] = 'unsupported_language'
        return result
    candidates = []
    truncated = False
    for spelling, meanings in HOMOGRAPH_MEANINGS.items():
        matches = get_occurrence_pronunciation_candidates(prepared, uid, text, spelling, ignore_case=True)
        truncated = truncated or matches['truncated']
        for candidate in matches['candidates']:
            candidates.append({
                'anchor': get_occurrence_candidate_anchor(candidate),
                'before': candidate['before'], 'after': candidate['after'], 'meanings': list(meanings),
            })
    candidates.sort(key=lambda candidate: candidate['anchor']['start'])
    result.update(analysis_status='completed', candidates=candidates[:MAX_OCCURRENCE_CANDIDATES],
                  truncated=truncated or len(candidates) > MAX_OCCURRENCE_CANDIDATES)
    return result


def get_occurrence_pronunciation_candidates(prepared, uid, text, query, *, ignore_case=False):
    """Offer bounded prepared-text spans, case-sensitive unless explicitly requested."""
    if not isinstance(query, str) or not query.strip() or len(query) > 128:
        raise ValueError('Choose an exact spelling of 1–128 characters.')
    if len(text) > 131072:
        raise ValueError('This line exceeds the occurrence editor text limit.')
    pattern = _compile({query: query}, ignore_case=ignore_case)
    anchors = {'coordinate_space': OCCURRENCE_COORDINATE_SPACE,
               'offset_unit': 'unicode_codepoint', 'uid': uid,
               'text_sha256': get_pronunciation_text_digest(text),
               'prepared_sha256': get_pronunciation_text_digest(prepared)}
    candidates = []
    for match in pattern.finditer(prepared):
        if len(candidates) == MAX_OCCURRENCE_CANDIDATES:
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


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
