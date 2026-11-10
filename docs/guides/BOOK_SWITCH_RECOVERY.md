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
| `inspect` | Read-only report: the pending journal, each file's expected and current state, the working folder, and every set-aside folder with its receipt. Add `--hash` to hash each file and report whether it holds the journal's `old` bytes, its `new` bytes, or `neither`. Timestamps alone can miss a same-size rewrite: FAT/exFAT drives keep 2-second times. `--no-lock` reads while the app is busy, as a best-effort snapshot. |
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
the journal moved, the receipt can stay `prepared` although nothing is left to
do: `inspect` shows `journal_held: true` for such a folder.

## Trust boundary

Receipts are not signed. Anyone who can write the data folder can already
change the book's files, so the tools trust that folder. A receipt's `forced`
flag is honoured only once its working folder has actually been moved, so a
hand-written receipt cannot turn ordinary recovery into a forced quarantine.
