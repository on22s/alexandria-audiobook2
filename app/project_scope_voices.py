"""Pure voice-timeline rebasing for retained scope rows.

The real caller passes the FULL book; ``chunk_start`` is the first retained
row. Anchors in the removed prefix become a legacy boundary point (with the
orphaned uid removed), so retained rows keep the voice they already had.
"""
import copy
import json

MAX_SCOPE_VOICE_MAPPING_BYTES = 8 * 1024**2
from tts import get_chunk_voice_inputs
from timeline_anchors import (TIMELINE_KEYS, build_uid_index, resolve_timeline_anchors,
                              resolve_timeline_index)


def get_scope_voice_plan(voices, chunks, chunk_start):
    """Keep effective voice inputs at each retained row's new position.

    This compares voice metadata, not referenced audio/adapter file bytes.
    The outer planner must bind those artifacts and the saved configuration.
    """
    if (not isinstance(voices, dict) or not isinstance(chunks, list) or not chunks
            or any(not isinstance(row, dict) for row in chunks)
            or type(chunk_start) is not int or not 0 < chunk_start < len(chunks)):
        raise ValueError('A voice configuration and explicit retained chunk boundary are required.')
    full_uid_index = build_uid_index(chunks)
    retained = chunks[chunk_start:]
    retained_uids = {row.get('uid') for row in retained if isinstance(row.get('uid'), str)}
    rebased = copy.deepcopy(voices)
    for data in rebased.values():
        if not isinstance(data, dict):
            raise ValueError('Voice configuration needs inspection before changing source scope.')
        for key in TIMELINE_KEYS:
            if key not in data or data[key] is None:
                continue
            points = data[key]
            if not isinstance(points, list) or any(not isinstance(point, dict) for point in points):
                raise ValueError('Voice timeline needs inspection before changing source scope.')
            ordered = []
            for point in points:
                try:
                    old_index = resolve_timeline_index(point, full_uid_index)
                except (TypeError, ValueError) as exc:
                    raise ValueError('Voice timeline needs inspection before changing source scope.') from exc
                ordered.append((old_index, point))
            ordered.sort(key=lambda item: item[0])
            rebuilt = []
            active = [point for index, point in ordered if index <= chunk_start]
            if active:
                boundary = dict(active[-1], from_index=0)
                uid = boundary.get('from_uid')
                if isinstance(uid, str) and uid not in retained_uids:
                    boundary.pop('from_uid', None)
                rebuilt.append(boundary)
            rebuilt.extend(dict(point, from_index=index - chunk_start)
                           for index, point in ordered if index > chunk_start)
            data[key] = rebuilt
    after_uid_index = {row['uid']: index for index, row in enumerate(retained)
                       if isinstance(row.get('uid'), str) and row.get('uid')}
    before_voices = resolve_timeline_anchors(voices, uid_index=full_uid_index)
    after_voices = resolve_timeline_anchors(rebased, uid_index=after_uid_index)
    mappings = []
    mapping_bytes = 0
    for new_index, row in enumerate(retained):
        old_index = chunk_start + new_index
        before = get_chunk_voice_inputs(before_voices, row, old_index)
        after = get_chunk_voice_inputs(after_voices, row, new_index)
        if before != after:
            raise ValueError('Changing scope would change a retained chunk voice.')
        mapping = {'uid': row.get('uid'), 'old_index': old_index, 'new_index': new_index,
                   'speaker': row.get('speaker'), 'effective_voices': before}
        mapping_bytes += len(json.dumps(mapping, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        if mapping_bytes > MAX_SCOPE_VOICE_MAPPING_BYTES:
            raise ValueError('Retained voice mappings exceed the bounded scope preview size.')
        mappings.append(mapping)
    return {'voices': rebased, 'retained': mappings}
