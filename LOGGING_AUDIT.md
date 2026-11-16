# Bug-report logging audit

Baseline: origin/main fetched 2026-11-15. Rankings are engineering judgment,
not measured incident frequency. Static survey is not exhaustive clearance.
The original audit predated removal of CLAUDE.md's token-budget rule.

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
| 15 | app/static/js/app-core.js: createSerializedSaveQueue / pagehide | Done: content-free, same-origin keepalive reports; five per tab per minute, deduplicated. Server independently bounds schema, body and report rate. Offline/unload delivery remains best-effort. |
| 16 | app/routers/lora.py: _get_candidate_summary | Done: missing optional evaluation.json remains normal; unreadable/invalid JSON, encoding and root/recommendation shapes record hashed-resource evidence, deduplicated for five minutes. Recommendation fallback is unchanged. |
| 17 | app/core.py: is_llm_on_this_gpu / llm_is_on_this_gpu | Done for the actual cited paths: GPU policy/config failures retain conservative True and record once per stage/task/exception type per five minutes. Correction: these paths are GPU-admission policy, not process-liveness probes; no return-alive exception handler exists in current core.py. |
| 18 | app/project.py: _finish_speech_retry_failure / _restore_and_release | Done: persist/restore failures record exception frames and hashed chunk identity, deduplicated for five minutes. Return values and best-effort restore behavior are unchanged. |
| 19 | app/routers/system.py: _log_llm_failure / _run_llm_test | Done: new LLM test/optimize evidence uses the existing trace writer, 256-KiB latest plus one previous per static kind, without raw endpoint/messages/tracebacks. log_file remains a path and /api/logs/llm_test?download=true retains its attachment contract. Old timestamped files are untouched. |
| 20 | app/app.py: _basic_auth_gate / _request_origin_gate | Done: static guard/stage plus numeric status, globally deduplicated per guard for one minute; no client/header/Host values recorded. Auth challenges, preflight bypass and security decisions remain unchanged. Other explicitly returned error responses remain outside exception-handler coverage. |

## Design and limits

Reuse the startup trace writer/rotation/readback for diagnostics-latest.log and
the existing /api/logs/diagnostics?download=true route. New records contain
static task/stage/operation labels, numeric status/exit codes, hashed resource
identity, exception type, bounded traceback frame basenames/function/line numbers.
Never serialize messages, source lines, locals, request URLs/queries/headers,
payloads, commands/output, titles/text, full paths. Resource hashes correlate
repeated resources but are not an anonymization guarantee.

No changes to ownership, cancellation, retries, GPU locks, refusal/default
semantics or LLM response-log retention. Unwritable evidence emits sanitized
console warnings: a full/read-only disk cannot guarantee an attachable file.
Existing legacy logs can contain private content; inspect before attaching.
Native GPU/model and real browser behavior remain unqualified.

Tests force real handler/helper failures with external I/O mocked, assert useful
context and unchanged outcomes, and reject synthetic secret/book/path sentinels
in captured records and download bytes. Verify rotation and persistence refusal.
Final test/gate counts belong in the handoff, not a claim of static clearance.

## Deferred-gap implementation boundaries

The browser posts only `stage` (save_flush_timer/save_pagehide) and `error_name`
(ten fixed JavaScript error names, unknown mapped to UnknownError) to the relative
/api/logs/browser-failure route. No message, text, URL or stack is sent. Fetch
uses same-origin credentials, refuses redirects, and never retries reporting
failures. No new UI text or consent UI is introduced. The existing dirty-draft,
onError and ambiguous-save recovery paths remain authoritative. This is local
AA2 evidence, not external telemetry; a stopped/offline server cannot retain it.

The route retains at most 256 body bytes, including chunked requests, rejects extra or
unknown fields and aborted uploads without logging the rejected body, and returns 204 for accepted
reports, even if deduplicated. It uses the same app auth/Host/origin gates as
other routes. Server-side browser output is limited to one record per stage per
minute across all clients in the server process. Shared dedupe retains at most 512 hashed-resource
keys, suppressing new keys at capacity until entries expire; raw resource ids
are not retained. All disk evidence uses the existing bounded writer.

Legacy LLM test/optimize files now use static llm_test-latest.log and
llm_optimize-latest.log names with one .previous file each. Safe evidence is also
available through the existing diagnostics download. Existing timestamped legacy
files may still contain secrets: they are not rewritten, deleted or attached
automatically. API response error strings/model lists are unchanged; their
presence in the API contract does not authorize copying them into diagnostics.

Regression tests execute real failure handlers and persistence/download paths,
with synthetic secret/book-text sentinels. Node VM exercises the actual save
timer and pagehide callbacks, dedupe/window reset, offline rejection and transport
options; this is not native-browser unload-delivery qualification. Initial
unchanged-production run: 10 tests, 11 assertion failures and two errors (subtest
failures counted separately), with no passing clearance claimed.
The final 12-test regression module was also run with all seven changed
production files temporarily restored to origin/main: all 12 tests were
unsuccessful (12 assertion failures and three errors, including subtests).
The implementation was then restored before final focused qualification.

PR #1972 follow-up: the markup ratchet explicitly exempts the reviewed browser
stage/error vocabulary and fetch protocol identifiers, not user-visible copy;
no numeric baseline or catalog key set changes. Diagnostic console emission and
persistence-warning emission are guarded centrally, and record_failure returns
False rather than letting a failed sink replace caller behavior. A guarded,
non-recursive sys.__stderr__ fallback writes only the sink exception type.
Missing exception evidence stays absent; non-exception values retain at most
their type name and are never stringified. The legacy LLM helper omits invalid
exception inputs and retains its empty-path response when persistence fails.
Six fail-first regressions cover both conservative GPU returns, exactly-once
retry restoration, all three guard statuses and the auth challenge, non-exception
inputs, and simultaneous console/file/fallback failure without masking the
original worker exception. The legacy config test now asserts distinct safe
records in the approved bounded latest file rather than obsolete raw files.
