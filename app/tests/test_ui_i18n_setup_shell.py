"""The Setup pane and the app shell render in every locale, and fall back to English.

P4 keyed the Setup pane's text nodes and title/placeholder/aria-label attributes,
the shell (nav, status strip, shared modals) and app-settings.js. These tests run
the real app-i18n.js in the existing Node DOM harness against the real catalogs,
so they verify what a reader sees rather than that a function returned a string:

  * every sampled Setup and shell node paints its locale's text (zh-CN, ja,
    es-419), not its authored English;
  * a `data-i18n-html` paragraph keeps its <strong>/<code> markup in every
    locale and restores the authored HTML in English;
  * a `safety.*` markup node renders the translation with the authored English
    beneath it, and English alone when the locale is English;
  * an unknown key falls back to the authored English, and switching back to
    English restores the authored text rather than keeping a translation.

Each test also asserts at least one P4 key, so all of them fail on the P3 tree,
which has neither the keys nor the HTML/safety sweep.
"""
import json
import subprocess
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
I18N = STATIC / 'i18n'
LOCALES = ('zh-CN', 'ja', 'es-419')

SETUP_KEYS = (
    'setup.overview.essential',
    'setup.overview.advanced',
    'setup.header.title',
    'setup.llm.llm-mode.label',
    'setup.llm.llm-mode.option.local',
    'setup.limits.llm-context-length.label',
    'setup.audio.pause-between-speakers.label',
    'setup.strategy.tp-segmentation.label',
    'setup.prompts.save-preset',
    'setup.save-bar.save',
)
SHELL_KEYS = (
    'shell.nav.setup',
    'shell.nav.voices',
    'shell.theme.label',
    'shell.status.build.label',
    'shell.clone-import.cancel',
    'shell.preset-editor.submit',
)
HTML_KEY = 'setup.strategy.tp-segmentation.help'
SAFETY_KEY = 'safety.voice.clone_unmeasured'
PLACEHOLDER_KEY = 'setup.limits.llm-context-length.placeholder'
ATTRIBUTE_KEYS = (
    ('input', 'placeholder', PLACEHOLDER_KEY),
    ('a', 'title', 'shell.nav.advanced.title'),
    ('optgroup', 'label', 'setup.overview.essential'),
    ('optgroup', 'label', 'setup.overview.advanced'),
)

