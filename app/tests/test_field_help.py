"""Field help (app-help.js): every static field is documented, advice is sourced.

WHAT THIS VERIFIES.
- Coverage: every user-facing static <input>/<select>/<textarea> across all
  tabs and shared dialogs has a FIELD_HELP entry, and no
  entry names a control that is not there - so a new control fails here until
  it is documented.
- Content: every `what` is plain text of at most 300 characters; every
  `recommend` names its `source` (advice must point at a measurement).
- Rendering against a small DOM stub: direct control and associated-label help,
  delayed presentation, plain-text content, and no injected help glyphs.
"""
from html.parser import HTMLParser
import json
import re
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"


class FieldInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attributes):
        if tag not in {"input", "select", "textarea"}:
            return
        attrs = dict(attributes)
        hidden_proxy = (attrs.get("type") == "file"
                        and re.search(r"display\s*:\s*none", attrs.get("style", "")))
        if attrs.get("type") == "hidden" or "hidden" in attrs or hidden_proxy:
            return
        if not attrs.get("id"):
            raise AssertionError("Static fields need an id for their help entry")
        self.ids.append(attrs["id"])


def get_static_field_ids(html):
    parser = FieldInventory()
    parser.feed(html)
    return parser.ids


def load_field_help():
    code = ("const fs=require('fs'),vm=require('vm');const c={};vm.createContext(c);"
            "const src=fs.readFileSync(process.argv[1],'utf8');"
            "vm.runInContext(src.slice(0,src.indexOf('/** The tooltip text'))+';this.H=FIELD_HELP;',c);"
            "process.stdout.write(JSON.stringify(c.H));")
    out = subprocess.run(["node", "-e", code, str(STATIC / "js" / "app-help.js")],
                         capture_output=True, text=True, encoding="utf-8", timeout=15)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


class FieldHelpContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.help = load_field_help()
        cls.ids = get_static_field_ids((STATIC / "index.html").read_text(encoding="utf-8"))

    def test_every_static_field_has_help(self):
        self.assertGreater(len(self.ids), 220)
        self.assertEqual([], sorted(set(self.ids) - set(self.help)))

    def test_no_help_entry_for_a_missing_control(self):
        self.assertEqual([], sorted(set(self.help) - set(self.ids)))

    def test_text_is_plain_and_short(self):
        for key, entry in self.help.items():
            with self.subTest(key=key):
                self.assertTrue(entry["what"].strip())
                self.assertLessEqual(len(entry["what"]), 300)
                for field in ("what", "recommend"):
                    self.assertNotRegex(entry.get(field, ""), r"<[a-z/][^>]*>")

    def test_every_recommendation_names_its_source(self):
        recommended = {k: e for k, e in self.help.items() if e.get("recommend")}
        self.assertTrue(recommended)
        for key, entry in recommended.items():
            with self.subTest(key=key):
                self.assertTrue((entry.get("source") or "").strip())


RENDER_TEST = r"""
const label=body.appendChild(new Element('label',{for:'prompt-preset-select'}));
const select=body.appendChild(new Element('select',{id:'prompt-preset-select'}));select.labels=[label];
const wrap=body.appendChild(new Element('label',{}));const box=wrap.appendChild(new Element('input',{id:'m4b-per-chunk'}));box.labels=[wrap];
const bare=body.appendChild(new Element('input',{id:'save-script-name'}));
load();context.renderHelpTips();context.ensureHelpTooltips();
assert.strictEqual(label.children.length,0,'help must not become part of the field label');
assert(select.getAttribute('data-bs-title').includes('Recommended:'));
assert(select.getAttribute('data-bs-title').includes('(source: '));
assert.strictEqual(box.parentElement,wrap);
assert(bare.getAttribute('data-bs-title').length>0);
assert.strictEqual(body.querySelectorAll('.help-tip,.help-nav-glyph').length,0);
label.control=select;label.fire('pointerover');tick(499);assert.strictEqual(shown.length,0);
tick(1);assert.strictEqual(shown.at(-1),select);
assert.strictEqual(body.listeners.focusin.length,1);
"""


class FieldHelpRenderTests(unittest.TestCase):
    def test_render_uses_direct_delayed_help_without_icons(self):
        from tests.test_ui_help_js import run_help_js
        result = run_help_js(RENDER_TEST)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
