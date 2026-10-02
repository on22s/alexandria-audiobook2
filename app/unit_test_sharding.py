"""Deterministic, time-balanced assignment of unit-test modules to CI shards.

A shard is a set of whole test MODULES, never a slice of one: class-level
fixtures and module state stay together, and every module belongs to exactly one
shard. Every shard computes the same assignment from the same inputs (the
sorted module names and the checked-in weight file), so no coordination is
needed, and the weights only affect balance. A stale or missing weight can make
a shard slower; it can never make a test run twice or not at all.

This file is deliberately not named test_*.py: discovery runs with that pattern
from app/ and would import it as a test module.
"""

import json
import math
import re
import unittest
from pathlib import Path

DURATIONS_PATH = Path(__file__).parent / "tests" / "module_durations.json"
# A module with no recorded time (a new test file) counts as this many seconds.
DEFAULT_WEIGHT_SECONDS = 1.0
_SPEC = re.compile(r"(\d+)/(\d+)")


def parse_shard_spec(spec):
    """Return (index, count) for 'I/N' with 1 <= I <= N."""
    match = _SPEC.fullmatch(spec.strip()) if isinstance(spec, str) else None
    if match is None:
        raise ValueError(f"Shard must look like 'I/N' (for example 2/3), got {spec!r}")
    index, count = int(match.group(1)), int(match.group(2))
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"Shard {spec!r} is out of range: need 1 <= I <= N")
    return index, count


def get_module_durations(path=DURATIONS_PATH):
    """Read module -> seconds, refusing anything that is not a finite number >= 0."""
    durations = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(durations, dict):
        raise ValueError("Module durations must be a JSON object")
    for module, seconds in durations.items():
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) \
                or not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"Module duration for {module!r} must be a finite number >= 0")
    return durations


def get_test_shards(module_names, durations, count):
    """Return {1..count: sorted module names}; each module appears in exactly one shard.

    Longest-processing-time-first: heaviest module to the currently lightest shard.
    Ties break by name and shard number, so the result never depends on input order.
    """
    names = sorted(module_names)
    if len(set(names)) != len(names):
        raise ValueError("Module names must be unique")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Shard count must be a positive integer")
    shards = {number: [] for number in range(1, count + 1)}
    totals = {number: 0.0 for number in shards}
    weight = lambda name: float(durations.get(name, DEFAULT_WEIGHT_SECONDS))
    for name in sorted(names, key=lambda item: (-weight(item), item)):
        number = min(totals, key=lambda item: (totals[item], item))
        shards[number].append(name)
        totals[number] += weight(name)
    return {number: sorted(modules) for number, modules in shards.items()}


def get_test_module_key(test):
    """The module stem a test belongs to ('test_foo' for tests.test_foo.Class.method).

    A module that fails to import becomes a unittest.loader._FailedTest whose id
    does not start with the module, so it is read from its method name instead.
    Dropping those would hide a broken test file behind a green shard.
    """
    identifier = test.id()
    if type(test).__name__ == "_FailedTest":
        identifier = getattr(test, "_testMethodName", identifier)
    parts = identifier.split(".")
    return parts[1] if parts[0] == "tests" and len(parts) > 1 else parts[0]


def get_leaf_tests(suite):
    """Flatten a (possibly nested) suite into its individual tests, in order."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from get_leaf_tests(item)
        else:
            yield item


def get_sharded_suite(suite, index, count, durations):
    """Return a suite holding only shard `index` of `count`; same tests, same order."""
    leaves = list(get_leaf_tests(suite))
    modules = {get_test_module_key(test) for test in leaves}
    chosen = set(get_test_shards(modules, durations, count)[index])
    return unittest.TestSuite(test for test in leaves if get_test_module_key(test) in chosen)
