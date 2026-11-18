"""One parser and one verdict for the reviewed native-Windows skip registry.

`app/tests/windows_only_skip_baseline.txt` registers every reviewed skip site in
`app/tests`, one row per `<location> = <sha256>  # <declaration>`.
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

Three kinds of row are reviewed. A decorator row names a `unittest.skip*` site
and reads `<file>:<QualName>:<decorator-index>`. A runtime row names a
`self.skipTest(...)`/`raise SkipTest(...)` site that only fires while a test is
running (it cannot be a decorator because the capability is probed inside the
test). It reads `<file>:<QualName>:runtime:<reason-digest>` and its declaration
begins `reason=<repr>;`, so the exact skip message a row authorizes is pinned
twice: in the location digest the verdict compares against the receipt, and in
reviewed prose the census re-parses. A runtime row never authorizes a different
message, and a receipt that records no message is not authorized at all.

A `runtime-prefix` row is the one exception, for a skip whose message is a stable
literal prefix followed by unpredictable text (a WinError that embeds a per-run
temp path, for example). It reads `<file>:<QualName>:runtime-prefix:<digest>`
and pins the stable prefix the same way: the location digest is checked against
the declaration, and the verdict accepts only a recorded message that starts
with that exact prefix. The varying remainder is never pinned; a message that
does not start with the prefix stays unregistered. The census still proves the
prefix is the literal prefix the site raises.
"""

import ast
import hashlib
import re
import sys
from pathlib import Path

SKIP_REGISTRY_PATH = Path(__file__).with_name('tests') / 'windows_only_skip_baseline.txt'
RUNTIME_SITE_FIELD = 'runtime'
RUNTIME_PREFIX_SITE_FIELD = 'runtime-prefix'
RUNTIME_FIELDS = frozenset((RUNTIME_SITE_FIELD, RUNTIME_PREFIX_SITE_FIELD))
RUNTIME_REASON_DIGEST = re.compile(r'[0-9a-f]{64}')
TEST_ID_PATTERN = re.compile(r'tests\.(?P<module>[A-Za-z_][A-Za-z0-9_]*)'
                             r'\.(?P<qualname>[A-Za-z_][A-Za-z0-9_]*'
                             r'(?:\.[A-Za-z_][A-Za-z0-9_]*)*)')
DECLARED_REASON_PREFIX = 'reason='
# How a site's pinned value is matched against a recorded skip message.
MATCH_ANY = 'any'          # decorator row: the location alone authorizes it
MATCH_EXACT = 'exact'      # runtime row: get_reason_key(reason) == value
MATCH_PREFIX = 'prefix'    # runtime-prefix row: reason startswith(value)


def get_reason_key(reason):
    """The digest a runtime row pins for one reason string (a message or prefix)."""
    return hashlib.sha256(str(reason).encode('utf-8', 'surrogatepass')).hexdigest()


def get_declared_reason(declaration):
    """The reason string a runtime row's declaration pins, or None.

    Runtime declarations start with `reason=<python repr>;` followed by prose.
    The literal is read back with `ast.literal_eval`, so a semicolon inside the
    message is part of the message, not the field separator.
    """
    if not isinstance(declaration, str) or not declaration.startswith(DECLARED_REASON_PREFIX):
        return None
    body = declaration[len(DECLARED_REASON_PREFIX):]
    for index, character in enumerate(body):
        if character != ';':
            continue
        try:
            value = ast.literal_eval(body[:index])
        except (ValueError, SyntaxError):
            continue
        if isinstance(value, str):
            return value
    return None


def parse_skip_registry_row(line, number=1):
    """Parse one row into `(location, digest, declaration)`.

    Raises ValueError for a line that is not the row shape, or for a runtime row
    whose location digest disagrees with the reason its declaration pins, so a
    malformed row fails loudly instead of quietly registering nothing.
    """
    location, separator, rest = line.partition(' = ')
    if not separator:
        raise ValueError(
            f'Registry line {number} is not "<location> = <digest>  # <declaration>"')
    digest, marker, declaration = rest.partition('  # ')
    if not marker or not declaration.strip():
        raise ValueError(f'Registry line {number} lacks a reviewed declaration')
    location, digest, declaration = location.strip(), digest.strip(), declaration.strip()
    fields = location.split(':')
    if len(fields) < 3 or not all(fields):
        raise ValueError(f'Registry line {number} has an unrecognised location {location!r}')
    if fields[2] not in RUNTIME_FIELDS:
        if len(fields) != 3 or not fields[2].isdigit():
            raise ValueError(f'Registry line {number} has an unrecognised location {location!r}')
        return location, digest, declaration
    if len(fields) != 4 or not RUNTIME_REASON_DIGEST.fullmatch(fields[3]):
        raise ValueError(f'Registry line {number} runtime row lacks a reason digest: {location!r}')
    declared = get_declared_reason(declaration)
    if declared is None:
        raise ValueError(
            f'Registry line {number} runtime row needs a "reason=<repr>;" declaration')
    if fields[2] == RUNTIME_PREFIX_SITE_FIELD and not declared:
        raise ValueError(
            f'Registry line {number} runtime-prefix row pins an empty prefix: {location!r}')
    if get_reason_key(declared) != fields[3]:
        raise ValueError(
            f'Registry line {number} reason digest disagrees with its declaration: {location!r}')
    return location, digest, declaration


