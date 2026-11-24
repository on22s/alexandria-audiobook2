"""Read-only, deterministic checks for annotated audiobook scripts."""

import difflib
import re
import shlex
import sys
import unicodedata
from collections import Counter

from speech_text import get_speech_normalization
from utils import get_unsafe_text_controls


_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
# `\w` includes UNDERSCORE, and Project Gutenberg marks italics with it:
# the source says "explain _myself_", so the token was "_myself_" and never
# matched the model's "myself". 23 of the 28 public-domain novels use the
# convention, so every one of them was being measured against a source whose
# emphasised words could not be matched.
#
# Alice in Wonderland was discarded over exactly this - all 919 entries, all
# 25 chunks generated - because a duplicated pair scored "not in the source"
# when the only difference was two underscores the model correctly dropped.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_FRONT_MATTER_RE = re.compile(
    r"\b(?:copyright|all rights reserved|isbn(?:-1[03])?|table of contents)\b",
    re.IGNORECASE,
)
_NARRATION_RE = re.compile(
    r"\b(?:he|she|they|subaru|emilia)\s+(?:said|asked|replied|thought|looked|felt|was|were)\b",
    re.IGNORECASE,
)
_SCRIPT_PREFIXES = ("LATIN", "CYRILLIC", "HIRAGANA", "KATAKANA", "CJK",
                    "ARABIC", "HEBREW", "HANGUL", "GREEK", "THAI")


def _character_script(character):
    name = unicodedata.name(character, "")
    return next((prefix for prefix in _SCRIPT_PREFIXES if name.startswith(prefix)), None)


def get_unicode_scripts(text):
    """Read the Unicode script identities of this exact text snapshot."""
    return frozenset(script for char in str(text or "")
                     if (script := _character_script(char)))


def audit_unicode_text(text, source_text=None, *, source_scripts=None):
    """Describe Unicode scripts and source-unsupported characters without mutation."""
    text = str(text or "")
    scripts = sorted(get_unicode_scripts(text))
    if source_scripts is None:
        source_scripts = get_unicode_scripts(source_text)
    introduced = (sorted(set(scripts) - source_scripts) if source_text is not None
                  else sorted(set(scripts) - {"LATIN"}))
    controls = get_unsafe_text_controls(text)
    mixed = []
    for match in _WORD_RE.finditer(text):
        word_scripts = sorted(get_unicode_scripts(match.group()))
        if "LATIN" in word_scripts and len(word_scripts) > 1:
            mixed.append({"text": match.group(), "scripts": word_scripts,
                          "offset": match.start()})
    return {"normalization": "NFC", "is_nfc": text == unicodedata.normalize("NFC", text),
            "scripts": scripts, "introduced_scripts": introduced,
            "replacement_character_count": text.count("\ufffd"),
            "unsafe_controls": controls,
            "format_controls": sorted({f"U+{ord(char):04X}" for char in text
                                       if unicodedata.category(char) == "Cf"}),
            "mixed_script_words": mixed}


def is_possible_misattributed_narration(text, speaker):
    return (bool(speaker) and speaker.casefold() not in {"narrator", "narration"}
            and bool(_NARRATION_RE.search(text)))


def _normalize(value):
    return " ".join(str(value or "").split()).casefold()


def get_normalized_word_tokens(value):
    """Read immutable word tokens using the existing preflight normalization."""
    return tuple(_WORD_RE.findall(_normalize(value)))


def _normalize_words(value):
    return " ".join(get_normalized_word_tokens(value))


def _finding(severity, code, message, entry_numbers=None, **details):
    result = {"severity": severity, "code": code, "message": message}
    if entry_numbers:
        result["entry_numbers"] = entry_numbers
    if details:
        result["details"] = details
    return result


# ONE definition of how much decode damage a source may carry, used by every
# path that generates a script. Before this existed the same question had three
# different answers: generate_script graded it, three_pass_generate refused any
# count, and the per-entry audit blocked at any count - so a repaired book
# generated single-pass and was refused three-pass, for the same input.
#
# 0.5% sits between index18 corrupt (1.40%) and index18 after deterministic
# repair (0.26%), so a mis-decoded file is still rejected and a repaired one
# runs. Raising it is a decision about accepting damaged prose, and belongs
# here where every caller sees the same number.
MAX_REPLACEMENT_SHARE = 0.005


