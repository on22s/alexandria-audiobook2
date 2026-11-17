"""One parser and one verdict for the reviewed native-Windows skip registry.

`app/tests/windows_only_skip_baseline.txt` registers every reviewed skip site in
`app/tests`, one row per
`<file>:<QualName>:<decorator-index> = <sha256>  # <declaration>`.
`test_windows_native_ci.py` proves that registry still matches the source at
rest. Nothing used it at runtime, so every shard counted a legitimate
POSIX-only skip as a failure and a native Windows run could never pass,
however clean it was.

Both shard layers ask the same two questions, so they ask them here:

    is_registered_skip - does a registry row name this skipped test id?
    get_shard_verdict  - do these shard facts earn a pass on this platform?

The rule is deliberately asymmetric. Only `sys.platform == 'win32'` may skip at
all, and only skips a registry row names. Every other platform keeps the
original rule, where any skip fails the shard, so Linux CI is unchanged.
"""

import sys
from pathlib import Path

SKIP_REGISTRY_PATH = Path(__file__).with_name('tests') / 'windows_only_skip_baseline.txt'


def parse_skip_registry(text):
    """Parse registry rows into `{location: (digest, declaration)}`.

    Raises ValueError for a line that is not that shape, or a location
    repeated by a second line, so a malformed registry fails loudly instead of
    quietly registering nothing.
    """
    rows = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        location, separator, rest = line.partition(' = ')
        if not separator:
            raise ValueError(
                f'Registry line {number} is not "<location> = <digest>  # <declaration>"')
        digest, marker, declaration = rest.partition('  # ')
        if not marker or not declaration.strip():
            raise ValueError(f'Registry line {number} lacks a reviewed declaration')
        if location in rows:
            raise ValueError(f'Registry line {number} repeats location {location}')
        rows[location] = (digest.strip(), declaration.strip())
    return rows


def get_registered_skip_locations(path=SKIP_REGISTRY_PATH):
    """Every location the reviewed registry names. Reads; never writes."""
    return set(parse_skip_registry(Path(path).read_text(encoding='utf-8')))


def get_test_id_location(test_id):
    """Map `tests.test_mod.Class.method` to `('test_mod.py', 'Class.method')`.

    Returns None for an id discovery did not produce in that shape; the caller
    treats that as unregistered, so an unparseable id can never pass.
    """
    parts = test_id.split('.') if isinstance(test_id, str) else []
    if len(parts) < 3 or parts[0] != 'tests' or not all(parts):
        return None
    return parts[1] + '.py', '.'.join(parts[2:])


def is_registered_skip(test_id, locations):
    """True when a registry row names this test, as itself or as its class.

    A class row (`test_mod.py:Case:0`) covers every method of that class; a
    method row (`test_mod.py:Case.test_a:0`) covers exactly that test. A name
    matches only on a dot boundary, so `Case.test_a` never covers
    `Case.test_abc`.
    """
    location = get_test_id_location(test_id)
    if location is None:
        return False
    module_file, qualname = location
    for entry in locations:
        entry_file, separator, entry_name = entry.partition(':')
        if not separator or entry_file != module_file:
            continue
        name = entry_name.partition(':')[0]
        if name and (qualname == name or qualname.startswith(name + '.')):
            return True
    return False


def get_shard_verdict(platform=None, *, tests_ran, complete, exit_code, failures, errors,
                      unexpected_successes, skips, skip_ids, locations=None):
    """Decide one shard's pass from its facts on `platform` (default: this one).

    This is the single description of the shard pass rule: `ci_env` writes a
    receipt status from it and `local_unit_shards` aggregates with it, so the
    two cannot drift apart. The arguments are facts; the returned dict is new
    and nothing passed in is modified.

    A native Windows shard passes when it completed, exited 0, had no
    failures, errors or unexpected successes, ran at least one test, and every
    skipped id is one the reviewed registry names. Every other platform keeps
    the stricter original rule, where any skip fails. `locations` defaults to
    the reviewed registry and is only read when Windows must judge a skip.
    """
    platform = sys.platform if platform is None else platform
    problems = []
    if complete is not True:
        problems.append('shard did not complete')
    if type(tests_ran) is not int or isinstance(tests_ran, bool) or tests_ran < 1:
        problems.append(f'shard ran no tests ({tests_ran!r})')
    if type(exit_code) is not int or isinstance(exit_code, bool) or exit_code != 0:
        problems.append(f'shard exit code was {exit_code!r}')
    for name, value in (('failures', failures), ('errors', errors),
                        ('unexpected successes', unexpected_successes)):
        if type(value) is not int or isinstance(value, bool) or value:
            problems.append(f'shard recorded {name}: {value!r}')

    registered, unregistered = 0, []
    count = skips if type(skips) is int and not isinstance(skips, bool) and skips >= 0 else None
    if count is None:
        problems.append(f'shard skip count is not a count ({skips!r})')
    elif count:
        ids = skip_ids
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
            problems.append(f'{count} skipped test(s) have no recorded identities')
        elif len(ids) != count:
            problems.append(f'{len(ids)} skip identities for {count} skipped tests')
        elif platform == 'win32':
            known = get_registered_skip_locations() if locations is None else locations
            unregistered = [item for item in ids if not is_registered_skip(item, known)]
            registered = count - len(unregistered)
        else:
            unregistered = list(ids)
        for identifier in unregistered:
            problems.append(f'unregistered skip: {identifier}')
        if platform != 'win32' and count:
            problems.append(f'{count} skipped test(s); only a native Windows shard may skip')
    return dict(passed=not problems, registered_skips=registered,
                unregistered_skips=len(unregistered),
                unregistered_skip_ids=list(unregistered), problems=problems)
