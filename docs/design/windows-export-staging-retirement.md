# Windows export-staging durability design

Status: design + qualification harness only. Production cleanup is unchanged.
This document audits the current POSIX retirement protocol and the native
Windows staging prototype, then specifies the missing Windows durability path
and the checks needed to qualify it.

## Current implementation

### POSIX retirement protocol

`app/export_staging_retirement.py::PosixRetirement` is the crash-recoverable
authority for export staging:

- A private `.aa2-export-retirement` directory contains a registry lock and
  bounded 32-hex slots (`MAX_SLOTS=128`, plus one reserved slot).
- Creation writes `allocation.json`, `creation.json` and `owner.json` before
  the stage is exposed. Retirement writes `reserved.json` and then
  `verified.json` around a rename of the public stage into the slot.
- Every path is opened descriptor-relative with `O_NOFOLLOW` /
  `O_DIRECTORY`; directory and owner identities are rechecked through the
  open descriptors before deletion. Child paths are removed through `dir_fd`.
- The owner lock is held through retirement. A live owner is preserved. A
  hard exit releases the kernel lock and leaves a bounded receipt/slot that a
  later recovery can inspect.
- Receipts are bounded JSON, duplicate-key/non-finite rejected, and identity
  bound to the parent, slot, stage and owner. A valid receipt alone never
  grants deletion authority.
- Recovery is idempotent: missing/unknown/malformed members, parent mismatch,
  receipt disagreement, recovery guards and rollback payloads preserve the
  slot and its payload.
- `reclaim_retired_slots` releases and reacquires the registry between slots
  so another reaper can complete a slot without a phantom error.

The public-name cleanup boundary is kept separate: `move()` reserves and
renames into the private slot; only a verified slot is cleaned. The permanent
root is never removed by ordinary cleanup.

### Current Windows prototype

`app/windows_export_staging_directory.py::WindowsStageDirectory` already
provides useful native primitives:

- `CreateFileW` handles for every ancestor and the stage, with
  `FILE_SHARE_READ|FILE_SHARE_WRITE` (delete sharing excluded),
  `FILE_FLAG_BACKUP_SEMANTICS` and `FILE_FLAG_OPEN_REPARSE_POINT`.
- `GetFileInformationByHandle` volume/file-index identity and explicit
  `FILE_ATTRIBUTE_REPARSE_POINT` refusal.
- `SetFileInformationByHandle(FileDispositionInfo)` deletion through the
  admitted handle, including empty directories.
- Owner-file admission, bounded marker reads/writes, recovery guards and
  handle-based payload recursion.

`export_staging.py::_cleanup_admitted` intentionally uses a direct Windows
cleanup path: `_remove_contents(...)` followed by `_finish_removal(...)`.
There is no Windows retirement registry, no `reserved.json`/`verified.json`
state machine, and `reclaim_export_staging` skips private retirement discovery
on Windows (`os.name != 'nt'`).

### Current code references

- `app/export_staging.py:319` selects the Windows public staging allocator;
  `app/export_staging.py:179-195::_cleanup_admitted` then deletes in place.
- `app/export_staging.py:392-421::reclaim_export_staging` only calls the private
  retirement registry when `os.name != 'nt'`.
- `app/windows_export_staging_directory.py:98-132` opens and verifies ancestor
  and stage handles; `:138-186` owner/marker admission; `:212-279` recursive
  handle deletion; `:281-291` delete-on-close directory removal; `:293-299`
  recovery guard.
- `app/export_staging_retirement.py:33-...::PosixRetirement` is the reference
  protocol: `:332::recover_slot`, `:401::cleanup_verified_stage`,
  `:859::move`.
- `app/tests/NATIVE_CHECKS.md` already states that the Windows interrupted
  finalization recovery protocol remains unfinished.

## Confirmed gaps

1. **No durable retirement state.** A hard termination between payload
   deletion, marker removal, owner release and directory deletion leaves a
   partially cleaned public stage. If the marker is already gone, the existing
   marker-based reclaimer has no authority and preserves the directory forever.
2. **No crash-safe finalization.** The current Windows path deletes the marker
   before the owner is fully released. A process death at that boundary can
   leave an unmarked owner/payload directory with no recovery record.
