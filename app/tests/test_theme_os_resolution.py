"""Theme resolution for #2058: stored choice > OS colour scheme > light.

Runs the real app-core.js theme helpers in the dependency-free Node DOM harness,
so the resolution order and the persist/no-persist distinction are exercised
against shipped code rather than a re-implementation. The inline anti-flash
snippet in index.html is a deliberate first-paint copy; a source check keeps it
in step with the resolver so the two cannot drift.
"""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
CORE = STATIC / 'js' / 'app-core.js'
INDEX = STATIC / 'index.html'
TOKENS = STATIC / 'css' / 'navbar-theme.css'
HELPERS_START = 'const THEMES = ['
INIT_START = '// Sync button label/icon'
INIT_END = 'function createSerializedSaveQueue'

# DOM_FIXTURE gives us document/body but no documentElement; applyTheme needs it.
PREAMBLE = r'''
document.documentElement = new Element('html');
Element.prototype.append = function(...children){for(const child of children){this.appendChild(child);}};
'''


def js(value):
    return json.dumps(value, ensure_ascii=False)


def slice_between(text, start, end):
    first = text.index(start)
    return text[first:text.index(end, first)]


class ThemeResolutionTests(unittest.TestCase):
    def run_node(self, body, setup=''):
        source = DOM_FIXTURE + PREAMBLE + setup + "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});\n"
        handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        try:
            with handle:
                handle.write(source)
            result = subprocess.run(['node', handle.name], capture_output=True, text=True,
                                    encoding='utf-8', timeout=20)
        finally:
            Path(handle.name).unlink(missing_ok=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def helpers(self):
        return slice_between(CORE.read_text(encoding='utf-8'), HELPERS_START, INIT_START)

    def init_slice(self):
        return slice_between(CORE.read_text(encoding='utf-8'), INIT_START, INIT_END)

    def test_resolution_order_stored_then_os_then_light(self):
        self.run_node('''
for(const id of ['theme-label','theme-icon','theme-toggle']){body.appendChild(new Element('div',{id}));}
''' + 'vm.runInContext(' + js(self.helpers()) + ',context);\n' + r'''
const cases = [
 [null,        false, 'light'],
 [null,        true,  'night'],
 ['night',     false, 'night'],
 ['bread',     true,  'bread'],
 ['light',     true,  'light'],
 ['super-night', false, 'super-night'],
 ['bogus',     true,  'night'],
 ['bogus',     false, 'light'],
];
for (const [stored, osDark, expected] of cases) {
 assert.strictEqual(context.resolveThemeKey(stored, osDark), expected,
   'stored=' + stored + ' osDark=' + osDark);
}
''')

    def test_prefers_dark_falls_back_safely(self):
        self.run_node(
            'context.window.matchMedia = q => ({matches: q === "(prefers-color-scheme: dark)"});\n'
            'vm.runInContext(' + js(self.helpers()) + ',context);\n' + r'''
assert.strictEqual(context.prefersDarkNow(), true);
context.window.matchMedia = () => ({matches: false});
assert.strictEqual(context.prefersDarkNow(), false);
delete context.window.matchMedia;
assert.strictEqual(context.prefersDarkNow(), false, 'no matchMedia -> light');
context.window.matchMedia = () => { throw Error('blocked'); };
assert.strictEqual(context.prefersDarkNow(), false, 'blocked matchMedia -> light');
''')

    def test_apply_theme_persists_only_explicit_choices(self):
        self.run_node('''
for(const id of ['theme-label','theme-icon','theme-toggle']){body.appendChild(new Element('div',{id}));}
let stored = null; const writes = [];
context.localStorage={getItem(){return stored;},setItem(key,value){writes.push([key,value]);stored=value;}};
vm.runInContext(''' + js(self.helpers()) + ''',context);
''' + r'''
writes.length = 0;
context.applyTheme('night', false);
assert.strictEqual(document.documentElement.getAttribute('data-theme'), 'night');
assert.strictEqual(writes.length, 0, 'OS/implicit theme must not create a stored choice');
context.applyTheme('bread');
assert.deepStrictEqual(writes.at(-1), ['alex-theme', 'bread'], 'explicit cycle must persist');
''')

    def test_init_follows_os_until_user_picks_a_theme(self):
        self.run_node('''
for(const id of ['theme-label','theme-icon','theme-toggle']){body.appendChild(new Element('div',{id}));}
let stored = null, dark = true, changeHandler = null;
const writes = [];
context.localStorage={getItem(){return stored;},setItem(key,value){writes.push([key,value]);stored=value;}};
context.window.matchMedia = query => {
 assert.strictEqual(query, '(prefers-color-scheme: dark)');
 return {get matches(){return dark;}, addEventListener(name,fn){assert.strictEqual(name,'change');changeHandler=fn;}};
};
context.restoreVoiceReviewVisibility=()=>{};
vm.runInContext(''' + js(self.helpers() + '\n' + self.init_slice()) + ''',context);
''' + r'''
// OS dark on a fresh profile: night, with no stored override.
assert.strictEqual(document.documentElement.getAttribute('data-theme'), 'night');
assert.strictEqual(writes.length, 0);
// OS flips to light: the app follows while no explicit choice is stored.
dark = false; changeHandler();
assert.strictEqual(document.documentElement.getAttribute('data-theme') || 'light', 'light');
assert.strictEqual(writes.length, 0);
// The user picks a theme: cycleTheme persists it.
context.cycleTheme();
assert.strictEqual(stored, 'night');
// A later OS flip must be ignored because an explicit choice now exists.
dark = true; changeHandler();
assert.strictEqual(document.documentElement.getAttribute('data-theme'), 'night');
assert.strictEqual(stored, 'night', 'OS change must not overwrite the stored choice');
''')

    def test_invalid_stored_value_does_not_block_os_following(self):
        self.run_node('''
for(const id of ['theme-label','theme-icon','theme-toggle']){body.appendChild(new Element('div',{id}));}
let stored = 'bogus', dark = true, changeHandler = null;
context.localStorage={getItem(){return stored;},setItem(_k,v){stored=v;}};
context.window.matchMedia = () => ({get matches(){return dark;}, addEventListener(_n,fn){changeHandler=fn;}});
context.restoreVoiceReviewVisibility=()=>{};
vm.runInContext(''' + js(self.helpers() + '\n' + self.init_slice()) + ''',context);
''' + r'''
// A corrupt/legacy stored value resolves to the OS theme on load.
assert.strictEqual(document.documentElement.getAttribute('data-theme'), 'night');
assert.strictEqual(stored, 'bogus', 'implicit resolution must not overwrite storage');
// A later OS flip must still be followed: "bogus" is not an explicit choice.
dark = false; changeHandler();
assert.strictEqual(document.documentElement.getAttribute('data-theme') || 'light', 'light');
''')

    def test_anti_flash_snippet_mirrors_the_resolver(self):
        source = INDEX.read_text(encoding='utf-8')
        snippet = slice_between(source, '/* Anti-flash:', '</script>')
        # Derive the expected key list from THEMES so adding a theme cannot leave
        # the two copies quietly out of step.
        keys = re.findall(r"\{ key: '([^']+)'", self.helpers())
        self.assertEqual(6, len(keys))
        self.assertIn('[' + ', '.join("'" + k + "'" for k in keys) + ']', snippet)
        self.assertIn("(prefers-color-scheme: dark)", snippet)
        self.assertIn("t = dark ? 'night' : 'light';", snippet)
        self.assertIn("localStorage.getItem('alex-theme')", snippet)
        # The snippet must never persist a resolved OS theme into the override.
        self.assertNotIn('setItem', snippet)

    def test_legacy_colors_removed_and_tokens_used(self):
        source = INDEX.read_text(encoding='utf-8')
        for legacy in ('#c4dcff', '#edf4ff', '#a5b4c8'):
            self.assertNotIn(legacy, source)
        self.assertIn('color: var(--nav-link);', source)
        self.assertIn('color: var(--nav-control); border-color: var(--nav-control-border);', source)
        self.assertIn('navbar-theme.css', source)
        tokens = TOKENS.read_text(encoding='utf-8')
        for token in ('--nav-link:', '--nav-control:', '--nav-control-border:'):
            self.assertIn(token, tokens)
        for theme in ('night', 'super-night', 'cyberpunk', 'bread', 'leek'):
            self.assertIn('html[data-theme="' + theme + '"]', tokens)


if __name__ == '__main__':
    unittest.main()