def replacement_repair_hint(source_path=None):
    """The exact command that fixes a mis-decoded source.

    A refusal that only states a percentage leaves the reader to discover that
    a repairer exists. Both generators refuse for the same reason, so they say
    the same thing, from here.
    """
    target = source_path or "<source.txt>"
    shell_hint = ""
    command = "cd app && env/bin/python repair_source_encoding.py --apply -- " + shlex.quote(str(target))
    if sys.platform == "win32":
        shell_hint = " in PowerShell"
        executable = "'" + sys.executable.replace("'", "''") + "'"
        argument = "'" + str(target).replace("'", "''") + "'"
        command = f"cd app; & {executable} repair_source_encoding.py --apply -- {argument}"
    return (
        "This is a decoding error, not lost content: the file was read with "
        "the wrong codec and every non-ASCII character became U+FFFD. Repair "
        f"it with{shell_hint}\n"
        f"    {command}\n"
        "which writes <source>.repaired.txt beside the original, leaves the "
        "original untouched, and reports every substitution. It refuses to "
        "write if a substitution would break sentence structure."
    )


def replacement_load_is_acceptable(count, length):
    """True when a text's replacement characters are within policy.

    Callers must not re-derive this. A gate that answers the same question a
    second time is a gate that will eventually answer it differently.
    """
    if not count:
        return True
    return (count / max(1, length)) <= MAX_REPLACEMENT_SHARE


def get_source_phrase_occurrences(source_normalized, phrase_normalized):
    """Count literal whole-token phrases in word-normalized source text."""
    if not source_normalized or not phrase_normalized:
        return 0
    pattern = r"(?<!\S)" + re.escape(phrase_normalized) + r"(?!\S)"
    return sum(1 for _match in re.finditer(pattern, source_normalized))


def get_source_phrase_counter(source_normalized):
    """Return a counter bound to this immutable source; index only when queried."""
    positions = None

    def ensure_occurrence_count(phrase_normalized):
        nonlocal positions
        if not source_normalized or not phrase_normalized:
            return 0
        if phrase_normalized != " ".join(phrase_normalized.split()):
            return get_source_phrase_occurrences(source_normalized, phrase_normalized)
        if positions is None:
            positions = {}
            for match in re.finditer(r"\S+", source_normalized):
                positions.setdefault(match.group(), []).append(match.start())
        count = 0
        next_start = 0
        first_word = phrase_normalized.split(" ", 1)[0]
        for start in positions.get(first_word, []):
            end = start + len(phrase_normalized)
            if (start >= next_start and source_normalized.startswith(phrase_normalized, start)
                    and (end == len(source_normalized) or source_normalized[end].isspace())):
                count += 1
                next_start = end
        return count

    return ensure_occurrence_count


def source_occurrences_for_text(source_normalized, text_normalized,
                                minimum_tokens=5, *, count_phrase=None):
    """How many times the source carries this entry's content.

    WHY NOT A PLAIN `count`. Two things break exact matching, and both are
    normal rather than defects:

      * Entries are not contiguous spans - narration sits between two spoken
        lines, so joining them finds nothing.
      * The model modernises archaic orthography. Carroll writes "ca'n't";
        the model emits "can't", so Alice's line matches her own book zero
        times while being an obviously faithful rendering.

    Exact matching therefore reports 0 - "the model invented this" - for text
    that is plainly in the book. Alice in Wonderland generated all 25 chunks
    and was discarded, all 919 entries, over two lines that differ from the
    source by one apostrophe.

    So: use the LONGEST window of the entry that the source actually
    contains, and report how often that window occurs. Longest is the most
    distinctive, which keeps the count meaningful - a short common phrase
    would appear everywhere and make a duplication look faithful.

    Entries shorter than `minimum_tokens` require their complete exact phrase;
    longer entries require a matching window of at least `minimum_tokens` words.
    """
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    tokens = text_normalized.split()
    if not tokens or not source_normalized:
        return 0
    if len(tokens) < minimum_tokens:
        return count_phrase(" ".join(tokens))
    for size in range(len(tokens), minimum_tokens - 1, -1):
        best = 0
        for start in range(0, len(tokens) - size + 1):
            window = " ".join(tokens[start:start + size])
            best = max(best, count_phrase(window))
        if best:
            return best
    return 0


