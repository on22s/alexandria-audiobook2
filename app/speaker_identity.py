"""Safe in-run speaker identity stabilization and uncertain-variant reporting."""

import copy
import re
import unicodedata
from difflib import SequenceMatcher

from silent_word_characters import remove_silent_word_characters


MAX_ALIAS_COUNT = 10000
MAX_ALIAS_NAME_LENGTH = 1024


def get_validated_alias_map(value):
    """Return a normalized copy of a string-to-string alias registry."""
    if not isinstance(value, dict):
        raise ValueError("alias registry must be an object of string-to-string mappings")
    if len(value) > MAX_ALIAS_COUNT:
        raise ValueError(f"alias registry count exceeds {MAX_ALIAS_COUNT}")
    result = {}
    targets = {}
    for variant, canonical in value.items():
        if not isinstance(variant, str) or not isinstance(canonical, str):
            raise ValueError("alias registry keys and targets must be strings")
        variant, canonical = variant.strip(), canonical.strip()
        if max(len(variant), len(canonical)) > MAX_ALIAS_NAME_LENGTH:
            raise ValueError(f"alias registry name length exceeds {MAX_ALIAS_NAME_LENGTH}")
        if not variant or not canonical:
            raise ValueError("alias registry keys and targets must be nonempty")
        if variant in result and result[variant] != canonical:
            raise ValueError(f"alias registry has conflicting targets for '{variant}'")
        key = _identity_key(variant)
        target = _identity_key(canonical)
        if key in targets and targets[key] != target:
            raise ValueError(f"alias registry has conflicting normalized targets for '{variant}'")
        targets[key] = target
        result[variant] = canonical
    return result


def stabilize_speaker_identities(entries, established_speakers=None):
    """Return copied entries with only exact normalized variants canonicalized."""
    repaired = copy.deepcopy(entries)
    canonicals = []
    canonical_keys = {}
    identity_index = {}
    changes = []
    review = []
    for speaker in established_speakers or []:
        value = remove_silent_word_characters(str(speaker or "").strip())
        if value and value not in canonical_keys:
            canonicals.append(value)
            key = _identity_key(value)
            canonical_keys[value] = key
            identity_index.setdefault(key, value)

    # Every spelling AS WRITTEN, before canonicalization rewrites it. The
    # first loop collapses variants onto whichever arrived first, so by the
    # time it finishes the better-formed spelling may have vanished - which is
    # exactly how `RUDEUS` disappeared behind `R UDEUS`.
    seen = []
    for index, entry in enumerate(repaired, 1):
        if not isinstance(entry, dict):
            continue
        original = str(entry.get("speaker") or "")
        # U+00AD/U+2060 are invisible inside a name (#2316), so the model's
        # "SON<SHY>YA" is the stored spelling "SONYA", never a second speaker.
        stripped = remove_silent_word_characters(" ".join(original.split()))
        if stripped:
            seen.append(stripped)
        exact = identity_index.get(_identity_key(stripped))
        if exact:
            canonical = exact
        else:
            canonical = stripped
            if canonical:
                candidates = _uncertain_candidates(canonical, canonicals, canonical_keys)
                if candidates:
                    review.append({"entry_number": index, "speaker": canonical,
                                   "candidates": candidates})
                canonicals.append(canonical)
                key = _identity_key(canonical)
                canonical_keys[canonical] = key
                identity_index.setdefault(key, canonical)
        if canonical and canonical != original:
            entry["speaker"] = canonical
            changes.append({"type": "speaker_identity", "entry_number": index,
                            "before": original, "after": canonical})

    # FIRST SEEN IS NOT BEST FORMED. Two spellings of one name collapse to
    # whichever arrived first, and on mushoku18 that was `R UDEUS` - the model
    # emitted the split form 86 times and `RUDEUS` 16, the split one came
    # first, and all 102 lines took its spelling. 137 more in mushoku23. Those
    # lines match no voice assignment, because nothing in the cast is called
    # "R UDEUS".
    #
    # Among spellings that are already the SAME identity (_identity_key ignores
    # spacing and punctuation), prefer the one with the fewest gaps: a name
    # broken mid-word carries an extra space that the intact spelling does not.
    # Roster names keep priority - they are the caller's own answer - and
    # genuinely multi-word names are unaffected, because every spelling of them
    # has the same gaps.
    roster = {_identity_key(name) for name in (established_speakers or [])}
    best = {}
    for order, name in enumerate(seen):
        key = _identity_key(name)
        if key in roster:
            continue
        rank = (len(str(name).split()), order)
        if key not in best or rank < best[key][0]:
            best[key] = (rank, name)
    for index, entry in enumerate(repaired, 1):
        if not isinstance(entry, dict):
            continue
        current = str(entry.get("speaker") or "")
        chosen = best.get(_identity_key(current), (None, None))[1]
        if chosen and chosen != current:
            entry["speaker"] = chosen
            changes.append({"type": "speaker_spelling", "entry_number": index,
                            "before": current, "after": chosen})
    # A SPLIT NAME WITH NO SURVIVING ALTERNATIVE. Preferring the better
    # spelling only helps while both are present. In a finished script they are
    # not: generation collapsed `RUDEUS` onto `R UDEUS` line by line, so the
    # saved artifact holds 67 of the broken form and none of the good one.
    #
    # For that case the book itself is the evidence. If a speaker looks like a
    # name broken after its first letter, and the joined form actually occurs
    # in the prose of these entries, the joined form is the name - mushoku18's
    # text says "Rudeus" 123 times and "R udeus" never. Without that corroboration
    # nothing is joined: "J SMITH" is a person, not a typo, and guessing would
    # invent a character.
    prose = remove_silent_word_characters(" ".join(
        str(e.get("text") or "") for e in repaired
        if isinstance(e, dict))).casefold()
    for entry in repaired:
        if not isinstance(entry, dict):
            continue
        name = " ".join(str(entry.get("speaker") or "").split())
        if _identity_key(name) in roster or not re.fullmatch(r"[A-Za-z]\s[A-Za-z]{2,}", name):
            continue
        joined = remove_silent_word_characters(name.replace(" ", ""))
        if re.search(r"(?<!\w)" + re.escape(joined.casefold()) + r"(?!\w)", prose):
            entry["speaker"] = joined
            changes.append({"type": "speaker_split_repair",
                            "before": name, "after": joined})

    canonicals = [n for n in canonicals
                  if _identity_key(n) in roster
                  or best.get(_identity_key(n), (None, None))[1] == n]
    canonical_names = set(canonicals)
    for _, name in best.values():
        if name not in canonical_names:
            canonicals.append(name)
            canonical_names.add(name)
    final_speakers = [str(entry.get("speaker") or "") for entry in repaired
                      if isinstance(entry, dict) and entry.get("speaker")]
    final_names = set(final_speakers)
    canonicals = [name for name in canonicals
                  if _identity_key(name) in roster or name in final_names]
    canonical_names = set(canonicals)
    for name in final_speakers:
        if name not in canonical_names:
            canonicals.append(name)
            canonical_names.add(name)
    return {"entries": repaired, "changes": changes, "review": review,
            "speakers": canonicals}


