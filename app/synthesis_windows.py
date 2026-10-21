"""Opt-in exact spans of already prepared speech; no backend limits are guessed."""
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
import hashlib
import unicodedata


MAX_WINDOW_SOURCE_CHARS = 1_000_000
MAX_SYNTHESIS_WINDOWS = 1024
_SENTENCE_END = frozenset('.!?。！？')
_CLAUSE_END = frozenset(',;:，；：—–')
_JOINERS = frozenset("'’ʼ-‐‑")


@dataclass(frozen=True)
class SynthesisWindow:
    start: int
    end: int
    text: str
    boundary: str


@dataclass(frozen=True)
class SynthesisPlan:
    text_sha256: str
    max_chars: int
    max_words: int | None
    windows: tuple[SynthesisWindow, ...]



def get_synthesis_plan_text(plan):
    """Validate a captured plan before dispatch or audio assembly."""
    if (type(plan) is not SynthesisPlan or type(plan.windows) is not tuple
            or not 1 <= len(plan.windows) <= MAX_SYNTHESIS_WINDOWS
            or type(plan.max_chars) is not int or not 1 <= plan.max_chars <= MAX_WINDOW_SOURCE_CHARS
            or plan.max_words is not None and (type(plan.max_words) is not int or not 1 <= plan.max_words <= 100000)):
        raise ValueError('Internal synthesis needs a bounded plan')
    cursor = 0
    for index, window in enumerate(plan.windows):
        if (type(window) is not SynthesisWindow or not isinstance(window.text, str)
                or type(window.start) is not int or type(window.end) is not int
                or window.start != cursor or window.end - window.start != len(window.text)
                or window.end > MAX_WINDOW_SOURCE_CHARS
                or not window.text or len(window.text) > plan.max_chars
                or plan.max_words is not None and len(window.text.split()) > plan.max_words
                or window.boundary not in ('paragraph', 'sentence', 'clause', 'continuation', 'end')
                or (window.boundary == 'end') != (index == len(plan.windows) - 1)
                or not any(c.isalnum() for c in window.text)):
            raise ValueError('Internal synthesis spans are incomplete or reordered')
        cursor = window.end
    text = ''.join(window.text for window in plan.windows)
    if (len(text) > MAX_WINDOW_SOURCE_CHARS
            or hashlib.sha256(text.encode('utf-8', errors='surrogatepass')).hexdigest() != plan.text_sha256):
        raise ValueError('Internal synthesis plan does not match its prepared text')
    return text


def _is_cjk(character):
    return unicodedata.name(character, '').startswith(('CJK', 'HIRAGANA', 'KATAKANA', 'HANGUL'))


def _base_before(text, end):
    index = end - 1
    while index > 0 and unicodedata.category(text[index]).startswith('M'):
        index -= 1
    return text[index]


def _numeric_separator(text, index):
    return (text[index] in '.,٫٬．，' and 0 < index < len(text) - 1
            and text[index - 1].isdigit() and text[index + 1].isdigit())


def _word_joiner(text, index):
    return (text[index] in _JOINERS and 0 < index < len(text) - 1
            and _base_before(text, index).isalnum() and text[index + 1].isalnum())


def _quote_seams(text):
    """Keep an opening/closing punctuation cluster with its spoken contents."""
    closing = bytearray(len(text))
    forbidden = bytearray(len(text) + 1)
    directional_closers = '’”»）)]}」』】〉》〕〗〙〛'
    openers = '‘“«（([{「『【〈《〔〖〘〚'
    ascii_open = {"'": False, '"': False}
    last_nonspace = -1
    pending_open = False
    for index, character in enumerate(text):
        is_close = False
        is_open = False
        if not _word_joiner(text, index):
            if character in ascii_open:
                starts_spoken = (index + 1 < len(text) and text[index + 1].isalnum()
                                 and (index == 0 or text[index - 1].isspace()))
                is_close = not starts_spoken and (ascii_open[character] or
                    (index > 0 and not text[index - 1].isspace()
                     and (text[index - 1].isalnum() or text[index - 1] in _SENTENCE_END)))
                is_open = not is_close
                ascii_open[character] = is_open
            else:
                is_close = character in directional_closers
                is_open = character in openers
        if is_close:
            closing[index] = 1
            for seam in range(last_nonspace + 1, index + 1):
                forbidden[seam] = 1
        if is_open:
            pending_open = True
            forbidden[index + 1] = 1
        elif pending_open and character.isspace():
            forbidden[index + 1] = 1
        elif not character.isspace():
            pending_open = False
        if not character.isspace():
            last_nonspace = index
    return closing, forbidden


def _can_split(text, position):
    raw_left, right = text[position - 1], text[position]
    if (raw_left == '\r' and right == '\n' or unicodedata.category(right).startswith('M')
            or raw_left == '\u200d' or right == '\u200d'):
        return False
    if raw_left.isspace() or right.isspace():
        return True
    left = _base_before(text, position)
    if _numeric_separator(text, position - 1) or _numeric_separator(text, position):
        return False
    if _word_joiner(text, position - 1) or _word_joiner(text, position):
        return False
    return not (left.isalnum() and right.isalnum()) or _is_cjk(left) or _is_cjk(right)