def get_source_block_occurrences(source_normalized, block, *, count_phrase=None):
    """Count contiguous support, then require support for every emitted line."""
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    contiguous = count_phrase(_normalize_words(" ".join(block)))
    if source_normalized and not contiguous:
        return min((source_occurrences_for_text(
            source_normalized, _normalize_words(text), count_phrase=count_phrase)
                    for text in block), default=0)
    return contiguous


def find_contained_source_duplicates(entries, source_text, *, source_normalized=None):
    """Reject proven excess quotation copies; never delete or trust greedy mapping.

    Compare distinct whole-token source occurrences with quoted occurrences in
    larger entries plus separate character entries. Plain narrative rendering
    can introduce words, so other containment is review-only. Known expansions
    add conservative capacity; unknown number/sound rendering cannot prove loss.
    Span coordinates below are word-normalized, not raw source offsets.
    """
    from dialogue_spans import spoken_spans

    def canonical(text):
        return _normalize_words(re.sub(r"(?<=\w)['’](?=\w)", "", str(text or "")))

    source_text = str(source_text or "")
    if len(source_text) + sum(len(e.get("text", "")) for e in entries
                              if isinstance(e, dict) and isinstance(e.get("text"), str)) > 4000000:
        return [_finding("manual_review", "contained_duplicate_budget_exhausted",
                         "Quotation provenance input exceeded its size budget; review without deleting text.",
                         [], character_budget=4000000)]
    texts = [canonical(e.get("text")) if isinstance(e, dict) else "" for e in entries]
    character_rows = {i for i, e in enumerate(entries) if isinstance(e, dict)
                      and str(e.get("speaker") or e.get("type") or "").casefold()
                      not in {"", "narrator", "narration"}}
    quoted = [[canonical(e["text"][a:b]) for a, b in spoken_spans(e["text"], "paired_quotes")]
              if isinstance(e, dict) and isinstance(e.get("text"), str) else [] for e in entries]
    # Token Aho-Corasick: scan each source/output region once, not once per
    # character phrase. Suffix matches have a fixed budget; exhaustion abstains.
    from collections import deque
    needles = list(dict.fromkeys(texts[i] for i in sorted(character_rows) if texts[i]))
    if not needles:
        return []
    # Reuse the audit word view unless apostrophe joining needs a different view.
    source = (source_normalized if source_normalized is not None
              and not re.search(r"(?<=\w)['’](?=\w)", source_text)
              else canonical(source_text))
    transitions, failure, terminal, suffix = [{}], [0], [None], [0]
    lengths = []
    for ident, needle in enumerate(needles):
        words = needle.split()
        lengths.append(len(words))
        state = 0
        for word in words:
            if word not in transitions[state]:
                if len(transitions) >= 50000:
                    return [_finding("manual_review", "contained_duplicate_budget_exhausted",
                                     "Quotation provenance index exceeded its node budget; review without deleting text.",
                                     [], node_budget=50000)]
                transitions[state][word] = len(transitions)
                transitions.append({}); failure.append(0); terminal.append(None); suffix.append(0)
            state = transitions[state][word]
        terminal[state] = ident
    queue = deque(transitions[0].values())
    while queue:
        state = queue.popleft()
        for word, child in transitions[state].items():
            queue.append(child)
            fallback = failure[state]
            while fallback and word not in transitions[fallback]:
                fallback = failure[fallback]
            failure[child] = transitions[fallback].get(word, 0)
            parent = failure[child]
            suffix[child] = parent if terminal[parent] is not None else suffix[parent]
    source_counts = [0] * len(needles)
    output_counts = [0] * len(needles)
    quote_counts = [0] * len(needles)
    source_spans = [[] for _ in needles]
    carriers = [set() for _ in needles]
    separate = [[] for _ in needles]
    needle_ids = {needle: i for i, needle in enumerate(needles)}
    for row in sorted(character_rows):
        if texts[row]:
            separate[needle_ids[texts[row]]].append(row)
    remaining = 200000
    longest = max(lengths)
    offsets = [0] * longest

    def scan(text, counts, row=None, quotation=False):
        nonlocal remaining
        state = 0
        last_end = {}
        # Shared offset ring bounds memory by longest phrase, not book size.
        for position, word_match in enumerate(re.finditer(r"\S+", text)):
            word = word_match.group()
            offsets[position % longest] = word_match.start()
            while state and word not in transitions[state]:
                state = failure[state]
            state = transitions[state].get(word, 0)
            hit = state
            while hit:
                ident = terminal[hit]
                if ident is not None:
                    remaining -= 1
                    if remaining < 0:
                        return False
                    start = position - lengths[ident] + 1
                    if start >= last_end.get(ident, 0):
                        last_end[ident] = position + 1
                        # Only strictly larger entries can carry a nested copy.
                        if row is None or len(texts[row]) > len(needles[ident]):
                            counts[ident] += 1
                            if row is None and len(source_spans[ident]) < 8:
                                source_spans[ident].append([offsets[start % longest], word_match.end()])
                            elif row is not None and not quotation:
                                carriers[ident].add(row)
                        elif not quotation:
                            counts[ident] += 1
                hit = suffix[hit]
        return True

    complete = scan(source, source_counts)
    for row, text in enumerate(texts):
        if not complete:
            break
        complete = scan(text, output_counts, row)
        for part in quoted[row]:
            if not complete:
                break
            complete = scan(part, quote_counts, row, True)
    if not complete:
        return [_finding("manual_review", "contained_duplicate_budget_exhausted",
                         "Quotation provenance scan exceeded its match budget; review without deleting text.",
                         [], match_budget=200000)]
    expansions = (
        (r"\bchapter\s+i\b", "one"), (r"\bdr\.", "doctor"),
        (r"\bmr\.", "mister"), (r"&", "and"), (r"\b3rd\b", "third"),
    )
    expansion_sites = [(word, len(re.findall(pattern, source_text, re.I)))
                       for pattern, word in expansions]
    unknown_rendering = bool(re.search(r"\d+", re.sub(r"\b3rd\b", "", source_text, flags=re.I))
                             or re.search(r"\bchapter\s+(?!i\b)[ivxlcdm]+\b", source_text, re.I)
                             or re.search(r"[\[<]", source_text))
    findings = []
    for ident, needle in enumerate(needles):
        if not needle:
            continue
        capacity = source_counts[ident]
        if not capacity:
            continue
        added_capacity = sum(count for word, count in expansion_sites if word in needle.split())
        all_emitted = output_counts[ident]
        if all_emitted <= capacity + added_capacity:
            continue
        if not carriers[ident] and not separate[ident]:
            continue
        proven_emitted = quote_counts[ident] + len(separate[ident])
        ambiguous = unknown_rendering or proven_emitted <= capacity + added_capacity
        findings.append(_finding(
            "manual_review" if ambiguous else "blocking", "contained_source_duplicate",
            "Contained phrase repetition needs review of its rendering/source provenance."
            if ambiguous else "A character line exceeds its distinct source support, either "
            "repeated as separate entries or also inside a larger entry. Regenerate losslessly; "
            "do not delete legitimate source repeats.",
            sorted(i + 1 for i in carriers[ident].union(separate[ident])), phrase=needle,
            source_occurrences=capacity, possible_expansion_occurrences=added_capacity,
            output_quotation_occurrences=proven_emitted, all_text_occurrences=all_emitted,
            rendering_capacity_unknown=ambiguous,
            source_word_normalized_spans=source_spans[ident],
            source_spans_truncated=capacity > len(source_spans[ident]),
        ))
    return findings


