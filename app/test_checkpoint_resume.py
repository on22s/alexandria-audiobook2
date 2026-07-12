#!/usr/bin/env python3
"""Standalone tests for resume-from-checkpoint helpers.

Run: python test_checkpoint_resume.py
(Also collectable by pytest if installed.)
"""
import json as _json
import os
import sys
import tempfile

import generate_script as gs


def test_script_checkpoint_roundtrip():
    """Per-chunk appends rebuild the full entry list on load (JSONL sidecar)."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        per_chunk = [[{"speaker": "A", "text": "hi"}],
                     [{"speaker": "B", "text": "yo"}]]
        for idx, chunk_entries in enumerate(per_chunk, 1):
            gs.save_script_checkpoint(out, completed_chunks=idx, total_chunks=5,
                                      chunk_size=3000, input_hash="abc",
                                      new_entries=chunk_entries)
        loaded = gs.load_script_checkpoint(out, total_chunks=5, chunk_size=3000, input_hash="abc")
        assert loaded is not None
        assert loaded["completed_chunks"] == 2
        assert loaded["all_entries"] == [{"speaker": "A", "text": "hi"},
                                         {"speaker": "B", "text": "yo"}]


def test_script_checkpoint_mismatch_returns_none():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 1, 5, 3000, "abc", [{"x": 1}])
        # Different input_hash -> the split would differ -> must refuse to resume
        assert gs.load_script_checkpoint(out, 5, 3000, "DIFFERENT") is None
        # Different chunk_size -> must refuse
        assert gs.load_script_checkpoint(out, 5, 1000, "abc") is None
        # Different total_chunks -> must refuse
        assert gs.load_script_checkpoint(out, 99, 3000, "abc") is None


def test_script_checkpoint_matches_validates_completed_chunks():
    """The acceptance predicate rejects a non-int / out-of-range completed_chunks
    (a corrupt/tampered checkpoint) instead of letting it crash or skip-all."""
    base = {"total_chunks": 5, "chunk_size": 3000, "input_hash": "h"}
    assert gs.script_checkpoint_matches({**base, "completed_chunks": 3}, 5, 3000, "h") is True
    assert gs.script_checkpoint_matches({**base, "completed_chunks": 0}, 5, 3000, "h") is True
    assert gs.script_checkpoint_matches({**base, "completed_chunks": "2"}, 5, 3000, "h") is False
    assert gs.script_checkpoint_matches({**base, "completed_chunks": 6}, 5, 3000, "h") is False
    assert gs.script_checkpoint_matches({**base, "completed_chunks": -1}, 5, 3000, "h") is False
    assert gs.script_checkpoint_matches({**base, "completed_chunks": True}, 5, 3000, "h") is False
    assert gs.script_checkpoint_matches({**base, "completed_chunks": 3}, 5, 1000, "h") is False
    assert gs.script_checkpoint_matches("not-a-dict", 5, 3000, "h") is False


def test_load_rejects_corrupt_completed_chunks():
    """A tampered meta (completed_chunks as a string) must fall back to fresh,
    not raise a TypeError in the resume loop."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 2, 5, 3000, "abc", [{"a": 1}])
        meta_path = gs._script_checkpoint_path(out)
        with open(meta_path) as f:
            meta = _json.load(f)
        meta["completed_chunks"] = "2"  # corrupt type
        with open(meta_path, "w") as f:
            _json.dump(meta, f)
        assert gs.load_script_checkpoint(out, 5, 3000, "abc") is None