def build_speaker_consistency_report(entries, identity_review=None):
    """Summarize speaker usage and uncertain variants without merging them."""
    usage = {}
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        speaker = str(entry.get("speaker") or "").strip()
        if not speaker:
            continue
        item = usage.setdefault(speaker, {"speaker": speaker, "entry_count": 0,
                                          "first_entry_numbers": []})
        item["entry_count"] += 1
        if len(item["first_entry_numbers"]) < 5:
            item["first_entry_numbers"].append(index)
    suggestions = []
    seen = set()
    for item in identity_review or []:
        key = (item.get("speaker"), tuple(candidate.get("speaker")
               for candidate in item.get("candidates", [])))
        if key in seen:
            continue
        seen.add(key)
        suggestions.append({"speaker": item.get("speaker"),
                            "candidates": item.get("candidates", []),
                            "example_entry_number": item.get("entry_number")})
    return {"speaker_count": len(usage),
            "speakers": sorted(usage.values(), key=lambda item: item["speaker"]),
            "review_suggestions": suggestions}


_IDENTITY_MARK_SCRIPTS = (
    # Brahmic abugidas: the vowel signs, viramas and nuktas are part of a name.
    "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA", "TAMIL",
    "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "BRAHMI", "KAITHI",
    "KHOJKI", "KHUDAWADI", "MULTANI", "SHARADA", "SIDDHAM", "TIRHUTA",
    "GRANTHA", "NEWA", "BHAIKSUKI", "MARCHEN", "MASARAM GONDI",
    "GUNJALA GONDI", "SOYOMBO", "ZANABAZAR SQUARE", "NANDINAGARI",
    "DIVES AKURU", "CHAKMA", "MODI", "TAKRI", "AHOM", "DOGRA",
    "SAURASHTRA", "SYLOTI NAGRI", "MEETEI MAYEK", "KHAROSHTHI", "LEPCHA",
    "LIMBU", "BUGINESE", "REJANG", "MAKASAR",
    # Southeast-Asian abugidas.
    "THAI", "LAO", "TIBETAN", "MYANMAR", "KHMER", "BALINESE", "JAVANESE",
    "SUNDANESE", "BATAK", "CHAM", "TAGALOG", "HANUNOO", "BUHID", "TAGBANWA",
    "TAI THAM", "TAI VIET", "NEW TAI LUE",
)