def find_adjacent_duplicate_blocks(texts, source_text, *, source_normalized=None, count_phrase=None):
    findings = []
    occupied = set()
    if source_normalized is None:
        source_normalized = _normalize_words(source_text)
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    for block_size in range(5, 1, -1):
        index = 0
        while index + (2 * block_size) <= len(texts):
            left = texts[index:index + block_size]
            right = texts[index + block_size:index + (2 * block_size)]
            positions = set(range(index, index + (2 * block_size)))
            if (positions.isdisjoint(occupied) and left == right and
                    all(len(text) >= 8 for text in left)):
                # TWO WAYS TO ASK "does the source contain this block?", and
                # the contiguous one alone is wrong.
                #
                # Entries are not contiguous spans of the source - a dialogue
                # entry is followed in the book by narration the next entry
                # skips. So joining two entries and searching for that string
                # finds nothing whenever anything sits between them, and a
                # plain duplication scores 0, which reads as "the model
                # invented this" when it means "these two lines are not
                # adjacent in the book".
                #
                # Alice in Wonderland: the Caterpillar's "Explain yourself!"
                # and Alice's reply were emitted twice. Each line occurs once
                # in the source; joined, they occur zero times, because "said
                # Alice" sits between them. The book generated all 25 chunks,
                # the repair knew how to fix it, and it was thrown away as
                # unresolvable - 919 entries discarded over two lines.
                #
                # So fall back to the per-entry minimum: if every line in the
                # block is in the source, the block is duplicated (removable).
                # Only a line the source lacks entirely is an invention.
                contiguous = count_phrase(_normalize_words(" ".join(left)))
                source_occurrences = get_source_block_occurrences(
                    source_normalized, left, count_phrase=count_phrase)
                # A block the SOURCE itself repeats is faithful
                # transcription, not a defect. grimgar03 opens with its
                # title eight times; the whole-book gate rejected the
                # finished book for reproducing it, after all 49 chunks had
                # generated cleanly. script_repair.py already made this
                # distinction for the repair path - having it in only one of
                # the two places is how the same book failed twice for the
                # same reason (Rule 15: one decision, one implementation).
                faithful = source_occurrences >= 2
                findings.append(_finding(
                    "manual_review" if faithful else "blocking",
                    "adjacent_duplicate_block",
                    f"Entries {index + 1}-{index + block_size} are repeated "
                    + ("immediately, and the source repeats them too."
                       if faithful else "immediately."),
                    list(range(index + 1, index + (2 * block_size) + 1)),
                    block_size=block_size,
                    source_occurrences=source_occurrences,
                    contiguous_block_occurrences=contiguous,
                ))
                occupied.update(positions)
                index += 2 * block_size
            else:
                index += 1
    return findings


