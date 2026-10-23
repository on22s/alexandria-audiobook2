# Sound-design cue foundation (#1444)

This foundation defines immutable cue recipes, read-only exact-frame projection,
a private asset-import store, and an owned-worker PCM mixer. Cue editing,
reviewed mix approval and delivery are not yet connected to a production
interface. Existing dry exports remain unchanged. The full enhancement remains open.

## Saved-book storage

Cue registries retain immutable history and one explicitly selected revision
per cue. The boundary accepts at most 2 MiB, 1,024 historical versions and 128
selected cues. It rejects duplicate JSON fields, nonfinite values, invalid text,
unknown structures, conflicting origins, missing parents and foreign selections
before publication. Checksums detect changed metadata and do not authenticate it.

The active manifest is `.sound_design_cues.json`. Named snapshots use private
companions such as `.example.sound_design.json`; these do not reserve or hide
previously valid book names such as `example.sound_design`. Save captures exact
validated bytes using the existing native book identity. Checkpoint-prefix
snapshots also retain exact history; old script/take bindings stay stale until
explicitly reviewed, rather than being rebound to the partial script. Load restores its own
companion in the existing book transaction. A legacy snapshot without a companion
clears active cues. Explicit source selection also clears valid active cues.
Malformed or unsafe metadata refuses the operation and remains available for
inspection; named snapshots preserve cue history across script repairs, where
old source/take bindings become stale rather than being silently rewritten.

Loading a book still discards its runtime chunks under the existing contract.
Historical cue metadata is retained without automatic reanchoring or approval.
The later cue interface must show missing/stale dialogue bindings and require
proof or explicit reanchoring before use.

Upload selection runs on a worker with the existing kernel task barrier and
book admission. The local task mutex is released during slow metadata work.
Cancellation waits for that worker to finish. Completed uploads become
library-owned when made publicly reusable, before active-book selection.
They and their published provenance remain quota-accounted and available even
if selection fails or cancellation makes its outcome uncertain. Cleanup removes
only the request's private stages. See [upload completion and recovery](wiki/Script-Generation.md#upload-completion-and-recovery)
for retained crash artifacts and filesystem requirements.

## Identity and selected sources

`get_sound_design_cue_version` returns a detached schema-1 document with a
cue identity, optional parent version, recipe and SHA-256 version. The digest
detects changed metadata; it is not authentication, media validation or approval.
Keep historical versions separately from the explicitly selected version.

Each recipe binds the existing book identity, exact raw-source and script
fingerprints, stable chunk UID, generation-input fingerprint, selected audio
fingerprint and opaque audio revision (nonempty UTF-8 text up to 128 characters).
Legacy revisions may be null, but still
require the selected audio fingerprint. Recipes select one asset identity,
content fingerprint and PCM shape. Callers must verify those facts against
admitted immutable media before rendering; a user-supplied digest proves nothing
about a file on disk.

Capture the book, script, ordered selected dialogue and native PCM plan together.
Use the project's existing generation-input helper, source snapshots and exact
export planner rather than deriving a parallel definition of generation or
timing. The dialogue selections must correspond one-for-one with the trimmed
sources passed to the PCM writer. Never obtain a fresh plan independently of
the selected media snapshot.

## Frame projection

`get_sound_design_cue_plan` accepts one explicitly selected revision per cue.
Anchors use the start or end of a native source-frame range plus an authored
integer-frame offset. The authored frame rate must match the dialogue output
clock. Asset resampling uses the existing PCM converter's frame-count rule;
assets cannot silently promote the dialogue clock or downmix stereo into mono.
Display milliseconds only as a view of the authoritative frame positions.

A changed book/source/script, missing UID, generation change, selected take
change or different output clock makes the projection stale. It never searches
for identical text or audio to relocate a missing UID. Empty dialogue sources,
out-of-timeline placement, invalid loop geometry and incompatible channel
selection remain unresolved. Both incoming and outgoing scope-crossing cues
require review; wholly disjoint cues are explicitly outside the requested scope.

Native asset duration uses the selected asset only. A fixed duration can trim
it, but extension requires explicitly selected ambience and a finite, positive
crossfade shorter than the converted source. The planner validates that recipe;
it does not render or qualify a loop. Gain, ducking and fades are bounded explicit
metadata. Rendering must later detect clipping in a wide accumulator before
quantization, preserve dry media, and verify cancellation and current sources
immediately before publication.

