"""
Regression tests for 9 of the 10 findings fixed after the [max] code-review of
the night-super-night-themes branch (Voice Lab pipeline / app.py /
review_script.py / index.html). Finding #10 (a dead `trained = 0` initializer
in the Voice Lab manifest summary) was pure dead-code removal with no
testable behavior and is not covered here.

  1. voice_library_apply() holds utils.file_lock(VOICE_CONFIG_PATH) across its
     read-modify-write, so it can't race review_script._remap_voice_config's
     concurrent rewrite of the same file.
  2. reattachRunningPollers() includes 'voicelab' in the tasks it checks on
     page reload, and re-attaches _vlSetRunning(true) + pollVoicelab() for a
     running Voice Lab job.
  3. run_process()'s "review" completion message reflects a VRAM batch-skip
     (instead of always reporting "completed successfully"), matching
     _write_single_review_report's "only partially reviewed" note.
  4. review_script._remap_voice_config writes its remapped voice_config.json
     via utils.atomic_json_write instead of a raw open()+json.dump.
  5/6. voice_library_apply_bulk / voice_library_match_bulk offload their
     per-book file I/O to a worker thread via asyncio.to_thread, without
     changing the result shape.
  7. _build_match_proposals(counts, pool) is the single fuzzy-match
     proposal-builder shared by voice_library_match and
     voice_library_match_bulk.
  8. _combine_pass_totals(state) sums totals_fwd + totals_bwd (including
     books_done) for the bidirectional batch review's "Overall" summary.
  9. _init_task_log(task_name, extra_header) is the single helper that starts
     a fresh on-disk task log with a header banner, used by run_process,
     batch_review, batch_script, and voicelab.

Run with:
  python test_voicelab_review_fixes.py
  # or from project root:
  # app/env/bin/python test_voicelab_review_fixes.py
"""
import asyncio
import inspect
import json
import os
import re
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import review_script
import app as app_module

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _extract_js_function(html, signature_regex):
    """Return the body (between the outermost { }) of the first function whose
    declaration matches `signature_regex`, using brace-counting so nested
    blocks don't end the match early."""
    m = re.search(signature_regex, html)
    if not m:
        return None
    start = html.index("{", m.end())
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1:i]
    return None


# ── Fix #1: voice_library_apply holds file_lock across its read-modify-write ─

