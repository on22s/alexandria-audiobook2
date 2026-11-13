"""Layer ordering for navigation menus over sticky page controls (#1940).

The dropdown's own z-index cannot escape its navbar stacking context. Keep
that ancestor above page controls and below blocking editor/modal surfaces.
Rendered hit testing is also required when changing these relationships.
"""
from pathlib import Path
import re
import unittest


STATIC = Path(__file__).resolve().parents[1] / "static"


class DropdownLayerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = "\n".join(re.findall(r"<style>(.*?)</style>", cls.html, re.S))

    def layer(self, selector):
        blocks = re.findall(re.escape(selector) + r"\s*\{([^}]+)\}", self.css)
        values = [int(value) for block in blocks
                  for value in re.findall(r"z-index:\s*(\d+)", block)]
        self.assertTrue(values, f"{selector} needs an explicit stacking level")
        return values[-1]

    def test_navbar_menu_clears_sticky_page_controls(self):
        navbar = self.layer(".navbar")
        for selector in ("#editor-tab .editor-toolbar", ".config-save-bar"):
            with self.subTest(selector=selector):
                self.assertGreater(navbar, self.layer(selector))

    def test_blocking_drawer_stays_above_navigation(self):
        self.assertLess(self.layer(".navbar"), self.layer(".editor-drawer-scrim"))
        self.assertLess(self.layer(".editor-drawer-scrim"), self.layer(".editor-drawer"))

    def test_modal_backdrop_stays_above_navigation(self):
        bootstrap = (STATIC / "vendor/bootstrap-5.3.0/bootstrap.min.css").read_text(encoding="utf-8")
        backdrop = re.search(r"--bs-backdrop-zindex:(\d+)", bootstrap)
        self.assertIsNotNone(backdrop, "Read the actual shipped Bootstrap layer")
        self.assertLess(self.layer(".navbar"), int(backdrop.group(1)))