`can_project` describes metadata geometry only. It does not permit delivery or
replace asset verification, mix audition, explicit approval or publication fences.
The returned `projection_revision` binds the entire captured book, ordered
dialogue selections, PCM geometry, scope and selected cue versions. A changed
preceding clip or pause can move an unchanged cue version; future preview/cache
and approval adapters must compare this overall revision as well.

## Private asset store

`SoundDesignAssetStore` retains original uploads and canonical signed 32-bit
little-endian PCM in `.sound_design_assets` under the project root. Assets are
independent of the active book and saved-book selection. Ready assets are retained
indefinitely so historical cue references remain usable. The asset API provides
upload, listing, explicit incomplete-import cleanup and verified auditions.
The Editor’s Sound asset library panel provides import/cancel, refresh, short
audition and explicit incomplete-import cleanup. Cue editing is not yet connected.

Admission fully decodes WAV, FLAC, MP3 or OGG through an owned FFmpeg process,
preserving the probed rate and mono/stereo geometry. It rejects nonfinite or
out-of-range decoded samples. Canonical identity includes the exact PCM hash,
frame count, rate, channels and sample width; deduplication also checks the
original bytes and conversion version. `open_canonical` verifies both retained
files and holds their descriptors through use. Consumers must finish this
context successfully before publishing a result.

Default limits are 256 assets, 256 MiB per original, 1 GiB per decoded asset and
4 GiB total. Import reserves the original size and the full decoded allowance
before writing media. Incomplete imports retain that charge, including after
cancellation or process death; ready records charge actual retained bytes.
Lowering a configured limit does not erase or invalidate existing records:
inspection and incomplete-import recovery remain available, while new imports
must fit the lower limit.
Import and explicit incomplete-asset cleanup share one kernel lease, so cleanup
cannot delete a live import. Callers must run these operations on a worker and
retain ownership until the worker and decoder finish.
Contending mutations wait at most ten seconds before refusing; retry after the
active import finishes. Upload cancellation remains responsive while waiting
for either lease, without releasing another operation's ownership.

`purge_incomplete` removes only the selected incomplete record's two owned media
files. Failed cleanup leaves its reservation until a successful retry. It
refuses ready assets. Unknown artifacts, aliases, malformed metadata and damaged
ready records stop mutations and preserve evidence. There is currently no
automatic repair for those cases: retain the directory for inspection rather
than deleting or rewriting its manifest. An interrupted atomic manifest write
can leave an unknown `.publication-*` file requiring such inspection.

Tests cover exact integer PCM, independent compressed-audio comparisons,
quota refusal, cancellation, process death, concurrent import/recovery and
retry after interrupted cleanup. Native CPU CI runs on Linux, macOS and Windows;
user-workflow and listening qualification remain separate. The MP3 block-decoding
defect #1536 was addressed for demonstrated take-history and stereo training
callers in #1540. Asset import already uses independent bounded FFmpeg decoding.

`POST /api/sound_design/assets/import` accepts an audio file. Its worker holds a
separate upload lease through copying and import. Private application stages in
`.sound_design_uploads` share a 256 MiB budget and a 16-file cap, including files
retained after process death. Those retained stages require inspection; they are
not silently expired. These limits apply to application staging, separately from
the HTTP server's multipart parsing. Cancellation waits for owned work before
releasing its stage; a late or lost response can leave a complete ready asset,
which remains visible in the library. Refresh before retrying an uncertain import.

`GET /api/sound_design/assets` lists retained records and storage charges.
`POST /api/sound_design/assets/{asset_id}/purge_incomplete` performs explicit
recovery and refuses ready records. `GET /api/sound_design/assets/{asset_id}/audition`
requires the selected canonical `sha256` and accepts a nonnegative `start_frame`.
It verifies both retained files, then returns up to ten seconds of exact canonical
PCM in a WAV with caching disabled. Positions beyond the asset are refused.
The upload route dispatches owned work to a worker; the synchronous read and
cleanup handlers run in the framework's worker pool. An audition
does not select a cue, approve a mix or change a book.

## Owned PCM mixing

`write_sound_design_pcm` is a synchronous worker component. Its caller captures
the dry `PCMSourceRange`, ordered selected dialogue, book identity and PCM plan
together, owns an empty named readable/writable regular output file and separate
canonical staging directory, and retains them through completion. The output
must be inside that staging directory; its path must identify the opened file.
Anonymous or integer-named descriptors are refused. Neither output nor temporary converted
assets may pollute the immutable asset inventory. The current existing PCM
converter requires a little-endian host.

