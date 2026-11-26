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
  and ``unicode_data/`` there - so ``HYPOTHESIS_STORAGE_DIRECTORY`` is pointed
  at a per-process temp directory below before Hypothesis is imported.
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
so in a process where speechbrain has been imported - CI shard 2 runs
``test_ecapa_portable_cache`` before this module's tests - every ``@given``
draw eventually raises ``ImportError``. There is no public opt-out
(HypothesisWorks/hypothesis#4627 was closed without one), and the scan is
incremental (once per input, only modules new since the previous input), so a
one-time ``sys.modules`` sweep at import cannot hold: a module imported after
the profile is imported would still break the next draw. The wrapper below runs
on every scan and hides only the entries whose ``__file__`` access raises,
restoring them immediately. If Hypothesis removes or renames
``_get_local_constants``, the import raises here and ``test_hypothesis_profile``
asserts the wrapper is the function in force, so a version bump fails loudly
instead of silently losing the guard.

This module is not named ``test_*.py`` on purpose: it is imported only by the
property-test modules, so adding Hypothesis never makes it a dependency of the
rest of the suite.
"""
import os
import sys
import tempfile
from pathlib import Path

# Documented Hypothesis setting. Read at the first storage access, which happens
# after this import (no database is configured), so the checkout stays clean.
STORAGE_DIRECTORY = Path(tempfile.mkdtemp(prefix="aa2-hypothesis-"))
os.environ.setdefault("HYPOTHESIS_STORAGE_DIRECTORY", str(STORAGE_DIRECTORY))

from hypothesis import HealthCheck, settings  # noqa: E402
from hypothesis.internal.conjecture import providers as _providers  # noqa: E402

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


def unreadable_module_names():
    """-> names in ``sys.modules`` whose ``__file__`` access raises.

    speechbrain's ``LazyModule``/``DeprecatedModuleRedirect`` entries raise
    ``ImportError`` (not ``AttributeError``) when the optional target is
    missing, so ``getattr(module, "__file__", None)`` propagates it.
    """
    names = []
    for name, module in list(sys.modules.items()):
        try:
            getattr(module, "__file__", None)
        except Exception:
            names.append(name)
    return names


try:
    _original_get_local_constants = _providers._get_local_constants
except AttributeError as error:  # pragma: no cover - version guard
    raise RuntimeError(
        "tests.hypothesis_profile guards Hypothesis's local-constant scan; "
        "hypothesis.internal.conjecture.providers._get_local_constants is "
        "missing, so review that guard for this Hypothesis version"
    ) from error


def safe_get_local_constants():
    """Call Hypothesis's incremental local-constant scan without unreadable modules.

    The scan runs first unchanged, so the common case pays nothing. If it
    raises, the modules whose ``__file__`` access raises are hidden, the scan is
    retried, and they are restored before the caller resumes; a later test sees
    ``sys.modules`` unchanged. Hiding them is correct for this suite: every
    local constant is a source literal Hypothesis may draw instead of a random
    value, and none of the properties depend on that hint.
    """
    try:
        return _original_get_local_constants()
    except Exception:
        hidden = []
        for name in unreadable_module_names():
            module = sys.modules.pop(name, None)
            if module is not None:
                hidden.append((name, module))
        if not hidden:
            raise
        try:
            return _original_get_local_constants()
        finally:
            for name, module in hidden:
                sys.modules[name] = module


_providers._get_local_constants = safe_get_local_constants


def given_test_settings(module):
    """-> {"Class.method": settings} for every ``@given`` test defined in ``module``.

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
        else:
            candidates.append((name, attribute))
    for name, function in candidates:
        configured = getattr(
            function, "_hypothesis_internal_use_settings", None)
        if configured is not None:
            found[name] = configured
    return found
