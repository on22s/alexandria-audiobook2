"""
Visual theme audit: captures screenshots of key UI sections in light, night,
and super-night modes so we can spot contrast/colour problems and fix them.

Run with:
  /home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python test_themes.py
"""
import asyncio
import os

os.environ["PLAYWRIGHT_BROWSERS_PATH"] = (
    "/home/fakemitch/pinokio/cache/XDG_CACHE_HOME/ms-playwright"
)

from playwright.async_api import async_playwright

APP_URL  = "http://127.0.0.1:4200"
OUT_DIR  = "/tmp/alexandria_theme_audit"
THEMES   = ["light", "night", "super-night"]

# Sections to screenshot: (label, selector-or-None-for-full-page)
SECTIONS = [
    ("generate_script",  "#script-tab .card"),
    ("voices",           "#voices-tab"),
    ("navbar",           ".navbar"),
    ("result_tab",       "#result-tab"),
]


async def set_theme(page, theme: str):
    if theme == "light":
        await page.evaluate("document.documentElement.removeAttribute('data-theme')")
    else:
        await page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
    await page.wait_for_timeout(300)   # let transition finish


async def show_running_state(page):
    """Simulate script-generation running: show Pause + Cancel buttons."""
    await page.evaluate("""() => {
        const genBtn    = document.getElementById('btn-gen-script');
        const cancelBtn = document.getElementById('btn-cancel-script');
        const pauseBtn  = document.getElementById('btn-pause-script');
        if (genBtn)    genBtn.disabled = true;
        if (cancelBtn) cancelBtn.style.display = 'inline-block';
        if (pauseBtn) {
            pauseBtn.style.display  = 'inline-block';
            pauseBtn.innerHTML = '<i class="fas fa-pause me-1"></i>Pause';
            pauseBtn.classList.remove('btn-outline-success');
            pauseBtn.classList.add('btn-outline-warning');
        }
    }""")


async def screenshot_section(page, label: str, selector, out_path: str):
    if selector:
        try:
            el = page.locator(selector).first
            await el.screenshot(path=out_path)
            return
        except Exception:
            pass
    await page.screenshot(path=out_path)


async def navigate_to_tab(page, tab_text: str) -> bool:
    links = page.locator(".nav-link")
    for i in range(await links.count()):
        txt = (await links.nth(i).inner_text()).strip()
        if tab_text.lower() in txt.lower():
            await links.nth(i).click()
            await page.wait_for_timeout(400)
            return True
    return False


async def run():
    os.makedirs(OUT_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.firefox.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1280, "height": 900})

        print(f"Loading {APP_URL} …")
        await page.goto(APP_URL, timeout=15000)
        await page.wait_for_load_state("networkidle")

        # Navigate to Script tab and simulate running state once
        await navigate_to_tab(page, "Script")
        await show_running_state(page)

        for theme in THEMES:
            print(f"\n── Theme: {theme} ──")
            await set_theme(page, theme)

            for label, selector in SECTIONS:
                # Navigate to the right tab for this section
                if "voices" in label:
                    await navigate_to_tab(page, "Voice")
                elif "result" in label:
                    await navigate_to_tab(page, "Result")
                elif "script" in label or "generate" in label:
                    await navigate_to_tab(page, "Script")

                out = os.path.join(OUT_DIR, f"{theme}_{label}.png")
                await screenshot_section(page, label, selector, out)
                print(f"  saved {out}")

            # Also full-page shot on Script tab with running state
            await navigate_to_tab(page, "Script")
            out = os.path.join(OUT_DIR, f"{theme}_full_script_tab.png")
            await page.screenshot(path=out, full_page=False)
            print(f"  saved {out}")

        await browser.close()

    print(f"\nDone — all screenshots in {OUT_DIR}/")
    print("Files:")
    for f in sorted(os.listdir(OUT_DIR)):
        print(f"  {OUT_DIR}/{f}")


asyncio.run(run())
