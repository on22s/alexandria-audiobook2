"""Static contracts for UI behavior that is easy to regress silently."""
import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).parent.parent / "static"


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tabs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("data-tab"):
            self.tabs.append(values)


class UIContractTests(unittest.TestCase):
    def test_primary_navigation_is_keyboard_addressable(self):
        parser = _NavigationParser()
        parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(11, len(parser.tabs))
        self.assertTrue(all(tab.get("href", "").startswith("#") for tab in parser.tabs))
        self.assertEqual(1, sum(tab.get("aria-current") == "page" for tab in parser.tabs))

    def test_pass_prompt_defaults_have_a_server_and_browser_path(self):
        route = (STATIC.parent / "routers" / "system.py").read_text(encoding="utf-8")
        script = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")
        for field in ("pass1_system_prompt", "pass1_user_prompt",
                      "pass3_system_prompt", "pass3_user_prompt"):
            self.assertIn(f'"{field}"', route)
            self.assertIn(f"defaults.{field}", script)
        self.assertRegex(script, r"preset\?\.system_prompt \|\| defaults\.system_prompt")
        self.assertRegex(script, r"preset\?\.user_prompt \|\| defaults\.user_prompt")

    def test_reset_preserves_numbered_prompt_presets(self):
        script = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")
        reset = script[script.index("window.resetPrompts"):script.index("// Toggle chevron")]
        self.assertIn("renderPassPromptPresets('pass1', passPromptPresets.pass1, 'default')", reset)
        self.assertIn("renderPassPromptPresets('pass3', passPromptPresets.pass3, 'default')", reset)
        self.assertNotIn("renderPassPromptPresets('pass1', [], 'default')", reset)
        self.assertNotIn("renderPassPromptPresets('pass3', [], 'default')", reset)

    def test_error_toasts_are_assertive_but_other_toasts_are_polite(self):
        script = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")
        self.assertIn("const liveRole = type === 'error' ? 'alert' : 'status';", script)
        self.assertIn("const livePriority = type === 'error' ? 'assertive' : 'polite';", script)

    def test_ui_docs_cover_required_review_dimensions(self):
        guide = (STATIC.parent.parent / "docs" / "UI_GUIDELINES.md").read_text(encoding="utf-8")
        checklist = (STATIC.parent.parent / "docs" / "UI_TEST_CHECKLIST.md").read_text(encoding="utf-8")
        for text in ("focus-visible", "reduced-motion", "Cyberpunk", "loading", "error"):
            self.assertIn(text, guide)
        for text in ("320px", "375px", "414px", "768px", "Pass 1", "Pass 3"):
            self.assertIn(text, checklist)


if __name__ == "__main__":
    unittest.main()