def test_load_rejects_short_entries_sidecar():
    """Meta claims more completed chunks than the JSONL has -> inconsistent -> fresh."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 1, 5, 3000, "abc", [{"a": 1}])
        # Bump the meta count without appending a matching entries line.
        meta_path = gs._script_checkpoint_path(out)
        with open(meta_path) as f:
            meta = _json.load(f)
        meta["completed_chunks"] = 3
        with open(meta_path, "w") as f:
            _json.dump(meta, f)
        assert gs.load_script_checkpoint(out, 5, 3000, "abc") is None


def test_truncate_drops_stale_trailing_line():
    """A crash between append and meta-write leaves an extra JSONL line; load uses
    only `completed_chunks` lines and truncate realigns the sidecar."""
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        for idx, ce in enumerate([[{"i": 0}], [{"i": 1}], [{"i": 2}]], 1):
            gs.save_script_checkpoint(out, idx, 5, 3000, "abc", ce)
        # Simulate a crashed extra append: append a 4th line but leave meta at 3.
        with open(gs._script_checkpoint_entries_path(out), "a", encoding="utf-8") as f:
            f.write(_json.dumps([{"i": "STALE"}]) + "\n")
        loaded = gs.load_script_checkpoint(out, 5, 3000, "abc")
        assert loaded["completed_chunks"] == 3
        assert loaded["all_entries"] == [{"i": 0}, {"i": 1}, {"i": 2}]
        gs.truncate_checkpoint_entries(out, 3)
        # After truncation the stale line is gone.
        assert gs._read_checkpoint_entries(out) == [[{"i": 0}], [{"i": 1}], [{"i": 2}]]


def test_clear_script_checkpoint_removes_both_sidecars():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 1, 2, 3000, "abc", [{"a": 1}])
        assert os.path.exists(gs._script_checkpoint_path(out))
        assert os.path.exists(gs._script_checkpoint_entries_path(out))
        gs.clear_script_checkpoint(out)
        assert not os.path.exists(gs._script_checkpoint_path(out))
        assert not os.path.exists(gs._script_checkpoint_entries_path(out))


def test_input_hash_is_stable_and_sensitive():
    assert gs.compute_input_hash("hello") == gs.compute_input_hash("hello")
    assert gs.compute_input_hash("hello") != gs.compute_input_hash("hello!")


def test_compute_split_signature_matches_manual():
    """The detect endpoint's signature helper must agree with a manual hash+split."""
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "book.txt")
        text = "First paragraph.\n\n" + ("word " * 2000) + "\n\nLast paragraph."
        with open(src, "w", encoding="utf-8") as f:
            f.write(text)
        total, ih = gs.compute_split_signature(src, chunk_size=1000)
        content = gs.load_book_content(src)
        assert ih == gs.compute_input_hash(content)
        assert total == len(gs.split_into_chunks(content, max_size=1000))
        assert total >= 1


def test_resume_offset_skips_completed_chunks():
    """Exercise the REAL resume-skip decision (generate_script.iter_resumable_chunks),
    not a hand-rolled copy of it, so an off-by-one regression in the actual loop
    is caught. Chunks 1-2 are done → only 3,4,5 (1-based) should be yielded."""
    from generate_script import iter_resumable_chunks
    chunks = ["c1", "c2", "c3", "c4", "c5"]
    yielded = list(iter_resumable_chunks(chunks, completed_chunks=2))
    assert yielded == [(3, "c3"), (4, "c4"), (5, "c5")], yielded
    # Boundary cases: nothing done → all yielded; all done → none.
    assert [c for _, c in iter_resumable_chunks(chunks, 0)] == chunks
    assert list(iter_resumable_chunks(chunks, len(chunks))) == []


def test_batch_state_roundtrip():
    """Exercise the REAL _save_batch_script_state, not a hand-rolled writer, so a
    regression in its persisted shape is actually caught. It stores only a
    per-file list (no separate cursor field)."""
    import app
    orig = app.BATCH_SCRIPT_STATE_PATH
    with tempfile.TemporaryDirectory() as d:
        app.BATCH_SCRIPT_STATE_PATH = os.path.join(d, ".batch_script_state.json")
        try:
            state = {"tasks": [{"filename": "a.txt", "status": "done", "saved_as": "a"},
                               {"filename": "b.txt", "status": "pending"}]}
            app._save_batch_script_state(state)
            with open(app.BATCH_SCRIPT_STATE_PATH) as f:
                loaded = _json.load(f)
        finally:
            app.BATCH_SCRIPT_STATE_PATH = orig
    assert set(loaded) == {"files"}          # no current_idx / cursor field
    assert "current_idx" not in loaded
    done = [f for f in loaded["files"] if f["status"] == "done"]
    assert [f["filename"] for f in done] == ["a.txt"]


def test_secure_filename_blocks_traversal():
    """The batch-script saved_as guard relies on secure_filename to neutralize a
    tampered '../' value: path separators are stripped so the result stays a flat
    filename inside SCRIPTS_DIR (a leftover '..' substring is harmless without a
    separator), and it can't reach outside the directory."""
    from utils import secure_filename
    for bad in ("../../etc/passwd", "..\\..\\win", "a/b/c"):
        s = secure_filename(bad)
        assert "/" not in s and "\\" not in s
        # os.path.join must not escape the directory with the sanitized name.
        joined = os.path.normpath(os.path.join("/scripts", f"{s}.json"))
        assert joined.startswith("/scripts" + os.sep)


