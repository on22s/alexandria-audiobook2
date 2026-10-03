"""Script-tab cast list (#653): the page is wired, model text stays text, and a
batch waiting on a manual reply shows the paste panel."""
import re
import subprocess
import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parent.parent / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
CORE = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")


class CastListUITests(unittest.TestCase):
    def test_every_element_the_script_uses_is_on_the_page(self):
        for element_id in ("btn-build-cast-list", "btn-cancel-cast-list", "cast-list-status",
                           "cast-list-panel", "script-batch-build-cast-lists"):
            self.assertIn(f'id="{element_id}"', INDEX, element_id)
            self.assertIn(f"'{element_id}'", CORE, element_id)
        self.assertIn('id="btn-edit-cast-list"', INDEX)

    def test_every_handler_the_page_calls_exists(self):
        handlers = set(re.findall(r'onclick="(\w+)\(', INDEX))
        for name in ("buildCastList", "loadCastList", "cancelCastList"):
            self.assertIn(name, handlers)
            self.assertRegex(CORE, rf"(async function {name}\(|window\.{name} =)")
        for name in ("addCastListRow", "saveCastList", "deleteCastList"):
            self.assertRegex(CORE, rf"(async function {name}\(|window\.{name} =)")

    def test_batch_poller_renders_the_manual_request(self):
        start = CORE.index("_startPolling('batch_script'")
        block = CORE[start:CORE.index("loadSavedScripts();", start)]
        self.assertIn("renderManualRequest(state, 'batch_script')", block)
        self.assertIn("renderManualRequest({ running: false }, 'batch_script')", block)

    def test_a_name_from_the_model_is_rendered_as_text(self):
        script = r'''
const fs = require('fs');
const core = fs.readFileSync(process.argv[1], 'utf8');
const take = (name) => {
    const start = core.indexOf('function ' + name + '(');
    let depth = 0, i = core.indexOf('{', start);
    for (; i < core.length; i++) {
        if (core[i] === '{') { depth++; }
        if (core[i] === '}') { depth--; if (depth === 0) { break; } }
    }
    return core.slice(start, i + 1);
};
eval(take('escapeHtml') + take('getCastListRowHtml'));
process.stdout.write(getCastListRowHtml({name: '<img src=x onerror=alert(1)>', aliases: ['"><b>X</b>']}));
'''
        html = subprocess.run(["node", "-e", script, str(STATIC / "js" / "app-core.js")],
                              check=True, capture_output=True, text=True).stdout
        self.assertNotIn("<img", html)
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)


if __name__ == "__main__":
    unittest.main()
