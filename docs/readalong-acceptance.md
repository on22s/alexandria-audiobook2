# Read-along acceptance evidence: #1720 and #1779

This is evidence for the combined implementation in one feature branch.
R1/R6 (EPUB conformance and source coverage), URI encoding, R4 (frame
clock/publication), R3 (standalone player), R5 (original retention), and
R2 (app/tasks/downloads) each received independent DeepSeek approval
following Crouton implementation and repair. Parent follow-ups are included
below. **Independent whole-feature implementation review: APPROVED.**
Release/readiness and hosted checks remain separate gates; implementation
approval is not a merge or release-readiness claim.

## Acceptance mapping

| Requirement | Implementation and bounded evidence |
| --- | --- |
| #1720 versioned audio/timeline/text/section/bookmark contract | `aa2-readalong-sidecar` v1; SHA-256 of the actual fresh MP3 plus promoted PCM frame layout; bounded Python and player validators |
| Reuse timing without invented precision | Cache-only word timing; `aligned-estimated`, `chunk-estimated`, `missing`, `ambiguous`; true trim offsets; no ASR/inference request |
| Local seek, highlighting, section navigation and portable bookmarks | Actual registered player handlers tested in a stateful Node DOM; both file orders, wrong audio, async replacements, half-open boundaries and Unicode offsets |
| Explicit disclosure choice and portable output | Include-text checkbox off by default; portable files omit local paths, credentials and voice/project internals; ordinary audio export remains available |
| Known-bad controls | Wrong/changed audio; malformed/oversized metadata; missing timing/clip data; changed source/rows/take/book/settings; pause/trim/layout drift; transaction faults |
| Costs and compatibility | Exact synthetic measurements below; Node/EPUBCheck evidence is separate from unrun browser/reader/listening qualification |
| #1779 retain original EPUB | Exact archive bytes retained beside extracted TXT/provenance under upload quota; canonical reuse requires a fresh descriptor hash; Calibre shares the upload path |
| Preserve source structure and metadata | Spine order, text, cover/resources, stable IDs and metadata preserved; EPUB2 NCX converted to EPUB3 nav; output adds SMIL Media Overlays |
| Justified text/audio mapping | Complete sequential normalized coverage (NFKC, casefold, word tokens); many chunks to one segment supported; unmappable edits and one chunk spanning source segments refused |
| Conforming references and overlay clocks | Correct manifest `media-overlay` attribute, SMIL clocks/durations, modified metadata, encoded URI boundaries; official EPUBCheck results below |
| Real Result-tab flow | Shared integrity approval; exact-owner CPU tasks/cancel/status; bounded typed polling and book-reset fencing; receipt-bound artifact and matching-MP3 downloads |

## Parent combined regression run

From `app/`, using the existing Windows virtual environment and:

```text
PYTHONUTF8=1
PYTHONIOENCODING=utf-8
PYTHONDONTWRITEBYTECODE=1
python -B -m unittest
  tests.test_readalong_defects tests.test_readalong_sidecar
  tests.test_readalong_r4 tests.test_readalong_r2
  tests.test_readalong_player_js tests.test_epub_readaloud
  tests.test_epub_readaloud_conformance tests.test_epub_readaloud_uri
  tests.test_epub_retention tests.test_epub_retention_r5
  tests.test_upload_publication_source tests.test_script_upload_quota
  tests.test_source_upload_publication tests.test_source_upload_visibility
  tests.test_epub_extraction_report tests.test_calibre_import
  tests.test_calibre_routes tests.test_export_frame_timeline
  tests.test_export_pcm_plan tests.test_export_publication_transaction
  tests.test_export_task_owner tests.test_cpu_task_claims
  tests.test_project_scope_exports tests.test_chapter_export
  tests.test_export_limits tests.test_epub_package
  tests.test_epub_publication tests.test_field_help tests.test_ui_help_js
```

The displayed command is line-wrapped for reading; module arguments belong
to one invocation. Result: **354 ran, 353 passed, 1 skipped, 0 failures,
0 errors, raw exit 0; 137.783 seconds**. The skip is the existing POSIX
directory-descriptor publication test on Windows; it is not counted as a
pass. This is the named integration/regression set, not full discovery or
hosted CI. Corrupt-audio/JSON diagnostics belong to intentional negative
fixtures and did not change the raw test outcome.

R2 focused suite after the parent follow-ups: **19 passed, 0 skipped,
0 failures/errors, raw exit 0; 13.995 seconds**. Those tests build real
artifacts, use real download handlers and HTTP routes, and control only
specific fault/timing boundaries. The Node portion drives actual app
handlers and separately checks HTML IDs/reset hooks; it does not parse the
page or operate a native media pipeline.