def test_probe_host_gate_blocks_arbitrary_hosts():
    """The context probe must still refuse a host the user never configured
    (SSRF guard), while allowing local + Thunder."""
    import lmstudio_settings as ls
    assert ls._is_verifiable_probe_host("http://localhost:1234/v1") is True
    assert ls._is_verifiable_probe_host("https://x-1234.thundercompute.net/v1") is True
    assert ls._is_verifiable_probe_host("http://definitely-not-configured.example.com/v1") is False
    ok, detail = ls._verify_served_context("http://definitely-not-configured.example.com/v1", "m")
    assert ok is False and "refusing" in detail


def test_probe_gate_allows_configured_remote():
    """A non-Thunder host the user configured in config.json is probe-allowed."""
    import lmstudio_settings as ls
    orig = ls._configured_remote_hosts
    ls._configured_remote_hosts = lambda: {"my-remote.example.com"}
    try:
        assert ls._is_verifiable_probe_host("http://my-remote.example.com:1234/v1") is True
        assert ls._is_verifiable_probe_host("http://other.example.com/v1") is False
    finally:
        ls._configured_remote_hosts = orig


def test_detect_descriptor_uniform_shape():
    """All four resume-detect endpoints return exactly this shape (no dead
    `mode` payload — the frontend reads only exists/done/total/label)."""
    import app
    empty = app._detect_descriptor(False)
    assert set(empty) == {"exists", "done", "total", "label"}
    assert empty["exists"] is False
    full = app._detect_descriptor(True, 2, 5, "2/5")
    assert full["exists"] is True and full["done"] == 2 and full["total"] == 5


def test_as_number_coerces_garbage():
    """Restored stats are coerced so a corrupt state file can't crash/skew totals."""
    import app
    assert app._as_number(5) == 5
    assert app._as_number(2.5) == 2.5
    assert app._as_number("x") == 0
    assert app._as_number(None) == 0
    assert app._as_number(True) == 0
    assert app._as_number([1]) == 0


def test_resolve_batch_script_paths_matches_runner_stem():
    """Checkpoint cleanup must target the same output path the runner creates."""
    import app
    ip, op = app._resolve_batch_script_paths("My Book.txt", 0)
    assert op and os.path.dirname(op) == app.SCRIPTS_DIR
    assert gs._script_checkpoint_path(op).endswith(".script_checkpoint.json")
    assert app._resolve_batch_script_paths("", 0) == (None, None)


def test_restore_review_progress_rebuilds_totals():
    """A resumed batch review must re-count books COMPLETED in a pass (fwd/bwd
    complete flag) whose script still exists, and skip everything else."""
    import app
    keys = list(app._new_review_totals().keys())
    sk = next(k for k in keys if k != "books_done")
    orig_dir = app.SCRIPTS_DIR
    with tempfile.TemporaryDirectory() as d:
        app.SCRIPTS_DIR = d
        try:
            # Only "a" and "b" exist on disk; "ghost" was deleted between runs.
            for name in ("a", "b"):
                with open(os.path.join(d, f"{name}.json"), "w") as f:
                    f.write("[]")
            state = {"totals_fwd": app._new_review_totals(), "totals_bwd": app._new_review_totals(),
                     "aliases_fwd": [], "aliases_bwd": [], "diff_pool": {"text": [], "speaker": []},
                     "tasks": [
                         {"name": "a", "status": "done", "fwd_complete": True,
                          "stats_fwd": {kk: (2 if kk == sk else 0) for kk in keys}},
                         {"name": "b", "status": "done", "fwd_complete": True, "bwd_complete": True,
                          "stats_fwd": {kk: (3 if kk == sk else 0) for kk in keys},
                          "stats_bwd": {kk: (1 if kk == sk else 0) for kk in keys},
                          "aliases_found": [{"variant": "B", "canonical": "Bob"}]},
                         # ghost: completed but its script no longer exists -> skipped.
                         {"name": "ghost", "status": "done", "fwd_complete": True,
                          "stats_fwd": {kk: (99 if kk == sk else 0) for kk in keys}},
                     ]}
            app._restore_review_progress(state, ["a", "b", "ghost"])
        finally:
            app.SCRIPTS_DIR = orig_dir
    assert state["totals_fwd"]["books_done"] == 2          # a + b, NOT ghost
    assert state["totals_fwd"][sk] == 5                    # 2 + 3
    assert state["totals_bwd"]["books_done"] == 1          # b only
    assert state["aliases_bwd"] == [{"variant": "B", "canonical": "Bob", "book": "b"}]