# A quoted lexical term can be copied at a split seam in three shapes: its own
# short narrator row, the head of the following narrator row, or twice inside
# one narrator row. The adjacent-block check cannot see a short copy, and at
# book chunk size the recall/trigram/ratio metrics barely move. Candidate
# phrases come from source provenance (bracketed reference terms and lexical
# quote regions) plus bounded narrator seam fragments, so this never becomes
# an unbounded scan of every word phrase in the chunk.
MAX_SEAM_DUPLICATE_WORDS = 10
MAX_SEAM_DUPLICATE_CHARS = 120
MAX_SEAM_DUPLICATE_CANDIDATES = 128
_BRACKETED_TERM = re.compile(r"\[([^\[\]\n]{1,80})\]")
# Quoted reference terms are lexical-quote evidence too. Scan only the short
# paired-quote contents: dialogue rows are excluded from the finding, so this
# cannot turn a repeated spoken line into a narration duplicate.
_QUOTED_TERM = re.compile(r'["“「『«]([^"”」』»\n]{1,80})["”」』»]')


def _is_narration_row(entry):
    if not isinstance(entry, dict):
        return False
    role = str(entry.get("speaker") or entry.get("type") or "")
    return not role or role.casefold() in {"narrator", "narration"}


def _phrase_near_edge(words, phrase_words, side):
    """Whole-phrase match within the bounded window of an entry edge."""
    if not phrase_words or not words:
        return False
    region = words[-MAX_SEAM_DUPLICATE_WORDS:] if side == "end" else words[:MAX_SEAM_DUPLICATE_WORDS]
    size = len(phrase_words)
    return any(region[i:i + size] == phrase_words for i in range(len(region) - size + 1))


