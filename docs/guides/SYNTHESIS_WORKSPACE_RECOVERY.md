# Disposable synthesis workspace recovery

Owned built-in local CustomVoice synthesis windows on Linux now register their
working WAVs before writing audio. When a process dies, the next idle application
startup or audio-task admission reclaims verified disposable files. This also
applies when reusable segment recovery is disabled. A retry can therefore remove
abandoned working copies without deleting saved takes or reusable cache evidence.

For the participating built-in local CustomVoice Project route, the raw parent
WAV also has its own registration, created before allocating the file and retained
through encoding and final selected-take publication. Inner windows use a separate workspace;
finishing those windows does not retire the raw parent. Raw-parent ownership is
independent of optional synthesis windows and reusable recovery. Default renders
with both features off still capture the live task, book, UID and input authority
needed for disposable cleanup. Their reusable cache, speech-input capture and
publication-proof policies remain unchanged; ownership alone enables none of
those optional features.

The same captured authority also covers built-in local CustomVoice Project
batch outputs. Each participating row has a separate parent workspace allocated
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
  scratch are disposable. Unknown files, symlinks, hardlinks, changed directory
  identities and invalid or missing registry authority are preserved.
- A crash between directory creation and committing its identity can leave an
  empty ambiguous directory and a reserved slot. No provider may write audio
  before identity commitment. This slot stays reserved for explicit inspection;
  cleanup does not infer authority from its name.
- Existing unregistered `.synthesis-render-*` and `.synthesis-join-*` directories
  remain untouched. Inspect known paths with the application stopped before any
  manual removal; never remove saved takes or recovery artifacts based on a
  shared filename prefix.
- Automatic reclamation currently covers known built-in owned Linux CustomVoice
  renders. Other voice categories, Windows/macOS, overridden providers and direct
  unleased Python callers retain their normal temporary-file behavior. They have
  no claimed hard-crash reclamation qualification yet.
- Batch rows outside the participating local CustomVoice path, direct unleased
  calls, and staging in `voicelines` or `.chapter-export-*` remain outside this
  raw-parent registration. Existing unregistered parent files are preserved;
  this change does not authorize
  filename-based cleanup of historical files.

Warnings identify preserved registrations or unavailable registry authority.
Retain those paths and metadata for diagnosis. The application does not reset a
corrupt registry, recursively sweep the project, or fall back to untracked spill
when the participating registry is full.
