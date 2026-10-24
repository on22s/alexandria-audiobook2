# Browser smoke tests

`smoke_play_sequence.py` opens the Editor in headless Chromium against a real,
isolated server and checks that **Play Sequence** plays every row in order with
the pause the server's playback plan gives after each row (measured from each
clip's `ended` to the next clip's `play`).

The unit suite already runs the frontend's actual functions in a Node VM; this
is the one place they run in a browser, with real media events and timers.

## Isolation

- A temporary `ALEXANDRIA_DATA_DIR`, seeded by `seed_book.py` with a synthetic
  three-row book (text is invented, audio is sine tones). No real book, voice
  or setting is read or written, and the directory is removed afterwards
  (`--keep` keeps it, with `server.log`).
- The server runs on a free `127.0.0.1` port in its own process group with its
  output written to a file, and the whole group is stopped at the end.

## Running

Playwright goes in its own environment, not `app/env` (the server still runs
under the app interpreter that `ready.sh` uses):

```bash
uv venv .e2e-venv && uv pip install --python .e2e-venv -r requirements-test.txt
.e2e-venv/bin/playwright install chromium-headless-shell   # once
.e2e-venv/bin/python tools/e2e/smoke_play_sequence.py
```

Exit status: 0 all checks pass, 1 a check failed, 2 it could not run (the
message names the server log). It is not part of unit discovery, so CI test
counts and the unit inventory are unaffected.

Timing bounds: a planned pause must measure between 50 ms early and 400 ms
late; a zero pause under 250 ms. On a desktop the 600 ms fixture pause measured
601-603 ms over five runs.
