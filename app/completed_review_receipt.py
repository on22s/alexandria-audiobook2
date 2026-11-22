"""Completed review evidence, separate from resumable checkpoints."""
import hashlib
import json
import os
from utils import atomic_json_write, file_lock

VERSION = 1
REVIEW_ALGORITHM_VERSION = 11
REVIEW_SOURCE_SPAN_ALGORITHM_VERSION = 13


def get_review_entries_fingerprint(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def get_review_sidecar_identity(path):
    if not path:
        return None
    identity = {'path': os.path.abspath(path)}
    try:
        with open(path, 'rb') as stream:
            identity['sha256'] = hashlib.sha256(stream.read()).hexdigest()
    except FileNotFoundError:
        identity['sha256'] = None
    return identity


def get_review_progress_paths(output_path):
    """Return the resumable checkpoint and completed receipt for one script."""
    return (output_path + '.review_checkpoint.json', output_path + '.review_completed.json')


def get_review_receipt_path(output_path):
    return get_review_progress_paths(output_path)[1]


def read_validated_completed_review(input_path, output_path, input_sha256, fingerprint,
                                    legacy_fingerprints=(), *, companions=None):
    """Validate the flat receipt once and return (matched, reason, identity).

    `identity` carries the exact digests and companion identities validated by
    this one read. A caller migrating a legacy receipt MUST use these values:
    a digest captured before validation or a later re-read of a mutable path
    can authenticate unrelated bytes that were never part of the reviewed
    publication (the flat ABA/double-replacement race).
    """
    acceptable = {fingerprint}
    acceptable.update(item for item in legacy_fingerprints if isinstance(item, str))
    identity = {'output_sha256': None, 'input_sha256': None, 'output_entry_count': None,
                'companions': companions if isinstance(companions, dict)
                else {'aliases': None, 'voice': None}}
    path = get_review_receipt_path(output_path)
    try:
        with open(path, encoding='utf-8') as stream:
            receipt = json.load(stream)
        if (not isinstance(receipt, dict) or type(receipt.get('version')) is not int
                or receipt['version'] != VERSION):
            return False, 'unsupported completed-review receipt', identity
        if (receipt.get('input_path') != os.path.abspath(input_path)
                or receipt.get('output_path') != os.path.abspath(output_path)
                or receipt.get('fingerprint') not in acceptable):
            return False, 'completed-review settings changed', identity
        with open(output_path, encoding='utf-8') as stream:
            output = json.load(stream)
        if not isinstance(output, list) or not output:
            return False, 'completed-review output is invalid', identity
        output_sha = get_review_entries_fingerprint(output)
        if receipt.get('output_sha256') != output_sha:
            return False, 'completed-review output changed', identity
        expected = output_sha if os.path.abspath(input_path) == os.path.abspath(output_path) else receipt.get('input_sha256')
        if input_sha256 != expected:
            return False, 'completed-review input changed', identity
        if os.path.exists(get_review_progress_paths(output_path)[0]):
            return False, 'an incomplete review checkpoint remains', identity
        identity['output_sha256'] = output_sha
        identity['input_sha256'] = expected
        identity['output_entry_count'] = len(output)
        return True, 'No changes since the completed review; use --force-review to rerun.', identity
    except (OSError, ValueError, TypeError) as error:
        return False, f'completed-review receipt unavailable: {type(error).__name__}', identity


def get_completed_review_match(input_path, output_path, input_sha256, fingerprint,
                               legacy_fingerprints=()):
    """Return (matched, reason). See ``read_validated_completed_review``."""
    matched, reason, _identity = read_validated_completed_review(
        input_path, output_path, input_sha256, fingerprint, legacy_fingerprints)
    return matched, reason


def save_completed_review_receipt(input_path, output_path, input_sha256, output_sha256, fingerprint, review_pass='forward'):
    """Publish flat fields and retain only facts validated in one read.

    A concurrent replacement with an unsupported version must never carry its
    facts into a current-version receipt, so the read, validation and write are
    one locked operation over the exact document seen here.
    """
    payload = {'version': VERSION, 'input_path': os.path.abspath(input_path),
        'output_path': os.path.abspath(output_path), 'input_sha256': input_sha256,
        'output_sha256': output_sha256, 'fingerprint': fingerprint,
        'review_pass': review_pass}
    path = get_review_receipt_path(output_path)
    with file_lock(path):
        existing = load_review_receipt_document(output_path)
        if isinstance(existing, dict) and type(existing.get('version')) is int \
                and existing['version'] == VERSION:
            retained = {direction: fact for direction in REVIEW_DIRECTIONS
                if (fact := get_durable_review_fact(existing, direction)) is not None}
            if retained:
                payload['facts'] = retained
        atomic_json_write(payload, path, allow_nonatomic_fallback=False)


def remove_completed_review_receipt(output_path):
    try:
        os.remove(get_review_receipt_path(output_path))
    except FileNotFoundError:
        pass


# ── Durable directional proof ────────────────────────────────────────────────
# A bidirectional run publishes twice. A single flat receipt cannot hold both
# passes' completion, and a separate sidecar collides with saved-book names.
# Both passes therefore keep their proof under "facts" in the one reserved
# receipt. Only the child computes identity (get_completed_review_fingerprint);
# these readers just validate what the child wrote, read-only.

REVIEW_SUMMARY_FIELDS = (
    'text_changed', 'speaker_changed', 'instruct_changed', 'annotations_changed',
    'entries_changed', 'entries_added', 'entries_removed', 'narrators_merged',
    'speakers_merged', 'batches_failed', 'batches_skipped_vram', 'total_changes',
)
REVIEW_DIRECTIONS = ('forward', 'backward')
REVIEW_COMPANION_KEYS = ('aliases', 'voice')


def is_sha256(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in '0123456789abcdef' for char in value))


