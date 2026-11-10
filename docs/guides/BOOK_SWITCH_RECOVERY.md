# Interrupted book saves and switches

Loading a saved book, or saving the active one, replaces several files at once:
the script, the voice settings, chapter files and so on. The app does this as a
journaled transaction so that a crash, power loss or full disk can never leave
a book half-old and half-new. This guide covers what happens after such an
interruption, and what to do when the app cannot finish the job by itself.

## What happens automatically

Before every book operation that reads or changes the book, the app looks for
an interrupted switch: a journal named `.active_book_transaction.json` in the
data folder, with its working folder `.book-switch-<id>/` beside it. (The Home
page's status reading is the exception: it does not recover, so it can show a
switch that `finish` would still complete.)

- **Recovery finishes or rolls back the switch** when every file is where the
  journal says, with the bytes it recorded. This is the usual case, and you see
  nothing.
- **Auto-quarantine** applies when every book file still matches the journal but
  the working folder does not: a staged file changed, an unknown file appeared,
  or a cleanup stopped partway. The working folder and the journal are moved
  into `.book-switch-quarantine-<journal hash>-<nonce>/` and the book opens.
  Nothing is deleted.
- **Otherwise the switch is refused** and the book does not open. This happens
  when a book file itself changed, a folder was swapped, or the journal is
  damaged. The app will not guess which version is right.

When a book cannot open for this reason, the Home page names the commands
below, in the order to try them: `inspect`, then `finish`, and only if `finish`
refuses, `quarantine --accept-current-files`.

## Leftovers from a crash

A crash can leave two things that no journal claims:

- `.book-switch-<id>/`: the working folder of a switch that died before its
  journal was published. It can hold a whole staged copy of the book.
- `.active_book_transaction.json.new-<id>`: a journal publication temporary
  that was written but never became the journal.

`inspect` lists these as `orphaned_staging` and `journal_temporaries`, with
their entry count, bytes and newest timestamp. The recovery summary reports
only the counts and total bytes (`leftovers`), never names or paths, and
reports `leftovers: null` while the app is busy (the app may be staging right
now). A name that only looks similar, a link, or the working folder the
pending journal itself names is never reported as an orphan.

The app never deletes these leftovers: a pre-journal folder carries no proof
that it is ours, so reclaiming it is your decision. After checking that the
book opens and the folder is not a live switch, delete it yourself, as you
would a set-aside folder. `inspect --no-lock` can show an entry that is really
a live switch's staging; re-run `inspect` under the lock before deleting it.

## What a crash leaves behind

On a filesystem that can exchange two names (the v4 protocol), a switch writes
its journal once, exchanges each replaced file with its staged copy, writes
`committed`, then cleans up. The table lists every point between those steps.
"Recovery" means `recover_book_state_locked` in `app/book_state_transaction.py`,
which every book operation runs before it reads or writes the book
(`ensure_book_state`). The function named at the end of each row is what decides
that case.

