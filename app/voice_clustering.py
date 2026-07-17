"""Deterministic complete-link clustering for Voice Lab speaker identities."""

import json
from pathlib import Path

import numpy as np


OVERRIDE_VERSION = 1


def load_cluster_overrides(path: Path, narrator: str) -> dict:
    if not path.is_file():
        return {"merge": [], "split": []}
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("version") != OVERRIDE_VERSION:
        raise ValueError(f"unsupported cluster override version: {data.get('version')}")
    narrators = data.get("narrators")
    if not isinstance(narrators, dict):
        raise ValueError("cluster overrides must contain a narrators object")
    override = narrators.get(narrator, {})
    merge = override.get("merge", [])
    split = override.get("split", [])
    if not isinstance(merge, list) or not isinstance(split, list):
        raise ValueError(f"cluster overrides for {narrator} must use merge/split lists")
    return {"merge": merge, "split": split}


def cluster_voices(labels: list[str], similarities, threshold: float,
                   overrides: dict | None = None) -> tuple[list[list[int]], list[dict]]:
    """Return order-independent complete-link clusters and decision evidence."""
    if len(set(labels)) != len(labels):
        raise ValueError("cluster labels must be unique")
    matrix = np.asarray(similarities, dtype=float)
    if matrix.shape != (len(labels), len(labels)):
        raise ValueError("similarity matrix shape does not match labels")
    label_to_index = {label: index for index, label in enumerate(labels)}
    overrides = overrides or {"merge": [], "split": []}

    split_pairs = set()
    for pair in overrides.get("split", []):
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("each split override must contain exactly two labels")
        unknown = [label for label in pair if label not in label_to_index]
        if unknown:
            raise ValueError(f"unknown split override label(s): {', '.join(unknown)}")
        split_pairs.add(frozenset(pair))

    clusters = [{label} for label in sorted(labels)]
    decisions = []
    for group in overrides.get("merge", []):
        if not isinstance(group, list) or len(group) < 2:
            raise ValueError("each merge override must contain at least two labels")
        unknown = [label for label in group if label not in label_to_index]
        if unknown:
            raise ValueError(f"unknown merge override label(s): {', '.join(unknown)}")
        group_set = set(group)
        if any(pair <= group_set for pair in split_pairs):
            raise ValueError("merge and split overrides conflict")
        matched = [cluster for cluster in clusters if cluster & group_set]
        merged = set().union(*matched)
        clusters = [cluster for cluster in clusters if cluster not in matched] + [merged]
        decisions.append({"type": "manual_merge", "labels": sorted(merged)})

    def blocked(left, right):
        return any(frozenset((a, b)) in split_pairs for a in left for b in right)

    while True:
        candidates = []
        for left_index in range(len(clusters)):
            for right_index in range(left_index + 1, len(clusters)):
                left, right = clusters[left_index], clusters[right_index]
                if blocked(left, right):
                    continue
                cross = [matrix[label_to_index[a], label_to_index[b]]
                         for a in left for b in right]
                minimum = float(min(cross))
                if minimum > threshold:
                    combined = tuple(sorted(left | right))
                    candidates.append((-minimum, combined, left_index, right_index, minimum))
        if not candidates:
            break
        _negative, combined, left_index, right_index, minimum = min(candidates)
        left, right = clusters[left_index], clusters[right_index]
        decisions.append({"type": "threshold_merge", "labels": list(combined),
                          "minimum_cross_similarity": minimum, "threshold": threshold})
        clusters = [cluster for index, cluster in enumerate(clusters)
                    if index not in (left_index, right_index)] + [left | right]
        clusters.sort(key=lambda cluster: tuple(sorted(cluster)))

    ordered = sorted((sorted(cluster) for cluster in clusters), key=lambda cluster: tuple(cluster))
    return [[label_to_index[label] for label in cluster] for cluster in ordered], decisions
