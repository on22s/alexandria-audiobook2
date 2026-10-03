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

TRAITS_FIELDS = '{"n", "speaker", "gender", "age_group", "ageless"}'
TRAITS_RULE = (
    "\n\nALSO, for every entry give the speaker's \"gender\" (MALE, FEMALE, GENDERLESS or "
    "UNKNOWN), \"age_group\" and \"ageless\". \"age_group\" is the age the speaker looks and "
    "acts at that point in the story, one of: "
    + ", ".join(f"{name.upper()} ({years})" for name, years in AGE_GROUPS if years)
    + ", UNKNOWN. \"ageless\" is true only for an immortal or otherwise un-aging being (a "
    "spirit, a god, a centuries-old vampire); give such a speaker the age they look and act. "
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


def get_speaker_trait_summary(lines):
    """-> the per-character view the Voices card shows, or None without data.

    `lines` are a character's script entries in order. The summary is the most
    common known gender and age, whether any line marked the speaker ageless,
    and the STATES: each point where the age moves by STATE_CHANGE_BANDS or
    more, or the gender changes, from the first known state on.
    """
    known = [entry for entry in lines if "speaker_gender" in entry]
    if not known:
        return None
    genders = collections.Counter(e["speaker_gender"] for e in known if e["speaker_gender"] != "unknown")
    ages = collections.Counter(e["speaker_age_group"] for e in known
                               if e["speaker_age_group"] != "unknown")
    states = []
    for entry in known:
        gender, age = entry["speaker_gender"], entry["speaker_age_group"]
        if gender == "unknown" and age == "unknown":
            continue
        if not states:
            states.append({"gender": gender, "age_group": age})
            continue
        last = states[-1]
        gender_changed = "unknown" not in (gender, last["gender"]) and gender != last["gender"]
        if gender_changed or get_age_distance(age, last["age_group"]) >= STATE_CHANGE_BANDS:
            states.append({"gender": gender if gender != "unknown" else last["gender"],
                           "age_group": age if age != "unknown" else last["age_group"]})
    return {"gender": genders.most_common(1)[0][0] if genders else "unknown",
            "age_group": ages.most_common(1)[0][0] if ages else "unknown",
            "ageless": any(e.get("speaker_ageless") for e in known),
            "lines": len(known), "states": states if len(states) > 1 else []}
