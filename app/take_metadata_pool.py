"""Lossless, bounded take metadata representation; no audio or selection state."""
import copy
from dataclasses import dataclass
import hashlib
import re

_PATHS = {
    'text': (('captured', 'text'), str),
    'generation_inputs': (('captured', 'generation_inputs'), dict),
    'provenance': (('captured', 'provenance'), dict),
    'synthesis_policy': (('captured', 'synthesis_receipt', 'policy'), dict),
}
_DIGEST = re.compile(r'[0-9a-f]{64}')


@dataclass(frozen=True)
class CompactRecord:
    fields: tuple
    references: tuple
    encoded: bytes


@dataclass(frozen=True)
class CompactedMetadata:
    encoded: bytes
    records: dict


def _get(row, path):
    value = row
    for key in path:
        if type(value) is not dict or key not in value:
            return None
        value = value[key]
    return value


def _copy_parent(row, path):
    result = dict(row)
    parent, original = result, row
    for key in path[:-1]:
        if type(original.get(key)) is not dict:
            raise ValueError('Take payload reference has no valid parent.')
        parent[key] = dict(original[key])
        parent, original = parent[key], original[key]
    return result, parent


def compact_metadata(document, *, encode, minimum_saving=0,
                     fingerprints=None, previous_records=None):
    """Encode a candidate using immutable entries bound to freshly validated rows."""
    if fingerprints is None:
        fingerprints = tuple(hashlib.sha256(encode(row)).digest() for row in document['records'])
    if len(fingerprints) != len(document['records']):
        raise ValueError('Take metadata fingerprints do not match the records.')
    previous_records = previous_records or {}
    entries, candidates = {}, []
    for row, fingerprint in zip(document['records'], fingerprints):
        cached = previous_records.get(fingerprint)
        if cached is not None:
            fields = cached.fields
        else:
            fields = []
            for name, (path, kind) in _PATHS.items():
                value = _get(row, path)
                if type(value) is kind:
                    raw = encode(value)
                    if len(raw) >= 200:
                        fields.append((name, raw))
            fields = tuple(fields)
        values = []
        canonical_fields = []
        for name, raw in fields:
            entry = entries.get(raw)
            if entry is None:
                entry = dict(raw=raw, count=0)
                entries[raw] = entry
            entry['count'] += 1
            values.append((name, _PATHS[name][0], entry))
            canonical_fields.append((name, entry['raw']))
        candidates.append((values, tuple(canonical_fields), cached))
    # A reference hash alone costs 66 encoded bytes. Ignoring pool keys and
    # wrappers deliberately overestimates savings, so this shortcut cannot
    # discard a candidate which would meet the caller's whole-file threshold.
    upper_bound = 0
    for raw, entry in entries.items():
        saving = (len(raw) - 66) * entry['count'] - len(raw)
        if entry['count'] > 1 and saving > 0:
            entry['digest'] = hashlib.sha256(raw).hexdigest()
            upper_bound += saving
    if not upper_bound or upper_bound < minimum_saving:
        return None
    payloads, records, current_records = {}, [], {}
    for original, fingerprint, (values, fields, cached) in zip(document['records'], fingerprints, candidates):
        row, references = original, {}
        for name, path, entry in values:
            digest = entry.get('digest')
            if digest is None:
                continue
            references[name] = digest
            if digest not in payloads:
                payloads[digest] = entry['raw']
        signature = tuple(sorted(references.items()))
        if cached is not None and cached.references == signature:
            encoded = cached.encoded
        else:
            for name in references:
                path = _PATHS[name][0]
                row, parent = _copy_parent(row, path)
                del parent[path[-1]]
            encoded = encode(dict(record=row, references=references))
        records.append(encoded)
        current_records[fingerprint] = CompactRecord(fields, signature, encoded)
    # Each fragment came from the same canonical encoder. Reusing immutable
    # record bytes avoids serializing unchanged records again; the complete
    # manifest is still rebuilt and atomically replaced.
    pool = b','.join(encode(digest) + b':' + payloads[digest] for digest in sorted(payloads))
    data = b''.join((b'{"payloads":{', pool, b'},"record_version":', encode(document['version']),
                     b',"records":[', b','.join(records), b'],"version":3}'))
    return CompactedMetadata(data, current_records)


def expand_metadata(document, *, encode, max_records, max_record_bytes,
                    max_manifest_bytes, check_depth):
    """Reject unsupported references before returning the legacy semantic shape."""
    check_depth(document)
    if (type(document) is not dict
            or set(document) != {'version', 'record_version', 'records', 'payloads'}
            or type(document['version']) is not int or document['version'] != 3
            or type(document['record_version']) is not int or document['record_version'] not in (1, 2)
            or type(document['records']) is not list or len(document['records']) > max_records
            or type(document['payloads']) is not dict
            or len(document['payloads']) > len(document['records']) * len(_PATHS)):
        raise ValueError('Take history pooled manifest is invalid; existing audio was preserved.')
    payloads = document['payloads']
    for digest, value in payloads.items():
        if type(digest) is not str or not _DIGEST.fullmatch(digest) or type(value) not in (str, dict):
            raise ValueError('Take history payload is invalid.')
        raw = encode(value)
        if len(raw) > max_record_bytes or hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Take history payload hash or size is invalid.')
    result = dict(version=document['record_version'], records=[])
    size, used = len(encode(result)), set()
    for entry in document['records']:
        if (type(entry) is not dict or set(entry) != {'record', 'references'}
                or type(entry['record']) is not dict or type(entry['references']) is not dict
                or not set(entry['references']) <= set(_PATHS)):
            raise ValueError('Take history pooled record is invalid.')
        row = entry['record']
        for name, digest in entry['references'].items():
            path, kind = _PATHS[name]
            if type(digest) is not str or digest not in payloads or type(payloads[digest]) is not kind:
                raise ValueError('Take history payload reference is missing or invalid.')
            row, parent = _copy_parent(row, path)
            if path[-1] in parent:
                raise ValueError('Take history payload reference conflicts with recorded metadata.')
            parent[path[-1]] = copy.deepcopy(payloads[digest])
            used.add(digest)
        check_depth(row)
        row_bytes = len(encode(row))
        size += row_bytes + bool(result['records'])
        if row_bytes > max_record_bytes or size > max_manifest_bytes:
            raise ValueError('Expanded take history exceeds its metadata limits.')
        result['records'].append(row)
    if used != set(payloads):
        raise ValueError('Take history contains unreferenced payloads.')
    return result
