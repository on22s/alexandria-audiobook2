[English](BOOK_SWITCH_RECOVERY.md) | [中文](BOOK_SWITCH_RECOVERY_CN.md) | [日本語](BOOK_SWITCH_RECOVERY_JA.md)

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

A switch changes the book's files in a fixed order. On Linux and macOS (where
the filesystem can swap two names in one step, the "v4" journal) it:

1. stages every new file in a private `.book-switch-<id>/` folder,
2. writes the journal `.active_book_transaction.json` (phase `publishing`),
3. swaps each staged file into place, one file at a time,
4. marks the journal `committed`,
5. deletes the staging folder and then the journal.

Every book operation runs recovery first (`recover_book_state_locked` in
`app/book_state_transaction.py`). Here is what you find after a crash at each
point, and what happens next time the book opens. The function in brackets is
the one that handles that case.

**Before the journal exists (during step 1 or 2).**
- On disk: a `.book-switch-<id>/` folder, and possibly a half-written
  `.active_book_transaction.json.new-<id>`. Every book file is untouched.
- Next open: nothing to undo, so the book opens as it was. `inspect` lists the
  folder under `orphaned_staging` and the half-written file under
  `journal_temporaries`; the Home page shows their count and size. Nothing is
  deleted for you. [`recover_book_state_locked`, `_get_leftover_staging_entries`]
- You: once you are sure no switch is running, delete them by hand if you want
  the space back.

**After the journal, before any file was swapped.**
- On disk: the journal says `publishing`; every book file is still the old one.
- Next open: each file is checked against the journal, the staging folder and
  journal are removed, and the book opens unchanged. [`_recover_book_exchange_journal`]
- You: nothing.

**Part-way through the swaps (step 3).**
- On disk: some files are new, some old. Each swapped file's old version (or,
  for a newly created file, a 64-byte placeholder) sits in the staging folder.
  A file being created can briefly show that placeholder at its real name.
- Next open: every swapped file is swapped back, after both sides are checked
  again, so the book returns exactly to how it was before the switch. Files the
  switch was creating are gone again. [`_recover_book_exchange_journal`, `_undo_book_exchange`]
- You: nothing.

**All files swapped, but the journal not yet `committed`.**
- On disk: every book file is new; the staging folder holds the old versions.
- Next open: treated the same as the previous case. The switch is rolled back
  and the book opens on its old files, because until `committed` is written
  nothing promised the switch had finished. The one exception is a journal where
  every row is a replacement, every file proves its new contents and no old
  copy is left to restore: that is finished forward instead. A mix of finished
  and unfinished files that cannot be restored is refused rather than opened as
  a half-old, half-new book. [`_recover_book_exchange_journal`]
- You: nothing, unless it refuses ("cannot be completed consistently"). Then
  run `inspect`, and `quarantine --accept-current-files` keeps the files as they
  are now.

**After `committed`, during cleanup (step 5).**
- On disk: the journal says `committed`; every book file is new; some of the
  staging folder may remain.
- Next open: each book file is checked against its new contents, then only
  what the switch itself created is deleted and the journal is removed. Anything
  in the staging folder that can't be proven to be ours is moved, together with
  the journal, into `.book-switch-quarantine-<hash>-<nonce>/`, and the book
  still opens with the new files. [`_recover_book_exchange_journal`, `_quarantine_book_transaction`]
- You: nothing. Delete the quarantine folder when you no longer want the evidence.

**During a quarantine (its `receipt.json` still says `prepared`).**
- On disk: the quarantine folder, with the staging folder and journal moved
  into it or still at the book root.
- Next open: if the journal is still at the root, the quarantine is finished,
  but only if everything still matches what the receipt recorded; otherwise
  recovery refuses and keeps everything. If the journal was already moved, the
  book opens, and the receipt is marked `completed` once the moved journal's
  size and hash and the moved staging folder's identity are checked again. A
  receipt that fails that check stays `prepared` and nothing moves.
  [`_quarantine_book_transaction`, `_complete_interrupted_quarantines`]
- You: nothing in the usual case. A receipt left at `prepared` is evidence that
  something changed; `inspect` shows it.

**Another program changed a book file while the switch was running.**
At any step after the journal, if a book file is neither the switch's old nor
its new version, recovery refuses: it does not swap, delete or set aside a file
it didn't write. The book stays closed until you choose, with `inspect` and
then `quarantine --accept-current-files`. (One exception: a Windows-style
switch that fails before moving any of its files, and finds another program's
change while giving up, removes its journal anyway. Nothing of the switch's
was applied, the other program's change stays, and the app log gets a warning
starting "Book switch ... was retired without recording recovered".)

**Windows, and filesystems that cannot swap names (the "v3" journal).**
These publish each file with two renames: the old file to `old-<i>` in the
staging folder, then the new one into place. There are no placeholders, so a
crash between the two renames leaves the book file briefly *missing*. Recovery
follows the same rules as above: it checks the saved old file, moves any
installed new file aside to `discard-<i>`, and puts the old one back, or
finishes forward if the journal already says `committed`.
[`_recover_book_journal`; `apply_book_state_locked` picks v3 whenever swapping
is unavailable, which includes every Windows switch.]

All of these follow one rule, from #1816: recovery never renames over a name it
can no longer prove is its own. When it can't tell which file is ours, it keeps
the journal, the staging folder and both files, and refuses to open the book
rather than guess.

## Readers during a switch

While a file is being created or removed on Linux or macOS, its name can
briefly hold a 64-byte placeholder: the text `alexandria-book-placeholder-v1 `,
a random id and a newline (`app/book_placeholder.py`). It is never real
content. Most of the app reads the book only after taking the book lock
(`ensure_book_state`), which finishes or refuses any pending switch first, so it
never sees a placeholder. The few readers that run without the lock check for it:

- **Downloads and exports** answer 409 `SWITCH_IN_PROGRESS` ("A book switch is
  in progress. Wait a moment and download again.") instead of serving the
  placeholder. The route checks the name (`_require_export_download`,
  `app/routers/editor.py`), and the response checks the file it actually opened
  (`ExportFileResponse`, `app/export_download.py`), so a switch that starts in
  between is still caught. ZIP downloads skip placeholder files, and answer 409
  if every file was one (`build_zip_download`).
- **Chunk reads** (`ProjectManager._read_chunks`, `app/project.py`) refuse with
  "A book switch is in progress; chunks are not available yet". They never parse
  the placeholder, rename it as corrupt or regenerate over it, and they check
  the bytes they read rather than the name, so a swap in between is caught too.
- **Scope exports** treat a placeholder `state.json` as "no inventory yet"
  (`is_scope_export_historical`, `app/project_scope_exports.py`).

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
