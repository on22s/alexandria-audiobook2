"""
Focused regression test for the _makePauseResumeHandler refactor in
app/static/index.html (derives paused/running from the button's own
class instead of a closure variable + .reset(), and retries on 503).

Run with:
  python test_pause_resume_refactor.py
  # or from project root:
  # app/env/bin/python test_pause_resume_refactor.py
"""
import asyncio
import os

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.expanduser("~/.cache/ms-playwright")
)

from playwright.async_api import async_playwright

FAILURES = []

# Timeout constants (in milliseconds)
PAGE_LOAD_TIMEOUT = 15000
UI_SETTLE_DELAY = 200  # wait for UI to settle after click
RETRY_POLL_DELAY = 2500  # wait for 503 retry attempts
FAILURE_POLL_DELAY = 500  # wait for non-retried failure
POST_CLICK_WAIT = 400  # wait after simulated button click
MANUAL_WAIT = 600  # wait after manual navigation
BROWSER_CLOSE_WAIT = 8000  # time to keep browser open for manual inspection
APP_URL = "http://127.0.0.1:4200"


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


async def run():
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        page = await browser.new_page()
        await page.goto(APP_URL, timeout=PAGE_LOAD_TIMEOUT)
        await page.wait_for_load_state("networkidle")

        # --- Stub out fetch so we can drive /pause and /resume responses
        # without a real script-generation process running, and count calls. ---
        await page.evaluate("""() => {
            window.__calls = [];
            window.__responses = {};   // url -> array of {status, body} consumed in order
            const realFetch = window.fetch.bind(window);
            window.__realFetch = realFetch;
            window.fetch = async (url, opts) => {
                if (typeof url === 'string' && (url.includes('/pause') || url.includes('/resume'))
                        && (url.includes('generate_script'))) {
                    window.__calls.push(url);
                    const queue = window.__responses[url] || [];
                    const next = queue.length ? queue.shift() : { status: 200, body: { status: 'ok' } };
                    return new Response(JSON.stringify(next.body), {
                        status: next.status,
                        headers: { 'Content-Type': 'application/json' }
                    });
                }
                return realFetch(url, opts);
            };
        }""")

        print("\n=== 1) Toggle derives state from DOM (no stale closure) ===")
        await page.evaluate("""() => {
            const btn = document.getElementById('btn-pause-script');
            btn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            btn.classList.remove('btn-outline-success');
            btn.classList.add('btn-outline-warning');
            window.__calls = [];
            window.__responses = {};
        }""")

        await page.evaluate("() => window.pauseResumeScript()")
        await page.wait_for_timeout(UI_SETTLE_DELAY)
        state1 = await page.evaluate("""() => ({
            calls: window.__calls.slice(),
            label: document.getElementById('btn-pause-script').innerHTML,
            success: document.getElementById('btn-pause-script').classList.contains('btn-outline-success'),
            warning: document.getElementById('btn-pause-script').classList.contains('btn-outline-warning'),
        })""")
        check("first click calls /pause (button started as Pause)",
              any('/pause' in c and 'batch' not in c for c in state1['calls']), state1['calls'])
        check("button flips to Resume / btn-outline-success", state1['success'] and 'Resume' in state1['label'])

        await page.evaluate("() => { window.__calls = []; }")
        await page.evaluate("() => window.pauseResumeScript()")
        await page.wait_for_timeout(200)
        state2 = await page.evaluate("""() => ({
            calls: window.__calls.slice(),
            label: document.getElementById('btn-pause-script').innerHTML,
            warning: document.getElementById('btn-pause-script').classList.contains('btn-outline-warning'),
        })""")
        check("second click calls /resume (button is now Resume)",
              any('/resume' in c and 'batch' not in c for c in state2['calls']), state2['calls'])
        check("button flips back to Pause / btn-outline-warning", state2['warning'] and 'Pause' in state2['label'])

        print("\n=== 2) A 'new run start' that resets the button to Pause is enough — no .reset() needed ===")
        # Put the button in 'paused' (Resume) visual state, then simulate what every
        # run-start code path does: reset label/classes back to Pause/warning.
        await page.evaluate("""() => {
            const btn = document.getElementById('btn-pause-script');
            btn.innerHTML = '<i class="fas fa-play me-1"></i>Resume';
            btn.classList.remove('btn-outline-warning');
            btn.classList.add('btn-outline-success');
            // what _startBatchScript / generate-click handlers do on a fresh run:
            btn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            btn.classList.remove('btn-outline-success');
            btn.classList.add('btn-outline-warning');
            window.__calls = [];
        }""")
        await page.evaluate("() => window.pauseResumeScript()")
        await page.wait_for_timeout(UI_SETTLE_DELAY)
        state3 = await page.evaluate("() => window.__calls.slice()")
        check("after a fresh-run reset, next click correctly calls /pause (not /resume)",
              any('/pause' in c and 'batch' not in c for c in state3), state3)

        print("\n=== 3) 503 ('starting up, retry in a moment') is retried automatically ===")
        await page.evaluate("""() => {
            const btn = document.getElementById('btn-pause-script');
            btn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            btn.classList.remove('btn-outline-success');
            btn.classList.add('btn-outline-warning');
            window.__calls = [];
            window.__toasts = [];
            const realToast = window.showToast;
            window.__realShowToast = realToast;
            window.showToast = (msg, kind) => { window.__toasts.push([msg, kind]); };
            window.__responses = {
                '/api/generate_script/pause': [
                    { status: 503, body: { detail: 'Script generation is starting up, retry in a moment.' } },
                    { status: 503, body: { detail: 'Script generation is starting up, retry in a moment.' } },
                    { status: 200, body: { status: 'paused' } },
                ]
            };
        }""")
        await page.evaluate("() => window.pauseResumeScript()")
        await page.wait_for_timeout(RETRY_POLL_DELAY)
        state4 = await page.evaluate("""() => ({
            calls: window.__calls.slice(),
            toasts: window.__toasts.slice(),
            success: document.getElementById('btn-pause-script').classList.contains('btn-outline-success'),
        })""")
        pause_calls = [c for c in state4['calls'] if '/pause' in c and 'batch' not in c]
        check("503 is retried until success (3 attempts seen)", len(pause_calls) == 3, pause_calls)
        check("button ends in Resume state after eventual success", state4['success'])
        check("no failure toast shown for an eventually-successful retry", len(state4['toasts']) == 0, state4['toasts'])

        print("\n=== 4) A persistent failure (non-503, or 503 exhausted) still shows exactly one toast ===")
        await page.evaluate("""() => {
            const btn = document.getElementById('btn-pause-script');
            btn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            btn.classList.remove('btn-outline-success');
            btn.classList.add('btn-outline-warning');
            window.__calls = [];
            window.__toasts = [];
            window.__responses = {
                '/api/generate_script/pause': [
                    { status: 400, body: { detail: 'No script generation is currently running.' } },
                ]
            };
        }""")
        await page.evaluate("() => window.pauseResumeScript()")
        await page.wait_for_timeout(FAILURE_POLL_DELAY)
        state5 = await page.evaluate("""() => ({
            calls: window.__calls.slice(),
            toasts: window.__toasts.slice(),
            warning: document.getElementById('btn-pause-script').classList.contains('btn-outline-warning'),
        })""")
        pause_calls5 = [c for c in state5['calls'] if '/pause' in c and 'batch' not in c]
        check("non-503 failure is NOT retried (single attempt)", len(pause_calls5) == 1, pause_calls5)
        check("exactly one 'Pause failed' toast shown", len(state5['toasts']) == 1
              and 'Pause failed' in state5['toasts'][0][0], state5['toasts'])
        check("button stays in Pause/warning state on failure (no optimistic flip)", state5['warning'])

        # restore real fetch/toast
        await page.evaluate("""() => {
            window.fetch = window.__realFetch;
            window.showToast = window.__realShowToast;
        }""")

        await browser.close()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED: {FAILURES}")
    else:
        print("RESULT: all checks passed")
    print("=" * 60)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
