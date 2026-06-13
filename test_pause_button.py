"""
Test: Verify that both Pause and Cancel buttons appear in the
Generate Script section when script generation is running.

Run with:
  /home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python test_pause_button.py
"""
import asyncio
import os

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
    "/home/fakemitch/pinokio/cache/XDG_CACHE_HOME/ms-playwright"
)

from playwright.async_api import async_playwright


async def run():
    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=False)
        page = await browser.new_page()

        print("Loading app at http://127.0.0.1:4200 ...")
        await page.goto("http://127.0.0.1:4200", timeout=15000)
        await page.wait_for_load_state("networkidle")

        # Navigate to the Generate Script tab (step 2)
        links = page.locator(".nav-link")
        clicked = False
        for i in range(await links.count()):
            txt = (await links.nth(i).inner_text()).strip()
            if "Script" in txt or "script" in txt:
                print(f"Clicking tab: '{txt}'")
                await links.nth(i).click()
                clicked = True
                break
        if not clicked:
            print("WARNING: could not find Script tab, testing from current page")
        await page.wait_for_timeout(600)

        pause_btn  = page.locator("#btn-pause-script")
        cancel_btn = page.locator("#btn-cancel-script")

        # 1) Initial state — both should be hidden
        pause_initial  = await pause_btn.is_visible()
        cancel_initial = await cancel_btn.is_visible()
        print(f"\nINITIAL STATE — Pause visible: {pause_initial}, Cancel visible: {cancel_initial}")
        if not pause_initial and not cancel_initial:
            print("  ✓ Both hidden initially (correct)")
        else:
            print("  ! Unexpected: one or both buttons are visible before generation starts")

        # 2) Simulate the JS the click-handler runs (without actually calling the API)
        await page.evaluate("""() => {
            const genBtn    = document.getElementById('btn-gen-script');
            const cancelBtn = document.getElementById('btn-cancel-script');
            const pauseBtn  = document.getElementById('btn-pause-script');
            if (!genBtn || !cancelBtn || !pauseBtn) {
                throw new Error('One or more buttons not found in DOM');
            }
            genBtn.disabled = true;
            cancelBtn.style.display = 'inline-block';
            pauseBtn.style.display  = 'inline-block';
            pauseBtn.innerHTML = '<i class=\"fas fa-pause me-1\"></i>Pause';
            pauseBtn.classList.remove('btn-outline-success');
            pauseBtn.classList.add('btn-outline-warning');
        }""")
        await page.wait_for_timeout(400)

        pause_running  = await pause_btn.is_visible()
        cancel_running = await cancel_btn.is_visible()
        print(f"\nRUNNING STATE — Pause visible: {pause_running}, Cancel visible: {cancel_running}")

        # Screenshot so we can see exactly what the user sees
        await page.screenshot(path="/tmp/alexandria_button_test.png")
        print("Screenshot saved to /tmp/alexandria_button_test.png")

        if pause_running and cancel_running:
            print("\n  ✓ PASS: Both Pause and Cancel are visible — layout is correct")
        elif cancel_running and not pause_running:
            print("\n  ✗ FAIL: Cancel visible but Pause is NOT — this is the bug!")
            style = await page.evaluate("""() => {
                const btn = document.getElementById('btn-pause-script');
                const cs  = window.getComputedStyle(btn);
                return {
                    display:    cs.display,
                    visibility: cs.visibility,
                    opacity:    cs.opacity,
                    width:      cs.width,
                    height:     cs.height,
                    position:   cs.position,
                };
            }""")
            print(f"  Computed style of #btn-pause-script: {style}")
        else:
            print(f"\n  ✗ FAIL: Unexpected state — Pause:{pause_running}, Cancel:{cancel_running}")

        print("\nKeeping browser open 8s so you can inspect…")
        await page.wait_for_timeout(8000)
        await browser.close()


asyncio.run(run())