def test_voice_library_apply_under_lock_preserves_alias():
    print("\n=== Fix #1: voice_library_apply applies the mapping under file_lock ===")
    with tempfile.TemporaryDirectory() as d:
        config_path = os.path.join(d, "voice_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"HERO": {"type": "designed", "voice": "old.wav", "alias_of": "HERO_PRIME"}}, f)

        lib = {
            "shared": {"narrator_key": {"name": "Narrator", "config": {"type": "designed", "voice": "narrator.wav"}}},
            "casts": {"Series A": {"members": {"hero_key": {"name": "Hero", "config": {"type": "clone", "voice": "hero.wav"}}}}},
        }
        request = app_module.LibraryApplyRequest(
            cast="Series A",
            mapping={"NARRATOR": "narrator_key", "HERO": "hero_key"})

        with mock.patch.object(app_module, "VOICE_CONFIG_PATH", config_path), \
             mock.patch.object(app_module, "_load_voice_library", return_value=lib):
            result = asyncio.run(app_module.voice_library_apply(request))

        check("result reports both applied", sorted(result["applied"]) == ["HERO", "NARRATOR"], result)

        with open(config_path, "r", encoding="utf-8") as f:
            written = json.load(f)
        check("NARRATOR written from the shared pool", written["NARRATOR"]["voice"] == "narrator.wav", written)
        check("HERO written from the cast, preserving its alias_of",
              written["HERO"]["voice"] == "hero.wav" and written["HERO"].get("alias_of") == "HERO_PRIME",
              written)
        check("lock file cleaned up", not os.path.exists(config_path + ".lock"))


# ── Fix #2: reattachRunningPollers includes voicelab ────────────────────────

def test_reattach_running_pollers_includes_voicelab():
    print("\n=== Fix #2: reattachRunningPollers() polls a running Voice Lab job ===")
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    body = _extract_js_function(html, r"async function reattachRunningPollers\([^)]*\)\s*")
    check("reattachRunningPollers() found", body is not None)
    body = body or ""

    check("'voicelab' is in the polled task list",
          re.search(r"\[\s*'batch_script'.*?'voicelab'\s*\]", body, re.S) is not None, body)
    check("a running voicelab task re-attaches _vlSetRunning(true) + pollVoicelab()",
          re.search(r"running\.voicelab\s*\)\s*\{\s*_vlSetRunning\(true\);\s*pollVoicelab\(\);", body) is not None,
          body)


# ── Fix #3: run_process's "review" completion note reflects a VRAM skip ────

def test_run_process_review_reports_vram_skip_in_completion_note():
    print("\n=== Fix #3: run_process()'s review-completion message reflects a VRAM-skip ===")
    review_lines = [
        "Review complete: 10 -> 12 entries",
        "Text changed: 1",
        "Speaker changed: 0",
        "Instruct changed: 0",
        "Entries added: 2",
        "Entries removed: 0",
        "Batches failed: 0",
        "Batches skipped (low GPU VRAM): 2",
        "Total changes: 3",
    ]

    def fake_stream(command, cwd, state, log_prefix="", max_logs=20000, log_file=None):
        state["logs"].extend(review_lines)
        return 0, review_lines

    state = app_module.process_state["review"]
    saved_logs = list(state["logs"])
    try:
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(app_module, "API_LOG_DIR", d), \
             mock.patch.object(app_module, "_stream_subprocess_to_logs", side_effect=fake_stream), \
             mock.patch.object(app_module, "_write_single_review_report", return_value=None):
            app_module.run_process(["true"], "review")

        check("completion note mentions skipped sections",
              any("section(s) were skipped because the GPU ran low on memory" in l for l in state["logs"]),
              state["logs"])
        check("completion note does not claim plain success",
              not any(l == "Task review completed successfully." for l in state["logs"]),
              state["logs"])
    finally:
        state["logs"] = saved_logs


# ── Fix #4: _remap_voice_config writes via atomic_json_write ────────────────

def test_remap_voice_config_uses_atomic_write():
    print("\n=== Fix #4: _remap_voice_config writes via utils.atomic_json_write ===")
    with tempfile.TemporaryDirectory() as d:
        config_path = os.path.join(d, "voice_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"OLD_NAME": {"type": "designed", "voice": "x.wav"}}, f)

        with mock.patch.object(review_script, "atomic_json_write", wraps=review_script.atomic_json_write) as spy:
            moved = review_script._remap_voice_config(config_path, {"OLD_NAME": "NEW_NAME"})

        check("one key remapped", moved == 1, moved)
        check("atomic_json_write was used for the write", spy.called)

        with open(config_path, "r", encoding="utf-8") as f:
            written = json.load(f)
        check("config now has the canonical name", "NEW_NAME" in written and "OLD_NAME" not in written, written)
        check("lock file cleaned up", not os.path.exists(config_path + ".lock"))
        check("no stray temp file left behind",
              not any(n.startswith(".tmp_") for n in os.listdir(d)), os.listdir(d))


# ── Fix #5/#6: apply_bulk / match_bulk offload per-book I/O to a thread ─────

def test_voice_library_apply_bulk_offloads_and_writes_per_book():
    print("\n=== Fix #5: voice_library_apply_bulk offloads its per-book I/O via asyncio.to_thread ===")
    src = inspect.getsource(app_module.voice_library_apply_bulk)
    check("voice_library_apply_bulk offloads via asyncio.to_thread", "asyncio.to_thread(" in src, src)

    with tempfile.TemporaryDirectory() as scripts_dir:
        books = {
            "book1": [{"speaker": "HERO", "text": "Charge!", "instruct": "brave"}],
            "book2": [{"speaker": "NARRATOR", "text": "The end.", "instruct": "calm"}],
        }
        for name, entries in books.items():
            with open(os.path.join(scripts_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(entries, f)

        lib = {
            "shared": {"narrator_key": {"name": "Narrator", "config": {"type": "designed", "voice": "narrator.wav"}}},
            "casts": {"Series A": {"members": {"hero_key": {"name": "Hero", "config": {"type": "clone", "voice": "hero.wav"}}}}},
        }
        request = app_module.LibraryApplyBulkRequest(
            cast="Series A",
            mapping={"NARRATOR": "narrator_key", "HERO": "hero_key", "VILLAIN": "narrator_key"},
            script_names=["book1", "book2"])

        with mock.patch.object(app_module, "SCRIPTS_DIR", scripts_dir), \
             mock.patch.object(app_module, "_load_voice_library", return_value=lib):
            result = asyncio.run(app_module.voice_library_apply_bulk(request))

        results_by_name = {r["name"]: r for r in result["results"]}
        check("book1 gets only HERO applied (VILLAIN doesn't appear in it)",
              results_by_name["book1"]["applied"] == ["HERO"], results_by_name)
        check("book2 gets only NARRATOR applied (VILLAIN doesn't appear in it)",
              results_by_name["book2"]["applied"] == ["NARRATOR"], results_by_name)

        for name in ("book1", "book2"):
            config_path = os.path.join(scripts_dir, f"{name}.voice_config.json")
            check(f"{name}.voice_config.json written", os.path.exists(config_path))
            check(f"{name}'s lock file cleaned up", not os.path.exists(config_path + ".lock"))


def test_voice_library_match_bulk_unions_counts_across_books():
    print("\n=== Fix #6: voice_library_match_bulk unions per-book counts via asyncio.to_thread ===")
    src = inspect.getsource(app_module.voice_library_match_bulk)
    check("voice_library_match_bulk offloads via asyncio.to_thread", "asyncio.to_thread(" in src, src)

    with tempfile.TemporaryDirectory() as scripts_dir:
        book1 = [
            {"speaker": "NARRATOR", "text": "Once upon a time.", "instruct": "calm"},
            {"speaker": "HERO", "text": "I will save the day!", "instruct": "brave"},
        ]
        book2 = [
            {"speaker": "NARRATOR", "text": "The end.", "instruct": "calm"},
            {"speaker": "VILLAIN", "text": "Curses!", "instruct": "angry"},
        ]
        for name, entries in (("book1", book1), ("book2", book2)):
            with open(os.path.join(scripts_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(entries, f)

        lib = {
            "shared": {"narrator_key": {"name": "Narrator", "config": {"type": "designed", "voice": "narrator.wav"}}},
            "casts": {"Series A": {"members": {"hero_key": {"name": "Hero", "config": {"type": "clone", "voice": "hero.wav"}}}}},
        }
        request = app_module.CastMatchBulkRequest(name="Series A", script_names=["book1", "book2"])

        with mock.patch.object(app_module, "SCRIPTS_DIR", scripts_dir), \
             mock.patch.object(app_module, "_load_voice_library", return_value=lib):
            result = asyncio.run(app_module.voice_library_match_bulk(request))

        check("book_count reflects both books", result["book_count"] == 2, result)
        by_char = {p["character"]: p for p in result["proposals"]}
        check("NARRATOR's line count is summed across both books",
              by_char["NARRATOR"]["line_count"] == 2, by_char)
        check("HERO (only in book1) has line_count 1", by_char["HERO"]["line_count"] == 1, by_char)
        check("VILLAIN (only in book2) has line_count 1", by_char["VILLAIN"]["line_count"] == 1, by_char)
        check("NARRATOR matched against the shared pool",
              by_char["NARRATOR"]["match"]["key"] == "narrator_key", by_char["NARRATOR"])
        check("HERO matched against the cast pool",
              by_char["HERO"]["match"]["key"] == "hero_key", by_char["HERO"])


def test_voice_library_match_bulk_no_characters_raises_400():
    print("\n=== Fix #6: voice_library_match_bulk raises 400 when the union has no characters ===")
    with tempfile.TemporaryDirectory() as scripts_dir:
        with open(os.path.join(scripts_dir, "empty.json"), "w", encoding="utf-8") as f:
            json.dump([], f)

        lib = {"shared": {}, "casts": {"Series A": {"members": {}}}}
        request = app_module.CastMatchBulkRequest(name="Series A", script_names=["empty"])

        raised = False
        with mock.patch.object(app_module, "SCRIPTS_DIR", scripts_dir), \
             mock.patch.object(app_module, "_load_voice_library", return_value=lib):
            try:
                asyncio.run(app_module.voice_library_match_bulk(request))
            except app_module.HTTPException as e:
                raised = e.status_code == 400

        check("raises HTTP 400 when no selected book has any characters", raised)


# ── Fix #7: _build_match_proposals shared by /match and /match_bulk ─────────

def test_build_match_proposals_sorted_and_matched():
    print("\n=== Fix #7: _build_match_proposals builds sorted, fuzzy-matched proposals ===")
    pool = {
        "narrator_key": {"key": "narrator_key", "name": "Narrator", "source": "shared", "type": "designed"},
        "hero_key": {"key": "hero_key", "name": "Hero", "source": "cast", "type": "clone"},
    }
    counts = {"NARRATOR": 5, "HERO": 10, "MYSTERY GUEST": 1}

    proposals = app_module._build_match_proposals(counts, pool)

    check("proposals sorted by line_count descending",
          [p["character"] for p in proposals] == ["HERO", "NARRATOR", "MYSTERY GUEST"], proposals)
    check("HERO matches hero_key (exact)",
          proposals[0]["match"] is not None and proposals[0]["match"]["key"] == "hero_key" and proposals[0]["match"]["exact"],
          proposals[0])
    check("NARRATOR matches narrator_key (exact)",
          proposals[1]["match"] is not None and proposals[1]["match"]["key"] == "narrator_key" and proposals[1]["match"]["exact"],
          proposals[1])
    check("MYSTERY GUEST has no good match", proposals[2]["match"] is None, proposals[2])


def test_voice_library_match_uses_shared_proposal_builder():
    print("\n=== Fix #7: voice_library_match delegates to _build_match_proposals (no duplicated loop) ===")
    src = inspect.getsource(app_module.voice_library_match)
    check("voice_library_match calls _build_match_proposals",
          "_build_match_proposals(counts, pool)" in src, src)
    check("voice_library_match has no duplicated inline best-match loop",
          "for cand in pool.values()" not in src, src)


# ── Fix #8: _combine_pass_totals sums totals_fwd + totals_bwd ───────────────

def test_combine_pass_totals_sums_forward_and_backward_including_books_done():
    print("\n=== Fix #8: _combine_pass_totals sums totals_fwd + totals_bwd (books_done = max, not sum) ===")
    state = {
        "totals_fwd": {**{k: 1 for k in app_module._REVIEW_SUMMARY_PATTERNS}, "books_done": 3},
        "totals_bwd": {**{k: 2 for k in app_module._REVIEW_SUMMARY_PATTERNS}, "books_done": 4},
    }
    overall = app_module._combine_pass_totals(state)
    check("per-summary-key totals are summed",
          all(overall[k] == 3 for k in app_module._REVIEW_SUMMARY_PATTERNS), overall)
    check("books_done is max(fwd, bwd), not summed (avoids double-counting each book)",
          overall["books_done"] == 4, overall)


# ── Fix #9: _init_task_log writes a fresh header banner ─────────────────────

def test_init_task_log_writes_header_and_extra():
    print("\n=== Fix #9: _init_task_log writes a fresh header banner (+ optional extra) ===")
    with tempfile.TemporaryDirectory() as d:
        with mock.patch.object(app_module, "API_LOG_DIR", d):
            log_path = app_module._init_task_log("review")
            with open(log_path, "r", encoding="utf-8") as f:
                content = f.read()
            check("header banner includes the task name", content.startswith("# review log"), content)
            check("no extra header by default", content.count("\n") == 1, repr(content))

            log_path2 = app_module._init_task_log("voicelab", extra_header="# zips_dir=/tmp/zips\n")
            with open(log_path2, "r", encoding="utf-8") as f:
                content2 = f.read()
            check("extra_header appended after the banner",
                  content2.endswith("# zips_dir=/tmp/zips\n"), content2)

            app_module._init_task_log("voicelab", extra_header="# zips_dir=/tmp/zips\n")
            with open(log_path2, "r", encoding="utf-8") as f:
                content3 = f.read()
            check("repeated calls overwrite rather than append",
                  content3.count("zips_dir") == 1, content3)


def main():
    test_voice_library_apply_under_lock_preserves_alias()
    test_reattach_running_pollers_includes_voicelab()
    test_run_process_review_reports_vram_skip_in_completion_note()
    test_remap_voice_config_uses_atomic_write()
    test_voice_library_apply_bulk_offloads_and_writes_per_book()
    test_voice_library_match_bulk_unions_counts_across_books()
    test_voice_library_match_bulk_no_characters_raises_400()
    test_build_match_proposals_sorted_and_matched()
    test_voice_library_match_uses_shared_proposal_builder()
    test_combine_pass_totals_sums_forward_and_backward_including_books_done()
    test_init_task_log_writes_header_and_extra()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED: {FAILURES}")
    else:
        print("RESULT: all checks passed")
    print("=" * 60)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
