"""Selectable front-matter and delivery-direction repairs."""

import copy
import re

from script_preflight import audit_unicode_text


MAX_REPAIR_DIRECTION_LENGTH = 400


_FRONT_MATTER_RE = re.compile(
    r"copyright|©|isbn|library of congress|rights reserved|yen press|yen on|"
    r"first published|edition|imprint|trademark|https?/?/?|\.com\b|\.tumblr\b|"
    r"avenue of the americas|new york, ny|classification|subjects cyac",
    re.IGNORECASE,
)


def build_content_review(entries):
    front_matter = []
    directions = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "")
        instruct = str(entry.get("instruct") or "")
        if index < 60 and _FRONT_MATTER_RE.search(text):
            front_matter.append({"entry_number": index + 1, "text": text,
                                 "speaker": entry.get("speaker")})
        normalized = " ".join(instruct.split())
        if instruct != normalized:
            directions.append({"entry_number": index + 1, "before": instruct,
                               "suggested": normalized, "reason": "whitespace"})
    return {"front_matter": front_matter, "direction_normalizations": directions}


def apply_content_selections(entries, removals, direction_changes):
    review = build_content_review(entries)
    allowed_removals = {item["entry_number"] for item in review["front_matter"]}
    allowed_directions = {item["entry_number"]: item["suggested"]
                          for item in review["direction_normalizations"]}
    repaired = copy.deepcopy(entries)
    changes = []
    removal_indexes = set()
    for selection in removals:
        number = selection["entry_number"]
        entry = _get_entry(repaired, number)
        if number not in allowed_removals:
            raise ValueError(f"Entry {number} was not offered for front-matter removal.")
        if entry.get("text") != selection["expected_text"]:
            raise ValueError(f"Entry {number} text changed after selection.")
        if number in removal_indexes:
            raise ValueError(f"Entry {number} was selected for removal more than once.")
        removal_indexes.add(number)
        changes.append({"type": "remove_front_matter", "entry_number": number,
                        "text": entry.get("text")})
    seen_directions = set()
    for selection in direction_changes:
        number = selection["entry_number"]
        entry = _get_entry(repaired, number)
        if number in removal_indexes:
            raise ValueError(f"Entry {number} cannot be removed and edited together.")
        if number in seen_directions:
            raise ValueError(f"Entry {number} has more than one direction replacement.")
        seen_directions.add(number)
        if entry.get("instruct") != selection["expected_instruct"]:
            raise ValueError(f"Entry {number} direction changed after selection.")
        if number not in allowed_directions:
            raise ValueError(f"Entry {number} was not offered for direction normalization.")
        replacement = allowed_directions[number]
        if selection["new_instruct"] != replacement:
            raise ValueError(f"Entry {number} requires the exact suggested direction.")
        if not replacement:
            raise ValueError(f"Entry {number} needs a non-empty direction.")
        if len(replacement) > MAX_REPAIR_DIRECTION_LENGTH:
            raise ValueError(f"Entry {number} direction exceeds {MAX_REPAIR_DIRECTION_LENGTH} characters.")
        if audit_unicode_text(replacement)["unsafe_controls"]:
            raise ValueError(f"Entry {number} direction contains unsafe control characters.")
        if replacement != entry.get("instruct"):
            changes.append({"type": "replace_direction", "entry_number": number,
                            "before": entry.get("instruct"), "after": replacement})
            entry["instruct"] = replacement
    for number in sorted(removal_indexes, reverse=True):
        del repaired[number - 1]
    return {"entries": repaired, "changes": changes}


def _get_entry(entries, number):
    if (not isinstance(number, int) or isinstance(number, bool)
            or number < 1 or number > len(entries)
            or not isinstance(entries[number - 1], dict)):
        raise ValueError(f"Entry {number} does not exist.")
    return entries[number - 1]
