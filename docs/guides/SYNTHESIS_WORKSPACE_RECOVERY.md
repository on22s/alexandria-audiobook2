# Disposable synthesis workspace recovery

Owned local synthesis windows on Linux now register their working WAVs before
writing audio. Participating categories are custom, clone, design, LoRA and
ensemble. When a process dies, the next idle application
startup or audio-task admission reclaims verified disposable files. This also
applies when reusable segment recovery is disabled. A retry can therefore remove
abandoned working copies without deleting saved takes or reusable cache evidence.

For participating local Project routes, the raw parent WAV also has its own
registration, created before allocating the file and retained
through encoding and final selected-take publication. Inner windows use a separate workspace;
finishing those windows does not retire the raw parent. Raw-parent ownership is
independent of optional synthesis windows and reusable recovery. Default renders
with both features off still capture the live task, book, UID and input authority
needed for disposable cleanup. Their reusable cache, speech-input capture and
publication-proof policies remain unchanged; ownership alone enables none of
those optional features.

For the original Project chunk encoder, the same parent registration also owns
the encoded intermediate and its encoder scratch. It does not allocate another
workspace or reduce the parallel-parent budget. MP3 encoding and WAV fallback
stay inside that workspace until the existing selected-slot publication moves
the result into `voicelines`. A hard exit before publication therefore leaves
owned scratch rather than an unregistered `.chunk-*` or `.render-*` file.
The encoder reads the raw parent through a checked, no-follow descriptor.
Its PCM, output and error files are created exclusively; the child checks their
registered inode identities before reading or truncating them. WAV fallback
also verifies that its completed filename still names the file it wrote.
The completed inode remains open through selected-slot publication. Both
renames use pinned source and destination directories, and the selected destination must name
that same inode before metadata changes. A replacement is preserved as evidence
and cannot become a successful take merely because it decodes as valid audio.
The child acknowledges its flushed output version in the existing bounded
encoder metadata file. WAV fallback verifies the copied bytes through held
descriptors, in cancellable blocks outside the publication locks. Relative
imports are frozen before the child enters pinned scratch; its launch cwd is
captured before startup so a later cwd change cannot redirect those imports.
Overridden chunk readers, encoders or file-publication helpers retain their existing staging
behavior; they are not handed this additional encoding authority.

The same captured authority also covers participating local Project batch
outputs for custom, clone, design, LoRA and ensemble. Each participating row has a separate parent workspace allocated
before dispatch and retained through all batch finalizers. Native list batching,
length sorting, sub-batching and OOM size reduction remain unchanged. Internal
routes bind the project directory, index and live claim to the registered path;
speech-input capture uses that same path. Unmapped rows keep their normal output
paths. Overridden batch writers do not receive registered output routes.

The registry is `.synthesis-workspaces-v1/registry.json` under the project data
root. It records opaque workspace IDs, hashed claim/book provenance, directory
identities and the declared window count. It contains no source text. Cleanup
uses the existing kernel task leases and book lock; a live task or busy book
defers maintenance. Normal cancellation cleans the same owner's disposable
workspace, while a revoked owner leaves it for a later idle operation.

The separate `.synthesis-recovery-v1` cache stores reusable verified segments;
clearing that cache does not authorize deletion of other files. Completed parent
publication and saved-take attachment retain their existing ownership checks.
Workspace cleanup failure is logged and cannot turn a published parent into a
failed render.

A zero-window row declares a parent-only workspace; ordinary window rows still
declare two through 1,024 windows. Older application versions that reject a
zero-window row preserve the registry rather than cleaning it. Use a version
that understands parent rows to resume automatic maintenance.

New encoding-enabled parent rows have an explicit `encoding: true` marker,
committed together with both directory identities before any writer runs. Their
existing private `join` directory is the encoder's temporary directory. Old
five-field rows keep their original strict join-file rules. An older client
that does not recognize the encoding marker preserves the registry and refuses
new registered allocations; use an encoding-aware version to finish idle
maintenance before downgrading. Saved takes and reusable recovery evidence are
separate from these disposable rows.

Retirement uses the canonical book root and directory identity captured during
registration. Retargeting a book-root symlink cannot redirect cleanup into a
different book. A replaced root or revoked owner preserves the original charged
registration for admitted idle recovery; cleanup does not search for moved
directories or force deletion after authority is lost.

## Limits and preserved evidence

- At most 256 workspaces are registered, with at most 1,024 windows each. This is
  a disposable-storage admission limit; requested parallel worker settings remain
  unchanged. Exhaustion refuses a participating render before allocating more
  working audio.
- Parent and inner-window registrations share that 256-workspace budget. An
  active participating windowed Project render can occupy two slots. Capacity
  refusal never falls back to an unregistered parent file.
- Participating batch rows each occupy one parent slot, plus an inner-window
  slot while a split parent is being rendered. All parent slots for the current
  batch attempt must be admitted before provider work. OOM retries use fresh
  scopes and registrations; prior attempt paths are not reused.
- Only the registered window files, private assembled parent and fixed join
  scratch, plus their declared WAV design publication staging files, are
  disposable. Unknown files, symlinks, hardlinks, changed directory
  identities and invalid or missing registry authority are preserved.
- Encoding-marked parent rows additionally declare fixed encoded, PCM and
  encoder-error filenames. Their pinned private join directory delegates at most
  16 regular, single-link, current-user temporary files, totaling at most 8 GiB,
  to the encoder. Nested directories, aliases, excessive scratch and unsafe
  entries preserve the entire registration. Every declared child is checked
  before the first deletion; the marker does not adopt historical files.
- A crash between directory creation and committing its identity can leave an
  empty ambiguous directory and a reserved slot. No provider may write audio
  before identity commitment. This slot stays reserved for explicit inspection;
  cleanup does not infer authority from its name.
- Existing unregistered `.synthesis-render-*` and `.synthesis-join-*` directories
  remain untouched. Inspect known paths with the application stopped before any
  manual removal; never remove saved takes or recovery artifacts based on a
  shared filename prefix.
- Automatic reclamation currently covers known unmodified local categories on
  Linux (custom, clone, design, LoRA and ensemble). Windows/macOS, overridden
  providers and direct unleased Python callers retain their normal
  temporary-file behavior. They have no claimed hard-crash reclamation
  qualification yet.
- Ensemble registration conservatively requires the ordinary custom, clone,
  design and LoRA member writers to be unmodified, even when an overridden
  category is not in that ensemble. Otherwise it retains ordinary temporary
  paths. A participating ensemble cannot exceed the 1,024-window limit; it
  refuses rather than bypassing bounded storage with an unregistered fallback.
- Ensemble member scratch and Project design previews use registered window
  workspaces. The Designer tab's previews under `designed_voices/previews` remain
  a separate staging path and are not covered by this registry.
- Batch rows outside the participating local category paths, direct unleased
  calls, legacy staging in `voicelines`, and chapter exports/PCM exports/merges
  outside the chunk encoder remain outside this registration. Existing
  unregistered parent files are preserved; this change does not authorize
  filename-based cleanup of historical files.

Warnings identify preserved registrations or unavailable registry authority.
Retain those paths and metadata for diagnosis. The application does not reset a
corrupt registry, recursively sweep the project, or fall back to untracked spill
when the participating registry is full.
