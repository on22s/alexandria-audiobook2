"""Tests for the shared Hypothesis profile and its local-constant guard.

The profile module is test support, not a test module, so the pieces of
behaviour that the property tests depend on are pinned here:

* the bounded/derandomized profile is in force and no example database is used;
* Hypothesis storage stays outside the checkout both when
  ``HYPOTHESIS_STORAGE_DIRECTORY`` is preset and when the profile creates an
  owned temp directory (which is removed at exit and not created on a preset),
  even if Hypothesis resolved storage before this profile was imported;
* Hypothesis's incremental "local constants" scan stays safe in the presence of
  a ``sys.modules`` entry whose ``__file__`` access raises (speechbrain's lazy
  modules do exactly that, and CI shard 2 imported speechbrain before the
  property tests). Both import orders, custom ``BaseException`` subclasses,
  ``KeyboardInterrupt``/``SystemExit`` propagation, no ``sys.modules`` pop
  window, and preservation of constants from modules scanned before the
  unreadable one are covered;
* ``given_test_settings`` inspects per-convention ``@given`` properties as well
  as class-level ``@given`` methods so a stray ``@settings`` cannot slip by.

The generated text is synthetic; no book or person data is used.
"""
import itertools
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

from hypothesis import given, settings, strategies as st
from hypothesis.internal.conjecture import providers

from tests import hypothesis_profile


_RUN_NONCE = itertools.count()


def _fresh_name(prefix):
    """-> a module name that is unique within this process.

    The marker modules are popped in cleanup, so a later test run can reuse
    their ``id()``. Hypothesis tracks modules by ``id()``; a unique name keeps
    failures readable and per-run ids genuinely fresh, and the guard tests also
    clear ``_seen_modules`` in setUp.
    """
    return f"{prefix}_{next(_RUN_NONCE)}"


def _fresh_magic_literal():
    """-> a large int literal not yet in Hypothesis's local constants."""
    while True:
        literal = 918273645 + next(_RUN_NONCE) * 1_000_003
        if literal not in providers._local_constants:
            return literal


class RaisingFileModule(types.ModuleType):
    """A module whose attributes - including ``__file__`` - cannot be read."""

    def __init__(self, name, exc_factory=None):
        super().__init__(name)
        self._exc_factory = exc_factory

    def __getattr__(self, name):
        if self._exc_factory is not None:
            raise self._exc_factory(self.__name__, name)
        raise ImportError(f"lazy import of {self.__name__} failed")


