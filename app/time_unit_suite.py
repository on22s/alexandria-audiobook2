"""Time the whole unit suite and write per-module seconds for CI sharding.

    python time_unit_suite.py                 # rewrites tests/module_durations.json
    python time_unit_suite.py --from-times X  # aggregate a saved per-test timing file instead

Run it from app/ on an idle machine. The weights only balance the CI shards
(unit_test_sharding.py); stale weights make a shard slower, never incorrect, so
this is refreshed occasionally from a real CI run, not gated.
"""
import argparse
import collections
import json
import os
import sys
import time
import unittest

from unit_test_sharding import DURATIONS_PATH, get_test_module_key


def time_suite():
    """Run discovery and return {test id: seconds} plus the wall time."""
    times, started = {}, {}

    class TimedResult(unittest.TextTestResult):
        def startTest(self, test):
            started[test.id()] = time.perf_counter()
            super().startTest(test)

        def stopTest(self, test):
            times[test.id()] = time.perf_counter() - started[test.id()]
            super().stopTest(test)

    begin = time.perf_counter()
    suite = unittest.defaultTestLoader.discover(".", pattern="test_*.py")
    runner = unittest.TextTestRunner(stream=open(os.devnull, "w"), resultclass=TimedResult, verbosity=0)
    result = runner.run(suite)
    return times, time.perf_counter() - begin, len(result.failures) + len(result.errors)


def get_module_seconds(test_times):
    """Sum per-test seconds by module stem, rounded to 0.1 s."""
    totals = collections.defaultdict(float)
    for identifier, seconds in test_times.items():
        parts = identifier.split(".")
        totals[parts[1] if parts[0] == "tests" and len(parts) > 1 else parts[0]] += seconds
    return {module: round(seconds, 1) for module, seconds in sorted(totals.items())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--from-times", metavar="JSON", help="aggregate a saved {'times': {id: seconds}} file")
    parser.add_argument("--out", default=str(DURATIONS_PATH))
    args = parser.parse_args(argv)
    if args.from_times:
        times = json.loads(open(args.from_times, encoding="utf-8").read())["times"]
    else:
        times, wall, problems = time_suite()
        print(f"{len(times)} tests in {wall:.0f}s ({problems} failed/errored; failing tests can skew weights)")
    durations = get_module_seconds(times)
    with open(args.out, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(durations, indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(durations)} module weights to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
