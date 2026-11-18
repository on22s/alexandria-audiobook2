# UI follow-ups after the first workflow pass (#1938)

The remaining controls use the existing vanilla-JS actions. No second narrator,
rendering configuration, or output policy is saved in Setup. Basic Setup links
to Voices for narrator selection/preview, Speech engine for rendering settings,
and Result for output/integrity controls. Card elevation uses restrained,
theme-specific shadows; Cyberpunk retains its established glow.

## Editor and connection feedback

- The saved-take waveform supports click and captured-pointer drag seeking.
  CSS coordinates map to the same width as the peak bars, independent of display
  pixel ratio. Left/Right seek five seconds; Home/End seek to the boundaries.
  A playhead and slider ARIA position follow the actual audio element. Unknown
  duration, a changed book, changed row UID, or changed take cannot seek old audio.
- Render Pending counts the same nonempty, non-done rows admitted by the batch
  renderer. The label includes retryable failures, and is reset when switching
  books; it is not a count of hidden or filtered rows.
- Row stripes reflect saved pending/generating/done/error state and voice-drift
  flags. Failed rows expose Retry through the original generation handler and its
  edit/source admission barriers. A rendered stripe does not certify quality.
  Locale changes repaint the live count and ingest facts without turning them
  back into their initial markup placeholders.
- System facts have individual subtle badges. Build information remains in the
  responsive cluster and the existing brand badge instead of disappearing below
  1600 pixels. A collapsed mobile menu still has its existing brand build badge.
  From 1600 pixels up the bar follows the page content width (85%) instead of
  Bootstrap's 1320px container cap. The returning build badge had been added
  without any added room, so the navbar wrapped onto a second row: measured in
  the browser on placeholder readings, the bar needed 1167px of a 1157px
  collapse at 1600 and became one 60px row at 1183px once the width followed the
  content. 1280 and 1440 are unchanged (1320px cap, one row at 48px slack), and
  1920 is one row at the same 1618px as the page content.
- Connection results sit next to the endpoint. Busy state includes a spinner;
  elapsed milliseconds cover the whole HTTP round trip, including queue/network
  time, not model-only latency. Failures offer local troubleshooting guidance.
  Edited endpoints and changed profiles cannot adopt older success/failure output.

## Ingest facts and optional tools

`GET /api/script/ingest-status` returns bounded, read-only facts for the active
source. It reuses the confined source reader and character-roster validator,
keys roster lookup by source bytes, compares the actual saved script through
the existing source-integrity policy, and rechecks source hash/book identity
before returning. An optional expected book token refuses an obsolete selection.
The comparison itself is bound to the captured source hash, so a source replaced
and then restored during comparison cannot borrow another version's success.
An accepted selection awaiting script publication reports not built while keeping
its source facts; it does not inspect the previous book's saved script. Existing
published-script admission remains unchanged. Duplicate in-flight refreshes are
coalesced; the bounded full-byte reads are retained for source freshness.
Source characters, whitespace-delimited words, and detected chapter headings
describe accepted source text. The 150-words/minute duration is an explicitly
labelled estimate; it is not reliable for every language or an audio plan.

The strip links to upload/accept, the existing character builder, and script
generation. Roster review is available before Voices. Character discovery and
scene context use existing cast/script generation; there is no invented separate
scene-analysis task. Progress displays evidence states such as not checked,
saved roster, source match, differences, or unavailable. It does not infer that a
model task ran from file presence. Selection, cast refresh, and task completion
refresh or invalidate these facts; an explicit refresh remains available.

OPDS/Calibre and translation previews already use collapsed `details` surfaces
on the audited base. Their explicit capture/import/preview actions and lazy
loading remain intact; moving them into a second modal would add another state
owner without reducing their default footprint.

The modal audit found that ordinary destructive confirmations in script batches,
Casting, and saved scripts already use `showConfirm`. Casting's remaining modal
is an interactive listening/review workflow, and clone import contains channel,
rights, source and cleanup forms. These are not yes/no questions; replacing them
with `showConfirm` would discard required controls and safety checks. They retain
their dedicated forms and Bootstrap focus handling.

## Inventory maintenance without unrelated ML imports

From `app`, `python update_test_inventory.py --module test_my_module` updates only
the named tracked module; repeat `--module` for more than one. Other test IDs are
preserved, the existing artifact must be valid, and writes remain atomic with LF
bytes. This is explicitly a targeted maintenance update, not proof that all
modules import. It is useful for adding a self-contained test in a lightweight
environment. A selected module still needs its own actual dependencies.