The mixer regenerates the exact cue projection and refuses stale, unresolved or
scope-crossing cues before conversion. Canonical asset descriptors must agree
with immutable store facts; conflicting descriptors for the same identity are
refused. Asset conversion retains the reviewed dialogue clock/channels and uses
32-bit intermediate PCM. A fixed crop retains only its required converted prefix;
repeated uses share one converted copy. All canonical bytes observed by conversion
are hashed, including the unused tail of a crop, and checked against the selected
fingerprint. Dry content and selected canonical content are checked again before
returning facts. Retained original files are not rewritten.

Rendering uses at most 8,192 frames per block. The default logical staging budget
is 4 GiB, counting the complete retained dry store, unique converted prefixes,
and scoped output together. This is separate from immutable asset retention and
does not represent filesystem allocation overhead. At most 128 simultaneous
source voices are supported, including overlapping cycles within ambience. A
conservative interval proof avoids costly preflight when possible. If that proof
is insufficient, exact bounded metadata blocks check actual per-frame counts;
a valid schedule is not rejected solely because per-cue peak densities differ
from simultaneous density. A shared processing deadline (300 seconds by default,
at most 3,600) and cancellation are checked between bounded preflight, conversion
and rendering operations. These checks are cooperative: a blocked synchronous
filesystem operation retains its owner until it returns; it is not forcibly
abandoned when the deadline expires.
Limits refuse work explicitly without changing authored timing or levels.

All spans are half-open frame intervals. Cue-relative time continues across
blocks. The strongest explicitly requested dialogue attenuation applies during
each active cue's full span, independent of its gain, fades or silent samples;
overlapping duck factors do not multiply. Cue gain and fades affect the cue only.
A zero-frame fade is absent; a one-frame fade silences its endpoint. Longer
fade-in/out windows have exact zero/one endpoints over their authored lengths.

Only explicitly extended ambience loops. For native length `N` and authored
crossfade `X`, cycle starts are `N-X` frames apart. Incoming cycles use weights
`(q+1)/(X+1)` in their first `X` local frames, except the first cycle. Every
cycle uses `(N-q)/(X+1)` in its final `X` frames. Overlapping incoming/outgoing
windows multiply. Each frame's cycle weights are then divided by their positive
sum to form a partition of unity; for crossfades longer than half the asset,
the raw weights need not sum to one.
This supports crossfades longer than half the asset without changing the period
or adding signal-dependent gain normalization to the rendered mix. Constant/impulse
controls establish timing and level accounting, not arbitrary ambience quality.

The mixer accumulates float64 samples before integer conversion, refuses
nonfinite values and positive/negative overload, and checks signed integer bounds
after round-to-nearest, ties-to-even. Integer scaling uses `2^(8*sample_width-1)`.
It never limits or normalizes a clipping mix. Raw output retains the reviewed
signed PCM width; untouched dry samples remain byte-identical. The returned
schema-1 facts include exact scope/layout/frames/bytes, output SHA-256, scoped dry
and complete dry-store fingerprints, logical staged bytes, and aggregate absolute
pre/post-quantization peaks. These sample peaks are not true-peak measurements.

Success requires complete output readback and source/lease fences. A failure,
cancellation or deadline produces no success facts and leaves the borrowed
output invalid. The caller must retain ownership and charge any partial output
until it is explicitly discarded or recovered; the mixer does not close borrowed
streams. Never approve or publish on an exception. After successful rendering,
the caller must still fence the current book/source selections and obtain explicit
review approval before delivery through existing completed-output validation.

## Required integration and qualification

Saved-book load discards runtime chunks. Neither a raw-source cache key nor a
temporary book-selection token provides saved-book isolation. Reloaded cues
require provable source lineage or explicit reanchoring when runtime IDs change.

Remaining work includes cue editing and scope-undo integration; connecting the
bounded mixer to captured dry exports and reviewed scene spans; a constrained
review interface; dry/mixed audition; explicit candidate selection and delivery;
completed-file validation; cancellation/crash/source-change controls; and
shareable audio with measured resources and listening outcomes. Later score
rendition lineage remains a separate subtask, with no provider selected.

The targeted tests exercise real small PCM assembly and exact end frames,
selection changes, repeated-content identity, saved-book identity, both scope
crossings, invalid metadata and cancellation. They do not qualify models,
native synthesis, listening quality, large-book resource use or platform support.