def _is_identity_mark(char):
    """True for a combining mark that changes a letter in an abugida.

    `\\w` does not match combining marks, so the old fold dropped them
    everywhere and different Devanagari names shared a key (`राम`/`रमा`). The
    marks that must survive are the vowel signs, viramas and nuktas of the
    Brahmic and Southeast-Asian abugidas. Elsewhere a mark is an optional
    diacritic - Latin/Greek/Cyrillic accents (including the ones casefold
    introduces, such as İ's dot), Arabic harakat, Hebrew niqqud - and it folds
    away exactly as it did before.
    """
    if unicodedata.category(char) not in ("Mn", "Mc"):
        return False
    name = unicodedata.name(char, "")
    return any(name.startswith(script + " ") for script in _IDENTITY_MARK_SCRIPTS)


def _identity_key(value):
    """Fold a speaker name to a comparable identity key.

    NFC runs before casefold so composed and decomposed accents compare equal;
    marks are then kept only when they are identity-bearing (see
    `_is_identity_mark`). Spacing and punctuation always fold.
    """
    normalized = unicodedata.normalize("NFC", str(value or "")).casefold()
    return "".join(char for char in normalized
                   if char == "_" or char.isalnum()
                   or _is_identity_mark(char))


def get_speaker_label_index(labels):
    """Index normalized identities, retaining the sorted first matching label."""
    index = {}
    for label in sorted(labels):
        key = _identity_key(label)
        if key:
            index.setdefault(key, label)
    return index


def resolve_speaker_label(name, labels):
    """Resolve a label using generation's NFC/casefold/word identity rules."""
    return get_speaker_label_index(labels).get(_identity_key(name))


def _uncertain_candidates(speaker, canonicals, identity_keys=None):
    key = _identity_key(speaker)
    results = []
    for canonical in canonicals:
        candidate_key = (identity_keys[canonical] if identity_keys is not None
                         else _identity_key(canonical))
        ratio = SequenceMatcher(None, key, candidate_key).ratio()
        if ratio >= 0.90 or _is_extended_person_name(speaker, canonical):
            results.append({"speaker": canonical, "similarity": round(ratio, 4)})
    return sorted(results, key=lambda item: (-item["similarity"], item["speaker"]))


def _is_extended_person_name(first, second):
    relation_words = {"mother", "father", "sister", "brother", "daughter", "son"}
    first_words = re.findall(r"[\w]+",
                             remove_silent_word_characters(first).casefold(), re.UNICODE)
    second_words = re.findall(r"[\w]+",
                              remove_silent_word_characters(second).casefold(), re.UNICODE)
    shorter, longer = sorted((first_words, second_words), key=len)
    return (len(shorter) == 1 and len(longer) == 2 and shorter[0] == longer[0]
            and not any(word.isdigit() or word in relation_words for word in longer))


def is_group_speaker_label(name):
    """True if a speaker label denotes multiple characters speaking together
    (e.g. 'RAM AND REM', 'EMILIA/PUCK (DUAL)', 'TWINS', 'CROWD'). Such labels must
    never be merged into a single character — that would collapse two voices into one."""
    n = (name or "").upper()
    if "/" in n or "&" in n or "+" in n:
        return True
    if re.search(r"\bAND\b", n):
        return True
    if re.search(r"\b(DUAL|CHORUS|TWINS|CROWD|GROUP|UNISON|BOTH|EVERYONE|TOGETHER|VOICES)\b", n):
        return True
    return False


def is_speaker_merge_allowed(variant, canonical):
    """Protect narration and groups, allowing the same group's spelling.

    Protected labels are compared by the same NFC/casefold/word identity the
    alias index uses, so ``UN-KNOWN``/``un known`` cannot be used as a spelling
    variant to launder a merge into UNKNOWN (#2203-adjacent reserved-edge bug).
    """
    protected_keys = {_identity_key(name) for name in (
        "NARRATOR", "NARRATION", "NARRATIVE", "UNKNOWN")}
    if (_identity_key(variant) in protected_keys
            or _identity_key(canonical) in protected_keys):
        return False
    if is_group_speaker_label(variant) or is_group_speaker_label(canonical):
        first = remove_silent_word_characters(
            " ".join(unicodedata.normalize("NFC", variant).casefold().split()))
        second = remove_silent_word_characters(
            " ".join(unicodedata.normalize("NFC", canonical).casefold().split()))
        return first == second
    return True


