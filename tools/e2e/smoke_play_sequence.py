#!/usr/bin/env python3
"""Real-browser smoke test: Editor "Play Sequence" plays every row in order, with
the pause the server's playback plan gives after each row.

The unit suite runs the frontend's actual functions in a Node VM; this drives the
real page in headless Chromium against a real, isolated server:

- a temporary ALEXANDRIA_DATA_DIR seeded with a synthetic book (seed_book.py),
  so no real book, voice or setting is read or changed;
- the server on a free 127.0.0.1 port in its own process group, output written
  to a file (never an unread pipe, which a chatty server fills and blocks on),
  and the whole group stopped at the end.

Needs Playwright in the interpreter running this script (requirements-test.txt;
`playwright install chromium-headless-shell` once), not in app/env.

Usage: python tools/e2e/smoke_play_sequence.py [--app-python PATH] [--keep]
Exit 0 when every check passes, 1 when one fails, 2 when it could not run.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
sys.path.insert(0, APP)
from export_limits import MAX_PAUSE_MS
from subprocess_ownership import run_owned_capture, start_owned_subprocess, stop_owned_subprocess
from seed_book import ROWS
# Browser timers and media events are not sample-exact; these bound the slack.
EARLY_MS, LATE_MS, IMMEDIATE_MS = 50, 400, 250

RECORDER = """
(() => {
  window.__aa2Media = [];
  const note = (element, event) => {
    const row = element.closest && element.closest('tr');
    window.__aa2Media.push({event, id: row ? Number(row.dataset.id) : null, t: performance.now()});
  };
  const observed = new WeakSet();
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (!observed.has(this)) {
      observed.add(this);
      for (const event of ['playing', 'ended', 'pause', 'timeupdate', 'error']) {
        this.addEventListener(event, () => note(this, event));
      }
    }
    note(this, 'play_called');
    try {
      const promise = play.apply(this, arguments);
      if (promise && promise.catch) promise.catch(() => note(this, 'play_rejected'));
      return promise;
    } catch (error) {
      note(this, 'play_rejected');
      throw error;
    }
  };
})();
"""


def get_app_python():
    result = run_owned_capture([os.path.join(REPO, "tools", "regen_derived.sh"), "--python"],
                               timeout=30, text=True)
    path = result.stdout.strip()
    if result.returncode != 0 or not os.path.isfile(path):
        raise RuntimeError(f"no app interpreter: {result.stderr.strip() or path}")
    return path


def get_free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def get_json(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("playback plan exceeded 64 KiB")
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate playback-plan field")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite playback plan")))


def get_plan(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("pauses"), list):
        raise ValueError("playback plan requires a pauses list")
    if len(payload["pauses"]) != len(ROWS) - 1:
        raise ValueError("playback plan does not cover the fixture")
    plan = {}
    for row in payload["pauses"]:
        if (not isinstance(row, dict) or type(row.get("chunk_id")) is not int
                or row["chunk_id"] not in range(len(ROWS) - 1) or row["chunk_id"] in plan
                or type(row.get("pause_ms")) is not int or not 0 <= row["pause_ms"] <= MAX_PAUSE_MS):
            raise ValueError("invalid or duplicate playback-plan row")
        plan[row["chunk_id"]] = row["pause_ms"]
    for index, row in enumerate(ROWS[:-1]):
        if row[2] is not None and plan[index] != row[2]:
            raise ValueError(f"playback plan disagrees with fixture pause at row {index}")
    return plan


def ensure_ready(url, server, log_path, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise RuntimeError(f"server exited with {server.returncode}; see {log_path}")
        try:
            urllib.request.urlopen(url, timeout=2).close()
            return
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    raise RuntimeError(f"server not ready after {timeout}s; see {log_path}")


def stop_group(server):
    try:
        stop_owned_subprocess(server, timeout=2, force_after_grace=True)
    finally:
        control = getattr(server, "_alexandria_control", None)
        if control is not None:
            control.close()


def get_gap_checks(media, plan):
    """-> list of (row, next_row, planned_ms, measured_ms, ok) from recorded media events."""
    plays = [(index, event) for index, event in enumerate(media) if event["event"] == "playing"]
    checks = []
    for (play_index, current), (next_index, following) in zip(plays, plays[1:]):
        planned = plan[current["id"]]
        # A trimmed row is paused at end_ms and never fires 'ended'. Use the
        # last completion-ish event for that row before the next row starts.
        prior = [(index, event) for index, event in enumerate(media)
                 if play_index < index < next_index and event["id"] == current["id"]
                 and event["event"] in ("ended", "pause", "timeupdate")]
        if not prior:
            checks.append((current["id"], following["id"], planned, None, False))
            continue
        end_index, end_event = prior[-1]
        measured = following["t"] - end_event["t"]
        ordered = play_index < end_index < next_index
        ok = (ordered and measured >= 0
              and (measured < IMMEDIATE_MS if planned == 0
                   else planned - EARLY_MS <= measured <= planned + LATE_MS))
        checks.append((current["id"], following["id"], planned, round(measured), ok))
    return checks


def run_browser(base, failures):
    from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True,
                                             args=["--autoplay-policy=no-user-gesture-required"])
        try:
            page = browser.new_page()
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.add_init_script(RECORDER)
            page.goto(f"{base}/#editor")
            rows = page.locator("#chunks-table-body tr.chunk-row")
            rows.nth(2).wait_for(timeout=20000)
            row_ids = [int(value) for value in rows.evaluate_all("rows => rows.map(r => r.dataset.id)")]
            button = page.locator("#btn-play-seq")
            button.click()
            try:
                page.wait_for_function(
                    "count => window.__aa2Media.some(e => ['error', 'play_rejected'].includes(e.event))"
                    " || (window.__aa2Media.filter(e => e.event === 'playing').length >= count"
                    " && !document.getElementById('btn-play-seq').innerHTML.includes('Stop'))",
                    arg=len(row_ids), timeout=20000)
            except BrowserTimeout as error:
                recent = page.evaluate("window.__aa2Media.slice(-12)")
                raise RuntimeError(f"Editor sequence incomplete; recent media {recent}; "
                                   f"page errors {[value[:512] for value in page_errors[:3]]}") from error
            media = page.evaluate("window.__aa2Media")
            label = button.inner_text().strip()
        finally:
            browser.close()
    played = [event["id"] for event in media if event["event"] == "playing"]
    if row_ids != list(range(len(ROWS))):
        failures.append(f"editor rows {row_ids} != seeded fixture")
    if played != row_ids:
        failures.append(f"play order {played} != editor rows {row_ids}")
    if any(event["event"] in ("error", "play_rejected") for event in media):
        failures.append("media error or rejected play()")
    if label != "Play Sequence":
        failures.append(f"button reads {label!r} after the sequence, not 'Play Sequence'")
    if page_errors:
        failures.append(f"page errors: {page_errors[:3]}")
    return row_ids, media


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app-python", help="interpreter for the server (default: as ready.sh resolves it)")
    parser.add_argument("--keep", action="store_true", help="keep the temporary data dir and server log")
    args = parser.parse_args(argv)
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("playwright is not installed in this interpreter; see requirements-test.txt", file=sys.stderr)
        return 2
    data_dir, server, failures, status = None, None, [], 0
    try:
        app_python = args.app_python or get_app_python()
        data_dir = tempfile.mkdtemp(prefix="aa2-e2e-")
        log_path = os.path.join(data_dir, "server.log")
        env = dict(os.environ, ALEXANDRIA_DATA_DIR=data_dir, ALEXANDRIA_PORT=str(get_free_port()),
                   ALEXANDRIA_HOST="127.0.0.1")
        base = f"http://127.0.0.1:{env['ALEXANDRIA_PORT']}"
        seed = run_owned_capture([app_python, os.path.join(REPO, "tools", "e2e", "seed_book.py")],
                                 timeout=60, cwd=APP, env=env, text=True)
        if seed.returncode:
            raise RuntimeError(f"seed failed ({seed.returncode}): {seed.stderr[-2048:]}")
        with open(log_path, "w") as log:
            server = start_owned_subprocess([app_python, "app.py"], cwd=APP, env=env, stdout=log,
                                            stderr=subprocess.STDOUT, start_new_session=os.name == 'posix',
                                            termination_grace=2)
        ensure_ready(f"{base}/", server, log_path)
        plan = get_plan(get_json(f"{base}/api/chunks/playback_plan"))
        row_ids, media = run_browser(base, failures)
        print(f"rows {row_ids}; played {[e['id'] for e in media if e['event'] == 'playing']}")
        for current, following, planned, measured, ok in get_gap_checks(media, plan):
            print(f"  gap {current} -> {following}: planned {planned} ms, measured "
                  f"{'missing completion' if measured is None else f'{measured} ms'} {'ok' if ok else 'FAIL'}")
            if not ok:
                failures.append(f"gap after row {current}: planned {planned} ms, measured {measured}")
    except Exception as error:  # report what could not run, with the log to read
        print(f"could not run: {type(error).__name__}: {error}", file=sys.stderr)
        status = 2
    finally:
        if server is not None:
            try:
                stop_group(server)
            except Exception as error:
                print(f"cleanup failed: {type(error).__name__}: {error}", file=sys.stderr)
                status = 2
        if data_dir is not None:
            if args.keep or status or failures:
                print(f"kept {data_dir}")
            else:
                try:
                    shutil.rmtree(data_dir)
                except OSError as error:
                    print(f"cleanup failed: {type(error).__name__}: {error}; kept {data_dir}", file=sys.stderr)
                    status = 2
    if status:
        return status
    for failure in failures:
        print(f"FAIL: {failure}")
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