class _CustomBaseException(BaseException):
    """Non-interrupt BaseException subclass raised by a misbehaving module."""


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

    def test_storage_redirect_guard_fails_loudly_if_the_api_disappears(self):
        # set_hypothesis_home_dir is public-named but not exported/documented;
        # the profile module must fail at import rather than silently leaving
        # storage in the checkout if a Hypothesis bump removes or renames it.
        app_directory = Path(__file__).resolve().parents[1]
        script = textwrap.dedent("""
            import sys
            from hypothesis import configuration
            del configuration.set_hypothesis_home_dir
            try:
                from tests import hypothesis_profile
            except RuntimeError as error:
                assert "set_hypothesis_home_dir" in str(error), error
                sys.exit(0)
            sys.exit("profile imported without set_hypothesis_home_dir")
        """)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(app_directory), capture_output=True, text=True,
            timeout=120, env=os.environ.copy())
        self.assertEqual(0, completed.returncode,
                         completed.stdout + completed.stderr)

    def test_hypothesis_storage_is_outside_the_checkout(self):
        from hypothesis import configuration
        repo_root = Path(__file__).resolve().parents[2]
        home = configuration.storage_directory(
            "unicode_data", intent_to_write=False).home_directory
        self.assertEqual(hypothesis_profile.STORAGE_DIRECTORY.resolve(),
                         home.resolve())
        resolved = home.resolve()
        self.assertFalse(
            resolved == repo_root or repo_root in resolved.parents,
            f"Hypothesis storage {resolved} is inside {repo_root}")

    def test_preset_storage_directory_is_used_without_creating_temp_dir(self):
        app_directory = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aa2-hyp-preset-test-") as raw:
            base = Path(raw)
            isolated_tmp = base / "tmp"
            preset = base / "preset-storage"
            isolated_tmp.mkdir()
            preset.mkdir()
            env = os.environ.copy()
            env["TMPDIR"] = str(isolated_tmp)
            env["TEMP"] = str(isolated_tmp)
            env["TMP"] = str(isolated_tmp)
            env["HYPOTHESIS_STORAGE_DIRECTORY"] = str(preset)
            script = textwrap.dedent("""
                import sys
                from pathlib import Path
                from unittest import TextTestRunner, defaultTestLoader
                from hypothesis import configuration
                from tests import hypothesis_profile
                from tests.test_hypothesis_profile import ProfileTests

                expected = Path(sys.argv[1]).resolve()
                assert hypothesis_profile.STORAGE_DIRECTORY.resolve() == expected, (
                    hypothesis_profile.STORAGE_DIRECTORY, expected)
                home = configuration.storage_directory(
                    "unicode_data", intent_to_write=False).home_directory.resolve()
                assert home == expected, (home, expected)
                suite = defaultTestLoader.loadTestsFromName(
                    "test_hypothesis_storage_is_outside_the_checkout",
                    ProfileTests)
                result = TextTestRunner(verbosity=0).run(suite)
                sys.exit(0 if result.wasSuccessful() else 1)
            """)
            completed = subprocess.run(
                [sys.executable, "-c", script, str(preset)],
                cwd=str(app_directory), capture_output=True, text=True,
                timeout=120, env=env)
            self.assertEqual(0, completed.returncode,
                             completed.stdout + completed.stderr)
            self.assertTrue(preset.is_dir(),
                            "preset storage directory must not be deleted at exit")
            self.assertEqual(
                [], list(isolated_tmp.glob("aa2-hypothesis-*")),
                "preset storage must not create an unused mkdtemp directory")

    def test_owned_temp_storage_directory_is_removed_at_exit(self):
        app_directory = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aa2-hyp-owned-test-") as raw:
            isolated_tmp = Path(raw)
            env = os.environ.copy()
            env.pop("HYPOTHESIS_STORAGE_DIRECTORY", None)
            env["TMPDIR"] = str(isolated_tmp)
            env["TEMP"] = str(isolated_tmp)
            env["TMP"] = str(isolated_tmp)
            script = textwrap.dedent("""
                import sys
                from unittest import TextTestRunner, defaultTestLoader
                from hypothesis import configuration
                from tests import hypothesis_profile
                from tests.test_hypothesis_profile import ProfileTests

                configuration.storage_directory("unicode_data").create_if_missing()
                print("CREATED:" + str(hypothesis_profile.STORAGE_DIRECTORY))
                suite = defaultTestLoader.loadTestsFromName(
                    "test_hypothesis_storage_is_outside_the_checkout",
                    ProfileTests)
                result = TextTestRunner(verbosity=0).run(suite)
                sys.exit(0 if result.wasSuccessful() else 1)
            """)
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(app_directory), capture_output=True, text=True,
                timeout=120, env=env)
            self.assertEqual(0, completed.returncode,
                             completed.stdout + completed.stderr)
            created_lines = [
                line.split("CREATED:", 1)[1].strip()
                for line in completed.stdout.splitlines()
                if line.startswith("CREATED:")
            ]
            self.assertEqual(1, len(created_lines), completed.stdout)
            self.assertFalse(
                Path(created_lines[0]).exists(),
                f"owned temp storage {created_lines[0]} was not removed at exit")
            self.assertEqual([], list(isolated_tmp.glob("aa2-hypothesis-*")))

    def test_storage_is_redirected_even_if_resolved_before_profile_import(self):
        app_directory = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="aa2-hyp-preresolved-") as raw:
            base = Path(raw)
            fake_cwd = base / "cwd"
            isolated_tmp = base / "tmp"
            fake_cwd.mkdir()
            isolated_tmp.mkdir()
            env = os.environ.copy()
            env.pop("HYPOTHESIS_STORAGE_DIRECTORY", None)
            env["TMPDIR"] = str(isolated_tmp)
            env["TEMP"] = str(isolated_tmp)
            env["TMP"] = str(isolated_tmp)
            script = textwrap.dedent("""
                import sys
                from pathlib import Path
                sys.path.insert(0, sys.argv[1])

                from hypothesis import configuration
                before = configuration.storage_directory(
                    "unicode_data", intent_to_write=False).home_directory.resolve()

                from tests import hypothesis_profile
                after = configuration.storage_directory(
                    "unicode_data", intent_to_write=False).home_directory.resolve()
                expected = hypothesis_profile.STORAGE_DIRECTORY.resolve()
                assert after == expected, (after, expected)
                assert after != before, (after, before)
                cwd = Path.cwd().resolve()
                assert after != cwd and cwd not in after.parents, (after, cwd)
            """)
            completed = subprocess.run(
                [sys.executable, "-c", script, str(app_directory)],
                cwd=str(fake_cwd), capture_output=True, text=True,
                timeout=120, env=env)
            self.assertEqual(0, completed.returncode,
                             completed.stdout + completed.stderr)

    def test_given_test_settings_covers_per_convention_properties_and_catches_stray_settings(self):
        from dialogue_spans import PAIRED
        from tests import test_property_dialogue_spans

        configured = hypothesis_profile.given_test_settings(
            test_property_dialogue_spans)
        # 4 direct @given methods + 9 well-formed + 9 inch-mark per-convention
        # properties must all be surfaced and bound to ACTIVE_SETTINGS.
        self.assertEqual(4 + 2 * len(PAIRED), len(configured), sorted(configured))
        for name, active in configured.items():
            self.assertIs(hypothesis_profile.ACTIVE_SETTINGS, active, name)

        # A stray @settings override on a per-convention @given must surface as
        # a different Settings object (so ProfileTests catches it).
        fake_module = types.ModuleType("aa2_fake_stray_settings_module")

        @settings(max_examples=1)
        @given(st.integers(0, 5))
        def stray_property(value):
            assert value >= 0

        fake_module._CONVENTION_PROPERTIES = {('"', '"'): stray_property}
        detected = hypothesis_profile.given_test_settings(fake_module)
        self.assertEqual(1, len(detected), detected)
        only_settings = next(iter(detected.values()))
        self.assertIsNot(hypothesis_profile.ACTIVE_SETTINGS, only_settings)
        self.assertEqual(1, only_settings.max_examples)


