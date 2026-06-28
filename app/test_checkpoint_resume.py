#!/usr/bin/env python3
"""Standalone tests for resume-from-checkpoint helpers.

Run: python test_checkpoint_resume.py
(Also collectable by pytest if installed.)
"""
import os
import sys
import tempfile

import generate_script as gs


def test_script_checkpoint_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        entries = [{"speaker": "A", "text": "hi"}]
        gs.save_script_checkpoint(out, completed_chunks=2, total_chunks=5,
                                  chunk_size=3000, input_hash="abc", all_entries=entries)
        loaded = gs.load_script_checkpoint(out, total_chunks=5, chunk_size=3000, input_hash="abc")
        assert loaded is not None
        assert loaded["completed_chunks"] == 2
        assert loaded["all_entries"] == entries


def test_script_checkpoint_mismatch_returns_none():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 2, 5, 3000, "abc", [])
        # Different input_hash -> the split would differ -> must refuse to resume
        assert gs.load_script_checkpoint(out, 5, 3000, "DIFFERENT") is None
        # Different chunk_size -> must refuse
        assert gs.load_script_checkpoint(out, 5, 1000, "abc") is None
        # Different total_chunks -> must refuse
        assert gs.load_script_checkpoint(out, 99, 3000, "abc") is None


def test_clear_script_checkpoint():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 1, 2, 3000, "abc", [])
        assert os.path.exists(gs._script_checkpoint_path(out))
        gs.clear_script_checkpoint(out)
        assert not os.path.exists(gs._script_checkpoint_path(out))


def test_input_hash_is_stable_and_sensitive():
    assert gs.compute_input_hash("hello") == gs.compute_input_hash("hello")
    assert gs.compute_input_hash("hello") != gs.compute_input_hash("hello!")


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
