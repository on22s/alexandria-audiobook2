"""Per-line speaker gender and age (#653): the one definition of the values,
the pass-2 prompt text that asks for them, and the per-character summary the
Voices tab shows.

Why per line: measured on three PDNC novels x2 runs (character_traits.json),
pass 2 returning gender on every line matched a whole-book cast list exactly
(98.8% per character, 99.8% per line, 100% consistent within a character),
cost 2.6% more tokens and left who-speaks accuracy unchanged. Per line is also
the only shape that can follow a character through a time skip or a gender
change, which light novels have and a single per-character value cannot.

AGE IS HOW THE CHARACTER LOOKS AND ACTS, and the voice follows it. Childhood
stages follow the CDC's developmental stages (infant 0-1, toddler 1-3,
preschooler 3-5, middle childhood 6-11, teens 12-17); the adult bands are the
app's existing ones. AGELESS is a separate flag for immortal or un-aging beings
- a centuries-old vampire who looks and acts like a child is CHILD + ageless -
because "ageless" alone says nothing about how someone should sound.
"""
import collections

GENDERS = ("male", "female", "genderless", "unknown")
AGE_GROUPS = (("infant", "0-1"), ("toddler", "1-3"), ("young_child", "3-5"), ("child", "6-11"),
              ("teen", "12-17"), ("young_adult", "18-29"), ("adult", "30-39"),
              ("middle_aged", "40-59"), ("elderly", "60+"), ("unknown", ""))
AGE_GROUP_NAMES = tuple(name for name, _ in AGE_GROUPS)
# A change of one band (a teen turning young adult) does not change a voice;
# only a bigger jump or a gender change is a new state (owner, 2026-10-03).
STATE_CHANGE_BANDS = 2
# A new state counts only after this many consecutive lines show it, so one
# stray label never changes a voice while a real time skip still does. Set
# after Mushoku Tensei Vol 1 (time_skip_traits.json): per-line age followed
# the time skip in every chapter, but single batches flipped Roxy (an adult who
# looks young) and gave Paul and Rudeus stray infant/teen lines.
PERSIST_LINES = 10
# Children are not given an age anchor on the roster: they grow within a
# volume, and an anchor fought the text - with "toddler" on the roster,
# Rudeus's labels swung toddler <-> child through ages 3-5 (time_skip_traits_v2).
UNANCHORED_AGES = ("infant", "toddler", "young_child", "child")

TRAITS_FIELDS = '{"n", "speaker", "gender", "age_group", "ageless"}'
TRAITS_RULE = (
    "\n\nALSO, for every entry give the speaker's \"gender\" (MALE, FEMALE, GENDERLESS or "
    "UNKNOWN), \"age_group\" and \"ageless\". \"age_group\" is the age the speaker SOUNDS "
    "AND LOOKS at that point in the story - the voice a director would cast, not their true "
    "age - one of: "
    + ", ".join(f"{name.upper()} ({years})" for name, years in AGE_GROUPS if years)
    + ", UNKNOWN. A child speaks with a child's voice even with an adult's mind. \"ageless\" "
    "is true when the speaker's real age differs greatly from how they look and sound (an "
    "immortal, a long-lived race, a centuries-old vampire, a reincarnated mind). A ROSTER "
    "name may carry [gender, age group, ageless] established by earlier passages: keep those "
    "for that speaker unless this passage shows a change (a time skip, a transformation). "
    "Use UNKNOWN when the text does not establish it, and UNKNOWN and false for narration "
    "entries. Each object is then " + TRAITS_FIELDS + ".")


def get_normalized_gender(value):
    value = str(value or "").strip().lower()
    return value if value in GENDERS else "unknown"


def get_normalized_age_group(value):
    value = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return value if value in AGE_GROUP_NAMES else "unknown"


def get_normalized_ageless(value):
    return value is True or str(value).strip().lower() in ("true", "yes", "1")


def get_traits_from_answer(item):
    """The three per-line fields from one pass-2 answer object, normalised.
    A value outside the lists becomes unknown - never a retry."""
    return {"speaker_gender": get_normalized_gender(item.get("gender")),
            "speaker_age_group": get_normalized_age_group(item.get("age_group")),
            "speaker_ageless": get_normalized_ageless(item.get("ageless"))}


def get_traits_response_schema(base_schema):
    """The attribution schema with the three fields allowed (not required, so a
    model that omits them is normalised to unknown instead of retried)."""
    schema = {**base_schema, "name": base_schema["name"] + "_traits"}
    items = dict(base_schema["schema"]["items"])
    items["properties"] = {**items["properties"],
                           "gender": {"type": "string"}, "age_group": {"type": "string"},
                           "ageless": {"type": "boolean"}}
    schema["schema"] = {**base_schema["schema"], "items": items}
    return schema


def get_age_distance(first, second):
    if "unknown" in (first, second):
        return 0
    return abs(AGE_GROUP_NAMES.index(first) - AGE_GROUP_NAMES.index(second))


def _get_state(entry, current):
    """(gender, age) for one line, unknowns filled from the current state."""
    gender, age = entry["speaker_gender"], entry["speaker_age_group"]
    if current:
        gender = current[0] if gender == "unknown" else gender
        age = current[1] if age == "unknown" else age
    return gender, age


def _is_new_state(state, current):
    return (current[0] == "unknown" and state[0] != "unknown") \
        or (current[1] == "unknown" and state[1] != "unknown") \
        or (state[0] != current[0] and "unknown" not in (state[0], current[0])) \
        or get_age_distance(state[1], current[1]) >= STATE_CHANGE_BANDS