After regenerating API snapshots with the repository generator, the API
contract check passed. The separate API-contract/HTML-wiring regression
set ran **21 tests, 21 passed, 0 skipped, raw exit 0**.

After rebasing onto the translation-preview merge, the 29-module set above
plus workspace, local assets, three translated-edition modules and API
contract regressions ran **480 tests: 479 passed, 1 existing POSIX-only skip,
0 failures/errors, raw exit 0; 145.377 seconds**. This replaces neither the
earlier run nor the independent review count. The first rebased pass had
one deterministic-snapshot failure from CRLF checkout bytes; the official
API generator restored LF and refreshed five route ordinals before the
final rerun. The separate contract suite passed all 17 tests.

## Discriminating repair evidence

| Area | Before repair | Repaired behavior/evidence |
| --- | --- | --- |
| EPUB conformance | Official checker rejected manifest overlay properties, ISO-style durations and malformed SMIL | Correct associations/clocks/nav/modified metadata; fresh valid output checked externally |
| Source coverage | Container text and interior missing rows could be lost | Every spoken node accounted for once; missing/corrupt admitted audio or incomplete mapping refuses publication |
| URI escaping | Valid space-containing source produced 3 RSC-020 errors | Decoded ZIP paths and separately encoded URI paths/fragments; literal percent, Unicode, spaces, nested/root OPF and regeneration covered |
| Audio timeline | Old MP3 could be combined with a new timeline; raw-layout frame arithmetic drifted | Fresh encode through shared producer/quality admission; ranges match independent promoted PCM plan and PCM byte count |
| Publication failure | Multiple output renames had no recoverable transaction | Reverse rollback restores old outputs; first build leaves none; rollback faults retain owned recovery files; parent test pins EPUB third-target receipt failure |
| Player | New real-handler harness: 23 ran, 16 failed, 7 passed | Load ordering, verification gates, generation fences, Unicode and bookmarks repaired; parent added max-count/prototype/import/embedded bounds regression |
| Retention | 16 ran, 10 failed, 6 passed | Exact edition lock, pre-reserved group quota, truthful visible recovery, bounded metadata inventory |
| Retained hash cache | One real HTTP regression, 2 failing subtests (in-place and replacement with preserved mtime) | Fresh bounded descriptor SHA, 409 on restored foreign bytes; prior group remains byte-identical |
| App integration | 10 ran, 4 failures, 4 errors, 2 passed | Owned start/status/cancel, late terminal results, bounded retries/reset fences and bound download bytes; independent 17-test R2 reproduction before parent follow-ups |
| Unreadable download settings | Parent HTTP regression: both sidecar and EPUB returned 500 | Both return 409 with settings/rebuild detail; restoring readability serves the same admitted artifact bytes |

Earlier RED counts describe runs captured before their respective repairs;
they are not reruns against the final implementation. Parent take-revision
and book-switch HTTP tests already passed before the settings-error fix and
now pin those previously reviewer-only probes in the committed suite.
An initial parent test invocation used a wrong relative path and failed to
load the new methods; that invocation is not counted as repair RED evidence.

Independent slice review reproduced: R1/URI 68 passed; R4 136 passed;
R3 52 passed before parent follow-ups; R5 146 ran (145 passed/1 POSIX skip),
followed by the narrow L1 recheck (43 plus 3 helper tests passed); R2
17 focused tests, 141 compatibility tests and 43 publication/owner/scope
tests passed. These overlapping runs must not be added together as a unique
test total. The R2 reviewer also encountered a pre-existing Windows symlink
privilege error in a separate export-staging test; that run is not green
evidence and was not hidden by a retry. An earlier implementation retention
run had a Calibre flake that passed on retry; the independent 146-test run
and parent combined run did not reproduce it.

## Exact synthetic cost and official EPUBCheck

Parent measurement on this checkout, November 7, 2026: five synthetic rows,
47,992 PCM frames at 24,000 Hz (about 2 seconds), real WAVs, real ffmpeg
MP3 encoding/quality admission and a valid PNG cover. Measurements are one
run each on this machine; no full-book performance extrapolation.

| Artifact | Wall seconds | Artifact bytes | Matching MP3 bytes |
| --- | ---: | ---: | ---: |
| Sidecar, text omitted | 0.672960 | 1,648 | 33,453 |
| Sidecar, text included | 0.546485 | 1,726 | 33,453 |
| EPUB with embedded audio | 0.581803 | 10,973 | 33,453 |

Original synthetic EPUB: 1,767 bytes. EPUB receipt: 583 bytes. ZIP
compression makes this repetitive tiny audio fixture especially compact;
that ratio is not representative of speech libraries. Text inclusion added
78 bytes in this fixture; word timing remained estimated, with no inference.