| Crash point | On disk | Next open | The operator |
|---|---|---|---|
| Before the journal exists, during staging | `.book-switch-<id>/` holding `new-<i>` replacements and, for creations, `old-<i>` placeholders; every target still has its old file; no journal. A crash exactly during the journal write also leaves `.active_book_transaction.json.new-<id>`. | There is nothing to restore: with no journal, recovery acts on no book file (it only finishes an interrupted quarantine receipt). `inspect` reports the folder as `orphaned_staging` and the temporary as `journal_temporaries`, with counts and bytes; the Home summary shows the same totals, or null while another operation is busy. Nothing is reclaimed. | The book opens on its old files. Check that the folder is not a live switch, then delete it yourself. (`recover_book_state_locked`, `_get_leftover_staging_entries`) |
| After the journal, before the first exchange | Journal `phase: publishing`; every target holds its old file; the workspace holds each staged replacement and each creation's placeholder. | Each target is proven against the journal's original bytes and identity, placeholders are deleted only when their nonce proves them, `recovered` is written, then the workspace and journal are removed. The book opens unchanged. (`_recover_book_exchange_journal`) | Nothing. |
| Mid-exchanges: some rows swapped, others not | Journal still `publishing`. A swapped replacement row has the new bytes at its target and the original at `new-<i>`; a swapped creation row has the new bytes at its target and its placeholder at `new-<i>`; a swapped removal row has its placeholder at the target and the original at `old-<i>`; untouched rows are as above. | No single state covers every row, so recovery rolls back each swapped row with the complementary exchange (re-proving both names first), writes `recovered`, then cleans up. Every row ends at its pre-switch state: the old bytes, or absent for a creation. (`_recover_book_exchange_journal`, `_undo_book_exchange`) | Nothing. |
| After every row is published, before the journal says `committed` | Journal still `publishing`; every target holds its new state (new bytes, or absent for a removal) and the workspace holds what each row displaced: the original, or a creation's placeholder. | Recovery treats this exactly like the previous row: each row is rolled back with its complementary exchange, then `recovered` and cleanup. The new files are discarded with the workspace; the book opens on its pre-switch files, with creations absent again. Recovery only writes `committed` on this path when every row is a replacement that proves its new bytes and no slot is left at all (for example a slot already gone); otherwise it rolls back, and a mix of published and unpublished rows is refused. (`_recover_book_exchange_journal`) | The book opens on its pre-switch files. If recovery refuses instead ("cannot be completed consistently"), the way out is `quarantine --accept-current-files`, which keeps whatever the targets hold now. |
| After `committed`, during cleanup | Journal `committed`; every target holds its new state; the workspace may already be gone, partly deleted, or fully present. | Each target is re-proven against its new bytes and identity, then only workspace entries proved ours are removed and the journal is unlinked. The book opens with the new files. A workspace entry that can no longer be proved (an edited backup, an unknown file) is auto-quarantined with the journal into `.book-switch-quarantine-<journal hash>-<nonce>/` and the book still opens with the new files. (`_recover_book_exchange_journal`, `_quarantine_book_transaction`) | Nothing, unless a target no longer proves its new state: then recovery refuses instead, the book does not open, and `inspect` says which file changed. The set-aside folder can be deleted once you no longer want the evidence. |
| During quarantine, receipt still `prepared` | `.book-switch-quarantine-<journal hash>-<nonce>/receipt.json` says `prepared`. The workspace may not have moved yet, or may have moved, or both workspace and journal may have moved. | With the journal still at the root, the next book operation resumes the same quarantine and finishes it, but only if the targets and workspace still prove what the receipt names; if they no longer do, it refuses and keeps everything. With no journal at the root, recovery re-proves the held journal's size and hash and the moved workspace's identity, and only then writes `completed`; an unprovable receipt stays `prepared` and nothing moves. (`_quarantine_book_transaction`, `_complete_interrupted_quarantines`) | If the journal was already moved, the book opens. If it was not and the quarantine can no longer prove itself, the book still refuses; `inspect` lists the folder, and `journal_held: true` means the receipt could not be completed. Leave it as evidence. |

**v3 note (Windows, or a filesystem that cannot exchange two names).** A v3
journal publishes each row with two renames: the target to `old-<i>`, then the
staged `new-<i>` to the target. There are no placeholders, so a crash between
those two renames leaves the target *absent* with the staged copy still in
`new-<i>`; a reader that runs without the book lock sees a missing file, not a
switch in progress. The journal is written the same way, so recovery behaves
like the rows above with one difference: it proves the backup at `old-<i>`,
moves any installed replacement aside to `discard-<i>`, and restores the backup
with a no-overwrite move; a journal that already says `committed` is finished
forward (`_recover_book_journal`). `apply_book_state_locked` chooses v3 whenever
exchange is unavailable, which includes every Windows switch.

The rule behind every row is #1816: recovery never renames over a name whose
authority it has lost. When it cannot prove which object is ours, it keeps the
journal, the workspace and both objects, and refuses the book rather than
guessing.

## Readers during a switch

While a switch holds the book lock, a name it is creating or removing can hold
a 64-byte placeholder instead of a file. It is the text
`alexandria-book-placeholder-v1 `, a nonce and a newline, defined once in
`app/book_placeholder.py`. It is never content: a reader that runs without the
lock must treat it as "not there yet".

