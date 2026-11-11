"""The Script pane, the Workbench and the Guides render in every locale.

P5 keyed the Script pane's text nodes and title/placeholder/aria-label
attributes, the task-guide copy in app-guides.js and the Dataset Builder
(Workbench) copy in app-workbench.js. These tests run the real modules in the
existing Node DOM harness against the real catalogs, so they verify what a
reader or a rendered row actually shows:

  * every sampled Script node paints its locale's text and an authored
    placeholder follows the locale, then English restores the authored text;
  * a rendered Workbench sample row carries translated aria-labels, a
    translated placeholder and a translated status badge, and its Generate vs
    Regenerate title flips with the row state;
  * a Workbench destructive confirm keeps the authored English on the line
    beneath the translation (formatUiSafetyText, the string-sink form of
    tSafety), and shows English alone when the UI is English;
  * the Guides paint their real titles, step text and step heading in each
    locale, and fall back to the authored English when a catalog is absent.

Every test asserts at least one key that the P4 tree does not have, so all of
them fail on origin/feat/ui-i18n-p4.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
I18N = STATIC / 'i18n'
LOCALES = ('zh-CN', 'ja', 'es-419')

SCRIPT_KEYS = (
    'script.header.title',
    'script.upload.label',
    'script.docx.accept',
    'script.review.start',
    'script.roster.title',
    'script.saved.title',
    'script.logs.title',
)
SCRIPT_SAFETY_KEY = 'safety.script.run.start-over'
PLACEHOLDER_KEY = 'script.narrator.first-person.placeholder'

PREAMBLE = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
document.documentElement=new Element('html');
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this._html??'';},set(value){this._html=String(value);}});
function addNode(attrs,text){const node=body.appendChild(new Element('div',attrs));node.textContent=text;return node;}
function catalog(messages,review={all:'machine-draft',safety:'not-reviewed'}){return {meta:{review_status:review},messages};}
function setUiCatalog(locale,payload){vm.runInContext('uiCatalogs.set('+JSON.stringify(locale)+','+JSON.stringify(payload)+')',context);}
context.getEscapedHtml=function(strings,...values){let out=strings[0];values.forEach((value,index)=>{out+=String(value).replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]))+strings[index+1];});return out;};
'''

GUIDE_SETUP = r'''
for(const id of ['task-guide-cards','task-guide-detail','task-guide-return','task-guide-title','task-guide-image','task-guide-step-title','task-guide-instruction','task-guide-expected','task-guide-cost','task-guide-prev','task-guide-next','task-guide-feedback','home-tab','script-tab','script-single-area']){body.appendChild(new Element('div',{id}));}
context.currentTabName='home';context.activateTab=()=>{};context.isEditorEditPending=()=>false;context.showToast=()=>{};
Element.prototype.focus=function(){};
Element.prototype.scrollIntoView=function(){};
'''


def js(value):
    return json.dumps(value, ensure_ascii=False)


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


class LocaleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def run_node(self, body, modules=(), setup=''):
        """Run the real modules in a temp file (Windows command lines are short)."""
        source = DOM_FIXTURE + PREAMBLE + setup
        for module in modules:
            source += f"vm.runInContext(fs.readFileSync({js(str(module))},'utf8'),context);\n"
        source += "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});\n"
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

    def catalogs_js(self, keys=None, drop=()):
        calls = []
        for locale in LOCALES:
            messages = {key: self.catalogs[locale][key] for key in (keys or self.catalogs[locale])
                        if not key.startswith('_') and key not in drop}
            calls.append(f"setUiCatalog({js(locale)},catalog({js(messages)}));")
        return '\n'.join(calls)


class ScriptPaneRenderTests(LocaleTestCase):
    def test_sampled_script_nodes_paint_each_locale_and_restore_english(self):
        for locale in LOCALES:
            for key in SCRIPT_KEYS + (SCRIPT_SAFETY_KEY,):
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        expected = {locale: {key: self.catalogs[locale][key] for key in SCRIPT_KEYS} for locale in LOCALES}
        expected['en'] = {key: self.english[key] for key in SCRIPT_KEYS}
        self.run_node(
            self.catalogs_js() + '\n'
            + ''.join(f"addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n" for key in SCRIPT_KEYS)
            + "const nodes=body.querySelectorAll('[data-i18n]');\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=Object.fromEntries(nodes.map(node=>[node.getAttribute('data-i18n'),node.textContent]));\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});\n"
            + f"assert.strictEqual(nodes.length,{len(SCRIPT_KEYS)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_script_placeholder_attribute_follows_the_locale(self):
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][PLACEHOLDER_KEY], self.english[PLACEHOLDER_KEY])
        expected = {locale: self.catalogs[locale][PLACEHOLDER_KEY] for locale in LOCALES}
        expected['en'] = self.english[PLACEHOLDER_KEY]
        self.run_node(
            self.catalogs_js([PLACEHOLDER_KEY]) + '\n'
            + f"const input=body.appendChild(new Element('input',{{'data-i18n-attr':{js('placeholder:' + PLACEHOLDER_KEY)}}}));\n"
            + f"input.setAttribute('placeholder',{js(self.english[PLACEHOLDER_KEY])});\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=input.getAttribute('placeholder');\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_a_script_key_missing_from_the_catalog_keeps_its_english(self):
        key = 'script.review.start'
        self.assertNotEqual(self.catalogs['zh-CN'][key], self.english[key])
        self.run_node(
            self.catalogs_js(drop=(key,)) + '\n'
            + f"const node=addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n"
            + "context.applyUiLocale('zh-CN');\n"
            + f"assert.strictEqual(node.textContent,{js(self.english[key])});",
            modules=(STATIC / 'js' / 'app-i18n.js',))


class WorkbenchRowRenderTests(LocaleTestCase):
    ROW_KEYS = ('workbench.row.emotion.aria-label', 'workbench.row.emotion.placeholder',
                'workbench.row.text.aria-label', 'workbench.row.seed.aria-label',
                'workbench.row.status.pending', 'workbench.row.generate', 'workbench.row.regenerate')

    def slice_source(self):
        text = (STATIC / 'js' / 'app-workbench.js').read_text(encoding='utf-8')
        start = text.index('function isDatasetRowGenerationRunning()')
        end = text.index('function applyDatasetTrainingSaveFeedback()')
        return text[start:end]

    def test_a_workbench_row_renders_translated_labels_and_status(self):
        for locale in LOCALES:
            for key in self.ROW_KEYS:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        for locale in LOCALES:
            result = self.run_node(
                self.catalogs_js(self.ROW_KEYS) + '\n'
                + "vm.runInContext(" + js(self.slice_source()) + ",context);\n"
                + "context.dsbBatchRunning=false;context.dsbRows=[];\n"
                + "context.applyUiLocale(" + js(locale) + ");\n"
                + "context.__pending=context.dsbBuildRowHtml({emotion:'',text:'hello',seed:'',status:'pending'},0);\n"
                + "context.__done=context.dsbBuildRowHtml({emotion:'',text:'hello',seed:'',status:'done'},0);\n"
                + "console.log(JSON.stringify([context.__pending,context.__done]));",
                modules=(STATIC / 'js' / 'app-i18n.js',),
                setup="context.window=context.window||{};")
            pending, done = json.loads(result.stdout.strip().splitlines()[-1])
            emotion = self.catalogs[locale]['workbench.row.emotion.aria-label'].replace(
                '{index}', '1')
            placeholder = self.catalogs[locale]['workbench.row.emotion.placeholder']
            status = self.catalogs[locale]['workbench.row.status.pending']
            with self.subTest(locale=locale):
                self.assertIn(f'aria-label="{emotion}"', pending)
                self.assertIn(f'placeholder="{placeholder}"', pending)
                self.assertIn(f'>{status}</span>', pending)
                self.assertIn(f'title="{self.catalogs[locale]["workbench.row.generate"]}"', pending)
                self.assertIn(f'title="{self.catalogs[locale]["workbench.row.regenerate"]}"', done)
                self.assertNotIn(self.english['workbench.row.emotion.placeholder'], pending)


class WorkbenchSafetyTextTests(LocaleTestCase):
    KEY = 'safety.workbench.project.delete'
    ENGLISH = 'Delete project "{project}" and all its samples?'

    def test_a_destructive_confirm_keeps_english_beneath_each_translation(self):
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][self.KEY], self.ENGLISH)
        expected = {locale: self.catalogs[locale][self.KEY].replace('{project}', 'Novel') + '\n'
                    + self.ENGLISH.replace('{project}', 'Novel') for locale in LOCALES}
        expected['en'] = self.ENGLISH.replace('{project}', 'Novel')
        self.run_node(
            self.catalogs_js() + '\n'
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + f" seen[locale]=context.formatUiSafetyText({js(self.KEY)},{js(self.ENGLISH)},{{project:'Novel'}});\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))


class GuideRenderTests(LocaleTestCase):
    TITLE_KEY = 'guides.first-book.title'
    STEP_KEY = 'guides.first-book.step.2.instruction'
    CHROME_KEY = 'guides.panel.step-heading'

    def test_the_guides_paint_their_translation_and_restore_english(self):
        for locale in LOCALES:
            for key in (self.TITLE_KEY, self.STEP_KEY, self.CHROME_KEY):
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        self.run_node(
            self.catalogs_js() + '\n'
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " context.renderTaskGuides();\n"
            + " context.openTaskGuide('first-book',1);\n"
            + " seen[locale]={\n"
            + "  card:document.getElementById('task-guide-cards').children[0].children[0].textContent,\n"
            + "  title:document.getElementById('task-guide-title').textContent,\n"
            + "  instruction:document.getElementById('task-guide-instruction').textContent,\n"
            + "  heading:document.getElementById('task-guide-step-title').textContent\n"
            + " };\n"
            + "}\n"
            + "const zh=seen['zh-CN'],en=seen['en'];\n"
            + f"assert.strictEqual(zh.card,{js(self.catalogs['zh-CN'][self.TITLE_KEY])});\n"
            + f"assert.strictEqual(zh.instruction,{js(self.catalogs['zh-CN'][self.STEP_KEY])});\n"
            + f"assert.strictEqual(en.instruction,{js(self.english[self.STEP_KEY])});\n"
            + "assert(zh.heading.includes(" + js(self.catalogs['zh-CN'][self.CHROME_KEY].split('{')[0]) + "));\n"
            + "assert(seen['ja'].title===(" + js(self.catalogs['ja'][self.TITLE_KEY]) + "));\n"
            + "assert(seen['es-419'].instruction===(" + js(self.catalogs['es-419'][self.STEP_KEY]) + "));",
            modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-guides.js'),
            setup=GUIDE_SETUP)

    def test_the_guides_fall_back_to_authored_english_without_a_catalog(self):
        self.run_node(
            "context.applyUiLocale('zh-CN');\n"
            "context.renderTaskGuides();\n"
            "context.openTaskGuide('first-book',1);\n"
            + f"assert.strictEqual(document.getElementById('task-guide-cards').children[0].children[0].textContent,{js(self.english[self.TITLE_KEY])});\n"
            + f"assert.strictEqual(document.getElementById('task-guide-instruction').textContent,{js(self.english[self.STEP_KEY])});\n"
            + "assert(document.getElementById('task-guide-feedback').textContent.startsWith('Steps are a checklist'));",
            modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-guides.js'),
            setup=GUIDE_SETUP)


class ScriptPaneKeyingTests(unittest.TestCase):
    """Source-level: the P5 surfaces are keyed in the markup and in the modules."""

    @classmethod
    def setUpClass(cls):
        cls.markup = (STATIC / 'index.html').read_text(encoding='utf-8')
        cls.workbench = (STATIC / 'js' / 'app-workbench.js').read_text(encoding='utf-8')
        cls.guides = (STATIC / 'js' / 'app-guides.js').read_text(encoding='utf-8')

    def test_script_nodes_and_attributes_are_keyed(self):
        for key in SCRIPT_KEYS + (SCRIPT_SAFETY_KEY,):
            with self.subTest(key=key):
                self.assertIn(f'data-i18n="{key}"', self.markup)
        self.assertIn(f'data-i18n-attr="placeholder:{PLACEHOLDER_KEY}"', self.markup)
        self.assertIn('data-i18n-skip class="form-control" id="calibre-root-url"', self.markup)

    def test_workbench_messages_and_safety_confirms_are_keyed(self):
        for key in WorkbenchRowRenderTests.ROW_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", self.workbench)
        self.assertIn("formatUiSafetyText('safety.workbench.project.delete'", self.workbench)
        self.assertIn("formatUiSafetyText('safety.workbench.sample.delete'", self.workbench)
        self.assertIn("formatUiSafetyText('safety.workbench.import.replace'", self.workbench)
        self.assertIn("formatUiSafetyText('safety.workbench.batch.regenerate'", self.workbench)

    def test_every_guide_text_is_a_literal_t_call(self):
        self.assertIn('function buildTaskGuides()', self.guides)
        self.assertEqual(106, self.guides.count("t('guides."))
        self.assertNotIn("title: 'Finish your first audiobook'", self.guides)


if __name__ == '__main__':
    unittest.main()