`python update_test_inventory.py --check` always imports and checks every tracked
module. It cannot be combined with `--module`; missing dependencies still refuse
full verification. Readiness and CI continue using complete discovery. Stage new
test modules before updating the inventory.

## Qualification boundaries

Regression fixtures use synthetic text/WAV audio and a loopback OpenAI-protocol
server. They exercise actual handlers, browser audio decoding, click/key seeking,
all six theme palettes and the real connection route/client. A protocol fixture
is not a live language model. The connection test uses whichever endpoint and
model the app user edits; no developer-specific server is required. Live-provider
success and full app end-to-end qualification are not inferred from these tests.
No paid calls, model downloads, or user books are needed by the regressions.


## Complete-page startup follow-up

A fresh isolated app/browser run exposed a help/localization startup-order bug:
help removed native titles before localization captured their authored English.
The next locale sweep threw on the Advanced navigation title and interrupted
settings initialization. Help now captures keyed attributes through the same
localization-owned map before moving them, including reused disabled labels.
The regression loads the real help and localization modules in their production
order, verifies translated tooltip text, and restores authored English without
reintroducing native titles. A complete app page was also exercised with an
isolated data root, four locale changes and a synthetic text upload; no browser
exceptions occurred and the source/roster/script summary showed the saved facts.
This smoke check does not exercise model generation or certify the entire
upload-to-audiobook workflow.


The follow-up also shares the pending-selection predicate with published-script
admission and saved-book publication. Missing or explicit `false` permits the
published-script path; every other stored flag value keeps the selection pending.
Ingest can therefore retain verified source facts while correctly reporting that
its script is not built, without reading the previous selection's script.
Feedback sentences use the shared locale-aware separator for Japanese, Chinese,
English and Spanish. Regressions exercise malformed legacy flag values against
the actual ingest and diff routes, disabled-label capture before locale startup,
and locale-specific sentence spacing using the actual localization module.

To repeat the manual complete-page smoke check, launch the app with a fresh
isolated `ALEXANDRIA_DATA_DIR`, open the complete page, and collect browser
exceptions from initial load. Change locales through Japanese, Chinese, Spanish
and English and verify the Advanced navigation help text each time. Upload a
synthetic text file through the file picker and refresh ingest facts: the selected
source should be verified while missing cast and script artifacts remain marked
as not built. Point the connection test at an explicitly configured test endpoint
and verify both its returned reply and the re-enabled button. A synthetic
OpenAI-compatible service can exercise that transport, but is not evidence of
live-provider or audiobook generation success. This procedure is manual evidence;
the committed automated regressions cover its startup-order and status defects,
not the complete procedure.


## Windows follow-up and joined synthetic workflow (2026-11-14)

A fresh worktree at merged main `70a63e313d6151ebea09161c86629833bffa9bdc`
was exercised with the existing Windows Python 3.10 environment. The full
readiness gate is still **not qualified on this Windows host**. The earlier
shard 3/6 attempt ran 2,187 tests with 29 failures, 46 errors and 142 skips.
The verifier rejects skipped unit tests, including deliberately Linux-only
process/queue tests. File symlink creation also fails with WinError 1314 on this
account. Additional failures involve POSIX-only fixtures, unavailable local
packages (`uv`, `romkan2`), a symlink materialized as target-name text, and
open-file cleanup. Neither gate semantics nor OS privilege settings were changed.

This follow-up repairs demonstrated test-fixture portability defects:

- Native CLI fixtures use a real Python payload and host launcher, including
  interpreter and command paths containing spaces; exit status and arguments
  are checked. ROCm and LM Studio reload tests still execute real subprocesses.
- Native path comparisons and dataset cache keys follow their production
  counterparts on Windows and POSIX, without changing expected behavior.
- Signal dispatch tests supply unavailable platform constants explicitly and
  check the Windows process-tree branch as well as the POSIX branch.
- The durable-writer test supplies directory-fsync capability on Windows while
  POSIX still syncs a real directory descriptor. Both retain real file
  sync/replacement and the exact sync/replace/sync order.
- Metadata-read tests intercept the actual owned-phase boundary and isolate
  unrelated preparer logging so their temporary files can close on Windows.