def format_skip_registry_row(location, digest, declaration):
    """Render one row; the only place the row grammar is written."""
    return f'{location} = {digest}  # {declaration}'


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
        location, digest, declaration = parse_skip_registry_row(line, number)
        if location in rows:
            raise ValueError(f'Registry line {number} repeats location {location}')
        rows[location] = (digest, declaration)
    return rows


def get_runtime_skip_target(location):
    """`(file, qualname, reason_digest, match)` for a runtime row, else None.

    The one reader of the runtime row's extra field; the census and the verdict
    both ask here instead of splitting the location themselves. `match` is
    `MATCH_EXACT` for `runtime` and `MATCH_PREFIX` for `runtime-prefix`.
    """
    fields = location.split(':')
    if len(fields) != 4 or fields[2] not in RUNTIME_FIELDS:
        return None
    match = MATCH_EXACT if fields[2] == RUNTIME_SITE_FIELD else MATCH_PREFIX
    return fields[0], fields[1], fields[3], match


def get_verdict_site(location, declaration=None):
    """The `(file, name, match, value)` verdict entry for one registry row.

    This is the one place a row's location and declaration are turned into what
    `is_registered_skip` consumes, so the parser, the census and the verdict
    cannot disagree on the tuple order. A runtime-prefix row needs its
    declaration to recover the pinned prefix; without one it fails closed.
    """
    target = get_runtime_skip_target(location)
    if target is not None:
        module_file, qualname, reason_digest, match = target
        value = get_declared_reason(declaration) if match == MATCH_PREFIX else reason_digest
        return module_file, qualname, match, value
    module_file, _, qualname = location.partition(':')
    return module_file, qualname.partition(':')[0], MATCH_ANY, None


def get_registered_skip_sites(path=SKIP_REGISTRY_PATH):
    """Every `(file, qualname, match, value)` the reviewed registry names.

    `match`/`value` are: `MATCH_ANY`/None for a decorator row, which authorizes
    the location alone; `MATCH_EXACT`/the pinned digest for a runtime row, which
    authorizes only that exact skip message; and `MATCH_PREFIX`/the pinned
    prefix for a runtime-prefix row, which authorizes only messages starting
    with that prefix.
    """
    return tuple(get_verdict_site(location, declaration) for location, (_digest, declaration)
                 in parse_skip_registry(Path(path).read_text(encoding='utf-8')).items())


def get_registered_skip_locations(path=SKIP_REGISTRY_PATH):
    """Every location the reviewed registry names. Reads; never writes."""
    return set(parse_skip_registry(Path(path).read_text(encoding='utf-8')))


def get_test_id_location(test_id):
    """Map `tests.test_mod.Class.method` to `('test_mod.py', 'Class.method')`.

    A unittest `subTest` reports `<id> (key=value)` or `<id> [message]`, which is
    still that test; matching only the leading identifiers means one registry
    row covers every subtest of the method it names, whatever the parameters
    contain.
    """
    match = TEST_ID_PATTERN.match(test_id) if isinstance(test_id, str) else None
    if match is None:
        return None
    return match.group('module') + '.py', match.group('qualname')


def is_registered_skip(test_id, sites, reason=None):
    """True when a registry row names this test and, for runtime rows, its message.

    A class row (`test_mod.py:Case:0`) covers every method of that class; a
    method row (`test_mod.py:Case.test_a:0`) covers exactly that test. A name
    matches only on a dot boundary, so `Case.test_a` never covers
    `Case.test_abc`. A runtime row also has to match the recorded message
    digest, and a runtime-prefix row its pinned prefix, so `reason=None` cannot
    authorize either.
    """
    location = get_test_id_location(test_id)
    if location is None:
        return False
    module_file, qualname = location
    for entry_file, name, match, value in sites:
        if not name or entry_file != module_file:
            continue
        if not (qualname == name or qualname.startswith(name + '.')):
            continue
        if match == MATCH_ANY:
            return True
        if reason is None:
            continue
        if match == MATCH_EXACT and get_reason_key(reason) == value:
            return True
        if match == MATCH_PREFIX and value and str(reason).startswith(value):
            return True
    return False


def get_shard_verdict(platform=None, *, tests_ran, complete, exit_code, failures, errors,
                      unexpected_successes, skips, skip_ids, skip_reasons=None, sites=None):
    """Decide one shard's pass from its facts on `platform` (default: this one).

    This is the single description of the shard pass rule: `ci_env` writes a
    receipt status from it and `local_unit_shards` aggregates with it, so the
    two cannot drift apart. The arguments are facts; the returned dict is new
    and nothing passed in is modified.

    A native Windows shard passes when it completed, exited 0, had no
    failures, errors or unexpected successes, ran at least one test, and every
    skipped id is one the reviewed registry names. Every other platform keeps
    the stricter original rule, where any skip fails. `sites` defaults to the
    reviewed registry and is only read when Windows must judge a skip;
    `skip_reasons` maps a skipped id to the message its receipt recorded, which
    a runtime row has to match.
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
            known = get_registered_skip_sites() if sites is None else sites
            reasons = skip_reasons if isinstance(skip_reasons, dict) else {}
            unregistered = [item for item in ids
                            if not is_registered_skip(item, known, reasons.get(item))]
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