def plan_synthesis_windows(text, *, max_chars, max_words=None):
    """Partition prepared text within explicit budgets, preserving every codepoint.

    Words here are whitespace-separated spans. The character cap also bounds
    non-spaced languages. Latin alphanumeric runs and their internal joiners
    stay intact; CJK may split at character boundaries, keeping combining marks.
    Impossible spans fail explicitly instead of dropping text or exceeding a cap.
    This planner does no speech normalization, pronunciation or audio work.
    """
    if (not isinstance(text, str) or len(text) > MAX_WINDOW_SOURCE_CHARS
            or type(max_chars) is not int or not 1 <= max_chars <= MAX_WINDOW_SOURCE_CHARS
            or (max_words is not None and (type(max_words) is not int or not 1 <= max_words <= 100_000))):
        raise ValueError('Synthesis windows need bounded text and positive integer budgets')
    digest = hashlib.sha256(text.encode('utf-8', errors='surrogatepass')).hexdigest()
    if not text:
        return SynthesisPlan(digest, max_chars, max_words, ())
    if len(text) <= max_chars and (max_words is None or len(text.split()) <= max_words):
        if not any(character.isalnum() for character in text):
            raise ValueError('Synthesis window text has no spoken content')
        return SynthesisPlan(digest, max_chars, max_words, (SynthesisWindow(0, len(text), text, 'end'),))
    alnums, words, positions = [], [], [0]
    boundaries = {'paragraph': [], 'sentence': [], 'clause': [], 'continuation': []}
    closing, forbidden = _quote_seams(text)
    line_breaks, prior_kind = 0, 'continuation'
    for index, character in enumerate(text):
        if character.isalnum():
            alnums.append(index)
        if not character.isspace() and (index == 0 or text[index - 1].isspace()):
            words.append(index)
        if character == '\r' or character == '\n' and (index == 0 or text[index - 1] != '\r'):
            line_breaks += 1
        elif not character.isspace():
            line_breaks = 0
        if character.isspace():
            kind = 'paragraph' if line_breaks >= 2 else prior_kind
        elif character in _SENTENCE_END and not _numeric_separator(text, index):
            kind = 'sentence'
        elif character in _CLAUSE_END and not _numeric_separator(text, index):
            kind = 'clause'
        elif closing[index]:
            kind = prior_kind
        else:
            kind = 'continuation'
        prior_kind = kind
        end = index + 1
        if end == len(text) or (not forbidden[end] and _can_split(text, end)):
            boundaries[kind].append(end)
            positions.append(end)
    if not alnums:
        raise ValueError('Synthesis window text has no spoken content')

    def hard_limit(start):
        end = min(len(text), start + max_chars)
        if max_words is not None:
            first_word = bisect_left(words, start)
            partial_word = int(start > 0 and not text[start].isspace() and not text[start - 1].isspace())
            excess = first_word + max_words - partial_word
            if excess < len(words):
                end = min(end, words[excess])
        return end

    def first_spoken_end(start):
        index = bisect_left(alnums, start)
        return alnums[index] + 1 if index < len(alnums) else len(text) + 1

    # Only select seams from which the remainder can also be partitioned.
    # Reachable end positions are appended in descending order, allowing an
    # interval-existence query by bisect without quadratic suffix rescanning.
    reachable = bytearray(len(text) + 1)
    reachable[len(text)] = 1
    ends = [-len(text)]
    for start in reversed(positions[:-1]):
        candidate = bisect_left(ends, -hard_limit(start))
        if candidate < len(ends) and ends[candidate] <= -first_spoken_end(start):
            reachable[start] = 1
            ends.append(-start)
    if not reachable[0]:
        raise ValueError('No complete speech span fits the synthesis window budget; increase the limit')
    boundaries = {kind: [end for end in values if reachable[end]] for kind, values in boundaries.items()}
    result = []
    start = 0
    while start < len(text):
        hard_end = hard_limit(start)
        minimum_end = first_spoken_end(start)
        chosen = None
        if hard_end == len(text):
            chosen = (hard_end, 'end')
        else:
            for kind, values in boundaries.items():
                candidate = bisect_right(values, hard_end) - 1
                if candidate >= 0 and values[candidate] >= minimum_end:
                    chosen = (values[candidate], kind)
                    break
        if chosen is None:
            raise RuntimeError('Reachable synthesis window has no continuation')
        end, kind = chosen
        part = text[start:end]
        if len(part) > max_chars or (max_words is not None and len(part.split()) > max_words):
            raise RuntimeError('Synthesis window planner exceeded its budget')
        if len(result) >= MAX_SYNTHESIS_WINDOWS:
            raise ValueError('Synthesis request exceeds the internal window count limit; increase the window budget')
        result.append(SynthesisWindow(start, end, part, kind))
        start = end
    if ''.join(window.text for window in result) != text:
        raise RuntimeError('Synthesis windows lost source coverage')
    return SynthesisPlan(digest, max_chars, max_words, tuple(result))