def get_seam_duplicate_candidates(source_text, entries, *, quote_analysis=None):
    """Bounded short phrases grounded in source quotes or narrator row edges."""
    candidates = []

    def add(value):
        phrase = _normalize_words(value)
        if (phrase and phrase not in candidates
                and len(phrase.split()) <= MAX_SEAM_DUPLICATE_WORDS
                and len(phrase) <= MAX_SEAM_DUPLICATE_CHARS
                and len(candidates) < MAX_SEAM_DUPLICATE_CANDIDATES):
            candidates.append(phrase)

    for match in _BRACKETED_TERM.finditer(str(source_text or "")):
        add(match.group(1))
    for match in _QUOTED_TERM.finditer(str(source_text or "")):
        add(match.group(1))
    for region in (quote_analysis or {}).get("regions", []):
        if region.get("quote_role") and region.get("type") == "NARRATOR":
            add(region.get("text"))
    for entry in entries:
        if not _is_narration_row(entry):
            continue
        words = _normalize_words(entry.get("text")).split()
        if words:
            add(" ".join(words[:MAX_SEAM_DUPLICATE_WORDS]))
            add(" ".join(words[-MAX_SEAM_DUPLICATE_WORDS:]))
    return candidates


def find_seam_duplicate_narration(source_text, entries, *, quote_analysis=None,
                                  role_specific=True, source_narration_text=None,
                                  source_normalized=None, count_phrase=None):
    """Find a short narration quote copied near a split, with source support.

    Pass 1 sometimes splits a sentence at a quoted lexical term (a title, a
    skill, a name) and emits that term a second time: as its own NARRATOR row,
    at the head of the next row, or twice inside one row. ``find_adjacent_
    duplicate_blocks`` starts at two repeated rows and the aggregate fidelity
    metrics stay clean at chunk size, so none of them see the extra copy
    (issue #2143).

    Candidates are short phrases with verified source provenance, not every
    word in the output. A finding needs both a locally duplicated shape and a
    strictly greater whole-token output count than the source. A phrase the
    source itself repeats that many times is faithful transcription and is
    left alone. Dialogue rows (SPOKEN or a character speaker) are excluded
    when ``role_specific`` is true: their contained-quote provenance is
    ``find_contained_source_duplicates``'s job. When a permissive quote policy
    allows a quoted source occurrence to be emitted inside a narrator row,
    source and output roles are not comparable, so both sides are counted in
    full. No fidelity threshold is changed here.
    """
    # Role-specific accounting is only sound when the run's quote policy
    # forces source and output roles to agree.
    allowance_text = (source_narration_text if role_specific and source_narration_text is not None
                      else source_text)
    if (role_specific and source_narration_text is not None) or source_normalized is None:
        source_normalized = _normalize_words(allowance_text)
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    texts = [_normalize_words(entry.get("text")) if isinstance(entry, dict) else ""
             for entry in entries]
    narration = [_is_narration_row(entry) for entry in entries] if role_specific else [True] * len(entries)
    word_lists = [text.split() for text in texts]
    findings = []
    for phrase in get_seam_duplicate_candidates(source_text, entries,
                                                quote_analysis=quote_analysis):
        source_count = count_phrase(phrase)
        if source_count < 1:
            # No verified source correspondence: abstain rather than invent
            # a duplicate verdict from a phrase the source cannot confirm.
            continue
        counts = [get_source_phrase_occurrences(text, phrase) if narration[i] else 0
                  for i, text in enumerate(texts)]
        emitted = sum(counts)
        if emitted <= source_count:
            continue
        phrase_words = phrase.split()
        numbers = {number for number, count in enumerate(counts, 1) if count >= 2}
        for index in range(len(texts) - 1):
            if (counts[index] and counts[index + 1]
                    and _phrase_near_edge(word_lists[index], phrase_words, "end")
                    and _phrase_near_edge(word_lists[index + 1], phrase_words, "start")):
                numbers.update((index + 1, index + 2))
        if not numbers:
            continue
        findings.append(_finding(
            "blocking", "seam_duplicate_narration",
            "A short quoted term is copied near a split seam or inside one "
            "entry; the source does not carry the extra copy.",
            sorted(numbers),
            phrase=phrase, source_occurrences=source_count, output_occurrences=emitted))
    return findings