- **Exports and downloads.** `_require_export_download` in
  `app/routers/editor.py` answers 404 when the export is missing and 409 with
  `SWITCH_IN_PROGRESS` ("A book switch is in progress. Wait a moment and
  download again.") when the name holds a placeholder. That is only the early
  answer: `ExportFileResponse.__call__` in `app/export_download.py` checks the
  descriptor it actually serves, so a switch landing after the name check still
  gets a 409 instead of 64 bytes named `audiobook.mp3`. ZIP downloads skip
  placeholder members and answer 409 when every member was one
  (`build_zip_download`); `/api/export_zip` treats a placeholder export as not
  existing, so it answers 404 when nothing else is there.
- **Chunk reads.** `ProjectManager._read_chunks` in `app/project.py` raises
  `ValueError("A book switch is in progress; chunks are not available yet")`
  when `chunks.json` or its status journal holds a placeholder. It never parses
  it, renames it aside as corrupt, or regenerates over it. The bytes are tested
  after being read through one descriptor (`_read_chunks_snapshot`), so a name
  swapped between the check and the read is still caught.
- **Book state.** A placeholder `state.json` means the source-scope inventory
  does not exist yet: `is_scope_export_historical` in `app/project_scope_exports.py`
  returns False rather than parsing it.
- **Book-state reads generally** take the book lock first through
  `ensure_book_state`, which recovers or refuses the pending switch before
  reading; those callers see the recovered book or the refusal described above,
  not a placeholder. The readers that deliberately run without the lock are the
  ones that need the checks listed here.

A placeholder is short-lived while the switch is healthy: it exists for the
duration of one row's publication, and a switch that dies is handled by the
next open as the table above describes.

## The recovery command

Run it in a terminal from the Alexandria folder, using the app's Python (under
Pinokio, the environment in `app/env`):

```
python app/book_state_recovery.py inspect --hash
python app/book_state_recovery.py finish
python app/book_state_recovery.py quarantine --accept-current-files
python app/book_state_recovery.py migrate-legacy --trust-journal-hashes
```

| Command | What it does |
|---|---|
| `inspect` | Read-only report: the pending journal, each file's expected and current state, the working folder, every set-aside folder with its receipt, and any crash leftovers (`orphaned_staging`, `journal_temporaries`). Add `--hash` to hash each file and report whether it holds the journal's `old` bytes, its `new` bytes, or `neither`. Timestamps alone can miss a same-size rewrite: FAT/exFAT drives keep 2-second times. `--no-lock` reads while the app is busy, as a best-effort snapshot; an entry it shows may be a live switch's staging. |
| `finish` | Runs the same recovery the app runs, now. |
| `quarantine --accept-current-files` | Keeps the book's files exactly as they are now, even though they do not match the journal. The working folder and the journal are moved aside, nothing is deleted, and the book opens. Use it after `inspect` shows the current files are the ones you want. |
| `migrate-legacy --trust-journal-hashes` | For a journal from an older version of the app (v1), which recorded each file's hash but not its identity. With the flag, a file holding the hash its phase recorded is trusted. When every file matches, the working folder and the old journal are moved into `.book-switch-migration-<hash>-<nonce>/` and the book opens. A save interrupted mid-publish matches neither state: each mismatched file is listed and nothing moves. |

Common options: `--json` prints one JSON object, `--data-dir DIR` names the
data folder (by default the app's own: `ALEXANDRIA_DATA_DIR`, else the
Alexandria folder), and `--wait SECONDS` waits for another book operation to
finish. Without `--wait`, a busy app is reported at once instead of hanging.

| Exit code | Meaning |
|---|---|
| 0 | Done, or nothing was pending. |
| 1 | System error, including an uncertain journal write (evidence kept; retry). |
| 2 | Usage error, for example a missing confirmation flag. |
| 3 | Refused; the message says why and nothing was moved. Also returned when `inspect` cannot read the journal. |
| 4 | Busy: another book operation holds the lock. |
| 5 | A legacy v1 journal is pending; use `migrate-legacy`. |

## Set-aside folders

Each `.book-switch-quarantine-*` or `.book-switch-migration-*` folder holds:

- `receipt.json`: `prepared` while the move is under way, `completed` after.
  It also records the journal's hash and phase and the reason for the move:
  `forced` is true for an operator quarantine, and a migration adds
  `kind: legacy-migration` and `trust: journal-digests`.
- `journal.json`: the journal itself, moved rather than copied, so it is
  byte-identical.
- `workspace/`: the working folder, moved, when one existed.

The app never deletes these folders. Delete one yourself once you no longer
need the evidence; `inspect` lists them all. If a move was interrupted while
the journal was still in the data folder, the next recovery (quarantine) or the
same command (operator quarantine, migration) finishes it. If it stopped after
the journal moved, the next book operation (or `finish`) completes the receipt,
but only after re-checking that `journal.json` still has the receipt's exact
size and hash and that `workspace/` is still the folder the receipt names. If
that cannot be proved, the receipt stays `prepared`, nothing is moved, and
`inspect` shows `journal_held: true` for it.

## Trust boundary

Receipts are not signed. Anyone who can write the data folder can already
change the book's files, so the tools trust that folder. A receipt's `forced`
flag is honoured only once its working folder has actually been moved, so a
hand-written receipt cannot turn ordinary recovery into a forced quarantine.