def _get_modal_state(states):
    genders = collections.Counter(g for g, _ in states if g != "unknown")
    ages = collections.Counter(a for _, a in states if a != "unknown")
    return (genders.most_common(1)[0][0] if genders else "unknown",
            ages.most_common(1)[0][0] if ages else "unknown")


def _get_settled_states(lines):
    """-> [((gender, age), start)] for one character's lines in order, `start`
    indexing `lines`: the first state from its first PERSIST_LINES known lines,
    then each change that PERSIST_LINES consecutive lines confirmed, starting
    at the first of those lines. The one implementation behind the card, the
    roster and the voice timeline (Rule 15)."""
    known = [(index, entry) for index, entry in enumerate(lines) if "speaker_gender" in entry
             and (entry["speaker_gender"], entry["speaker_age_group"]) != ("unknown", "unknown")]
    if not known:
        return []
    raw = [(e["speaker_gender"], e["speaker_age_group"]) for _, e in known]
    states = [(_get_modal_state(raw[:PERSIST_LINES]), known[0][0])]
    candidate = []
    for index, entry in known[len(raw[:PERSIST_LINES]):]:
        state = _get_state(entry, states[-1][0])
        if not _is_new_state(state, states[-1][0]):
            candidate = []
            continue
        if candidate and _is_new_state(state, _get_modal_state([s for s, _ in candidate])):
            candidate = []
        candidate.append((state, index))
        if len(candidate) >= PERSIST_LINES:
            states.append((_get_modal_state([s for s, _ in candidate]), candidate[0][1]))
            candidate = []
    return states


def get_speaker_trait_summary(lines):
    """-> the per-character view, or None without data.

    `lines` are a character's script entries in order. The STATES are the
    character's settled gender/age over the book: it starts from the most
    common values of its first PERSIST_LINES known lines, and moves to a new
    state only after PERSIST_LINES consecutive lines show a change of gender or
    of STATE_CHANGE_BANDS age bands. `current` is the last settled state - what
    a later batch is told (get_established_traits) and what a voice follows.
    """
    traited = [entry for entry in lines if "speaker_gender" in entry]
    if not traited:
        return None
    raw = [(e["speaker_gender"], e["speaker_age_group"]) for e in traited
           if (e["speaker_gender"], e["speaker_age_group"]) != ("unknown", "unknown")]
    states = [state for state, _ in _get_settled_states(lines)]
    overall = _get_modal_state(raw)
    current = states[-1] if states else ("unknown", "unknown")
    return {"gender": overall[0], "age_group": overall[1],
            "ageless": any(e.get("speaker_ageless") for e in traited),
            "lines": len(traited),
            "current": {"gender": current[0], "age_group": current[1]},
            "states": [{"gender": g, "age_group": a} for g, a in states] if len(states) > 1 else []}


def get_state_timeline(script_entries):
    """-> {SPEAKER: [{"from_entry", "gender", "age_group"}]} for every speaker
    whose settled state changes; `from_entry` indexes `script_entries`."""
    by_speaker = collections.defaultdict(list)
    for index, entry in enumerate(script_entries):
        if isinstance(entry, dict) and "speaker_gender" in entry and entry.get("speaker"):
            by_speaker[str(entry["speaker"]).strip().upper()].append((index, entry))
    out = {}
    for speaker, rows in by_speaker.items():
        states = _get_settled_states([entry for _, entry in rows])
        if len(states) > 1:
            out[speaker] = [{"from_entry": rows[start][0], "gender": g, "age_group": a}
                            for (g, a), start in states]
    return out


# The voice library tags adapters with the older, coarser age groups; the
# early-childhood stages all match its "child".
LIBRARY_AGE_GROUPS = {"infant": "child", "toddler": "child", "young_child": "child"}


def get_library_age_group(age_group):
    return LIBRARY_AGE_GROUPS.get(age_group, age_group)


def get_chunk_index_for_entry(chunks, speaker, text, start=0):
    """-> the index of the first chunk at or after `start` spoken by `speaker`
    whose text contains `text`, or None. By content, so chunks merged or
    edited after they were built still map."""
    wanted = " ".join(str(text or "").split())
    if not wanted:
        return None
    for index in range(max(0, start), len(chunks)):
        chunk = chunks[index] if isinstance(chunks[index], dict) else {}
        if (str(chunk.get("speaker") or "").strip().upper() == str(speaker).strip().upper()
                and wanted in " ".join(str(chunk.get("text") or "").split())):
            return index
    return None


def get_established_traits(named_entries):
    """-> {SPEAKER: "female, young adult, ageless"} for the roster a later
    pass-2 batch sees: each speaker's settled current state, from the lines
    attributed so far. Speakers with nothing settled are left out."""
    by_speaker = collections.defaultdict(list)
    for entry in named_entries:
        if entry and "speaker_gender" in entry and entry.get("speaker"):
            by_speaker[str(entry["speaker"]).strip().upper()].append(entry)
    out = {}
    for speaker, lines in by_speaker.items():
        summary = get_speaker_trait_summary(lines)
        if not summary:
            continue
        current = summary["current"]
        parts = [current["gender"]] if current["gender"] != "unknown" else []
        if current["age_group"] not in ("unknown", *UNANCHORED_AGES):
            parts.append(current["age_group"].replace("_", " "))
        if summary["ageless"]:
            parts.append("ageless")
        if parts:
            out[speaker] = ", ".join(parts)
    return out
