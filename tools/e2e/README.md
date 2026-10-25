# Browser smoke tests

`smoke_play_sequence.py` opens the Editor in headless Chromium against a real,
isolated server and checks that **Play Sequence** plays every row in order with
the pause the server's playback plan gives after each row (measured from each
clip's `ended` event to the next clip's `playing` event).

The unit suite already runs the frontend's actual functions in a Node VM; this
is the one place they run in a browser, with real media events and timers.
This checks browser playback scheduling with synthetic tones; it does not
qualify speech quality or audible timing on physical output hardware.

## Isolation

- A temporary `ALEXANDRIA_DATA_DIR`, seeded by `seed_book.py` with a synthetic
  three-row book (text is invented, audio is sine tones). No real book, voice
  or setting is read or written, and the directory is removed afterwards
  (`--keep` keeps it, with `server.log`; failures also retain diagnostics).
  The seeder refuses a populated or symlinked target directory.
- The seed and server use the application's existing subprocess ownership.
  The server runs on a free `127.0.0.1` port with output written to a file;
  cleanup retires its descendants even if the server leader has exited.

## Running

Playwright goes in its own environment, not `app/env` (the server still runs
under the app interpreter that `ready.sh` uses):

```bash
uv venv .e2e-venv && uv pip install --python .e2e-venv -r requirements-test.txt
.e2e-venv/bin/playwright install chromium-headless-shell   # once
.e2e-venv/bin/python tools/e2e/smoke_play_sequence.py
```

Exit status: 0 all checks pass, 1 a check failed, 2 it could not run.
Failures retain their temporary directory and available diagnostics; a seed
failure can occur before a server log exists. It is not part of unit discovery, so CI test
counts and the unit inventory are unaffected.

The playback plan must be available, structurally valid, cover all fixture
gaps and preserve the explicit 600 ms and zero pauses. The final row has no
following gap and is absent from the plan. Missing or failed
plan requests make the run incomplete; they cannot produce PASS.

Timing bounds are coarse: a planned pause must measure between 50 ms early
and 400 ms late; a zero pause from 0 to under 250 ms. Every row must produce
`playing` before its own `ended`, and must end before the next row starts.
Media errors and rejected `play()` calls fail the run.
These bounds include browser decoding and timer variability.
Browser waits that time out are incomplete runs (exit 2), retain diagnostics
and never report PASS.
