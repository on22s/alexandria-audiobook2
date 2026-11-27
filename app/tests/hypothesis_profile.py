"""The one bounded, deterministic Hypothesis profile for the unit suite.

The suite runs as six CI shards under ``unittest discover``, so a property test
must cost a few seconds at most and must never flake. These settings buy that:

* ``derandomize=True`` makes every run draw the same sequence, so a red CI run
  reproduces locally by re-running the same test under the same profile. No
  seed is printed (``print_blob=False``) and none is needed: rerunning the test
  is the reproduction.
* ``database=None`` means no example database: one shard cannot feed examples
  to another, and a clean run stays clean. On its own it is NOT enough to keep
  ``.hypothesis/`` out of the checkout - Hypothesis also writes ``constants/``
  and ``unicode_data/`` there - so storage is pointed at either a user-supplied
  ``HYPOTHESIS_STORAGE_DIRECTORY`` or a per-process temp directory (removed at
  exit via ``atexit``). Setting ``HYPOTHESIS_STORAGE_DIRECTORY`` works before
  the first storage access, and ``hypothesis.configuration.set_hypothesis_home_dir``
  also redirects storage if something already resolved it before this module
  imported. That function has a public name but is NOT part of Hypothesis's
  exported or documented API: ``hypothesis/__init__.py`` never mentions it and
  there is no reference-doc page (Hypothesis's own tests use it). The version is
  pinned, and this module raises ``RuntimeError`` at import if a bump removes or
  renames it, rather than silently leaving storage in the checkout.
* ``deadline=None``: a wall-clock deadline turns a paused CI runner into a
  failure that has nothing to do with the code under test.
* ``max_examples`` is the cost knob (``DEFAULT_MAX_EXAMPLES`` for CI, roughly a
  few seconds for the whole set of property modules).

Raise the budget locally with the environment variable that names the profile::

    HYPOTHESIS_PROFILE=thorough python -m unittest tests.test_property_dialogue_spans
    HYPOTHESIS_PROFILE=thorough python -m unittest tests.test_property_speaker_identity
    HYPOTHESIS_PROFILE=thorough python -m unittest discover -s . -p 'test_property_*.py'

The name is read once, at import, so every property test in one process uses the
same profile. An unknown name fails loudly (``InvalidArgument``) rather than
silently falling back to an unbounded profile.

The local-constant scan is guarded here too. Hypothesis (6.168.5, and 6.170.17)
computes "local constants" by walking ``sys.modules`` and reading
``module.__file__`` with no exception guard. speechbrain installs lazy redirect
modules whose ``__getattr__`` raises ``ImportError`` for an optional dependency,
so in a process where speechbrain has been imported - e.g. a shard running
``test_ecapa_portable_cache`` before a property test - every ``@given`` draw
eventually raises ``ImportError``. There is no public opt-out
(HypothesisWorks/hypothesis#4627 was closed without one), and the scan is
incremental (once per input, only modules new since the previous input), so a
one-time ``sys.modules`` sweep at import cannot hold: a module imported after
the profile is imported would still break the next draw. The wrapper below runs
on every scan: if the scan raises, it rolls back Hypothesis's ``_seen_modules``
set so readable modules scanned before the failing one are not skipped on retry,
marks only the unreadable module objects in ``_seen_modules``, and retries.
``sys.modules`` is never mutated, so there is no pop-window race with other
threads and an interrupt leaves ``sys.modules`` identical. If Hypothesis removes
or renames ``_get_local_constants``, ``_seen_modules``, or ``_sys_modules_len``,
the import raises here and ``test_hypothesis_profile`` asserts the wrapper is
the function in force, so a version bump fails loudly instead of silently losing
the guard.

This module is not named ``test_*.py`` on purpose: it is shared test support,
not a test module of its own. Note that ``unittest discover`` still imports the
``test_property_*.py`` and ``test_hypothesis_profile.py`` modules in every
shard during discovery, so every shard imports Hypothesis once (~0.07 s).
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, configuration, settings
from hypothesis.internal.conjecture import providers as _providers

# set_hypothesis_home_dir has a public name but is not exported from
# ``hypothesis`` and is not in the documented API; Hypothesis's own tests use
# it. It is the only way to redirect storage after something resolved it, so
# keep the redirect and fail loudly if a version bump removes or renames it.
try:
    _set_hypothesis_home_dir = configuration.set_hypothesis_home_dir
except AttributeError as error:  # pragma: no cover - version guard
    raise RuntimeError(
        "tests.hypothesis_profile redirects Hypothesis storage with "
        "hypothesis.configuration.set_hypothesis_home_dir, a private/"
        "undocumented function that this Hypothesis version does not provide; "
        "review the storage redirect for this version"
    ) from error
if not callable(_set_hypothesis_home_dir):  # pragma: no cover - version guard
    raise RuntimeError(
        "hypothesis.configuration.set_hypothesis_home_dir is no longer "
        "callable; review the storage redirect for this Hypothesis version"
    )

# Respect an explicit user preset without creating an unused temp directory.
# Otherwise create a per-process temp directory and remove it on normal exit.
# The redirect also works if storage was already resolved before this module
# imported; setting the env var as well keeps child Python processes out of the
# checkout.
_preset_storage = os.environ.get("HYPOTHESIS_STORAGE_DIRECTORY")
if _preset_storage:
    STORAGE_DIRECTORY = Path(_preset_storage)
else:
    STORAGE_DIRECTORY = Path(tempfile.mkdtemp(prefix="aa2-hypothesis-"))
    os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] = str(STORAGE_DIRECTORY)
    atexit.register(shutil.rmtree, str(STORAGE_DIRECTORY), True)
_set_hypothesis_home_dir(STORAGE_DIRECTORY)

DEFAULT_MAX_EXAMPLES = 30
THOROUGH_MAX_EXAMPLES = 500

_BOUNDED = dict(
    derandomize=True,
    database=None,
    deadline=None,
    print_blob=False,
    # Generating text and re-scanning it is microseconds; a slow CI runner is
    # not evidence of a bug in the code under test.
    suppress_health_check=[HealthCheck.too_slow],
)

CI_SETTINGS = settings(max_examples=DEFAULT_MAX_EXAMPLES, **_BOUNDED)
THOROUGH_SETTINGS = settings(max_examples=THOROUGH_MAX_EXAMPLES, **_BOUNDED)

settings.register_profile("ci", CI_SETTINGS)
settings.register_profile("thorough", THOROUGH_SETTINGS)
PROFILE_NAME = os.environ.get("HYPOTHESIS_PROFILE", "ci")
settings.load_profile(PROFILE_NAME)
ACTIVE_SETTINGS = settings.get_profile(PROFILE_NAME)

# Process-control BaseExceptions must never be swallowed by the guard.
_UNRECOVERABLE_BASE_EXCEPTIONS = (KeyboardInterrupt, SystemExit, GeneratorExit)


def _unreadable_modules(skip_ids=()):
    """-> [(name, module), ...] in ``sys.modules`` whose ``__file__`` raises.

    speechbrain's ``LazyModule``/``DeprecatedModuleRedirect`` entries raise
    ``ImportError`` (not ``AttributeError``) when the optional target is
    missing, so ``getattr(module, "__file__", None)`` propagates it. Custom
    ``BaseException`` subclasses from ``__getattr__`` are treated the same way,
    while ``KeyboardInterrupt``, ``SystemExit``, and ``GeneratorExit`` always
    propagate.
    """
    bad = []
    for name, module in list(sys.modules.items()):
        if id(module) in skip_ids:
            continue
        try:
            getattr(module, "__file__", None)
        except _UNRECOVERABLE_BASE_EXCEPTIONS:
            raise
        except BaseException:
            bad.append((name, module))
    return bad


def unreadable_module_names():
    """-> names in ``sys.modules`` whose ``__file__`` access raises."""
    return [name for name, _module in _unreadable_modules()]


try:
    _original_get_local_constants = _providers._get_local_constants
    _providers._seen_modules
    _providers._sys_modules_len
except AttributeError as error:  # pragma: no cover - version guard
    raise RuntimeError(
        "tests.hypothesis_profile guards Hypothesis's local-constant scan; "
        "hypothesis.internal.conjecture.providers internals "
        "(_get_local_constants, _seen_modules, _sys_modules_len) changed, "
        "so review that guard for this Hypothesis version"
    ) from error


def safe_get_local_constants():
    """Call Hypothesis's incremental local-constant scan without unreadable modules.

    The scan runs first unchanged, so the common case pays nothing (and when
    ``len(sys.modules)`` is unchanged, no snapshot is taken). If it raises a
    non-interrupt exception, we roll back ``_providers._seen_modules`` to its
    pre-scan membership so readable modules scanned before the failing one are
    rescanned and keep their local constants, mark only the unreadable module
    objects in ``_providers._seen_modules``, and retry. ``sys.modules`` is
    never modified, eliminating any pop-window race with other threads and
    guaranteeing ``sys.modules`` stays identical on any exception including
    ``KeyboardInterrupt``.
    """
    if len(sys.modules) == _providers._sys_modules_len:
        return _original_get_local_constants()

    seen = _providers._seen_modules
    seen_before = set(seen)
    try:
        return _original_get_local_constants()
    except _UNRECOVERABLE_BASE_EXCEPTIONS:
        seen.difference_update(seen - seen_before)
        raise
    except BaseException:
        seen.difference_update(seen - seen_before)
        bad_ids = [id(module) for _name, module in _unreadable_modules(seen_before)]
        if not bad_ids:
            raise
        seen.update(bad_ids)
        try:
            return _original_get_local_constants()
        except BaseException:
            seen.difference_update(seen - seen_before)
            raise


_providers._get_local_constants = safe_get_local_constants


def given_test_settings(module):
    """-> {"name": settings} for every ``@given`` property defined in ``module``.

    Walks ``TestCase`` methods, module-level callables, and module-level
    mappings/sequences of ``@given`` callables (such as per-convention tables).
    A test module uses this to prove its own property tests run under the
    shared profile: ``@given`` captures ``settings.default`` at decoration time,
    so a stray ``@settings`` would show up here as a different object.
    """
    import unittest

    found = {}
    candidates = []
    for name in dir(module):
        attribute = getattr(module, name)
        if isinstance(attribute, type) and issubclass(attribute, unittest.TestCase):
            for method_name in dir(attribute):
                candidates.append(
                    (f"{name}.{method_name}", getattr(attribute, method_name)))
        elif isinstance(attribute, dict):
            for key, value in attribute.items():
                candidates.append((f"{name}[{key!r}]", value))
        elif isinstance(attribute, (list, tuple)):
            for index, value in enumerate(attribute):
                candidates.append((f"{name}[{index}]", value))
        else:
            candidates.append((name, attribute))
    for name, function in candidates:
        configured = getattr(
            function, "_hypothesis_internal_use_settings", None)
        if configured is not None:
            found[name] = configured
    return found
