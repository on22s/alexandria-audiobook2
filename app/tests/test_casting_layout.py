from html.parser import HTMLParser
from pathlib import Path
import unittest


class CastingLayoutParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.elements = {}
        self.duplicates = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        identity = attrs.get("id")
        if identity:
            if identity in self.elements:
                self.duplicates.append(identity)
            self.elements[identity] = (tag, attrs, tuple(self.stack))
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append((tag, identity))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class CastingLayoutTests(unittest.TestCase):
    def test_research_sidebar_and_results_are_siblings_with_stable_hooks(self):
        root = Path(__file__).resolve().parents[1]
        parser = CastingLayoutParser()
        parser.feed((root / "static/index.html").read_text(encoding="utf-8"))
        self.assertEqual([], parser.duplicates)
        sidebar = parser.elements["casting-research"][2]
        results = parser.elements["casting-results"][2]
        self.assertTrue(any(tag == "aside" for tag, identity in sidebar))
        self.assertFalse(any(tag == "aside" for tag, identity in results))
        for identity in ("casting-research", "casting-results", "casting-research-toolbar", "casting-pagination", "casting-activity"):
            self.assertIn(("div", "casting-workspace"), parser.elements[identity][2])
        self.assertNotIn(("div", "casting-workspace"), parser.elements["casting-review-modal"][2])
        for identity in ("casting-play-all", "casting-summary", "casting-playback-status"):
            self.assertFalse(any(tag == "aside" for tag, name in parser.elements[identity][2]))
            self.assertIn(("div", "casting-workspace"), parser.elements[identity][2])

    def test_research_assets_load_in_order_and_reuse_host_theme(self):
        root = Path(__file__).resolve().parents[1]
        markup = (root / "static/index.html").read_text(encoding="utf-8")
        self.assertLess(markup.index("/static/js/app-casting-research.js"), markup.index("/static/js/app-casting.js"))
        self.assertIn("/static/css/casting.css?v=__APP_BUILD__", markup)
        stylesheet = (root / "static/css/casting.css").read_text(encoding="utf-8")
        self.assertIn("var(--border-input)", stylesheet)
        self.assertIn("var(--accent)", stylesheet)
        self.assertIn(".casting-focus .casting-sidebar", stylesheet)
        self.assertIn("max-width: 575px", stylesheet)
