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
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "app")
# Browser timers and media events are not sample-exact; these bound the slack.
EARLY_MS, LATE_MS, IMMEDIATE_MS = 50, 400, 250

RECORDER = """
(() => {
  window.__aa2Media = [];
  const note = (element, event) => {
    const row = element.closest && element.closest('tr');
    window.__aa2Media.push({event, id: row ? Number(row.dataset.id) : null, t: performance.now()});
  };
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () { note(this, 'play'); return play.apply(this, arguments); };
  document.addEventListener('ended', event => note(event.target, 'ended'), true);
})();
"""


def get_app_python():
    result = subprocess.run([os.path.join(REPO, "tools", "regen_derived.sh"), "--python"],
                            capture_output=True, text=True)
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
        return json.load(response)


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
    if server.poll() is not None:
        return
    os.killpg(server.pid, signal.SIGTERM)
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(server.pid, signal.SIGKILL)
        server.wait(timeout=10)


def get_gap_checks(media, plan):
    """-> list of (row, next_row, planned_ms, measured_ms, ok) from recorded media events."""
    plays = [event for event in media if event["event"] == "play"]
    ended = {event["id"]: event["t"] for event in media if event["event"] == "ended"}
    checks = []
    for current, following in zip(plays, plays[1:]):
        planned = plan.get(current["id"], 0)
        if current["id"] not in ended:
            checks.append((current["id"], following["id"], planned, None, False))
            continue
        measured = following["t"] - ended[current["id"]]
        ok = (measured < IMMEDIATE_MS if planned <= 0
              else planned - EARLY_MS <= measured <= planned + LATE_MS)
        checks.append((current["id"], following["id"], planned, round(measured), ok))
    return checks


def run_browser(base, failures):
    from playwright.sync_api import sync_playwright
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
            page.wait_for_function(
                "() => window.__aa2Media.filter(e => e.event === 'ended').length >= 3"
                " && !document.getElementById('btn-play-seq').innerHTML.includes('Stop')",
                timeout=20000)
            media = page.evaluate("window.__aa2Media")
            label = button.inner_text().strip()
        finally:
            browser.close()
    played = [event["id"] for event in media if event["event"] == "play"]
    if played != row_ids:
        failures.append(f"play order {played} != editor rows {row_ids}")
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
    app_python = args.app_python or get_app_python()
    data_dir = tempfile.mkdtemp(prefix="aa2-e2e-")
    log_path = os.path.join(data_dir, "server.log")
    env = dict(os.environ, ALEXANDRIA_DATA_DIR=data_dir, ALEXANDRIA_PORT=str(get_free_port()),
               ALEXANDRIA_HOST="127.0.0.1")
    base = f"http://127.0.0.1:{env['ALEXANDRIA_PORT']}"
    server, failures = None, []
    try:
        subprocess.run([app_python, os.path.join(REPO, "tools", "e2e", "seed_book.py")],
                       cwd=APP, env=env, check=True, capture_output=True, text=True)
        with open(log_path, "w") as log:
            server = subprocess.Popen([app_python, "app.py"], cwd=APP, env=env, stdout=log,
                                      stderr=subprocess.STDOUT, start_new_session=True)
        ensure_ready(f"{base}/", server, log_path)
        try:
            plan = {row["chunk_id"]: row["pause_ms"]
                    for row in get_json(f"{base}/api/chunks/playback_plan")["pauses"]}
        except urllib.error.HTTPError as error:
            plan = None
            print(f"note: no playback plan on this build (HTTP {error.code}); checking order only")
        row_ids, media = run_browser(base, failures)
        print(f"rows {row_ids}; played {[e['id'] for e in media if e['event'] == 'play']}")
        if plan is not None:
            for current, following, planned, measured, ok in get_gap_checks(media, plan):
                print(f"  gap {current} -> {following}: planned {planned} ms, measured "
                      f"{'missing ended' if measured is None else f'{measured} ms'} {'ok' if ok else 'FAIL'}")
                if not ok:
                    failures.append(f"gap after row {current}: planned {planned} ms, measured {measured}")
    except Exception as error:  # report what could not run, with the log to read
        print(f"could not run: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    finally:
        if server is not None:
            stop_group(server)
        if args.keep:
            print(f"kept {data_dir}")
        else:
            shutil.rmtree(data_dir, ignore_errors=True)
    for failure in failures:
        print(f"FAIL: {failure}")
    print("PASS" if not failures else f"{len(failures)} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
