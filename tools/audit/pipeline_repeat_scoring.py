"""Full-gold attribution scoring shared by repeat reports and the results index."""
import collections
import re


def get_pipeline_repeat_scores(checkpoint, gold):
    """Count every gold line; unlocated or unnamed lines are incorrect."""
    def normalize(text):
        return re.sub(r"\W+", "", text or "").lower()
    aliases = [{name.upper() for name in group} for group in gold.get("aliases", [])]
    def same(left, right):
        left, right = (left or "").upper(), (right or "").upper()
        return left == right or any(left in group and right in group for group in aliases)
    occurrences = collections.Counter(normalize(entry.get("text"))
                                      for entry in (checkpoint.get("segmented") or []))
    speakers = {}
    for entry in (row for row in (checkpoint.get("named") or []) if row):
        speakers.setdefault(normalize(entry.get("text")), entry.get("speaker"))
    scored, matched = {}, 0
    for entry in gold["entries"]:
        key = normalize(entry["line"])
        located = occurrences.get(key) == 1 and key in speakers
        matched += located
        scored[entry["id"]] = bool(located and same(speakers[key], entry["expected_speaker"]))
    if len(scored) != len(gold["entries"]):
        raise ValueError("Pipeline-repeat gold IDs must be unique")
    n, correct = len(gold["entries"]), sum(scored.values())
    return {"scored": scored, "n": n, "correct": correct, "matched": matched,
            "accuracy_pct": round(correct / n * 100, 1) if n else ""}