def _get_alias_roots(mapping, *, allowed_edge=None):
    """Return terminal labels and variants whose paths reach a cycle.

    With allowed_edge, traversal stops at an edge it refuses and reports that
    edge's canonical as the terminal label. Callers that pass no predicate keep
    the permissive legacy read behaviour.
    """
    index = {}
    for variant in sorted(mapping):
        index.setdefault(_identity_key(variant), variant)
    roots, cyclic = {}, set()
    for variant in mapping:
        current, seen = variant, set()
        while current not in seen:
            if current in roots:
                roots.update((alias, roots[current]) for alias in seen)
                break
            if current in cyclic:
                cyclic.update(seen)
                break
            seen.add(current)
            canonical = mapping[current]
            following = index.get(_identity_key(canonical))
            terminal = following if following is not None else canonical
            if allowed_edge is not None and not allowed_edge(current, terminal):
                roots.update((alias, terminal) for alias in seen)
                break
            if following is None or following == current:
                roots.update((alias, canonical) for alias in seen)
                break
            current = following
        else:
            cyclic.update(seen)
    return {variant: roots[variant] for variant in mapping if variant in roots}, cyclic


def get_validated_alias_graph(value):
    """Return a structurally valid alias map, refusing existing cycles."""
    mapping = get_validated_alias_map(value)
    _, cyclic = _get_alias_roots(mapping)
    if cyclic:
        raise ValueError("alias registry contains a cycle reached from " +
                         ", ".join(sorted(cyclic)))
    return mapping


def get_saveable_alias_graph(value):
    """Validate an alias graph for publication, refusing protected merges.

    Reads stay permissive (get_validated_alias_graph, get_resolved_speaker_merge_map)
    so a legacy registry holding an UNKNOWN edge remains readable and never folds
    that edge. Only a write that introduces a protected narrator/UNKNOWN/group
    merge is refused, on either side of the pair.
    """
    mapping = get_validated_alias_graph(value)
    for variant, canonical in mapping.items():
        if not is_speaker_merge_allowed(variant, canonical):
            raise ValueError(f"protected speaker mapping '{variant}' -> '{canonical}' cannot be saved")
    return mapping


def get_resolved_speaker_merge_map(mapping):
    """Flatten alias chains; omit cycles and every alias leading into one.

    Every edge is checked with is_speaker_merge_allowed, not only the two
    endpoints (#1801): resolution stops before a protected narrator/group
    edge, so flattening cannot launder one. An unprotected prefix survives.
    """
    mapping = get_validated_alias_map(mapping)
    _, cyclic = _get_alias_roots(mapping)
    for variant in sorted(cyclic):
        print(f"  [skip] alias cycle reached from '{variant}'")
    index = {}
    for variant in sorted(mapping):
        index.setdefault(_identity_key(variant), variant)
    resolved = {}
    for variant in mapping:
        if variant in cyclic:
            continue
        current = target = variant
        while True:   # terminates: variants outside `cyclic` reach a root
            canonical = mapping[current]
            if not is_speaker_merge_allowed(current, canonical):
                break
            target = canonical
            following = index.get(_identity_key(canonical))
            if following is None or following == current:
                break
            current = following
        if target != variant and is_speaker_merge_allowed(variant, target):
            resolved[variant] = target
    return resolved


def get_safe_alias_proposals(proposal_maps, roster, existing_aliases=None, flatten=True):
    """Return safe proposals, protecting existing human keys and roots.

    With flatten=False, retain proposed edges after validating their terminal
    roots so a later locked check can follow intervening human root changes.
    """
    current = get_validated_alias_graph({} if existing_aliases is None else existing_aliases)
    human_roots, _ = _get_alias_roots(current, allowed_edge=is_speaker_merge_allowed)
    human_keys = {_identity_key(variant) for variant in current}
    human_keys.update(_identity_key(root) for root in human_roots.values())
    choices, conflicts = {}, set()
    for proposals in proposal_maps:
        for variant, canonical in get_validated_alias_map(proposals).items():
            key = _identity_key(variant)
            if key in human_keys or key in conflicts:
                continue
            if not is_speaker_merge_allowed(variant, canonical):
                print(f"  [skip] protected speaker mapping '{variant}' -> '{canonical}'")
                continue
            previous = choices.get(key)
            if previous and _identity_key(previous[1]) != _identity_key(canonical):
                choices.pop(key)
                conflicts.add(key)
                print(f"  [skip] conflicting automatic alias targets for '{variant}'")
                continue
            choices.setdefault(key, (variant, canonical))
    proposed = dict(choices.values())
    combined = dict(proposed)
    combined.update(current)
    roots, cyclic = _get_alias_roots(combined, allowed_edge=is_speaker_merge_allowed)
    allowed_roots = list(roster) + list(human_roots.values())
    safe = {}
    for variant in proposed:
        if variant in cyclic:
            print(f"  [skip] alias cycle reached from '{variant}'")
            continue
        canonical = roots[variant]
        root = resolve_speaker_label(canonical, allowed_roots)
        if root is None:
            print(f"  [skip] alias target '{canonical}' is not a roster or human-approved root")
            continue
        if variant != root and is_speaker_merge_allowed(variant, root):
            safe[variant] = root if flatten else proposed[variant]
    return safe
