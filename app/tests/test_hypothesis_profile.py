"""Tests for the shared Hypothesis profile and its local-constant guard.

The profile module is test support, not a test module, so its two pieces of
behaviour that the property tests depend on are pinned here:

* the bounded/derandomized profile is in force and no example database is used;
* Hypothesis's incremental "local constants" scan stays safe in the presence of
  a ``sys.modules`` entry whose ``__file__`` access raises (speechbrain's lazy
  modules do exactly that, and CI shard 2 imports speechbrain before the
  property tests). Both import orders are covered: a module added after the
  profile is imported, and a module already present before it.

The generated text is synthetic; no book or person data is used.
"""
import os
import subprocess
import sys
import textwrap
import types
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st
from hypothesis.internal.conjecture import providers

from tests import hypothesis_profile


class RaisingFileModule(types.ModuleType):
    """A module whose attributes - including ``__file__`` - cannot be read."""

    def __getattr__(self, name):
        raise ImportError(f"lazy import of {self.__name__} failed")


class ProfileTests(unittest.TestCase):
    def test_the_ci_profile_is_active(self):
        self.assertIs(hypothesis_profile.ACTIVE_SETTINGS, settings.default)
        self.assertTrue(settings.default.derandomize)
        self.assertIsNone(settings.default.database)
        self.assertIsNone(settings.default.deadline)

    def test_the_local_constant_guard_is_installed(self):
        # If a Hypothesis version removes or renames the private collector, the
        # profile module raises at import; if someone undoes the assignment,
        # this fails instead of the suite silently losing the guard.
        self.assertIs(hypothesis_profile.safe_get_local_constants,
                      providers._get_local_constants)

    def test_hypothesis_storage_is_outside_the_checkout(self):
        from hypothesis import configuration
        app_directory = Path(__file__).resolve().parents[1]
        home = configuration.storage_directory("unicode_data").home_directory
        self.assertEqual(hypothesis_profile.STORAGE_DIRECTORY, home)
        self.assertFalse(home == app_directory or app_directory in home.parents,
                         f"Hypothesis storage {home} is inside {app_directory}")


class LocalConstantGuardTests(unittest.TestCase):
    def test_a_raising_module_added_after_the_profile_is_imported(self):
        trap = RaisingFileModule("aa2_unreadable_after_profile")
        self.addCleanup(sys.modules.pop, trap.__name__, None)
        sys.modules[trap.__name__] = trap

        # Deterministic: adding an entry changes len(sys.modules), which is the
        # cheap check that makes Hypothesis rescan new modules.
        self.assertIsNotNone(providers._get_local_constants())
        self.assertIs(trap, sys.modules.get(trap.__name__))

        @given(st.integers(min_value=0, max_value=10_000))
        def property_(value):
            self.assertIsInstance(value, int)

        property_()

    def test_a_raising_module_present_before_the_profile_is_imported(self):
        script = textwrap.dedent("""
            import sys
            import types

            from unittest import TestCase, TextTestRunner, defaultTestLoader

            class RaisingFileModule(types.ModuleType):
                def __getattr__(self, name):
                    raise ImportError(f"lazy import of {self.__name__} failed")

            sys.modules["aa2_unreadable_before_profile"] = RaisingFileModule(
                "aa2_unreadable_before_profile")

            from hypothesis import given, strategies as st
            from hypothesis.internal.conjecture import providers

            from tests import hypothesis_profile

            providers._get_local_constants()

            class PropertyTest(TestCase):
                @given(st.integers(min_value=0, max_value=10000))
                def test_any_integer_is_a_falsifiable_draw(self, value):
                    self.assertIsInstance(value, int)

            result = TextTestRunner(verbosity=0).run(
                defaultTestLoader.loadTestsFromTestCase(PropertyTest))
            sys.exit(0 if result.wasSuccessful() else 1)
        """)
        app_directory = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(app_directory), capture_output=True, text=True, timeout=120,
            env=os.environ.copy())
        self.assertEqual(0, completed.returncode,
                         completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