def _read_receipt_unlocked(path):
    try:
        with open(path, encoding='utf-8') as stream:
            document = json.load(stream)
    except (OSError, ValueError, TypeError):
        return None
    return document if isinstance(document, dict) else None


def load_review_receipt_document(output_path):
    return _read_receipt_unlocked(get_review_receipt_path(output_path))


def _absolute_string_path(value):
    """Normalise a path field, returning None on anything but a real string."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return os.path.abspath(value)
    except (TypeError, ValueError, OSError):
        return None


def get_durable_review_fact(document, direction):
    """Return a strict-validated forward/backward fact, else None.

    A malformed fact is always a cache miss: an unsupported document version,
    non-string path, missing digest or bad summary must neither complete a run
    nor crash the caller (including a forced rerun). Older facts without the
    transaction-bound identity fields are deliberately not accepted.
    """
    if not isinstance(document, dict) or direction not in REVIEW_DIRECTIONS:
        return None
    if type(document.get('version')) is not int or document['version'] != VERSION:
        return None
    facts = document.get('facts')
    if not isinstance(facts, dict):
        return None
    fact = facts.get(direction)
    if not isinstance(fact, dict):
        return None
    if fact.get('direction') != direction:
        return None
    for key in ('input_path', 'output_path'):
        normalised = _absolute_string_path(fact.get(key))
        if normalised is None or fact[key] != normalised:
            return None
    if not all(is_sha256(fact.get(key)) for key in
               ('fingerprint', 'input_sha256', 'output_sha256', 'settings_fingerprint')):
        return None
    for key in ('entries_before', 'entries_after'):
        if type(fact.get(key)) is not int or fact[key] < 0:
            return None
    consumed = fact.get('consumed_forward_sha256')
    consumed_fp = fact.get('consumed_forward_fingerprint')
    if consumed is not None and not is_sha256(consumed):
        return None
    if consumed_fp is not None and not is_sha256(consumed_fp):
        return None
    # The cross-direction proof is a pair. A half-present pair is never a valid
    # fact: it cannot be a standalone completion (both fields absent) nor a
    # verified bound proof (both fields present).
    if is_sha256(consumed) != is_sha256(consumed_fp):
        return None
    companions = fact.get('companions')
    if not isinstance(companions, dict):
        return None
    for key, identity in companions.items():
        if key not in REVIEW_COMPANION_KEYS:
            return None
        if identity is not None and not isinstance(identity, dict):
            return None
        if isinstance(identity, dict):
            if not isinstance(identity.get('path'), str):
                return None
            sha = identity.get('sha256')
            if sha is not None and not is_sha256(sha):
                return None
    summary = fact.get('summary')
    if not isinstance(summary, dict):
        return None
    if any(type(summary.get(key)) is not int or summary[key] < 0
           for key in REVIEW_SUMMARY_FIELDS):
        return None
    if type(summary.get('accounting_unverified')) is not bool:
        return None
    return fact


def read_durable_review_fact(output_path, direction):
    return get_durable_review_fact(load_review_receipt_document(output_path), direction)


def build_review_fact(input_path, output_path, input_sha256, output_sha256, fingerprint,
                      companions, summary, *, settings_fingerprint=None,
                       consumed_forward_sha256=None, consumed_forward_fingerprint=None,
                       parent_input_sha256=None, parent_publication_sha256=None,
                       parent_entry_count=None,
                       entries_before=0, entries_after=0):
    """Build one durable fact from the publication transaction's own digests.

    ``output_sha256`` is the digest of the bytes the publisher committed, and
    ``companions`` are the identities of the bytes it committed. Callers must
    not re-read the output after publication to derive either.
    """
    return {
        'direction': None,
        'input_path': os.path.abspath(input_path),
        'output_path': os.path.abspath(output_path),
        'input_sha256': input_sha256,
        'output_sha256': output_sha256,
        'fingerprint': fingerprint,
        'settings_fingerprint': settings_fingerprint,
        'consumed_forward_sha256': consumed_forward_sha256,
        'consumed_forward_fingerprint': consumed_forward_fingerprint,
        'parent_input_sha256': parent_input_sha256,
        'parent_publication_sha256': parent_publication_sha256,
        'parent_entry_count': parent_entry_count,
        'entries_before': entries_before,
        'entries_after': entries_after,
        'companions': dict(companions),
        'summary': {key: summary.get(key, 0) for key in REVIEW_SUMMARY_FIELDS}
                   | {'accounting_unverified': bool(summary.get('accounting_unverified'))},
    }


def save_durable_review_fact(output_path, direction, fact, *, top_level=None):
    """Store one direction's fact without destroying the other's.

    The flat fields are updated too so the flat matcher and older readers keep
    working for single-pass reviews; the durable facts stay authoritative for
    bidirectional reuse.
    """
    path = get_review_receipt_path(output_path)
    with file_lock(path):
        document = load_review_receipt_document(output_path)
        if document is None or type(document.get('version')) is not int or document['version'] != VERSION:
            document = {'version': VERSION}
        if top_level:
            document.update(top_level)
        facts = document.get('facts')
        if not isinstance(facts, dict):
            facts = document['facts'] = {}
        stored = dict(fact)
        stored['direction'] = direction
        facts[direction] = stored
        atomic_json_write(document, path, allow_nonatomic_fallback=False)


def invalidate_flat_review_receipt(output_path, direction='forward'):
    """Retire prior completion for a new run.

    A new forward run supersedes any earlier completion, so its whole receipt
    is removed. A new backward run supersedes only the previous backward pass:
    the stale flat fields and the superseded backward fact are retired while a
    strict-validated forward fact is retained. Facts from an unsupported
    version are never promoted into a current-version receipt.
    """
    path = get_review_receipt_path(output_path)
    with file_lock(path):
        document = _read_receipt_unlocked(path)
        if direction == 'forward':
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return
        forward = get_durable_review_fact(document, 'forward')
        supported = (isinstance(document, dict) and type(document.get('version')) is int
                     and document['version'] == VERSION)
        if supported and forward is not None:
            atomic_json_write({'version': VERSION, 'facts': {'forward': forward}},
                              path, allow_nonatomic_fallback=False)
        else:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def resolve_legacy_review_fact(input_path, output_path, input_sha256, output_sha256,
                               fingerprint, direction, *, companions=None,
                               settings_fingerprint=None, entries_before=0, entries_after=0):
    """A validated flat receipt becomes a durable fact with unknown counters.

    The original counters were never stored, so they are reported as zero with
    ``accounting_unverified`` set rather than presented as measured values. The
    companion identities are the ones validated before migration, otherwise the
    promoted fact could not match the very next unchanged run.
    """
    return build_review_fact(input_path, output_path, input_sha256, output_sha256,
        fingerprint, companions or {}, {key: 0 for key in REVIEW_SUMMARY_FIELDS}
        | {'accounting_unverified': True}, settings_fingerprint=settings_fingerprint,
        entries_before=entries_before, entries_after=entries_after)
