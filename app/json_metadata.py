"""Small strict JSON primitives shared by bounded metadata boundaries."""
import json


def get_unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON fields are not supported')
        result[key] = value
    return result


def get_metadata_json_bytes(value, limit, label):
    """Callers validate domain structure before this bounded encoding step."""
    parts, size = [], 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True,
                                 separators=(',', ':')).iterencode(value):
        raw = part.encode('utf-8'); size += len(raw)
        if size > limit:
            raise ValueError(f'{label} exceeds its byte limit')
        parts.append(raw)
    return b''.join(parts)
