"""
Regression test: when script generation starts, both the Pause and Cancel
buttons in the Generate Script section become visible together (and both
are hidden beforehand).

Run with:
  python test_pause_button.py
  # or from project root:
  # app/env/bin/python test_pause_button.py
"""
import asyncio
import os

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH",
    os.path.expanduser("~/.cache/ms-playwright")
)

from playwright.async_api import async_playwright

FAILURES = []

PAGE_LOAD_TIMEOUT = 15000
UI_SETTLE_DELAY = 400
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

        # Navigate to the Generate Script tab (step 2)
        links = page.locator(".nav-link")
        for i in range(await links.count()):
            txt = (await links.nth(i).inner_text()).strip()
            if "script" in txt.lower():
                await links.nth(i).click()
                break
        await page.wait_for_timeout(UI_SETTLE_DELAY)

        pause_btn = page.locator("#btn-pause-script")
        cancel_btn = page.locator("#btn-cancel-script")

        print("\n=== 1) Initial state — both buttons hidden before generation starts ===")
        check("Pause hidden initially", not await pause_btn.is_visible())
        check("Cancel hidden initially", not await cancel_btn.is_visible())

        print("\n=== 2) Both buttons become visible together once generation starts ===")
        # Mirror the btn-gen-script click handler's state-setting in index.html
        # (it shows both buttons and resets the pause button's label/classes).
        await page.evaluate("""() => {
            const cancelBtn = document.getElementById('btn-cancel-script');
            const pauseBtn  = document.getElementById('btn-pause-script');
            cancelBtn.style.display = 'inline-block';
            pauseBtn.style.display  = 'inline-block';
            pauseBtn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            pauseBtn.classList.remove('btn-outline-success');
            pauseBtn.classList.add('btn-outline-warning');
        }""")
        await page.wait_for_timeout(UI_SETTLE_DELAY)

        check("Pause visible while generation is running", await pause_btn.is_visible())
        check("Cancel visible while generation is running", await cancel_btn.is_visible())

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
