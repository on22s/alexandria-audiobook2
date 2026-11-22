"""Regression coverage for the bounded timing-preference read contract (#1874).

The production reader and the Editor's raw-metadata reader must both refuse
a preference file that changed while it was being read, including a same-size
atomic replacement that leaves the descriptor's old bytes in the caller's
hands. Fixtures are disposable and use synthetic preference scalars only.
"""
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import readalong_export as rexp

A_EN = {"timing_mode": "qwen", "timing_device": "cpu", "timing_language": "en"}
B_JA = {"timing_mode": "qwen", "timing_device": "cpu", "timing_language": "ja"}
DEFAULT = {"timing_mode": "auto", "timing_device": "auto", "timing_language": None}


def _bytes(preference):
    return (json.dumps(preference, separators=(",", ":")) + "\n").encode("utf-8")


def _same_size_pair():
    first, second = _bytes(A_EN), _bytes(B_JA)
    size = max(len(first), len(second))
    return (first + b" " * (size - len(first)),
            second + b" " * (size - len(second)))


def _fixture(tmp, first, second=None):
    target = Path(tmp) / "readalong_timing.json"
    target.write_bytes(first)
    replacement = None
    if second is not None:
        replacement = Path(tmp) / "replacement.json"
        replacement.write_bytes(second)
    return target, replacement


def _swap_on_first_read(target, replacement):
    real_read, real_replace = os.read, os.replace
    state = {"swapped": False}

    def read(descriptor, count):
        block = real_read(descriptor, count)
        if block and not state["swapped"]:
            real_replace(str(replacement), str(target))
            state["swapped"] = True
        return block

    return read, state


class TimingPreferenceReadTests(unittest.TestCase):
    def test_missing_first_install_keeps_stable_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(DEFAULT, rexp.get_readaloud_timing_preference(tmp))
            self.assertIsNone(rexp.read_bounded_metadata_bytes(
                os.path.join(tmp, "readalong_timing.json")))

    @unittest.skipUnless(os.name == "posix", "atomic rename over an open descriptor is POSIX-only")
    def test_same_size_atomic_rename_during_read_is_refused(self):
        first, second = _same_size_pair()
        self.assertEqual(len(first), len(second))
        with tempfile.TemporaryDirectory() as tmp:
            target, replacement = _fixture(tmp, first, second)
            read, state = _swap_on_first_read(target, replacement)
            with patch.object(os, "read", side_effect=read):
                with self.assertRaises(ValueError):
                    rexp.get_readaloud_timing_preference(tmp)
            self.assertTrue(state["swapped"])
            self.assertEqual("ja",
                             rexp.get_readaloud_timing_preference(tmp)["timing_language"])

    @unittest.skipUnless(os.name == "posix", "atomic rename over an open descriptor is POSIX-only")
    def test_gate_refuses_stale_expectation_after_swap_during_read(self):
        first, second = _same_size_pair()
        with tempfile.TemporaryDirectory() as tmp:
            target, replacement = _fixture(tmp, first, second)
            read, state = _swap_on_first_read(target, replacement)
            with patch.object(os, "read", side_effect=read):
                with self.assertRaises(ValueError):
                    rexp.require_timing_preference_unchanged(A_EN, tmp)
            self.assertTrue(state["swapped"])

    def test_gate_accepts_stable_preference_without_a_swap(self):
        first, _second = _same_size_pair()
        with tempfile.TemporaryDirectory() as tmp:
            _fixture(tmp, first)
            rexp.require_timing_preference_unchanged(A_EN, tmp)

    def _assert_named_path_change_at_eof_is_refused(self, reader, target):
        real_read, real_lstat = os.read, os.lstat
        state = {"eof": False}

        def read(descriptor, count):
            block = real_read(descriptor, count)
            if not block:
                state["eof"] = True
            return block

        def named(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if state["eof"] and os.fspath(path) == os.fspath(target):
                return types.SimpleNamespace(
                    st_mode=info.st_mode, st_dev=info.st_dev,
                    st_ino=info.st_ino + 1, st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=info.st_ctime_ns)
            return info

        with patch.object(os, "read", side_effect=read), \
                patch.object(os, "lstat", side_effect=named):
            with self.assertRaisesRegex(ValueError, "changed during read"):
                reader(str(target))
        self.assertTrue(state["eof"])

    def test_named_path_identity_change_at_eof_is_refused(self):
        first, _second = _same_size_pair()
        with tempfile.TemporaryDirectory() as tmp:
            target, _ = _fixture(tmp, first)
            self._assert_named_path_change_at_eof_is_refused(
                rexp.read_bounded_metadata_bytes, target)

    def test_truncation_during_read_is_refused(self):
        first, _second = _same_size_pair()
        with tempfile.TemporaryDirectory() as tmp:
            target, _ = _fixture(tmp, first)
            real_read = os.read
            state = {"truncated": False}

            def read(descriptor, count):
                block = real_read(descriptor, count)
                if block and not state["truncated"]:
                    with open(target, "r+b") as handle:
                        handle.truncate(1)
                    state["truncated"] = True
                return block

            with patch.object(os, "read", side_effect=read):
                with self.assertRaises(ValueError):
                    rexp.get_readaloud_timing_preference(tmp)
            self.assertTrue(state["truncated"])

    def test_growth_during_read_is_refused(self):
        first, _second = _same_size_pair()
        with tempfile.TemporaryDirectory() as tmp:
            target, _ = _fixture(tmp, first)
            real_read = os.read
            state = {"grown": False}

            def read(descriptor, count):
                block = real_read(descriptor, count)
                if block and not state["grown"]:
                    with open(target, "ab") as handle:
                        handle.write(b" " * 32)
                    state["grown"] = True
                return block

            with patch.object(os, "read", side_effect=read):
                with self.assertRaises(ValueError):
                    rexp.get_readaloud_timing_preference(tmp)
            self.assertTrue(state["grown"])

    def test_corrupt_malformed_and_nonregular_states_are_typed(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readalong_timing.json").write_bytes(b"{not json")
            with self.assertRaises(ValueError):
                rexp.get_readaloud_timing_preference(tmp)

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readalong_timing.json").mkdir()
            with self.assertRaises(ValueError):
                rexp.get_readaloud_timing_preference(tmp)

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "readalong_timing.json").write_bytes(b"x" * 16385)
            with self.assertRaises(ValueError):
                rexp.get_readaloud_timing_preference(tmp)

    def test_editor_reader_enforces_the_same_contract(self):
        from routers import editor
        first, _second = _same_size_pair()
        with tempfile.TemporaryDirectory() as tmp:
            target, _ = _fixture(tmp, first)
            self.assertEqual(first, editor._read_bounded_preference_bytes(str(target)))
            self._assert_named_path_change_at_eof_is_refused(
                editor._read_bounded_preference_bytes, target)
