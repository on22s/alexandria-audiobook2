# CLAUDE.md Rule-Compliance Audit — Findings Log

Plan: `docs/superpowers/plans/2026-06-19-claude-md-rule-compliance-audit.md`
Progress: `docs/audits/claude_md_rule_audit/PROGRESS.md`

Findings use a single incrementing `F-001, F-002, ...` counter across the whole audit. Use the Finding Template and Fix-now criteria from the plan's Task 1.

---

### [F-001] Rule 9 — `atomic_json_write` retry budget quietly halved for manifest cache write
- **Piece:** P02 — app/hf_utils.py
- **Location:** `app/hf_utils.py:44` (`fetch_builtin_manifest`)
- **Severity:** low
- **Description:** `_atomic_json_write(entries, local_path, max_retries=3)` overrides the shared helper's default `max_retries=5` (see `app/utils.py:69`) with no comment explaining why this call site needs a smaller retry budget than every other caller. This is the only call site in the repo that overrides `max_retries`.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either restore the default 5 retries, or add a comment explaining why the local manifest-cache write can tolerate fewer retries than other atomic writes (e.g. it's a best-effort offline-fallback cache rather than authoritative state).

### [F-002] Rule 2 — Three near-duplicate prompt-file loaders, only one cached
- **Piece:** P03 — app/default_prompts.py + app/persona_prompts.py + app/review_prompts.py
- **Location:** `app/default_prompts.py:9-41` (`load_default_prompts`) vs `app/persona_prompts.py:6-27` (`load_persona_prompts`) vs `app/review_prompts.py:6-26` (`load_review_prompts`)
- **Severity:** low
- **Description:** All three modules implement the same pattern (read a `---SEPARATOR---`-delimited `.txt` file, split into N parts, raise `RuntimeError` if missing/malformed), but `load_default_prompts` alone added an mtime-based cache while the other two re-read and re-split the file from disk on every call. `app/app.py`'s `get_config()` and `get_default_prompts()` call all three loaders together, repeatedly, on the same request paths (e.g. `app/app.py:1716-1830`), so the caching behavior is inconsistent across what is otherwise one conceptual operation.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either factor the shared read/split/validate logic into one parametrized helper (delimiter count, error message) used by all three, with caching applied uniformly, or add a comment explaining why `default_prompts.py` needed caching and the others didn't.

### [F-003] Rule 8 — `measure_throughput` swallows any exception identically, indistinguishable from a real bug
- **Piece:** P05 — app/llm_bench.py
- **Location:** `app/llm_bench.py:78-82` (`measure_throughput`)
- **Severity:** low
- **Description:** `except Exception: return None` inside the `as_completed` loop catches every possible error from `_one_call` (network timeout, auth failure, a `TypeError` from a future code change, malformed API response, etc.) with no logging at all, then silently returns `None`. The docstring documents `None` as meaning "this concurrency level isn't safe," but a latent bug producing the exact same `None` every time would be indistinguishable from a real server limitation in `get_cached_or_benchmarked_concurrency`'s caller-facing prints (which only print the chosen concurrency, never that a request actually raised an exception or what kind).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log the exception type/message (e.g. `logger.debug` or a print) before returning `None`, so a real bug surfaces differently from an expected timeout/connection failure.

### [F-004] Rule 15 — `get_cached_or_benchmarked_concurrency` branches on raw `llm_mode == "remote"` instead of `is_remote_llm(llm_mode, base_url)`
- **Piece:** P05 — app/llm_bench.py
- **Location:** `app/llm_bench.py:167,179,183` (`get_cached_or_benchmarked_concurrency`)
- **Severity:** medium
- **Description:** [rule15-candidate] This function takes both `llm_mode` and `base_url` as parameters, but its three remote/local branch points (`profile_key` selection at line 167, the `status` fetch at line 179, and the GPU-probe/label selection at line 183) all test `llm_mode == "remote"` directly rather than calling `lmstudio_settings.is_remote_llm(llm_mode, base_url)`. `is_remote_llm`'s own docstring in `app/lmstudio_settings.py` exists specifically because `llm_mode` alone "misses... a save_config edge case where llm_mode and the active base_url have drifted out of sync" — this function has `base_url` in scope and could hit exactly that drift, picking the wrong cache profile / status-fetch path / GPU probe.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — not resolved here per audit scope; flagging for the Rule 15 cross-cutting pass (Task 4 in PROGRESS.md).

### [F-005] Rule 8 — `tts_vram_benchmark.py`'s VRAM/duration probes swallow all exceptions identically to "no GPU" and feed silently-wrong numbers into the tuning recommendation
- **Piece:** P07 — app/tts_vram_benchmark.py
- **Location:** `app/tts_vram_benchmark.py:70-81` (`vram_state`), `:83-88` (`gpu_name`), `:119-127` (duration-estimation block in `run_sweep`)
- **Severity:** medium
- **Description:** All three sites use `except Exception: return None` / `return "unknown"` / `pass`, with zero logging. `vram_state()` returns `None` both when there's genuinely no CUDA device (`torch.cuda.is_available()` is `False` — expected) and when `torch.cuda.mem_get_info()` itself raises for any other reason (a real bug) — both cases produce the identical `None`. In `main()`, `model_vram_gb = (snap_post["allocated_gb"] - snap_pre["allocated_gb"]) if (snap_pre and snap_post) else 0` silently substitutes `0` for the model's actual VRAM footprint if either snapshot failed, and that `0` then flows straight into `print_summary`'s headroom math and the "Tier table recommendation" that's meant to be pasted into `index.html`'s `_computeAutoSettings`. Likewise the duration-estimation `except Exception: pass` leaves `total_audio = 0.0` on any `soundfile` failure (missing import, corrupt WAV, etc.), silently producing `rtf = None` for every row instead of surfacing that the duration measurement itself failed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — this is a dev/tuning script whose entire output is meant to be trusted and pasted into production tier tables, so at minimum print the exception type/message at each site before falling back, so a "no GPU" run is distinguishable from a "GPU present but probe broke" run.