Before these repairs, the selected Windows cohort ran 90 tests with 19 failures,
2 errors and one existing Linux-only skip. With the new launcher regression it
ran 91 tests: the first repair run had one LM Studio assertion mismatch at the initial-status path
(initial endpoint refusal rather than the intended fresh-status refusal) and one
HTTP worker connection reset (WinError 10054); a repeat had only the reset. The
two modules passed all four tests when isolated. Their intermittent behavior
remains unqualified; no causal CLI/file race has been established and no test
retries or enlarged timeouts hide it. Diagnostic-only probes subsequently
captured WinError 10054 on the initial native inventory request before any CLI
operation (one of five attempts). Explicit HTTP response framing did not fix it
(three of twenty attempts still refused initially), so that experiment was
reverted. The precise socket-reset cause remains unresolved.

Freebuff review found two sibling dataset cache fixtures with the same separator
defect. Before repair, their 11 tests produced 14 failing subtests; native-path
keys preserve their cache rebuild/reuse assertions. The expanded cohort then ran
101 tests with one LM Studio assertion mismatch at the initial-status path, zero errors and one existing
Linux-only skip. The new Windows CI command (`tests.test_cli_fixture` and
`tests.test_gpu_stats`) ran nine tests with zero failures/errors/skips.

A later full shard 3/6 gate ran 2,556 tests with 17 failures, 64 errors and 180
skips (521.703 seconds including gate overhead). This differs in discovered
scope from the earlier 2,187-test run and is not an improvement comparison. It
remains red. The final pre-publication run of that same shard ran 2,556 tests
with 18 failures, 66 errors and 180 skips (517.859 seconds including gate
overhead). None of the changed test modules appears among that run's failing
tests; the earlier selected-module HTTP intermittency remains recorded above.
The hosted Windows ownership job now also runs the two native CLI/
ROCm modules so their CMD launchers execute on Windows in CI.

A separate, isolated full application session used actual uvicorn, Chromium,
Gradio client/server transport, command-line cast/script workers and ffmpeg.
All input and output were synthetic: the OpenAI-compatible fixture returned
fixed replies, and TTS returned a one-second sine wave rather than speech.
The observed sequence was:

1. Connection UI displayed model `synthetic-workflow`, reply `pong`, and 162 ms
   round-trip time with the busy button released.
2. Uploaded `The traveler waves. "Hello there."` through the page's file handler;
   cast discovery saved TRAVELER with THE TRAVELER as its alias.
3. Three-pass generation preserved both source entries, attributed narration
   and speech correctly, and the ingest comparison reported source match.
4. Saved NARRATOR and TRAVELER assignments through the guarded revision/book API.
5. Batch rendering deliberately failed the spoken line once. The row exposed
   inline Retry; it succeeded. Render Pending moved from 2 to 1 to 0 and both
   rows became rendered.
6. The browser decoded a saved one-second chunk MP3 into waveform peaks. Clicking at 65 percent
   moved its one-second audio to 0.648812 seconds; the canvas was a visible slider.
7. MP3 merge returned a 41,517-byte artifact; ffprobe measured 2.499875 seconds,
   including the configured inter-speaker pause. Audacity export returned a
   valid 63,298-byte ZIP with project.lof, labels.txt and two PCM tracks.
8. No runtime exceptions or unhandled rejections were observed after the page
   loaded. This observation does not replace the earlier startup-specific checks.

The workflow establishes joined application/transport behavior with synthetic
audio. Graceful-stop warnings required forced cleanup; both owned application
and Gradio processes were reaped. Actual speech quality and source-scan
performance remain unqualified.

A subsequent real-model session used the user-provided Qwen3.8-27B-UD-Q2_K_XL
GGUF and existing llama.cpp b12385 Vulkan build on an AMD Radeon RX 9070 XT. The
server listened only on loopback, with 8,192 context tokens, one slot, GPU
offload and reasoning disabled. Through the actual connection button, Chromium
showed visible Connected/Model present/Reply pong pills, a 263 ms round trip,
and a released busy button. The real cast CLI saved one character; actual
three-pass generation returned three source-preserving entries (narration,
THE TRAVELER speech, narration), with both workers exiting zero. All source was
self-authored synthetic text. This closes the live-LLM check without a model
download or external paid call. The application needed forced cleanup after a
graceful-stop warning; its launcher/child and the temporary model server were
stopped. This did not exercise real speech synthesis or benchmark long books.


