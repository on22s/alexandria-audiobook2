"""Safe in-run speaker identity stabilization and uncertain-variant reporting."""

import copy
import re
from difflib import SequenceMatcher


def stabilize_speaker_identities(entries, established_speakers=None):
    """Return copied entries with only exact normalized variants canonicalized."""
    repaired = copy.deepcopy(entries)
    canonicals = []
    changes = []
    review = []
    for speaker in established_speakers or []:
        value = str(speaker or "").strip()
        if value and value not in canonicals:
            canonicals.append(value)

    for index, entry in enumerate(repaired, 1):
        if not isinstance(entry, dict):
            continue
        original = str(entry.get("speaker") or "")
        stripped = " ".join(original.split())
        exact = next((name for name in canonicals
                      if _identity_key(name) == _identity_key(stripped)), None)
        if exact:
            canonical = exact
        else:
            canonical = stripped
            if canonical:
                candidates = _uncertain_candidates(canonical, canonicals)
                if candidates:
                    review.append({"entry_number": index, "speaker": canonical,
                                   "candidates": candidates})
                canonicals.append(canonical)
        if canonical and canonical != original:
            entry["speaker"] = canonical
            changes.append({"type": "speaker_identity", "entry_number": index,
                            "before": original, "after": canonical})
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


def _identity_key(value):
    return re.sub(r"[^\w]+", "", str(value or "").casefold(), flags=re.UNICODE)


def resolve_speaker_label(name, labels):
    """Return the label in `labels` that `name` refers to under the same
    normalization generation uses (_identity_key: casefold + strip all
    non-word characters), or None if none match. Deterministic on duplicate
    keys: labels are considered in sorted order, first match wins."""
    key = _identity_key(name)
    if not key:
        return None
    for label in sorted(labels):
        if _identity_key(label) == key:
            return label
    return None


def _uncertain_candidates(speaker, canonicals):
    key = _identity_key(speaker)
    results = []
    for canonical in canonicals:
        candidate_key = _identity_key(canonical)
        ratio = SequenceMatcher(None, key, candidate_key).ratio()
        if ratio >= 0.90 or _is_extended_person_name(speaker, canonical):
            results.append({"speaker": canonical, "similarity": round(ratio, 4)})
    return sorted(results, key=lambda item: (-item["similarity"], item["speaker"]))


def _is_extended_person_name(first, second):
    relation_words = {"mother", "father", "sister", "brother", "daughter", "son"}
    first_words = re.findall(r"[\w]+", first.casefold(), re.UNICODE)
    second_words = re.findall(r"[\w]+", second.casefold(), re.UNICODE)
    shorter, longer = sorted((first_words, second_words), key=len)
    return (len(shorter) == 1 and len(longer) == 2 and shorter[0] == longer[0]
            and not any(word.isdigit() or word in relation_words for word in longer))