def find_adjacent_near_duplicate_entries(texts, source_text, minimum_ratio=0.90, *, exact_findings=None, source_normalized=None, count_phrase=None):
    """Adjacent entry pairs that are near-duplicates of each other but not
    supported by the source - likely model re-generation at a seam.

    Non-blocking evidence only: findings are always ``manual_review``, never
    a rejection - a fallback net for paraphrased seam repeats that slip past
    the exact ``find_adjacent_duplicate_blocks`` check. Deliberately NOT
    wired into ``chunk_quality.validate_chunk_quality`` - any finding there
    sets ``passed: False`` and triggers a retry, which would turn this
    non-blocking net into a blocking gate.
    """
    findings = []
    occupied = set()
    if source_normalized is None:
        source_normalized = _normalize_words(source_text)
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    if exact_findings is None:
        exact_findings = find_adjacent_duplicate_blocks(
            texts, source_text, source_normalized=source_normalized, count_phrase=count_phrase)
    for finding in exact_findings:
        occupied.update(number - 1 for number in finding["entry_numbers"])

    for index in range(len(texts) - 1):
        if index in occupied or (index + 1) in occupied:
            continue
        first, second = texts[index], texts[index + 1]
        if len(first) < 8 or len(second) < 8:
            continue
        first_words = _normalize_words(first).split()
        second_words = _normalize_words(second).split()
        if len(first_words) < 5 or len(second_words) < 5:
            continue
        matcher = difflib.SequenceMatcher(None, first_words, second_words, autojunk=False)
        # Both quick ratios are upper bounds; preserve candidates at the threshold.
        if matcher.real_quick_ratio() < minimum_ratio or matcher.quick_ratio() < minimum_ratio:
            continue
        ratio = matcher.ratio()
        if ratio < minimum_ratio:
            continue

        if source_text:
            # Identical text used twice needs two occurrences in the source to be
            # "genuinely repeated prose"; one occurrence can't back both uses.
            required_occurrences = 2 if first_words == second_words else 1
            first_supported = count_phrase(_normalize_words(first)) >= required_occurrences
            second_supported = count_phrase(_normalize_words(second)) >= required_occurrences
            if first_supported and second_supported:
                continue
            source_checked = True
            source_supported = [first_supported, second_supported]
        else:
            source_checked = False
            source_supported = None

        findings.append(_finding(
            "manual_review", "adjacent_near_duplicate",
            f"Entries {index + 1}-{index + 2} are near-duplicates of each other.",
            [index + 1, index + 2],
            similarity=round(ratio, 4),
            source_checked=source_checked,
            source_supported=source_supported,
        ))
    return findings


def get_invalid_entry_fields(entry):
    """Return present script fields that cannot be consumed as strings."""
    return [field for field in ("text", "speaker", "instruct")
            if field in entry and not isinstance(entry[field], str)]