## Native Windows continuation (2026-11-14)

Qualification continued on Windows from main
`cd02ff1d475ed016111fb7464cad0f0143072444`, in the isolated
`fix/1938-native-windows-followup` worktree. The live checkout was not edited.
This follow-up does not establish completion of #1938 or #1954.

Production defects reproduced on Windows before repair:

- A same-size rewrite with restored mtime can preserve Windows creation time.
  LoRA evaluation now rehashes the live bundle on Windows instead of accepting
  a stat-authorized cached digest. POSIX retains its existing fast path. The
  new regression failed for each of four bundle files before the repair.
- An automatic prior-take receipt now records a bounded streaming SHA-256
  digest (receipt version 2), and archive capture compares it before reserving
  an archive. A same-size/mtime-restored rewrite failed both JSON and journal
  manager regressions before repair. Hashing observes generation cancellation
  outside the publication owner mutex; that regression also failed before the
  repair. Publication continues to recheck row and book admission.
- UID backfill and literal-narration admission cached only stat identity. Their
  Windows paths now bind cache reuse to content digests. The existing UID test
  and a new same-stat script-rewrite regression both failed before repair.
  The write cache hashes staged write bytes rather than rereading the mutable
  destination; a separate external-edit-at-publication regression failed before
  that repair. Its UID/journal cohort passed 14 tests with one POSIX-only skip.
- Voice profiling now escapes characters unsupported by a legacy redirected
  console while preserving its encoding. Success and failure publication paths
  previously raised UnicodeEncodeError under cp1252; a real narrow-encoder
  regression reproduces the failure and verifies both outcomes.
- ASR discarded a NumPy reference while backend/slice references retained its
  mapping, preventing native directory removal. The owner now explicitly closes
  its mapping on success and model failure before removing temporary PCM.
- Successful checkpoint-tail repair translated LF into CRLF on Windows. The
  replacement writer now retains LF; byte-preservation regressions failed
  before repair and pass after the change.

Version 1 receipts remain readable, including already verified archives.
On Windows, an unarchived v1 receipt cannot authorize generation using stat
identity alone. The error directs the user to disable automatic history and
regenerate that line; a real publication regression covers that remedy. Scope
planning preserves historical v1 metadata without upgrading it to generation
content authority. V2 scope plans compare the stored digest to captured bytes.

Fixture repairs use host paths, the shared native file-lock helper, the actual
Git Bash resolver and host-executable Python launchers. GPU lock workers fail
with their child exit/error instead of skipping after startup failure. UTF-8
Python settings alone do not fix CMD decoding: the generated test launcher now
selects code page 65001 before its Unicode command, then restores the original
console code page while preserving the command exit status. A separate fail-first
assertion covers restoration from OEM 437. Its Unicode-path regression
failed under OEM 437 before repair and passes afterward. Windows CI runs the
CLI/ROCm cohort from OEM 437 and the three automatic take/scope modules natively.
No OS privilege or safety gate was changed.

Measured local focused results after the source fixes use the existing Windows
hook interpreter with PYTHONUTF8=1 and PYTHONIOENCODING=utf-8 (also set by the
hosted native steps):

- 31 take-capture, automatic-history and scope-history tests: zero failures,
  errors or skips.
- 44 CLI, dispatch, Colab, subprocess, streaming-hash and LoRA evaluation tests:
  zero failures, errors or skips.
- 91 speech-receipt, GPU-guard, pipeline and required-input tests: zero failures
  or errors, six existing Linux-tooling skips. Freebuff independently reran this
  cohort without UTF-8 environment variables after the console repair.
- Exact Windows CI CLI/ROCm command under OEM 437: 10 tests, zero failures,
  errors or skips. Cohorts overlap; these are not additive coverage totals.

The stabilized current-base Git Bash readiness shard ran 2,262 tests in
456.346 seconds: zero assertion failures, 16 errors and 240 skips. All 16 error
tracebacks are WinError 1314 at fixture symlink creation, before the containment
assertions execute. The account lacks that capability. No privilege setting,
link semantics, test expectation or skip waiver was changed. The broad native
gate remains red; Linux hosted checks and selected native jobs do not substitute
for it. The release verifier also rejects unit skips, so these 240 platform/tool
skips cannot be represented as a passing release gate even on a symlink-capable
host. Its later gates were not executed after the unit gate failed.

