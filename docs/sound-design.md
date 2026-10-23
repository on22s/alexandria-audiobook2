# Sound-design cue foundation (#1444)

This foundation defines immutable cue recipes and read-only, exact-frame
projection. It does not yet add a production sound-design interface, import
assets, approve media, render loops or mixes, or change existing dry exports.
The full enhancement remains open.

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

Saved-book load currently discards runtime chunks. Persist an active cue manifest
and an exact named-library companion using the existing book transaction; a
missing companion must clear active cues. Neither a raw-source cache key nor a
temporary book-selection token provides saved-book isolation. Reloaded cues
require provable source lineage or explicit reanchoring when runtime IDs change.

Remaining work includes immutable asset admission and reference protection;
saved-book/undo/recovery integration; bounded overlap mixing; actual gain,
ducking, fades, explicit ambience loops and reviewed scene spans; a constrained
review interface; dry/mixed audition; explicit candidate selection and delivery;
completed-file validation; cancellation/crash/source-change controls; and
shareable audio with measured resources and listening outcomes. Later score
rendition lineage remains a separate subtask, with no provider selected.

The targeted tests exercise real small PCM assembly and exact end frames,
selection changes, repeated-content identity, saved-book identity, both scope
crossings, invalid metadata and cancellation. They do not qualify models,
native synthesis, listening quality, large-book resource use or platform support.
