"""Pure voice-timeline rebasing for retained scope rows."""
import copy
import json

MAX_SCOPE_VOICE_MAPPING_BYTES = 8 * 1024**2
from tts import get_chunk_voice_inputs, get_style_timeline_index


def get_scope_voice_plan(voices, chunks, chunk_start):
    """Keep effective voice inputs at each retained row's new position.

    This compares voice metadata, not referenced audio/adapter file bytes.
    The outer planner must bind those artifacts and the saved configuration.
    """
    if (not isinstance(voices, dict) or not isinstance(chunks, list) or not chunks
            or any(not isinstance(row, dict) for row in chunks)
            or type(chunk_start) is not int or not 0 < chunk_start < len(chunks)):
        raise ValueError('A voice configuration and explicit retained chunk boundary are required.')
    rebased = copy.deepcopy(voices)
    for data in rebased.values():
        if not isinstance(data, dict):
            raise ValueError('Voice configuration needs inspection before changing source scope.')
        for key in ('style_timeline', 'version_timeline'):
            if key not in data or data[key] is None:
                continue
            points = data[key]
            if not isinstance(points, list) or any(not isinstance(point, dict) for point in points):
                raise ValueError('Voice timeline needs inspection before changing source scope.')
            ordered = sorted(((get_style_timeline_index(point), point) for point in points),
                             key=lambda item: item[0])
            active = [point for index, point in ordered if index <= chunk_start]
            future = [(index, point) for index, point in ordered if index > chunk_start]
            data[key] = ([dict(active[-1], from_index=0)] if active else []) + [
                dict(point, from_index=index - chunk_start) for index, point in future]
    mappings = []
    mapping_bytes = 0
    for old_index in range(chunk_start, len(chunks)):
        row = chunks[old_index]
        new_index = old_index - chunk_start
        before = get_chunk_voice_inputs(voices, row, old_index)
        after = get_chunk_voice_inputs(rebased, row, new_index)
        if before != after:
            raise ValueError('Changing scope would change a retained chunk voice.')
        mapping = {'uid': row.get('uid'), 'old_index': old_index, 'new_index': new_index,
                   'speaker': row.get('speaker'), 'effective_voices': before}
        mapping_bytes += len(json.dumps(mapping, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        if mapping_bytes > MAX_SCOPE_VOICE_MAPPING_BYTES:
            raise ValueError('Retained voice mappings exceed the bounded scope preview size.')
        mappings.append(mapping)
    return {'voices': rebased, 'retained': mappings}