The earlier current-base run had 2,259 tests, 12 failures, 42 errors and 239
skips. It predates the final fixes; the original pre-edit run used a different
shell resolution and is not a comparable baseline. No complete unchanged-base
shard was run. #1954 retains the qualification limit. A new hosted Windows step
runs the CPU-only containment fixtures without suppressing privilege failures;
the PEFT-backed evidence fixtures remain in the full Linux suite and are not
claimed as extra native coverage.

Additional final focused evidence: the profiler module passed 40 tests with no
skips; the UID/journal cohort passed 14 with one POSIX-only skip; and the native
cache/ASR/HTTP/cancellation plus static-file URL cohort passed 24 with no skips.
These overlapping cohorts are not additive totals. Freebuff DeepSeek's final
code-review-20 delta pass confirmed the console and cache repairs. Its nine
angles ran serially in one model, with no independent finder/verifier clearance.
The speculative future non-memmap cleanup concern was not reproduced; the actual
decoder requires a nonempty memmap and success/model-failure cleanup is tested.

The native LM Studio auth-revocation probe failed once in 12 runs and twice in
30 further instrumented runs before reload began. Both latter failures accepted
the HTTP requests in the current fixture process, then reset during the initial
inventory request (WinError 10054). This rules out calling them successful auth
refusal tests; it does not establish the transport root cause. No retry or
weaker assertion was added. The final fixture now uses fully framed HTTP/1.1 keep-alive responses and a
threaded server. Its 30-run revoked-auth probe passed without retries. The
revoked-auth assertion requires an actual CLI load and both authenticated
inventory requests, so an early reset cannot satisfy it. This is measured
fixture behavior; the deeper Windows socket-reset mechanism is not proven.

Additional native fixture repairs retain the production contracts: current
speech receipts remain historical on Windows; unowned batch intents still
refuse automatic recovery; cancellation runs through the actual owned streaming
worker. Known held lock sidecars are compared by exact identity while all data
files are compared by bytes. Real socket sharing exercises the static-file URL
transport without unsupported pass_fds. Legal native filenames preserve URL,
glob, quoting and Unicode cases. The one POSIX SIGINT queue test now declares its
existing Linux-tooling requirement; native cancellation has separate real
process coverage. No symlink privilege failure is suppressed.


### CI corrections after the native follow-up

Hosted head `50e0becd34c0969d0eef5d43ad08df1de03a04e8` exposed two
integration gaps and a Windows path-spelling defect. The rolling Linux shard
ran 2,045 tests with four errors: the Windows receipt fixtures patched the
shared `sys.platform`, making Linux libsndfile attempt its Windows-only wide
filename API. The fixture now replaces only the history module's `sys` binding;
real audio validation and both journal modes remain exercised.

The native containment step could not import the evaluation module without
librosa. Resampling now imports that runtime only when the embedding input
actually needs a sample-rate conversion; containment and manifest refusal
can run with their existing CPU dependencies. A fresh-process regression blocks
librosa imports and checks that the real evaluation module still loads.

The hosted Windows temp directory used a DOS short name in render rows and a
long name in the resolved pilot root. Control admission now uses the shared
plain-path validator before canonicalizing spelling, then retains the bounded,
confined descriptor capture and proof digest check. A fail-first native test
uses an actual DOS path from GetShortPathNameW. The POSIX branch uses an
equivalent plain spelling, and the containment fixture also rejects a symlink
to the same proof-matching control, so resolution cannot hide that alias.

The focused native correction cohort passed 49 tests with zero failures,
errors or skips. An overlapping broader run of 71 tests had zero assertion
failures, four WinError 1314 errors at real symlink creation, and zero skips.
These observations do not clear the broad native readiness limitation above.

The required correction run of real Git Bash `ready.sh --shard 6/6` executed
2,045 tests in 619.217 seconds: 26 failures, 47 errors and 263 skips. Of the
47 error entries, 37 are WinError 1314 at fixture symlink creation. Other
entries expose native filename/path, fixture JSON, cleanup and identity
assumptions; the assertion failures also include archive hashes, receipt
freshness, queue parsing and lexicon cache behavior. These remain unadjudicated
under #1954. No equivalent full unchanged-base shard was run, so this record
does not classify them as pre-existing. The release-verifier receipt is failed
at the unit gate and later gates did not execute. Inventory/native-CI checks
separately passed 26 tests with zero failures, errors or skips. The correction
sources did not change after the readiness measurement.