3. **Parent replacement after process death.** Ancestor handles deny rename
   while they are open, but no persistent identity receipt or private slot
   protects a name-based recovery after a hard exit.
4. **Owner lock lifetime is not fully native.** `WindowsStageDirectory` opens
   the owner file through a guard handle, then returns a Python file stream and
   closes the guard. The stream is byte-range locked by `msvcrt`, but the
   design does not yet bind the owner file sharing mode, handle identity and
   deletion denial through the full cleanup transaction.
5. **Recovery-guard durability.** `create_recovery_guard` writes one byte but
   does not flush it. A crash after a failed deletion may lose the guard.
6. **Reparse-point coverage is partial.** Ancestors/stage refuse reparse
   points and top-level discovery skips them, but deleted child reparse links
   and junction/symlink replacement between handle admission and deletion need
   native qualification.
7. **Malformed receipt coverage is absent on Windows.** There is no receipt
   parser/schema for Windows identity tuples, so malformed private metadata
   cannot yet be exercised.
8. **No restart-recovery protocol for a moved stage.** POSIX moves a stage into
   a private slot before deleting payload; Windows has no equivalent move.

These are gaps, not passing behavior. The harness reports the missing Windows
retirement checks as `unsupported`, never `passed`.

## Proposed Windows retirement protocol

Add `app/windows_export_staging_retirement.py` with a `WindowsRetirement`
registry that mirrors the POSIX state machine. Production integration is a
later handoff; this document and harness define the contract.

### Namespace and locks

- Private root: `.aa2-export-retirement` beside the export parent, created with
  an owner-only ACL (not a POSIX mode).
- `owner.lock` in the private root is locked with `LockFileEx`
  (`LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY`). A live registry
  owner is preserved; recovery never waits while holding a stage owner.
- Slots: bounded 32-hex names, `MAX_SLOTS=128`, plus one reserved slot.
- Every directory handle is opened with
  `FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT` and share mode
  `FILE_SHARE_READ|FILE_SHARE_WRITE` (no `FILE_SHARE_DELETE`).
  `FILE_ATTRIBUTE_REPARSE_POINT` on any ancestor, stage, slot or owner path is
  refused.
- File identity: `(dwVolumeSerialNumber, nFileIndexHigh/Low)` from
  `GetFileInformationByHandle`. Receipts store that tuple; the POSIX dev/ino
  fields are not reused as Windows identity. The tuple is not durable on every
  filesystem (FAT/exFAT can change IDs, ReFS/SMB may not be unique), so the
  native implementation must gate on filesystem capability and retain the
  marker token as a content backstop. A capability check, not an assumption,
  decides whether a volume is eligible.
- Private directories and files receive an explicit protected DACL granting
  only the current user (plus the minimum system principal needed by the
  deployment). `FILE_PERSISTENT_ACLS` is checked; filesystems without ACL
  support are declared unsupported rather than silently downgraded.

### Receipts and state machine

Creation must also be reserved before the public stage is exposed, matching
POSIX `allocation.json`/`creation.json`/`owner.json`. Without a creation
receipt, a hard termination between directory creation and marker/owner write
leaves an unmarked public directory that no Windows reclaim path can identify.

Receipts are bounded (8 KiB), duplicate-key and non-finite rejecting JSON with
an exact schema. The required fields are:

- `schema`, `original_name`, `parent_identity`, `stage_identity`,
  `owner_identity`, `marker`.

The retirement state sequence is:

1. `reserved`: write `reserved.json` durably before moving the public stage.
2. Move the stage directory to `slot/stage` with a no-replace rename.
3. `verified`: re-open the moved stage by handle, verify its identity and
   marker, then write `verified.json`.
4. Clean payload through the stage handle while retaining marker/owner.
5. Move the owner file into the slot as `owner.retained`, delete the marker,
   and delete the empty stage directory by handle.
6. Delete `verified.json`, then `reserved.json`, then the empty slot.

Any mismatch preserves the slot and payload. Pending receipt files are never
deletion authority. Recovery classifies `reserved_unmoved` (the POSIX name),
`moved_unverified`, `verified`, `removed` and `mismatch_preserve`. It also has a
creation-recovery branch for missing `reserved.json` that mirrors POSIX
`recover_creation_slot`; it does not treat every missing retirement receipt as
safe deletion.