class LocalConstantGuardTests(unittest.TestCase):
    def setUp(self):
        # Hypothesis's scan state is process-global. Start every test from a
        # clean scan and restore the pre-test state afterwards, so this class
        # can be run again in the same interpreter: a previous pass can leave
        # the ids of marker modules (popped in cleanup) in _seen_modules, and a
        # new marker can land on a recycled id and be silently skipped; a
        # successful retry also unions its literal into _local_constants.
        self._saved_seen_modules = set(providers._seen_modules)
        self._saved_local_constants = providers._local_constants
        self._saved_sys_modules_len = providers._sys_modules_len
        providers._seen_modules.clear()
        providers._sys_modules_len = None
        self.addCleanup(self._restore_provider_state)

    def _restore_provider_state(self):
        # Cleanups run last-in first-out, so marker modules are popped from
        # sys.modules before this restores the scan bookkeeping.
        providers._seen_modules.clear()
        providers._seen_modules.update(self._saved_seen_modules)
        providers._local_constants = self._saved_local_constants
        providers._sys_modules_len = self._saved_sys_modules_len

    def _assert_sys_modules_identical(self, snapshot):
        current = dict(sys.modules)
        missing = sorted(set(snapshot) - set(current))
        extra = sorted(set(current) - set(snapshot))
        changed = sorted(
            name for name in set(snapshot) & set(current)
            if current[name] is not snapshot[name]
        )
        self.assertEqual(([], [], []), (missing, extra, changed),
                         f"sys.modules mutated: missing={missing}, extra={extra}, changed={changed}")

    def test_a_raising_module_added_after_the_profile_is_imported(self):
        trap = RaisingFileModule(_fresh_name("aa2_unreadable_after_profile"))
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

    def test_base_exception_from_module_getattr_is_hidden_while_interrupts_propagate(self):
        # Custom BaseException subclasses from a module's __getattr__ are
        # treated as unreadable and hidden from the scan.
        custom = RaisingFileModule(
            _fresh_name("aa2_custom_base_exc_module"),
            exc_factory=lambda mod, attr: _CustomBaseException(f"{mod}.{attr}"))
        self.addCleanup(sys.modules.pop, custom.__name__, None)
        sys.modules[custom.__name__] = custom

        self.assertIsNotNone(providers._get_local_constants())
        self.assertIs(custom, sys.modules.get(custom.__name__))

        @given(st.integers(min_value=0, max_value=100))
        def property_(value):
            self.assertIsInstance(value, int)

        property_()

        # Real process interrupts (KeyboardInterrupt, SystemExit) are never
        # swallowed and leave sys.modules identical.
        for exc_cls in (KeyboardInterrupt, SystemExit):
            name = _fresh_name(f"aa2_interrupt_{exc_cls.__name__}_module")
            interrupt_mod = RaisingFileModule(
                name, exc_factory=lambda mod, attr, cls=exc_cls: cls(mod))
            self.addCleanup(sys.modules.pop, name, None)
            sys.modules[name] = interrupt_mod
            snapshot = dict(sys.modules)
            with self.assertRaises(exc_cls):
                providers._get_local_constants()
            self._assert_sys_modules_identical(snapshot)
            sys.modules.pop(name, None)

    def test_keyboard_interrupt_mid_scan_leaves_sys_modules_identical_with_no_pop_window(self):
        bad_1 = RaisingFileModule(_fresh_name("aa2_canary_unreadable_1"))
        bad_2 = RaisingFileModule(_fresh_name("aa2_canary_unreadable_2"))
        self.addCleanup(sys.modules.pop, bad_1.__name__, None)
        self.addCleanup(sys.modules.pop, bad_2.__name__, None)
        sys.modules[bad_1.__name__] = bad_1
        sys.modules[bad_2.__name__] = bad_2
        snapshot = dict(sys.modules)

        # Trace every line during the scan: if any code pops bad_1 or bad_2 out
        # of sys.modules (creating a pop window / unguarded pop loop), raise
        # KeyboardInterrupt immediately to expose missing-restore bugs.
        saw_pop = False

        def trace(frame, event, arg):
            nonlocal saw_pop
            if bad_1.__name__ not in sys.modules or bad_2.__name__ not in sys.modules:
                saw_pop = True
                raise KeyboardInterrupt("canary: sys.modules entry popped")
            return trace

        sys.settrace(trace)
        try:
            providers._get_local_constants()
        except KeyboardInterrupt:
            pass
        finally:
            sys.settrace(None)

        self._assert_sys_modules_identical(snapshot)
        self.assertFalse(saw_pop, "guard must not pop entries out of sys.modules")

        # Also verify an actual KeyboardInterrupt mid-scan leaves both
        # sys.modules identical and any earlier unscanned module unstranded in
        # Hypothesis's _seen_modules set.
        earlier = types.ModuleType(_fresh_name("aa2_canary_before_interrupt"))
        earlier.__file__ = None
        interrupter = RaisingFileModule(
            _fresh_name("aa2_canary_mid_scan_interrupt"),
            exc_factory=lambda mod, attr: KeyboardInterrupt(mod))
        self.addCleanup(sys.modules.pop, earlier.__name__, None)
        self.addCleanup(sys.modules.pop, interrupter.__name__, None)
        sys.modules[earlier.__name__] = earlier
        sys.modules[interrupter.__name__] = interrupter
        snapshot_before_ki = dict(sys.modules)

        with self.assertRaises(KeyboardInterrupt):
            providers._get_local_constants()

        self._assert_sys_modules_identical(snapshot_before_ki)
        self.assertNotIn(id(earlier), providers._seen_modules)

    def test_keyboard_interrupt_during_collector_pass_is_not_hidden(self):
        # The unguarded scan fails on the ImportError module, so the guard
        # enters _unreadable_modules() to collect it; that collector then
        # reaches a module raising KeyboardInterrupt. The interrupt must
        # propagate and neither module may be marked as seen. Without
        # _UNRECOVERABLE_BASE_EXCEPTIONS in _unreadable_modules the collector
        # swallows the interrupt, marks both, and the retry succeeds.
        importing = RaisingFileModule(_fresh_name("aa2_collector_importerror"))
        interrupting = RaisingFileModule(
            _fresh_name("aa2_collector_interrupt"),
            exc_factory=lambda mod, attr: KeyboardInterrupt(mod))
        self.addCleanup(sys.modules.pop, importing.__name__, None)
        self.addCleanup(sys.modules.pop, interrupting.__name__, None)
        sys.modules[importing.__name__] = importing
        sys.modules[interrupting.__name__] = interrupting
        snapshot = dict(sys.modules)

        with self.assertRaises(KeyboardInterrupt):
            providers._get_local_constants()

        self._assert_sys_modules_identical(snapshot)
        self.assertNotIn(id(importing), providers._seen_modules)
        self.assertNotIn(id(interrupting), providers._seen_modules)

    def test_retry_preserves_local_constants_from_modules_before_unreadable_one(self):
        # Avoid "test" / "tests" in path parts and "test_" filename prefix,
        # which Hypothesis's is_local_module_file intentionally skips. The
        # literal is new on every run so a previous in-process pass cannot
        # satisfy the assertion for this one.
        magic_literal = _fresh_magic_literal()
        with tempfile.TemporaryDirectory(prefix="aa2_local_const_") as raw:
            mod_path = Path(raw) / f"sample_literals_{next(_RUN_NONCE)}.py"
            mod_path.write_text(f"MAGIC = {magic_literal}\n", encoding="utf-8")

            local_mod = types.ModuleType(_fresh_name("aa2_local_before_unreadable"))
            local_mod.__file__ = str(mod_path)
            local_mod.MAGIC = magic_literal
            unreadable = RaisingFileModule(
                _fresh_name("aa2_unreadable_after_local"))

            self.addCleanup(sys.modules.pop, local_mod.__name__, None)
            self.addCleanup(sys.modules.pop, unreadable.__name__, None)

            # Insert the readable local module BEFORE the unreadable one so the
            # first scan processes it, adds it to _seen_modules, and then fails
            # on the unreadable module before unioning new_constants.
            self.assertNotIn(magic_literal, providers._local_constants)
            sys.modules[local_mod.__name__] = local_mod
            sys.modules[unreadable.__name__] = unreadable
            providers._sys_modules_len = None

            constants = providers._get_local_constants()
            self.assertIn(magic_literal, constants)


if __name__ == "__main__":
    unittest.main()
