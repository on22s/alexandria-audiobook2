# Sound-design cue foundation (#1444)

This foundation defines immutable cue recipes and read-only, exact-frame
projection. It does not yet add a production sound-design interface, import
assets, approve media, render loops or mixes, or change existing dry exports.
The full enhancement remains open.

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

## Required integration and qualification

Saved-book load discards runtime chunks. Neither a raw-source cache key nor a
temporary book-selection token provides saved-book isolation. Reloaded cues
require provable source lineage or explicit reanchoring when runtime IDs change.

Remaining work includes immutable asset admission and reference protection;
cue editing and scope-undo integration; bounded overlap mixing; actual gain,
ducking, fades, explicit ambience loops and reviewed scene spans; a constrained
review interface; dry/mixed audition; explicit candidate selection and delivery;
completed-file validation; cancellation/crash/source-change controls; and
shareable audio with measured resources and listening outcomes. Later score
rendition lineage remains a separate subtask, with no provider selected.

The targeted tests exercise real small PCM assembly and exact end frames,
selection changes, repeated-content identity, saved-book identity, both scope
crossings, invalid metadata and cancellation. They do not qualify models,
native synthesis, listening quality, large-book resource use or platform support.