PREAMBLE = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
document.documentElement=new Element('html');
load();
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this._html??'';},set(value){this._html=String(value);}});
function addNode(attrs,text){const node=body.appendChild(new Element('div',attrs));node.textContent=text;return node;}
function catalog(messages,review={all:'machine-draft',safety:'not-reviewed'}){return {meta:{review_status:review},messages};}
function setUiCatalog(locale,payload){vm.runInContext('uiCatalogs.set('+JSON.stringify(locale)+','+JSON.stringify(payload)+')',context);}
'''


def js(value):
    return json.dumps(value, ensure_ascii=False)


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


class SetupShellRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def run_js(self, code):
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + PREAMBLE + "\n(async()=>{\n" + code
             + "\n})().catch(error=>{console.error(error);process.exitCode=1;});",
             str(STATIC / 'js' / 'app-i18n.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def catalogs_js(self, keys):
        calls = []
        for locale in LOCALES:
            messages = {key: self.catalogs[locale][key] for key in keys}
            calls.append(f"setUiCatalog({js(locale)},catalog({js(messages)}));")
        return '\n'.join(calls)

    def nodes_js(self, keys):
        return '\n'.join(
            f"addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});" for key in keys)

    def assert_translated(self, keys):
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key],
                                        f'{locale} has no translation for {key}')

    def paint_all_locales(self, keys):
        expected = {locale: {key: self.catalogs[locale][key] for key in keys} for locale in LOCALES}
        return (
            self.catalogs_js(keys) + '\n'
            + self.nodes_js(keys) + '\n'
            + "const nodes=body.querySelectorAll('[data-i18n]');\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=Object.fromEntries(nodes.map(node=>[node.getAttribute('data-i18n'),node.textContent]));\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});\n"
            + f"assert.strictEqual(nodes.length,{len(keys)});")

    def test_the_setup_pane_paints_its_translation_in_each_locale(self):
        self.assert_translated(SETUP_KEYS)
        self.run_js(self.paint_all_locales(SETUP_KEYS))

    def test_the_app_shell_paints_its_translation_in_each_locale(self):
        self.assert_translated(SHELL_KEYS)
        self.run_js(self.paint_all_locales(SHELL_KEYS))

    def test_a_keyed_placeholder_attribute_follows_the_locale(self):
        for tag, attribute, key in ATTRIBUTE_KEYS:
            with self.subTest(attribute=attribute, key=key):
                self.assert_translated((key,))
                expected = {locale: self.catalogs[locale][key] for locale in LOCALES}
                expected['en'] = self.english[key]
                self.run_js(
                    self.catalogs_js([key]) + '\n'
                    + f"const input=body.appendChild(new Element({js(tag)},{{'data-i18n-attr':{js(attribute + ':' + key)}}}));\n"
                    + f"input.setAttribute({js(attribute)},{js(self.english[key])});\n"
                    + "const seen={};\n"
                    + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
                    + " context.applyUiLocale(locale);\n"
                    + f" seen[locale]=input.getAttribute({js(attribute)});\n"
                    + "}\n"
                    + f"assert.deepStrictEqual(seen,{js(expected)});")

    def test_a_markup_paragraph_keeps_its_tags_and_restores_english(self):
        for locale in LOCALES:
            self.assertIn('<strong>', self.catalogs[locale][HTML_KEY], locale)
            self.assertNotEqual(self.catalogs[locale][HTML_KEY], self.english[HTML_KEY], locale)
        expected = {locale: self.catalogs[locale][HTML_KEY] for locale in LOCALES}
        expected['en'] = self.english[HTML_KEY]
        self.run_js(
            self.catalogs_js([HTML_KEY]) + '\n'
            + f"const block=body.appendChild(new Element('div',{{'data-i18n-html':{js(HTML_KEY)}}}));\n"
            + f"block.innerHTML={js(self.english[HTML_KEY])};\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=block.innerHTML;\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});\n"
            + "assert(seen['zh-CN'].includes('<strong>')&&seen['zh-CN'].includes('</strong>'));\n"
            + "assert(!seen['zh-CN'].includes('Quote marks first'));")

    def test_a_safety_markup_node_shows_the_english_beneath_the_translation(self):
        english = self.english[SAFETY_KEY]
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][SAFETY_KEY], english, locale)
        self.run_js(
            self.catalogs_js([SAFETY_KEY]) + '\n'
            + f"const node=body.appendChild(new Element('span',{{'data-i18n':{js(SAFETY_KEY)}}}));\n"
            + f"node.textContent={js(english)};\n"
            + "context.applyUiLocale('zh-CN');\n"
            + "assert.strictEqual(node.children.length,2);\n"
            + "assert.strictEqual(node.children[0].className,'ui-safety-primary');\n"
            + f"assert.strictEqual(node.children[0].textContent,{js(self.catalogs['zh-CN'][SAFETY_KEY])});\n"
            + "assert.strictEqual(node.children[1].className,'ui-safety-secondary');\n"
            + f"assert.strictEqual(node.children[1].textContent,{js(english)});\n"
            + "context.applyUiLocale('en');\n"
            + "assert.strictEqual(node.children.length,1);\n"
            + f"assert.strictEqual(node.children[0].textContent,{js(english)});")

    def test_markup_literal_braces_survive_the_sweep(self):
        key = 'setup.prompts.system-prompt.the-pipeline-fills-roster-and-batch'
        english = self.english[key]
        translated = self.catalogs['zh-CN'][key]
        self.assertIn('{roster}', english)
        self.assertIn('{roster}', translated)
        self.assertNotEqual(translated, english)
        self.run_js(
            self.catalogs_js([key]) + '\n'
            + f"const node=addNode({{'data-i18n':{js(key)}}},{js(english)});\n"
            + "context.applyUiLocale('zh-CN');\n"
            + f"assert.strictEqual(node.textContent,{js(translated)});\n"
            + "assert(node.textContent.includes('{roster}')&&node.textContent.includes('{batch}'));\n"
            + "context.applyUiLocale('en');\n"
            + f"assert.strictEqual(node.textContent,{js(english)});")

    def test_an_unknown_key_falls_back_to_english_and_english_restores_authored_text(self):
        key = 'setup.llm.llm-mode.label'
        self.assertNotEqual(self.catalogs['zh-CN'][key], self.english[key])
        self.run_js(
            self.catalogs_js([key]) + '\n'
            + f"const known=addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n"
            + "const unknown=addNode({'data-i18n':'setup.future.not-translated'},'A future Setup label');\n"
            + "context.applyUiLocale('zh-CN');\n"
            + f"assert.strictEqual(known.textContent,{js(self.catalogs['zh-CN'][key])});\n"
            + "assert.strictEqual(unknown.textContent,'A future Setup label');\n"
            + "context.applyUiLocale('en');\n"
            + f"assert.strictEqual(known.textContent,{js(self.english[key])});\n"
            + "assert.strictEqual(unknown.textContent,'A future Setup label');")


class MarkupKeyingTests(unittest.TestCase):
    """Source-level: the P4 surfaces are keyed in the markup and in the catalogs."""

    @classmethod
    def setUpClass(cls):
        cls.markup = (STATIC / 'index.html').read_text(encoding='utf-8')
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def test_setup_and_shell_nodes_are_keyed(self):
        for key in SETUP_KEYS + SHELL_KEYS + (SAFETY_KEY,):
            with self.subTest(key=key):
                self.assertIn(f'data-i18n="{key}"', self.markup)
        for _tag, attribute, key in ATTRIBUTE_KEYS:
            self.assertIn(f'data-i18n-attr="{attribute}:{key}"', self.markup)
        self.assertIn(f'data-i18n-html="{HTML_KEY}"', self.markup)

    def test_every_sampled_p4_key_is_translated_in_every_locale(self):
        keys = SETUP_KEYS + SHELL_KEYS + (HTML_KEY, SAFETY_KEY, PLACEHOLDER_KEY)
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertIn(key, self.catalogs[locale])
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])

    def test_the_nav_tab_names_and_shared_modal_buttons_are_keyed(self):
        for key in ('shell.nav.setup', 'shell.nav.script', 'shell.nav.voices',
                    'shell.nav.editor', 'shell.nav.result', 'shell.nav.designer',
                    'shell.confirm.cancel', 'shell.clone-import.cancel', 'shell.preset-editor.cancel'):
            with self.subTest(key=key):
                self.assertIn(f'data-i18n="{key}"', self.markup)

    def test_the_brand_is_a_deliberate_english_skip(self):
        self.assertIn('data-i18n-skip', self.markup)
        self.assertIn('Alexandria', self.markup)


if __name__ == '__main__':
    unittest.main()