def audit_script(entries, source_text=None, is_generic_speaker_fn=None):
    """Return deterministic findings without modifying ``entries``."""
    findings = []
    if not isinstance(entries, list):
        findings.append(_finding("blocking", "invalid_script", "The script root must be a JSON array."))
        return _build_report(0, findings)

    texts = []
    instructions = []
    source_scripts = None
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            findings.append(_finding("blocking", "invalid_entry", "Entry must be a JSON object.", [index]))
            texts.append("")
            continue

        raw_text = entry.get("text")
        raw_speaker = entry.get("speaker")
        raw_instruct = entry.get("instruct")
        for field in get_invalid_entry_fields(entry):
            findings.append(_finding("blocking", "invalid_field_type",
                                     f"Entry {field} must be a string.", [index],
                                     field=field))
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        speaker = raw_speaker.strip() if isinstance(raw_speaker, str) else ""
        instruct = raw_instruct.strip() if isinstance(raw_instruct, str) else ""
        texts.append(_normalize(text))
        instructions.append(_normalize(instruct))

        if not text:
            findings.append(_finding("blocking", "empty_text", "Entry has no speakable text.", [index]))
        if not speaker:
            findings.append(_finding("blocking", "missing_speaker", "Entry has no speaker.", [index]))
        if not instruct:
            findings.append(_finding("manual_review", "missing_instruction", "Entry has no delivery instruction.", [index]))
        if source_scripts is None:
            source_scripts = get_unicode_scripts(source_text)
        unicode_report = audit_unicode_text(text, source_text, source_scripts=source_scripts)
        if unicode_report["introduced_scripts"]:
            findings.append(_finding(
                "blocking", "introduced_unicode_script",
                "Entry contains a writing system absent from the source.",
                [index], scripts=unicode_report["introduced_scripts"],
            ))
        if unicode_report["mixed_script_words"]:
            findings.append(_finding("blocking", "mixed_script_word",
                                     "A word combines multiple writing systems.", [index],
                                     words=unicode_report["mixed_script_words"]))
        if unicode_report["unsafe_controls"]:
            findings.append(_finding("blocking", "unsafe_unicode_character",
                                     "Entry contains unsafe control characters.",
                                     [index], unicode=unicode_report))
        elif unicode_report["replacement_character_count"]:
            # Non-blocking: the SOURCE gate already decided whether this book's
            # damage is acceptable. Re-refusing here would mean a source we
            # admitted could never produce an entry we accept - which is how
            # index18 passed the front door and then failed at chunk 31.
            findings.append(_finding("manual_review", "replacement_character",
                                     "Entry contains replacement characters "
                                     "the source gate already accepted.",
                                     [index], unicode=unicode_report))
        for field, value in (("speaker", speaker), ("instruct", instruct)):
            controls = get_unsafe_text_controls(value)
            if controls:
                findings.append(_finding("blocking", "unsafe_unicode_character",
                                         f"Entry {field} contains unsafe control characters.",
                                         [index], field=field, unsafe_controls=controls))
        if index <= 30 and _FRONT_MATTER_RE.search(text):
            findings.append(_finding("manual_review", "front_matter", "Possible publication front matter.", [index]))
        if is_possible_misattributed_narration(text, speaker):
            findings.append(_finding(
                "manual_review", "possible_misattributed_narration",
                "Third-person narration may be assigned to a character.", [index], speaker=speaker,
            ))
        if speaker and is_generic_speaker_fn and is_generic_speaker_fn(speaker):
            findings.append(_finding(
                "manual_review", "generic_speaker", "Generic speaker label needs book-local review.",
                [index], speaker=speaker,
            ))
        speech = get_speech_normalization(text)
        if speech["risk_categories"]:
            findings.append(_finding(
                "manual_review", "nonprose_speech_risk",
                "Non-prose content has elevated TTS error risk; review the "
                "spoken preview and validate the generated audio.",
                [index], categories=speech["risk_categories"],
                normalized_preview=speech["text"],
                transformations=speech["transformations"],
                validation={
                    "recommended": True,
                    "method": "targeted_transcription_or_human_listening",
                    "status": "not_run",
                    "reason": "Script preflight does not generate or transcribe audio.",
                },
            ))

    source_tokens = get_normalized_word_tokens(source_text)
    source_normalized = " ".join(source_tokens)
    count_phrase = get_source_phrase_counter(source_normalized)
    exact_findings = find_adjacent_duplicate_blocks(
        texts, source_text, source_normalized=source_normalized, count_phrase=count_phrase)
    findings.extend(exact_findings)
    findings.extend(find_contained_source_duplicates(
        entries, source_text, source_normalized=source_normalized))
    findings.extend(find_adjacent_near_duplicate_entries(
        texts, source_text, exact_findings=exact_findings,
        source_normalized=source_normalized, count_phrase=count_phrase))
    del source_normalized, count_phrase

    nonempty_instructions = [value for value in instructions if value]
    if len(nonempty_instructions) >= 20:
        uniqueness = len(set(nonempty_instructions)) / len(nonempty_instructions)
        if uniqueness >= 0.95:
            findings.append(_finding(
                "informational", "high_instruction_uniqueness",
                "Nearly every entry has a unique delivery instruction.",
                uniqueness=round(uniqueness, 4),
            ))

    if source_text:
        source_words = Counter(source_tokens)
        del source_tokens
        script_words = Counter(word for text in texts for word in _WORD_RE.findall(text))
        source_total = sum(source_words.values())
        matched = sum(min(count, script_words.get(word, 0)) for word, count in source_words.items())
        coverage = matched / source_total if source_total else 1.0
        if source_total and coverage < 0.85:
            findings.append(_finding(
                "manual_review", "low_source_word_coverage",
                "Annotated text has low word coverage relative to the source.",
                coverage=round(coverage, 4),
            ))

    return _build_report(len(entries), findings)


def _build_report(entry_count, findings):
    counts = {severity: 0 for severity in ("blocking", "manual_review", "informational")}
    for finding in findings:
        counts[finding["severity"]] += 1
    return {
        "entry_count": entry_count,
        "counts": counts,
        "can_apply_repairs": counts["blocking"] == 0,
        "findings": findings,
    }
