"""The one bounded, deterministic Hypothesis profile for the unit suite.

The suite runs as six CI shards under ``unittest discover``, so a property test
must cost a few seconds at most and must never flake. These settings buy that:

* ``derandomize=True`` makes every run draw the same sequence, so a red CI run
  is reproducible locally from the printed seed instead of "it passed here".
* ``database=None`` keeps ``.hypothesis/`` out of the checkout and out of CI:
  one shard must not influence another, and a clean run must reproduce.
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

This module is not named ``test_*.py`` on purpose: it is imported only by the
property-test modules, so adding Hypothesis never makes it a dependency of the
rest of the suite.
"""
import os

from hypothesis import HealthCheck, settings

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