Official checker: **EPUBCheck 5.3.0 / Java 8**. Fresh source and freshly built
output both returned **exit 0, zero errors**. Source had one PKG-010 warning
and output had two: decoded archive filenames contain spaces. Generated
references are correctly percent-encoded; those interoperability warnings
are disclosed, not counted as zero-warning results. Prior independent
ordinary EPUB3, EPUB2 conversion, regeneration, root OPF and special-character
fixtures also passed the checker within their recorded slice scope.
The whitespace-ID fixture (`caf? sec`) proves serialization only; that source
ID is invalid XHTML and is not EPUB conformance evidence.

## Compatibility, costs and unrun gates

- The whole EPUB, including its audiobook audio, is loaded into memory and
  hash/structure-validated on every download. Large books can require
  substantial memory. MP3 downloads hash a pinned open descriptor before
  streaming; they avoid a second whole-audio memory copy.
- The EPUB embeds its audio; downloading its separate matching MP3 is
  optional. Media Overlays reader compatibility remains unqualified.
- Each build fresh-encodes audio on CPU. Concurrent sidecar/EPUB exports
  share the current MP3; the last successful writer may invalidate the other
  artifact, which then refuses download until rebuilt.
- Ordinary multi-file renames are recoverable on exceptions, not crash-atomic.
  A hard kill can leave mismatched outputs; receipts refuse them and a fresh
  build is required. Regenerating an EPUB may retain orphaned old embedded
  audio, adding archive size.
- Different retained editions with identical extracted text explicitly
  refuse reuse (409). Retention has no automatic deletion; the inventory
  identifies groups for manual backup/storage cleanup, not an app delete UI.
- SMIL clips that collapse to zero or overlap after nearest-millisecond
  rounding refuse; sub-millisecond spans that round to 1 ms may be accepted.
  MP3 codec delay/padding is not claimed equal to PCM frames.
- Missing/unsupported timing stays missing/ambiguous. Exact normalized source
  coverage is not pronunciation, listening quality or literal text parity.
- Direct download links can display a JSON refusal for a stale artifact;
  users must rebuild it. The local player verifies hashes but buffers local
  files subject to its caps (64 MiB sidecar, 2 GiB audio).
- Node DOM tests do not establish browser decoding, native event behavior,
  playback timing, accessibility or listening quality. No browser, native
  reader, Thorium/Storyteller, listening/pronunciation, full release or hosted
  CI qualification is claimed by the measurements above.

The final independent whole-feature review verified all 32 frozen files,
the staged scope and generated contracts/inventory. It reproduced 218 tests:
217 passed, one existing POSIX-only directory-descriptor test skipped on
Windows, zero failures/errors, all commands RC 0. It approved the combined
implementation with no material blocker. This is separate evidence from the
parent's 354-test run above; the totals are not added together.

The subsequent broad Windows readiness run exposed a missing workspace task
navigation entry for each new export. The existing registered-task test was
RED; both tasks now map to the Audio/Result tab that contains their controls.
The local asset registry test also reproduced the omitted read-along script
entry. Its explicit allowlist now includes the new script and asserts that
the page loads it. The asset, workspace and R2 suites then ran 35 tests:
35 passed, zero skips, zero failures/errors, RC 0. These small follow-ups
postdate the independent freeze.

The broad Windows readiness run passed compilation and test inventory, then
reported failures and platform skips, including WinError 1314 while existing
chapter-download fixtures create symlinks. It was deliberately interrupted
before updating the checkout; the verifier recorded unit-gate failure and
raw exit 1, with no final test total. It is diagnostic evidence, not a clean
local release gate; its initial run predates the navigation follow-up.
Hosted checks remain pending and will be recorded when completed.


## Hosted CI fixture follow-up

The first hosted run at `ceb98107` failed unit shard 1 (4,120 tests run,
two failures) and shard 3 (4,357 tests run, seven errors). The failures
were stale integration expectations for the new task-observer notices and
retained EPUB file. The rollback errors were fault injectors that omitted
the descriptor-relative `os.replace` keyword arguments used by Linux staging.

On unchanged source, the two integration modules reproduced 22 tests with
two failures (RC 1). A new descriptor-forwarding regression reproduced the
`src_dir_fd` TypeError locally (one test, one error, RC 1). After the fixture
repairs, the task-reattachment, upload-event-loop and R4 modules ran 46 tests:
46 passed, zero skips, zero failures/errors, RC 0. The upload test now also
asserts exact retained archive bytes; the rollback/cancel/stale/recovery
assertions remain intact. No production implementation changed.

The descriptor-forwarding regression uses a recording OS boundary on Windows;
actual POSIX staging qualification still requires the new hosted run. The
earlier broad Windows readiness failure remains unresolved release evidence.