### Native primitives

- **No-replace rename:** use `SetFileInformationByHandle(FileRenameInfo)` on
  the admitted handle, with `ReplaceIfExists=FALSE` and `RootDirectory` set to
  the destination directory handle. This is the only handle-relative,
  no-replace primitive compatible with the stage handle's delete-denying share
  mode. `MoveFileExW` is not an acceptable fallback: it opens the source by
  pathname and fails against the protocol's own stage handle; a path-based
  fallback would also reopen the parent-replacement race. A failure must leave
  both names recoverable; the native harness must verify the chosen primitive,
  not assume it.
- **Delete:** `SetFileInformationByHandle(FileDispositionInfo)` on a handle
  opened with `DELETE`; recurse only through handles. Delete-on-close leaves
  the name in a delete-pending state until the last handle closes, so the
  protocol must close children/stage handles in order and classify
  `ERROR_DELETE_PENDING`/`ACCESS_DENIED` as a preserved retry state, never as
  removed. Read-only file attributes are cleared through `FileBasicInfo` before
  disposition, or the slot is preserved with the attribute recorded.
- **Owner lock:** open the owner file with `GENERIC_READ|GENERIC_WRITE|DELETE` and
  share mode excluding delete. `LockFileEx` requires read/write access and
  `FlushFileBuffers` also needs write access; a DELETE-only handle cannot hold
  the advertised byte lock. `DELETE` is needed for the handle-relative owner
  rename while that same handle and its lock remain held. The future protocol
  must use that handle for locking, flushing and rename, or establish compatible
  sharing for any additional handle; the current prototype does not do this.
  While that handle has `DELETE` access, a separate probe must include
  `FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE` in its share mode and
  request only access permitted by the original handle's sharing flags. This
  interaction requires native testing
  ([CreateFile sharing rules](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea)).
  Hold the same handle through the full transaction and use a nonblocking
  exclusive lock for live-owner probes. Byte locks are
  process-scoped and advisory; lock range, offset, and registry/stage lock
  ordering must be fixed, and a non-cooperating memory-mapped writer remains
  outside the guarantee.
- **Owner retention:** mirror POSIX `owner.retained`: retain the owner handle
  and receipt in the slot until the stage and marker are gone, then remove the
  retained owner before the empty slot. A crash after owner movement is a
  distinct recoverable state, not an unknown-member preservation dead end.
- **File admission:** marker, owner, receipts and retained owner must be
  regular, single-link files with no reparse attribute. Hard links are refused;
  `FILE_FLAG_OPEN_REPARSE_POINT` applies to files as well as directories.
- **ACL check:** verify the private root/slot DACL through the opened handle and
  refuse a writable foreign ACE. FAT/exFAT and ACL-less SMB shares are
  unsupported for the private registry; the public staging path may still be
  reclaimable but not under the private durability guarantee.
- **Reparse ancestors:** refusing every reparse ancestor rejects legitimate
  OneDrive/Known-Folder-Move and volume-mount layouts. The implementation must
  either resolve and document an approved staging root or declare those
  layouts unsupported in configuration. The harness must exercise a junction,
  not only `os.symlink`.
- **Marker:** create with `CREATE_NEW`, write bounded bytes, flush with
  `FlushFileBuffers`, and recheck handle/named identity before publishing.
- **Receipts:** write a `.pending` file with `CREATE_NEW`/`FlushFileBuffers`,
  then rename no-replace. Never overwrite a published receipt.
- **Recovery guard:** write and flush the guard before returning a failed
  deletion to the caller.

### Durability boundary

`FlushFileBuffers` is used on writable file handles (marker, owner, receipts,
retained owner). Windows provides no portable, documented directory-entry fsync
equivalent for every filesystem; `FlushFileBuffers` on a directory handle is
not a general guarantee. The protocol must state this limitation clearly:
process-death restart recovery is in scope; power-loss durability of directory
entries is **not** claimed until qualified per filesystem. The harness records
this as a limitation, not a pass. A torn state in which the rename persists but
the receipt directory entry does not is explicitly unqualified.

