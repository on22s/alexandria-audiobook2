"""Field help (app-help.js): every static field is documented, advice is sourced.

WHAT THIS VERIFIES.
- Coverage: every user-facing static <input>/<select>/<textarea> across all
  tabs and shared dialogs has a FIELD_HELP entry, and no
  entry names a control that is not there - so a new control fails here until
  it is documented.
- Content: every `what` is plain text of at most 300 characters; every
  `recommend` names its `source` (advice must point at a measurement).
- Rendering, against a small DOM stub: one ⓘ beside each control label, including a
  wrapping checkbox label; the tooltip text is set as an attribute,
  never as markup; unlabelled fields receive a visible help glyph;
  calling renderHelpTips() again adds nothing; one delegated Bootstrap tooltip.
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
                         capture_output=True, text=True, timeout=15)
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
const icon=tipFor(select);assert.strictEqual(icon.tagName,'SPAN');assert.strictEqual(icon.previousElementSibling,label);assert.strictEqual(icon.getAttribute('data-help-for'),'prompt-preset-select');
assert(icon.getAttribute('data-bs-title').includes('Recommended:'));assert(icon.getAttribute('data-bs-title').includes('(source: '));assert(icon.getAttribute('aria-label').startsWith('Help: '));assert.strictEqual(icon.textContent,'ⓘ');
assert.strictEqual(tipFor(box).previousElementSibling,wrap,'no help control nested in the checkbox label');
assert.strictEqual(tipFor(bare).previousElementSibling,bare,'unlabelled field gets a visible glyph');
assert(tipFor(bare).getAttribute('data-bs-title').length>0);
assert.strictEqual(body.querySelectorAll('.help-tip').length,3,'repeat render has no duplicate icons');assert.strictEqual(tooltipCalls.length,1);assert.strictEqual(tooltipCalls[0].options.selector,'[data-help-tip]');assert.strictEqual(tooltipCalls[0].options.html,false);
"""


class FieldHelpRenderTests(unittest.TestCase):
    def test_render_places_icons_once_and_initialises_one_delegated_tooltip(self):
        from tests.test_ui_help_js import run_help_js
        result = run_help_js(RENDER_TEST)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
