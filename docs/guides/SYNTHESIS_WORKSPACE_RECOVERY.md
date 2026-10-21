# Disposable synthesis workspace recovery

Owned built-in local CustomVoice synthesis windows on Linux now register their
working WAVs before writing audio. When a process dies, the next idle application
startup or audio-task admission reclaims verified disposable files. This also
applies when reusable segment recovery is disabled. A retry can therefore remove
abandoned working copies without deleting saved takes or reusable cache evidence.

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

## Limits and preserved evidence

- At most 256 workspaces are registered, with at most 1,024 windows each. This is
  a disposable-storage admission limit; requested parallel worker settings remain
  unchanged. Exhaustion refuses a participating render before allocating more
  working audio.
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

Warnings identify preserved registrations or unavailable registry authority.
Retain those paths and metadata for diagnosis. The application does not reset a
corrupt registry, recursively sweep the project, or fall back to untracked spill
when the participating registry is full.
