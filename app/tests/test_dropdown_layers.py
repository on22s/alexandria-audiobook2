"""Rendered dropdown stacking regressions (#1940), using installed Chromium.

Set AA2_TEST_BROWSER to a Chrome/Edge/Chromium executable if it is not on PATH
or in a standard Windows installation. Missing browsers fail explicitly.
The fixture uses actual index markup/styles and shipped Bootstrap, without
application scripts, API calls, or book data. It is not a full app E2E test.
"""
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


STATIC = Path(__file__).resolve().parents[1] / "static"
FIXTURE = Path(__file__).with_name("dropdown_layers_fixture.js")


def find_browser():
    configured = os.environ.get("AA2_TEST_BROWSER")
    if configured:
        executable = shutil.which(configured)
        if not executable:
            raise RuntimeError(f"AA2_TEST_BROWSER is not executable: {configured}")
        return executable
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge", "chrome"):
        executable = shutil.which(name)
        if executable:
            return executable
    for variable in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            for relative in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
                executable = Path(base) / relative
                if executable.is_file():
                    return str(executable)
    raise RuntimeError("Rendered layer tests require Chrome, Edge, or Chromium; set AA2_TEST_BROWSER")


def render_layer_cases():
    browser = find_browser()
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    source = re.sub(r"<script\b[^>]*>.*?</script>", "", source, flags=re.S | re.I)
    source = re.sub(r"<link\b[^>]*>", "", source, flags=re.I)
    vendor = STATIC / "vendor/bootstrap-5.3.0"
    css = (vendor / "bootstrap.min.css").read_text(encoding="utf-8")
    bootstrap = (vendor / "bootstrap.bundle.min.js").read_text(encoding="utf-8")
    checks = FIXTURE.read_text(encoding="utf-8")
    source = source.replace("<style>", "<style>" + css + "</style><style>", 1)
    source = source.replace("</body>", "<script>" + bootstrap + "</script><script>" + checks + "</script></body>")
    rows = []
    with tempfile.TemporaryDirectory(prefix="aa2-dropdown-layers-") as directory:
        fixture = Path(directory) / "fixture.html"
        for width in (1280, 1024, 390):
            # An iframe supplies an exact viewport, including widths below the
            # minimum top-level Chromium window size. srcdoc inherits origin.
            wrapper = (f'<html><body style="margin:0"><iframe style="border:0;width:{width}px;height:800px" '
                       + 'srcdoc="' + html.escape(source, quote=True) + '"></iframe></body></html>')
            fixture.write_text(wrapper, encoding="utf-8")
            command = [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                       "--disable-dev-shm-usage", "--disable-background-networking",
                       "--no-first-run", "--no-default-browser-check",
                       "--user-data-dir=" + str(Path(directory) / f"profile-{width}"),
                       "--window-size=1400,900", "--virtual-time-budget=3000",
                       "--dump-dom", fixture.as_uri()]
            result = subprocess.run(command, capture_output=True, encoding="utf-8",
                                    errors="replace", timeout=45)
            if result.returncode:
                raise RuntimeError(f"Browser failed ({result.returncode}): {result.stderr[-2000:]}")
            match = re.search(r'<pre id="dropdown-layer-results">(.*?)</pre>', result.stdout, re.S)
            if not match:
                raise RuntimeError(f"Browser did not finish rendered checks: {result.stderr[-2000:]}")
            data = json.loads(html.unescape(match.group(1)))
            if data.get("error"):
                raise RuntimeError(data["error"])
            cases = data.get("rows", [])
            if len(cases) != 6:
                raise RuntimeError(f"Browser returned {len(cases)} theme cases instead of six")
            if any(case["width"] != width for case in cases):
                raise RuntimeError(f"Browser did not render the requested {width}px viewport")
            rows.extend(cases)
    return rows


class DropdownLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = render_layer_cases()

    def assert_cases(self, *keys):
        for case in self.cases:
            with self.subTest(width=case["width"], theme=case["theme"]):
                for key in keys:
                    self.assertTrue(case[key], f"{key}: {case}")

    def test_advanced_menu_clears_editor_toolbar(self):
        self.assert_cases("navAbove")
        self.assertTrue(all(case["overlap"] for case in self.cases if case["width"] >= 992))

    def test_mobile_navigation_uses_nonoverlapping_layout(self):
        mobile = [case for case in self.cases if case["width"] < 992]
        self.assertEqual(len(mobile), 6)
        self.assertTrue(all(not case["overlap"] for case in mobile))

    def test_batch_menu_clears_content_before_and_after_scrolling(self):
        self.assert_cases("batchAbove", "stickyBatchAbove")

    def test_editor_drawer_and_scrim_block_navigation(self):
        self.assert_cases("drawerAbove", "scrimAbove")

    def test_modal_blocks_navigation(self):
        self.assert_cases("modalAbove", "backdropAbove")
