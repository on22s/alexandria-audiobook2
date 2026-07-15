"""Preview and apply explicitly selected speaker-attribution changes."""

import copy

from script_preflight import is_possible_misattributed_narration


def build_speaker_review(entries):
    candidates = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        speaker = str(entry.get("speaker") or "").strip()
        text = str(entry.get("text") or "").strip()
        if not is_possible_misattributed_narration(text, speaker):
            continue
        starts_in_third_person = text.casefold().startswith(speaker.casefold() + " ")
        candidates.append({
            "entry_number": index + 1,
            "current_speaker": speaker,
            "suggested_speaker": "NARRATOR" if starts_in_third_person else None,
            "reason": ("entry_starts_with_current_speaker_in_third_person"
                       if starts_in_third_person else "possible_narration_in_character_entry"),
            "text": text,
            "previous": _context(entries, index - 1),
            "next": _context(entries, index + 1),
        })
    return candidates


def apply_speaker_selections(entries, selections):
    repaired = copy.deepcopy(entries)
    seen = set()
    changes = []
    for selection in selections:
        number = selection["entry_number"]
        if number in seen:
            raise ValueError(f"Entry {number} was selected more than once.")
        seen.add(number)
        if number < 1 or number > len(repaired) or not isinstance(repaired[number - 1], dict):
            raise ValueError(f"Entry {number} does not exist.")
        entry = repaired[number - 1]
        current = str(entry.get("speaker") or "").strip()
        expected = str(selection["expected_speaker"] or "").strip()
        new_speaker = str(selection["new_speaker"] or "").strip()
        if current != expected:
            raise ValueError(f"Entry {number} speaker changed from the previewed value.")
        if not new_speaker:
            raise ValueError(f"Entry {number} needs a non-empty new speaker.")
        if new_speaker == current:
            continue
        entry["speaker"] = new_speaker
        changes.append({"entry_number": number, "before": current, "after": new_speaker})
    return {"entries": repaired, "changes": changes}


def _context(entries, index):
    if index < 0 or index >= len(entries) or not isinstance(entries[index], dict):
        return None
    return {"entry_number": index + 1, "speaker": entries[index].get("speaker"),
            "text": entries[index].get("text")}
