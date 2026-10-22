"""Bounded, read-only continuity proposals for explicitly selected experiments.

This is not a rendering or take-reuse authority. A delivery digest must still
be combined with the actual voice/model/seed and serving contract by a caller.
Existing instruction/pause fields are explicit choices, including empty/None,
unless an experiment row explicitly declares its instruction generated.
"""
import re

from synthesis_recovery_identity import get_recovery_digest

MAX_CONTINUITY_ROWS = 512
MAX_CONTINUITY_BYTES = 2 * 1024**2
_VERBS = r'(?:said|asked|replied|whispered|shouted|answered|added|murmured|cried|exclaimed)'


def _get_text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or not empty and not value.strip()
            or any(0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ValueError('Invalid or oversized continuity text')
    return value


def _get_rows(entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CONTINUITY_ROWS:
        raise ValueError('Continuity requires between 1 and 512 rows')
    rows, ids, size = [], set(), 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('Continuity rows must be objects')
        row = {key: _get_text(entry.get(key), limit) for key, limit in (
            ('uid', 128), ('speaker', 256), ('text', 20000))}
        if row['uid'] in ids:
            raise ValueError('Continuity row identities must be unique')
        ids.add(row['uid'])
        if 'instruct' in entry:
            row['instruct'] = _get_text(entry['instruct'], 20000, empty=True)
        if 'instruction_origin' in entry:
            origin = entry['instruction_origin']
            if not isinstance(origin, str) or origin not in ('manual', 'generated') or 'instruct' not in row:
                raise ValueError('Instruction origin requires an instruction and must be manual or generated')
            row['instruction_origin'] = origin
        if 'pause_after' in entry:
            pause = entry['pause_after']
            if pause is not None and (type(pause) is not int or not 0 <= pause <= 3600000):
                raise ValueError('Invalid continuity pause')
            row['pause_after'] = pause
        size += sum(len(value.encode('utf-8')) for value in row.values() if isinstance(value, str))
        if size > MAX_CONTINUITY_BYTES:
            raise ValueError('Continuity input exceeds the 2 MiB limit')
        rows.append(row)
    return rows


def _get_ending(text):
    text = text.rstrip().rstrip('\"\'”’»」』')
    for endings, kind in ((('...', '…'), 'trailing'), (('—', '–', '-'), 'interrupted'),
                          (('?', '？'), 'question'), (('!', '！'), 'exclamation'),
                          (('.', '。'), 'period'), ((',', ';', ':', '，', '；', '：'), 'open')):
        if text.endswith(endings):
            return kind
    return 'unknown'


def _is_attached_tag(text, speaker):
    # Require the exact named speaker beside a reporting verb. Pronouns, aliases
    # and indirect speech remain unproven; no speaker assignment is invented.
    name = re.escape(speaker)
    return re.match(rf'^(?:{name}\s+{_VERBS}|{_VERBS}\s+{name})'
                    r'(?:\s+[a-z]{2,20}ly)?(?=[,.;:!?…]|$)',
                    text.strip().lstrip('\"\'“‘'), re.IGNORECASE) is not None


def _get_proposal(rows, index, narrators):
    row = rows[index]
    before = rows[index - 1] if index else None
    after = rows[index + 1] if index + 1 < len(rows) else None
    narrator = row['speaker'] in narrators
    ending = _get_ending(row['text'])
    prior = rows[index - 2] if index >= 2 else None
    resuming = (not narrator and prior is not None and before['speaker'] in narrators
                and prior['speaker'] == row['speaker'] and _is_attached_tag(before['text'], row['speaker'])
                and _get_ending(before['text']) == 'open' and _get_ending(prior['text']) in ('open', 'trailing'))
    if not narrator and after and after['speaker'] in narrators and _is_attached_tag(after['text'], row['speaker']):
        if ending in ('open', 'trailing', 'question', 'exclamation', 'period'):
            if ending == 'open':
                hint, pause = 'Carry the sentence into the attached narrator attribution without a full stop.', 120
            elif ending == 'trailing':
                hint, pause = 'Preserve the authored ellipsis and hesitant handoff to the narrator attribution.', 160
            else:
                hint, pause = f'Preserve the complete authored {ending} cadence before the narrator attribution.', 180
            if resuming:
                return 'resumed_speech_before_tag', 'Resume the same sentence after the narrator attribution. ' + hint, pause
            return 'speech_before_tag', hint, pause
    if narrator and before and before['speaker'] not in narrators and _is_attached_tag(row['text'], before['speaker']):
        if (after and after['speaker'] == before['speaker'] and ending == 'open'
                and _get_ending(before['text']) in ('open', 'trailing')):
            return 'parenthetical_tag', 'Read as a parenthetical attribution inside the same unfinished sentence.', 120
        if _get_ending(before['text']) in ('open', 'trailing', 'question', 'exclamation', 'period'):
            return 'attached_tag', 'Begin as an attached narrator attribution; respect any later authored sentence boundary.', None
    if ending == 'interrupted' and not narrator and after and after['speaker'] not in narrators and after['speaker'] != row['speaker']:
        if resuming:
            return 'resumed_interrupted_speech', 'Resume after the narrator attribution, then preserve the authored interruption at the dash without completing the sentence.', 0
        return 'interrupted_speech', 'Preserve the authored interruption at the dash without completing the sentence.', 0
    if resuming:
        return 'resumed_speech', 'Resume the same sentence after the narrator attribution; preserve the authored ending.', None
    if narrator and after and after['speaker'] not in narrators and ending in ('period', 'question', 'exclamation'):
        return 'independent_narration', 'Finish the complete authored sentence before the fresh dialogue turn.', None
    return None, None, None


def get_dialogue_continuity_plan(entries, *, narrator_speakers):
    """Return independent baseline/deterministic proposals, never mutate inputs.

    Narrator labels are explicit exact identities, not name-prefix guesses.
    Hidden-context synthesis/cropping is a separate, unqualified experiment.
    """
    rows = _get_rows(entries)
    if (not isinstance(narrator_speakers, (list, tuple)) or not 1 <= len(narrator_speakers) <= 16):
        raise ValueError('Declare between 1 and 16 narrator speaker identities')
    narrators = [_get_text(name, 256) for name in narrator_speakers]
    if len(set(narrators)) != len(narrators):
        raise ValueError('Narrator identities must be unique')
    narrators = set(narrators)
    planned = []
    for index, row in enumerate(rows):
        role, hint, pause = _get_proposal(rows, index, narrators)
        explicit = 'instruct' in row and row.get('instruction_origin') != 'generated'
        baseline_instruction = row.get('instruct', '')
        instruction = baseline_instruction if explicit else '\n'.join(
            part for part in (baseline_instruction, hint) if part)
        effective_pause = row['pause_after'] if 'pause_after' in row else pause
        baseline = {'text': row['text'], 'instruct': baseline_instruction,
                    'pause_after': row.get('pause_after')}
        proposed = {'text': row['text'], 'instruct': instruction, 'pause_after': effective_pause}
        planned.append({'uid': row['uid'], 'speaker': row['speaker'], 'role': role,
            'suggested_instruct': hint, 'suggested_pause_after_ms': pause,
            'instruction_source': 'explicit' if explicit else 'generated_with_suggestion' if hint and 'instruct' in row
                else 'suggestion' if hint else 'generated' if 'instruct' in row else 'default',
            'pause_source': 'explicit' if 'pause_after' in row else 'suggestion' if pause is not None else 'default',
            'context_digest': get_recovery_digest(rows[max(0, index - 2):index + 2]),
            'delivery_digest': get_recovery_digest({'uid': row['uid'], 'speaker': row['speaker'],
                                                   'text': row['text'], 'instruct': instruction}),
            'baseline': baseline, 'deterministic': proposed})
    return {'version': 1, 'source_digest': get_recovery_digest({'rows': rows, 'narrators': sorted(narrators)}),
            'rows': planned, 'pending_arms': ['hidden_context'],
            'qualification': 'planning_only; no audible benefit, rendering or take-reuse authority established'}


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_code
_LOADED_CONTINUITY_CODE_SHA256 = _get_loaded_code()
