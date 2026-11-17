"""Run the unit suite with the dependency visibility provided by CI.

CI installs CPU Torch and all requirements, including PEFT/Transformers, so
structural adapter tests use real config and tensor parsers. Explicit import
blocking remains available to exercise missing-dependency failure paths.
Keep BLOCKED_MODULES aligned with .github/workflows/tests.yml.
"""

import sys
import time

from skip_registry import get_shard_verdict
from utils import atomic_json_write

# CI now installs every ML import required for CPU structural checks.
BLOCKED_MODULES = ()

class _BlockedImportFinder:
    """Raise ImportError for BLOCKED_MODULES and anything under them."""

    def __init__(self, blocked):
        self._blocked = tuple(blocked)

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in self._blocked:
            raise ImportError(
                f"No module named {fullname!r} (blocked: CI installs without "
                f"{', '.join(self._blocked)})")
        return None  # not ours; let the normal finders handle it


def block_ml_imports(blocked=BLOCKED_MODULES):
    """Apply the current policy with one finder. Drop already-imported modules so a module
    imported before this call cannot mask the block."""
    blocked = tuple(dict.fromkeys(blocked))
    finders = [finder for finder in sys.meta_path if isinstance(finder, _BlockedImportFinder)]
    finder = finders[0] if finders else _BlockedImportFinder(blocked)
    finder._blocked = blocked
    sys.meta_path[:] = [item for item in sys.meta_path if not isinstance(item, _BlockedImportFinder)]
    for name in list(sys.modules):
        if name.split(".", 1)[0] in blocked:
            del sys.modules[name]
    if blocked:
        sys.meta_path.insert(0, finder)


def pop_shard_option(argv):
    """Remove '--shard I/N' (or '--shard=I/N') from argv; return (rest, spec or None)."""
    rest, spec, position = [], None, 0
    while position < len(argv):
        if argv[position] == "--shard":
            if position + 1 >= len(argv):
                raise SystemExit("--shard needs a value like 2/3")
            spec = argv[position + 1]
            position += 2
        elif argv[position].startswith("--shard="):
            spec = argv[position].split("=", 1)[1]
            position += 1
        else:
            rest.append(argv[position])
            position += 1
    return rest, spec


def main(argv=None):
    """Run unittest with the same import exclusions as CI.

    Usage: python -m ci_env discover -s . -p "test_*.py" [--shard I/N]

    With --shard, only that shard's test MODULES run (see unit_test_sharding.py);
    without it the behaviour is exactly the plain unittest run.
    """
    import unittest

    argv = list(sys.argv[1:] if argv is None else argv)
    receipt_path = None
    if "--test-report" in argv:
        position = argv.index("--test-report")
        if position + 1 >= len(argv):
            raise SystemExit("--test-report needs a path")
        receipt_path = argv[position + 1]
        del argv[position:position + 2]
    test_ids = []
    result_holder = [None]
    completed = 0
    active_test = None
    clock = time.monotonic
    started = clock()
    last_published = 0.0

    def publish(*, complete=False, status=None, force=False):
        nonlocal last_published
        if receipt_path is None or (not force and clock() - last_published < 5):
            return
        result = result_holder[0]
        successful = result is not None and result.wasSuccessful()
        skipped = list(result.skipped) if result else []
        skip_ids = [test.id() for test, _reason in skipped]
        if status is None:
            status = 'running'
            if complete:
                verdict = get_shard_verdict(
                    complete=True,
                    tests_ran=result.testsRun if result else 0,
                    exit_code=0 if successful else 1,
                    failures=len(result.failures) if result else 0,
                    errors=len(result.errors) if result else 0,
                    unexpected_successes=len(result.unexpectedSuccesses) if result else 0,
                    skips=len(skipped), skip_ids=skip_ids)
                status = 'passed' if verdict['passed'] else 'failed'
        payload = {
            'schema_version': 1,
            'shard': shard,
            'status': status,
            'complete': complete,
            'tests_ran': result.testsRun if result else 0,
            'tests_completed': completed,
            'test_ids': test_ids,
            'failures': len(result.failures) if result else 0,
            'errors': len(result.errors) if result else 0,
            'skips': len(result.skipped) if result else 0,
            'expected_failures': len(result.expectedFailures) if result else 0,
            'unexpected_successes': len(result.unexpectedSuccesses) if result else 0,
            'failure_ids': [test.id() for test, _ in result.failures] if result else [],
            'error_ids': [test.id() for test, _ in result.errors] if result else [],
            'skip_ids': skip_ids,
            'skip_reasons': {test.id(): str(reason) for test, reason in skipped},
            'active_test': active_test,
            'duration_seconds': round(clock() - started, 3),
        }
        atomic_json_write(payload, str(receipt_path), allow_nonatomic_fallback=False)
        last_published = clock()

    class RecordedResult(unittest.TextTestResult):
        def __init__(self, *arguments, **kwargs):
            super().__init__(*arguments, **kwargs)
            result_holder[0] = self

        def startTest(self, test):
            nonlocal active_test
            test_ids.append(test.id())
            super().startTest(test)
            active_test = test.id()
            publish(force=len(test_ids) == 1)

        def stopTest(self, test):
            nonlocal completed, active_test
            super().stopTest(test)
            completed += 1
            active_test = None
            publish()

    class RecordedRunner(unittest.TextTestRunner):
        def __init__(self, **kwargs):
            super().__init__(resultclass=RecordedResult, **kwargs)
    argv, shard = pop_shard_option(argv)
    publish(force=True)
    try:
        block_ml_imports()
        if shard is None:
            runner = unittest.main(module=None, argv=["python -m ci_env"] + argv,
                                   testRunner=RecordedRunner, exit=False)
        else:
            from unit_test_sharding import get_module_durations, get_sharded_suite, parse_shard_spec
            try:
                index, count = parse_shard_spec(shard)
                durations = get_module_durations()
            except ValueError as error:
                raise SystemExit(f"ci_env: {error}") from error

            class ShardedProgram(unittest.TestProgram):
                def runTests(self):
                    self.test = get_sharded_suite(self.test, index, count, durations)
                    super().runTests()

            runner = ShardedProgram(module=None, argv=["python -m ci_env"] + argv,
                                    testRunner=RecordedRunner, exit=False)
    except BaseException as error:
        publish(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'crashed', force=True)
        raise
    publish(complete=True, force=True)
    return 0 if runner.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