### Recovery rules

- Open the private root by handle, verify parent identity and owner-only ACL.
- For each slot: read `reserved.json`; never trust it alone. Verify parent,
  stage and owner identities by handle.
- If the stage is present, open it by handle, refuse reparse points, verify
  marker and owner lock. A live owner means preserve.
- If `verified.json` is absent, write it only after re-verifying the moved
  stage. If it disagrees with `reserved.json`, preserve.
- If payload deletion fails, flush a recovery guard and preserve the slot.
- If the slot is empty of a stage and all receipts are valid, remove metadata
  and the empty slot; unknown members preserve.
- Do not delete a pathname after a handle check. Delete through the admitted
  handle. This is the Windows analogue of the POSIX descriptor-relative rule.

## Qualification harness

`tools/windows_export_staging_qualification.py` is standalone and does not
modify production cleanup. It records each check as `passed`, `failed` or
`unsupported` with a reason. On Linux it runs the POSIX reference checks and
marks native Windows checks unsupported. On Windows it runs the current
Windows staging checks; private-retirement checks are unsupported until an
adapter is bound. Module presence alone is not sufficient. `--require-windows` fails when
any required native Windows/durability check is unsupported; POSIX-reference
checks skipped on Windows do not count against that strict mode.

Checks:

| Check | Linux | Native Windows |
|---|---|---|
| hard termination while creating a stage | POSIX full and partial creation | current Windows full-stage creation; partial unsupported |
| hard termination after retirement move | POSIX verified/unverified move | unsupported: no Windows retirement |
| restart recovery by a new process | POSIX | current Windows creation recovery |
| restart after retirement move | POSIX | unsupported: no Windows retirement |
| live owner preservation | POSIX | current Windows creation owner |
| parent replacement / rename denial | POSIX registry parent mismatch | native handle/rename denial |
| reparse point / symlink refusal | POSIX private slot symlink | native junction |
| malformed receipts/markers | POSIX truncated/duplicate-key receipt | current Windows marker |
| failed deletion preserves payload | POSIX fault injection | Python fault injection + native read-only |
| private Windows retirement state machine | unsupported | unsupported until an adapter is bound (module presence alone is not enough) |
| power-loss directory durability | unsupported | unsupported |
| native handle/delete ABI | unsupported | required open/identity mapping/delete smoke; no rename/lock coverage |

The harness uses only synthetic files and subprocesses. It does not start the
application, load a model, use the GPU, or touch user project data.

Current Linux evidence: 12 POSIX checks passed, 0 failed, 17 native Windows /
unimplemented checks unsupported; `qualified=false`. `--require-windows` exits 2
because required Windows checks are unsupported. The existing staging suites
also pass: 57 tests (`tests.test_export_staging`,
`tests.test_project_export_staging`). The private-retirement checks are
deliberate placeholders: the future implementation handoff must bind an adapter
before they can pass. The harness does not silently treat the current Windows
public cleanup as the missing retirement protocol.

## Remaining limitations

- No native Windows runner is available in this environment, so no Windows
  kernel behavior is claimed.
- The private Windows retirement implementation is not part of this handoff.
  Placeholders currently include the four private-retirement checks, the
  Windows partial-creation check, the two Windows finalization checks, and the
  power-loss check. The private-retirement placeholders need an adapter; the
  finalization and partial-creation checks also need production protocol work.
- `windows_partial_creation_recovery` is required and currently unsupported:
  current Windows creation has no durable allocation/creation receipt.
- The harness's Windows fault injection is a Python mock of
  `remove_contents`; the native read-only-attribute check is a separate
  preservation probe, not a general ACL/delete-pending qualification.
- The harness does not exercise `FileRenameInfo`, `LockFileEx`, the
  `ERROR_DELETE_PENDING`/`ACCESS_DENIED` retry branch, or ACL failure because
  the Windows retirement protocol does not exist yet. The native ABI smoke
  check covers CreateFileW identity mapping and FileDispositionInfo
  delete-on-close only.
- Directory-entry power-loss durability and ACL equivalence to POSIX
  owner-only modes require native evidence.
- The existing POSIX protocol remains the reference; this design does not
  change it.
