# Bug-report logging audit

Baseline: origin/main fetched 2026-11-15. Rankings are engineering judgment,
not measured incident frequency. Static survey is not exhaustive clearance.
The assigned whole-app scope exceeds the CLAUDE.md 4,000-token ad-hoc budget;
this overrun is explicit rather than silent.

## Existing evidence

- core._init_task_log/run_process/_stream_subprocess_to_logs/run_background_task:
  logs/api/*-latest.log, merged stderr/stdout, capped process_state tails, history.
- generate_script.call_llm_for_entries: logs/review_responses.log and existing
  rotation; request/response-log write failures already warn. Do not expand payloads.
- review_startup.ReviewStartupTrace and routers.script.get_task_log (recent
  startup diagnostics): correlation IDs, stages, exception types, exit/PID data,
  256-KiB rotation plus one previous file, existing log download. Reuse this writer.
- diagnostics.build_diagnostics and /api/tts/diagnostics, /api/voicelab/diagnostics:
  bounded redacted runtime/OS/GPU information. Pinokio Get Help/session bundles
  already documented; do not duplicate a full archive/runtime collector.
- Production Python AST survey excluding tests/env/cache: 1512 root-module,
  595 router, 436 experiment exception handlers. Loose heuristic candidates
  without obvious logging/reraise: 796/108/331. These are NOT confirmed defects:
  optional missing files, parse probes, cancellation, polling timeouts are normal.
- Frontend shared API/polling, voice-save queues, audio channels, export,
  training, preparer and Voice Lab catches surveyed. Several empty catches
  intentionally consume previously displayed errors or queue rejections.

## Ranked gaps (baseline file:line)

| Rank | Location | Gap and disposition |
| --- | --- | --- |
| 1 | app/app.py:114 | HTTP exceptions and uncaught route errors lack uniform downloadable evidence. Implement handlers preserving response contracts. |
| 2 | app/core.py:474 | Task-log creation silently fails. Implement. |
| 3 | app/core.py:1508 | Worker-log open failure loses durable output silently. Implement. |
| 4 | app/core.py:1631 | Worker-log write failure only reaches transient UI tail with raw exception text. Implement safe durable record. |
| 5 | app/core.py:1547 | Reader thread failure lacks durable task/stack context. Implement. |
| 6 | app/core.py:1675 | Launch/stream failure only has review-specific durable lifecycle coverage. Implement cross-task record. |
| 7 | app/core.py:1672 | Nonzero worker exit lacks uniform summary outside review. Implement code/task without copying stderr. |
| 8 | app/utils.py:258,262 | JSON corruption/type fallback warnings expose paths/messages and lack durable trace. Implement; missing files stay normal. |
| 9 | app/routers/speaker_review.py:434 | Per-candidate provider/verdict failure only retains type. Implement run/stage/stack. |
| 10 | app/core.py:1410; app/routers/speaker_review.py:450 | Shared background callback and outer review failures lack uniform durable stacks. Implement without altering history/state cleanup. |
| 11 | app/routers/system.py:710 | Failed model enumeration returns empty list/UI error only. Implement. |
| 12 | app/routers/voicelab.py:173 | Nonzero/empty interpreter probe looks like missing capabilities. Implement safe exit/empty summary. |
| 13 | app/routers/voicelab.py:174 | Probe launch/timeout/JSON failure becomes empty dict. Implement. |
| 14 | app/routers/system.py:570 | LLM diagnostic write failure returns empty path silently. Implement secondary warning. |
| 15 | app/static/js/app-core.js:1123,7642 | Save flush timer/pagehide consumes errors. Defer browser-local retention/transport: offline failures cannot report to the server; needs consent/rate-limit UX and browser qualification. |
| 16 | app/routers/lora.py:322 | Candidate evaluation load defaults to no recommendation. Defer distinguishing expected optional missing evidence from corruption without warning on every old adapter. |
| 17 | app/core.py:667,675 | Liveness exceptions conservatively return alive. Defer high-frequency polling instrumentation; preserve safety fence. |
| 18 | app/project.py:2605,2615 | Retry-persistence/restore failure prints warning only. Defer artifact-owner-focused follow-up. |
| 19 | app/routers/system.py:660,673 | Legacy LLM test files retain raw endpoint/traceback and have no bounded rotation. Defer migration of existing download contracts; do not expand contents. |
| 20 | app/app.py:157,180 | Direct auth/origin middleware refusals bypass exception handlers. Defer rate-limited summaries; do not change the security gates or record headers. Explicitly returned error responses are also outside exception-handler coverage. |

## Design and limits

Reuse the startup trace writer/rotation/readback for diagnostics-latest.log and
the existing /api/logs/diagnostics?download=true route. New records contain
static task/stage/operation labels, numeric status/exit codes, hashed resource
identity, exception type, bounded traceback frame basenames/function/line numbers.
Never serialize messages, source lines, locals, request URLs/queries/headers,
payloads, commands/output, titles/text, full paths. Resource hashes correlate
repeated resources but are not an anonymization guarantee.

No changes to ownership, cancellation, retries, GPU locks, refusal/default
semantics or legacy response-log retention. Unwritable evidence emits sanitized
console warnings: a full/read-only disk cannot guarantee an attachable file.
Existing legacy logs can contain private content; inspect before attaching.
Native GPU/model and real browser behavior remain unqualified.

Tests force real handler/helper failures with external I/O mocked, assert useful
context and unchanged outcomes, and reject synthetic secret/book/path sentinels
in captured records and download bytes. Verify rotation and persistence refusal.
Final test/gate counts belong in the handoff, not a claim of static clearance.
