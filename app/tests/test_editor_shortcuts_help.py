"""The Editor's shortcut help and its key handler must name the same keys (#2061).

The help dialog reads one data structure in app-guides.js. The key handler lives
in app-editor-review.js. Neither file can import the other's list, so these
tests compare the two sources directly: a documented key the handler does not
consume, or a handled key nobody can discover, fails here. The render test runs
the real module in the shared Node DOM harness, so the dialog is proven to be
built from that list rather than from hard-coded HTML.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
I18N = STATIC / 'i18n'
GUIDES = STATIC / 'js' / 'app-guides.js'
EDITOR = STATIC / 'js' / 'app-editor-review.js'
I18N_JS = STATIC / 'js' / 'app-i18n.js'
SETTINGS = STATIC / 'js' / 'app-settings.js'
INDEX = STATIC / 'index.html'
LOCALES = ('zh-CN', 'ja', 'es-419')

SHORTCUT_CODES = re.compile(r'codes: \[([^\]]+)\]')
QUOTED = re.compile(r'"([^"]+)"')
HANDLED = re.compile(r"event\.code === '([A-Za-z]+)'")
HELP_KEY = re.compile(r"t\('(editor\.shortcuts\.[a-z0-9.-]+)'")
# The key names a keyboard prints are the same glyph in every language.
KEEP_GLYPH = {'R', 'E', '↑ ↓'}

RENDER = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}this.textContent='';this.append(...children);};
for(const [tag,id] of [['div','task-guide-cards'],['ul','editor-shortcuts-list']]){body.appendChild(new Element(tag,{id}));}
context.currentTabName='home';context.activateTab=()=>{};context.isEditorEditPending=()=>false;context.showToast=()=>{};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const items=document.getElementById('editor-shortcuts-list').children;
assert.strictEqual(items.length,4,'one help row per documented shortcut');
const first=items[0].children;
assert.strictEqual(first[0].tagName,'KBD');
assert.strictEqual(first[1].tagName,'STRONG');
assert.strictEqual(first[0].textContent,'Space');
assert.strictEqual(first[1].textContent,'Audition the selected line');
assert(first[2].textContent.includes('saved audio'));
assert.strictEqual(items[1].children[0].textContent,'↑ ↓');
assert.strictEqual(items[2].children[1].textContent,'Regenerate the selected line');
assert.strictEqual(items[3].children[1].textContent,'Expand the row and edit its text');
'''


LOCALE_RENDER = r'''
(async()=>{
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}this.textContent='';this.append(...children);};
for(const [tag,id] of [['div','task-guide-cards'],['ul','editor-shortcuts-list'],['div','setup-settings-search-status']]){body.appendChild(new Element(tag,{id}));}
context.currentTabName='home';context.activateTab=()=>{};context.isEditorEditPending=()=>false;context.showToast=()=>{};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
vm.runInContext('uiCatalogs.set("ja", {messages:' + JSON.stringify({
  'editor.shortcuts.key.space': 'スペース',
  'editor.shortcuts.key.arrows': '↑ ↓',
  'editor.shortcuts.space.action': '選択した行を試聴',
  'editor.shortcuts.regenerate.action': '選択した行を再生成',
  'editor.shortcuts.edit.action': '行を展開してテキストを編集',
}) + '})',context);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
let rows=document.getElementById('editor-shortcuts-list').children;
assert.strictEqual(rows.length,4);
assert.strictEqual(rows[0].children[0].textContent,'Space','English is the first paint');
assert.strictEqual(rows[2].children[1].textContent,'Regenerate the selected line');
// Slice the real locale-change entry point out of app-settings.js. Its own
// renderer list is what must repaint the JS-built rows; a data-i18n pass does
// not see them.
context.renderSetupSearchStatus=()=>{};
const settings=fs.readFileSync(process.argv[3],'utf8');
vm.runInContext(settings.slice(settings.indexOf('function renderUiLocaleChrome()'),settings.indexOf('function renderSetupSearchStatus()')),context);
await context.applyUiLocaleAndSearchStatus('ja');
rows=document.getElementById('editor-shortcuts-list').children;
assert.strictEqual(rows.length,4);
assert.strictEqual(rows[0].children[0].textContent,'スペース','the key label follows the UI language');
assert.strictEqual(rows[2].children[1].textContent,'選択した行を再生成','the action follows the UI language');
assert.strictEqual(rows[3].children[1].textContent,'行を展開してテキストを編集');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''


def messages(path):
    payload = json.loads(path.read_text(encoding='utf-8'))
    return {key: value for key, value in payload.items() if key != '_meta'}


class ShortcutSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guides = GUIDES.read_text(encoding='utf-8')
        cls.editor = EDITOR.read_text(encoding='utf-8')
        cls.index = INDEX.read_text(encoding='utf-8')
        cls.english = messages(I18N / 'ui.en.json')
        cls.help = cls.guides[cls.guides.index('function buildEditorShortcutHelp'):
                              cls.guides.index('function renderEditorShortcutHelp')]

    def test_the_documented_keys_are_exactly_the_keys_the_handler_consumes(self):
        start = self.editor.index("document.addEventListener('keydown'")
        end = self.editor.index('\n});', start)
        handled = {code.lower() for code in HANDLED.findall(self.editor[start:end])}
        documented = {code.lower()
                      for block in SHORTCUT_CODES.findall(self.help)
                      for code in QUOTED.findall(block)}
        self.assertEqual({'space', 'arrowdown', 'arrowup', 'keyr', 'keye'}, handled,
                         'the Editor key handler changed; update the help list with it')
        self.assertEqual(handled, documented,
                         'the shortcut help and the key handler must document the same keys')

    def test_every_help_row_carries_translated_action_and_detail(self):
        entries = SHORTCUT_CODES.findall(self.help)
        self.assertEqual(4, len(entries))
        keys = HELP_KEY.findall(self.help)
        self.assertEqual(12, len(keys), 'each row needs a key label, action and detail')
        for key in keys:
            self.assertIn(key, self.english, f'{key} is not in ui.en.json')
        for locale in LOCALES:
            translated = messages(I18N / f'ui.{locale}.json')
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertIn(key, translated)
                    if self.english[key] not in KEEP_GLYPH:
                        self.assertNotEqual(self.english[key], translated[key],
                                            f'{locale} left {key} untranslated')

    def test_the_help_dialog_is_reachable_from_the_editor_toolbar(self):
        self.assertIn('id="editorShortcutsModal"', self.index)
        self.assertRegex(self.index, r'id="btn-editor-shortcuts"[^>]*'
                                     r'data-bs-target="#editorShortcutsModal"')
        self.assertRegex(self.index, r'id="editor-shortcuts-list"[^>]*'
                                     r'aria-label=')
        self.assertIn('data-i18n="editor.shortcuts.context"', self.index)
        self.assertIn('editor.shortcuts.list-aria', self.index)
        # The authoring button is a real control, not a badge that only mouse
        # users can read: type=button makes it focusable and Enter/Space opens it.
        self.assertRegex(self.index, r'<button[^>]*id="btn-editor-shortcuts"[^>]*type="button"')

    def test_the_dialog_renders_from_the_shared_list(self):
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + RENDER + '\n', str(GUIDES)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_dialog_rows_follow_a_locale_change(self):
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + LOCALE_RENDER + '\n',
             str(I18N_JS), str(GUIDES), str(SETTINGS)],
            capture_output=True, text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