def test_restore_review_progress_skips_incomplete_pass():
    """A book without the completion flag (queued for re-review) is NOT folded in,
    so it can't be double-counted once it finishes."""
    import app
    keys = list(app._new_review_totals().keys())
    sk = next(k for k in keys if k != "books_done")
    orig_dir = app.SCRIPTS_DIR
    with tempfile.TemporaryDirectory() as d:
        app.SCRIPTS_DIR = d
        try:
            with open(os.path.join(d, "a.json"), "w") as f:
                f.write("[]")
            state = {"totals_fwd": app._new_review_totals(), "totals_bwd": app._new_review_totals(),
                     "aliases_fwd": [], "aliases_bwd": [], "diff_pool": {"text": [], "speaker": []},
                     "tasks": [
                         # has stats but NOT fwd_complete (was interrupted/incomplete)
                         {"name": "a", "status": "incomplete",
                          "stats_fwd": {kk: (7 if kk == sk else 0) for kk in keys}},
                     ]}
            app._restore_review_progress(state, ["a"])
        finally:
            app.SCRIPTS_DIR = orig_dir
    assert state["totals_fwd"]["books_done"] == 0
    assert state["totals_fwd"][sk] == 0


def test_review_resume_offset_input_vs_output_space():
    """_load_resume_state picks the right resume offset whether `entries` is the
    previously-written OUTPUT (clean resume) or the ORIGINAL input (hard kill),
    once review changed entry counts so the two diverge.

    Scenario: 5 original input entries, batch_size 2. First two batches consumed
    4 input entries; batch 1 was split 2->3, so all_corrected has 5 entries while
    only 4 input entries were consumed. The un-reviewed original entry is index 4.
    """
    import review_script as rs
    orig_input = [{"t": f"o{i}"} for i in range(5)]
    all_corrected = [{"t": f"c{i}"} for i in range(5)]   # 3 (split) + 2
    batch_lengths = [3, 2]          # OUTPUT lengths
    input_batch_lengths = [2, 2]    # INPUT lengths -> 4 input entries consumed
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")

        def _save(input_lens):
            rs.save_checkpoint(out, completed_batches=2, total_batches=3, batch_size=2,
                               context_window=0, all_corrected=all_corrected,
                               total_stats={}, previous_tail=all_corrected[-2:],
                               batch_lengths=batch_lengths, failed_batches=[],
                               input_batch_lengths=input_lens)

        # Clean resume: entries == previous OUTPUT (all_corrected + remainder).
        _save(input_batch_lengths)
        entries_output = all_corrected + orig_input[4:]
        ro_clean = rs._load_resume_state(out, 3, 2, 0, [], {}, entries_output)[7]
        assert ro_clean == len(all_corrected) == 5, ro_clean

        # Hard kill: entries == ORIGINAL input (output never rewritten). Using
        # len(all_corrected)=5 would slice entries_input[5:]==[] and DROP entry 4;
        # the input offset (sum of input lengths = 4) correctly reviews [entry 4].
        _save(input_batch_lengths)
        ro_kill = rs._load_resume_state(out, 3, 2, 0, [], {}, orig_input)[7]
        assert ro_kill == sum(input_batch_lengths) == 4, ro_kill
        assert orig_input[ro_kill:] == [orig_input[4]]

        # Back-compat: a checkpoint with no input lengths keeps the old output-space
        # behavior (never switches to input space), even against original input.
        _save(None)
        ro_old = rs._load_resume_state(out, 3, 2, 0, [], {}, orig_input)[7]
        assert ro_old == len(all_corrected) == 5, ro_old


def test_atomic_json_write_roundtrip_and_no_temp_leak():
    """atomic_json_write persists data and leaves no temp file behind; an
    overwrite doesn't corrupt the target (these underpin every JSON write)."""
    import utils
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "x.json")
        utils.atomic_json_write({"a": 1, "b": [1, 2, 3]}, p)
        assert utils.safe_load_json(p) == {"a": 1, "b": [1, 2, 3]}
        utils.atomic_json_write({"a": 2}, p)          # atomic overwrite
        assert utils.safe_load_json(p) == {"a": 2}
        leftover = [f for f in os.listdir(d) if f.startswith(".tmp_")]
        assert leftover == [], f"temp file(s) leaked: {leftover}"


def test_file_lock_mutual_exclusion_and_timeout():
    """file_lock refuses a second concurrent hold (raising TimeoutError) and is
    re-acquirable after release."""
    import utils
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "res.json")
        with utils.file_lock(target):
            try:
                with utils.file_lock(target, timeout=0.3):
                    raise AssertionError("file_lock granted a second concurrent hold")
            except TimeoutError:
                pass
        # After release the lock is acquirable again.
        with utils.file_lock(target, timeout=2):
            pass


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
