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
