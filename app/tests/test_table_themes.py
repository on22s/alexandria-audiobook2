"""Rendered theme regression for #1926 and shared contextual table consumers.

Uses the shipped CSS and actual Text Integrity/theme render functions with
synthetic hunks in installed Chromium. No API, book data, or live model calls.
"""
import html
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from tests.test_dropdown_layers import find_browser

STATIC = Path(__file__).resolve().parents[1] / "static"
FIXTURE = Path(__file__).with_name("table_themes_fixture.js")


def extract_function(source, name):
    """Extract a named app function without depending on its next sibling."""
    match = re.search(r"(?m)^([ \t]*)function " + re.escape(name) + r"\([^\n]*\) \{[\s\S]*?^\1\}", source)
    if not match:
        raise RuntimeError(f"Could not extract app function {name}")
    return match.group(0)


def render_theme_cases():
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    source = re.sub(r"<script\b[^>]*>.*?</script>", "", source, flags=re.S | re.I)
    source = re.sub(r"<link\b[^>]*>", "", source, flags=re.I)
    vendor = (STATIC / "vendor/bootstrap-5.3.0/bootstrap.min.css").read_text(encoding="utf-8")
    source = source.replace("<style>", "<style>" + vendor + "</style><style>", 1)
    core = (STATIC / "js/app-core.js").read_text(encoding="utf-8")
    escape = extract_function(core, "escapeHtml")
    theme_list = re.search(r"const THEMES = \[[\s\S]*?\];", core)
    if not theme_list:
        raise RuntimeError("Could not extract app themes")
    themes = theme_list.group(0) + "\n" + extract_function(core, "applyTheme")
    render = extract_function(core, "renderTextDiff")
    checks = FIXTURE.read_text(encoding="utf-8")
    script = "let textDiffRequest = 0;\n" + escape + themes + render + checks
    source = source.replace("</body>", "<script>" + script + "</script></body>")
    with tempfile.TemporaryDirectory(prefix="aa2-table-themes-") as directory:
        fixture = Path(directory) / "fixture.html"
        fixture.write_text(source, encoding="utf-8")
        result = subprocess.run([
            find_browser(), "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check",
            "--user-data-dir=" + str(Path(directory) / "profile"),
            "--window-size=1280,900", "--virtual-time-budget=3000", "--dump-dom", fixture.as_uri(),
        ], capture_output=True, encoding="utf-8", errors="replace", timeout=45)
    if result.returncode:
        raise RuntimeError(f"Browser failed ({result.returncode}): {result.stderr[-2000:]}")
    match = re.search(r'<pre id="table-theme-results">(.*?)</pre>', result.stdout, re.S)
    if not match:
        raise RuntimeError(f"Rendered theme checks did not finish: {result.stderr[-2000:]}")
    data = json.loads(html.unescape(match.group(1)))
    if data.get("error"):
        raise RuntimeError(data["error"])
    if len(data["rows"]) != 6:
        raise RuntimeError("Expected all six shipped themes")
    return data["rows"]


class TableThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = render_theme_cases()

    def assert_cases(self, key):
        for case in self.cases:
            with self.subTest(theme=case["theme"]):
                self.assertTrue(case[key], f"{key}: {case}")

    def test_integrity_added_deleted_changed_rows_follow_current_theme(self):
        self.assert_cases("integrityTheme")

    def test_integrity_labels_and_entry_links_remain_readable(self):
        self.assert_cases("integrityContrast")
        self.assert_cases("hasContext")
        self.assert_cases("callerLinkTheme")

    def test_plain_tables_follow_current_surface_and_foreground(self):
        self.assert_cases("plainTheme")
        self.assert_cases("plainMutedTheme")
        self.assert_cases("headerTheme")

    def test_shared_contextual_classes_follow_theme_on_rows_and_cells(self):
        self.assert_cases("sharedTheme")

    def test_contextual_colors_remain_distinct_in_all_themes(self):
        self.assert_cases("distinctStatuses")

    def test_striped_hover_and_active_foregrounds_follow_current_theme(self):
        self.assert_cases("stateTheme")
        self.assert_cases("statePaint")
        self.assert_cases("neutralStates")
