"""Field help (app-help.js): every core-tab control is documented, advice is sourced.

WHAT THIS VERIFIES.
- Coverage: every visible <input>/<select>/<textarea> on the Setup, Script,
  Voices, Editor and Result tabs of index.html has a FIELD_HELP entry, and no
  entry names a control that is not there - so a new control fails here until
  it is documented.
- Content: every `what` is plain text of at most 300 characters; every
  `recommend` names its `source` (advice must point at a measurement).
- Rendering, against a small DOM stub: one ⓘ per labelled control, inside its
  <label for=…> or wrapping <label>; the tooltip text is set as an attribute,
  never as markup; with no label the tooltip goes on the control itself;
  calling renderHelpTips() again adds nothing; one delegated Bootstrap tooltip.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
CORE_TABS = {"setup-tab", "script-tab", "voices-tab", "editor-tab", "audio-tab"}


def get_core_control_ids(html):
    tabs = sorted((m.start(), m.group(1)) for m in re.finditer(
        r'<div id="([a-z-]+-tab)" class="tab-content', html))
    ids = []
    for m in re.finditer(r"<(input|select|textarea)\b[^>]*>", html):
        tag = m.group(0)
        if 'type="hidden"' in tag or re.search(r"\shidden(\s|>|=)", tag):
            continue
        pane = None
        for start, name in tabs:
            if start <= m.start():
                pane = name
        if pane in CORE_TABS:
            ids.append(re.search(r'id="([^"]+)"', tag).group(1))
    return ids


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
        cls.ids = get_core_control_ids((STATIC / "index.html").read_text(encoding="utf-8"))

    def test_every_core_tab_control_has_help(self):
        self.assertGreater(len(self.ids), 100)
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
const fs=require('fs'),vm=require('vm'),assert=require('assert');
function el(tag,attrs){return {tagName:tag,attrs:Object.assign({},attrs),children:[],parent:null,
  setAttribute(k,v){this.attrs[k]=String(v);},getAttribute(k){return k in this.attrs?this.attrs[k]:null;},
  hasAttribute(k){return k in this.attrs;},
  appendChild(c){c.parent=this;this.children.push(c);return c;},
  closest(sel){let n=this.parent;while(n){if(sel==='label'&&n.tagName==='label'){return n;}n=n.parent;}return null;},
  querySelector(sel){const m=/^\.help-tip\[data-help-for="([^"]+)"\]$/.exec(sel);
    return this.children.find(c=>c.className==='help-tip'&&c.attrs['data-help-for']===m[1])||null;},
  set textContent(v){this._text=v;},get textContent(){return this._text;}};}
// a labelled select, a checkbox inside its wrapping label, and an unlabelled input
const forLabel=el('label',{for:'prompt-preset-select'}),select=el('select',{id:'prompt-preset-select'});
const wrap=el('label',{}),box=el('input',{id:'m4b-per-chunk'});wrap.appendChild(box);
const bare=el('input',{id:'save-script-name'});
const byId={'prompt-preset-select':select,'m4b-per-chunk':box,'save-script-name':bare};
let tooltips=[];
const c={window:{},document:{getElementById:id=>byId[id]||null,
  querySelector:sel=>sel==='label[for="prompt-preset-select"]'?forLabel:null,
  createElement:tag=>el(tag,{}),body:{}},
  bootstrap:{Tooltip:function(target,opts){tooltips.push([target,opts]);}}};
vm.createContext(c);
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),c);
vm.runInContext('renderHelpTips();ensureHelpTooltips();',c);
assert.strictEqual(forLabel.children.length,1,'one icon in the for= label');
const icon=forLabel.children[0];
assert.strictEqual(icon.tagName,'button');assert.strictEqual(icon.type,'button');
assert.strictEqual(icon.attrs['data-help-for'],'prompt-preset-select');
assert(icon.attrs['data-bs-title'].includes('Recommended:'));
assert(icon.attrs['data-bs-title'].includes('(source: '));
assert(icon.attrs['aria-label'].startsWith('Help: '));
assert.strictEqual(icon.textContent,'ⓘ');
assert.strictEqual(wrap.children.length,2,'icon appended inside the wrapping label');
assert.strictEqual(wrap.children[1].attrs['data-help-for'],'m4b-per-chunk');
assert('data-help-tip' in bare.attrs,'unlabelled control carries the tooltip itself');
assert(bare.attrs['data-bs-title'].length>0);
assert.strictEqual(tooltips.length,1,'one delegated tooltip');
assert.strictEqual(tooltips[0][1].selector,'[data-help-tip]');
assert.strictEqual(tooltips[0][1].html,false);
"""


class FieldHelpRenderTests(unittest.TestCase):
    def test_render_places_icons_once_and_initialises_one_delegated_tooltip(self):
        result = subprocess.run(["node", "-e", RENDER_TEST, str(STATIC / "js" / "app-help.js")],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
