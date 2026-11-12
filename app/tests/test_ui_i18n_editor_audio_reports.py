"""The Editor, Audio, Reports and Home surfaces render in every locale.

P7 keyed the editor, audio and reports panes and the home workspace, and the
user-facing text of app-reports.js, app-workspace.js, app-take-history.js,
app-editor-review.js, app-readalong.js, app-audiobookshelf.js, the sound
modules and the Editor/Audio strings in app-core.js. These tests run the real
catalogs and real module code against the existing dependency-free Node DOM
harness, so they verify what a reader actually sees:

  * sampled pane nodes paint each locale and restore the authored English;
  * an authored placeholder follows the locale;
  * a safety markup node keeps the English sentence beneath the translation;
  * the real report kind label, the real take-state label and the real editor
    issue label translate;
  * the destructive export, regeneration and purge confirms keep the English
    underneath the translation.

Every test references at least one key the P6 tree does not have, so all of
them fail on origin/feat/ui-i18n-p6.
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

MARKUP_KEYS = (
    'editor.toolbar.shortcuts',
    'editor.header.title',
    'editor.queue.title',
    'editor.toolbar.merge-all',
    'audio.header.title',
    'audio.logs.title',
    'audio.m4b.title',
    'reports.title',
    'reports.checkpoints.title',
    'home.workspace.title',
    'home.workspace.running',
)
PLACEHOLDER_KEY = 'audio.abs.api-key-placeholder'
SAFETY_MARKUP_KEY = 'safety.abs.rights'
SAFETY_CONFIRMS = (
    ('safety.take.purge',
     'Permanently remove this snapshot? This frees its archive storage and cannot be undone. The selected line audio is kept.'),
    ('safety.core.editor.regenerate-all',
     'Regenerate all {count} non-empty chunks? This will replace existing audio.'),
    ('safety.sound.mixes.discard-confirm',
     'Permanently discard this saved mix and its approval? Original dialogue takes and imported sounds are kept.'),
    ('safety.core.export.preset.delete',
     'Delete chapter filename preset "{name}"? This cannot be undone unless you exported your presets.'),
)


def js(value):
    return json.dumps(value, ensure_ascii=False)


PREAMBLE = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
document.documentElement=new Element('html');
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this._html??'';},set(value){this._html=String(value);}});
function addNode(attrs,text){const node=body.appendChild(new Element('div',attrs));node.textContent=text;return node;}
'''


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


class LocaleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def run_node(self, body, modules=(), setup=''):
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

    def catalogs_js(self, drop=()):
        calls = []
        for locale in LOCALES:
            messages = {key: self.catalogs[locale][key] for key in self.catalogs[locale]
                        if not key.startswith('_') and key not in drop}
            payload = {'meta': {'review_status': {'all': 'machine-draft', 'safety': 'not-reviewed'}},
                       'messages': messages}
            calls.append("vm.runInContext('uiCatalogs.set(' + " + js(js(locale)) + " + ',' + "
                         + js(js(payload)) + " + ')', context);")
        return '\n'.join(calls)

    def slice_source(self, name, start, end):
        text = (STATIC / 'js' / name).read_text(encoding='utf-8')
        first, second = text.index(start), text.index(end, text.index(start))
        return text[first:second]


class MarkupRenderTests(LocaleTestCase):
    def test_editor_audio_reports_and_home_nodes_paint_each_locale_and_restore_english(self):
        for locale in LOCALES:
            for key in MARKUP_KEYS:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        expected = {locale: {key: self.catalogs[locale][key] for key in MARKUP_KEYS} for locale in LOCALES}
        expected['en'] = {key: self.english[key] for key in MARKUP_KEYS}
        self.run_node(
            self.catalogs_js() + '\n'
            + ''.join(f"addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n" for key in MARKUP_KEYS)
            + "const nodes=body.querySelectorAll('[data-i18n]');\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=Object.fromEntries(nodes.map(node=>[node.getAttribute('data-i18n'),node.textContent]));\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});\n"
            + f"assert.strictEqual(nodes.length,{len(MARKUP_KEYS)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_audiobookshelf_api_key_placeholder_follows_the_locale(self):
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][PLACEHOLDER_KEY], self.english[PLACEHOLDER_KEY])
        expected = {locale: self.catalogs[locale][PLACEHOLDER_KEY] for locale in LOCALES}
        expected['en'] = self.english[PLACEHOLDER_KEY]
        self.run_node(
            self.catalogs_js() + '\n'
            + f"const input=body.appendChild(new Element('input',{{'data-i18n-attr':{js('placeholder:' + PLACEHOLDER_KEY)}}}));\n"
            + f"input.setAttribute('placeholder',{js(self.english[PLACEHOLDER_KEY])});\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=input.getAttribute('placeholder');\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_a_safety_node_keeps_english_below_each_translation(self):
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][SAFETY_MARKUP_KEY], self.english[SAFETY_MARKUP_KEY])
        self.run_node(
            self.catalogs_js() + '\n'
            + f"const node=addNode({{'data-i18n':{js(SAFETY_MARKUP_KEY)}}},{js(self.english[SAFETY_MARKUP_KEY])});\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=[...node.children].map(child=>[child.className,child.textContent]);\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen['zh-CN'],[['ui-safety-primary',{js(self.catalogs['zh-CN'][SAFETY_MARKUP_KEY])}],['ui-safety-secondary',{js(self.english[SAFETY_MARKUP_KEY])}]]);\n"
            + "assert.strictEqual(seen['en'].length,1);\n"
            + f"assert.strictEqual(seen['en'][0][1],{js(self.english[SAFETY_MARKUP_KEY])});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_a_missing_catalog_key_keeps_its_english(self):
        key = 'editor.header.title'
        self.assertNotEqual(self.catalogs['ja'][key], self.english[key])
        self.run_node(
            self.catalogs_js(drop=(key,)) + '\n'
            + f"const node=addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n"
            + "context.applyUiLocale('ja');\n"
            + f"assert.strictEqual(node.textContent,{js(self.english[key])});",
            modules=(STATIC / 'js' / 'app-i18n.js',))


class RealRendererTests(LocaleTestCase):
    def test_report_kind_labels_translate(self):
        keys = ('reports.kind.pauses', 'reports.kind.batch')
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        for locale in LOCALES:
            result = self.run_node(
                self.catalogs_js() + '\n'
                + "vm.runInContext(" + js(self.slice_source('app-reports.js', '        function reportKindLabel(', '        async function loadReports(')) + ",context);\n"
                + f"context.applyUiLocale({js(locale)});\n"
                + "console.log(JSON.stringify([context.reportKindLabel('pauses'),context.reportKindLabel('batch')]));",
                modules=(STATIC / 'js' / 'app-i18n.js',))
            labels = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual([self.catalogs[locale]['reports.kind.pauses'],
                              self.catalogs[locale]['reports.kind.batch']], labels)

    def test_take_state_labels_translate(self):
        key = 'take.state.incomplete'
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-take-history.js', 'function takeStateLabel(', 'function renderEditorTakeSnapshots(')) + ",context);\n"
            + "context.applyUiLocale('es-419');\n"
            + "console.log(context.takeStateLabel('incomplete'));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        self.assertEqual(self.catalogs['es-419'][key], result.stdout.strip().splitlines()[-1])

    def test_editor_issue_labels_translate(self):
        key = 'editor.review.issue.speech-review'
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-editor-review.js', 'function editorIssueLabel(', 'function editorDriftLabel(')) + ",context);\n"
            + "context.applyUiLocale('ja');\n"
            + "console.log(context.editorIssueLabel('speech_review'));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        self.assertEqual(self.catalogs['ja'][key], result.stdout.strip().splitlines()[-1])


class SafetyConfirmTests(LocaleTestCase):
    def test_each_destructive_confirm_keeps_english_beneath_the_translation(self):
        for key, english in SAFETY_CONFIRMS:
            for locale in LOCALES:
                with self.subTest(key=key, locale=locale):
                    self.assertNotEqual(self.catalogs[locale][key], english)
            values = {'count': 3, 'name': 'Preset'} if '{count}' in english or '{name}' in english else {}
            rendered = english.format(**values) if values else english
            expected = {locale: self.catalogs[locale][key].format(**values) if values else self.catalogs[locale][key] for locale in LOCALES}
            self.run_node(
                self.catalogs_js() + '\nconst seen={};\n'
                + "for(const locale of ['zh-CN','ja','es-419','en']){context.applyUiLocale(locale);\n"
                + f"seen[locale]=context.formatUiSafetyText({js(key)},{js(english)},{js(values)});}}\n"
                + f"assert.deepStrictEqual(seen['zh-CN'],{js(expected['zh-CN'] + chr(10) + rendered)});\n"
                + f"assert.deepStrictEqual(seen['ja'],{js(expected['ja'] + chr(10) + rendered)});\n"
                + f"assert.deepStrictEqual(seen['es-419'],{js(expected['es-419'] + chr(10) + rendered)});\n"
                + f"assert.strictEqual(seen['en'],{js(rendered)});",
                modules=(STATIC / 'js' / 'app-i18n.js',))


class EditorAudioKeyingTests(unittest.TestCase):
    """Source-level: the P7 panes and modules are keyed."""

    @classmethod
    def setUpClass(cls):
        cls.markup = (STATIC / 'index.html').read_text(encoding='utf-8')

    def test_panes_are_keyed(self):
        for key in MARKUP_KEYS:
            with self.subTest(key=key):
                self.assertIn(f'data-i18n="{key}"', self.markup)
        self.assertIn('data-i18n-attr="title:editor.toolbar.shortcuts.title"', self.markup)
        self.assertIn(f'data-i18n-attr="placeholder:{PLACEHOLDER_KEY}"', self.markup)
        self.assertIn(f'data-i18n="{SAFETY_MARKUP_KEY}"', self.markup)

    def test_each_module_keys_its_safety_confirms(self):
        expectations = (
            ('app-take-history.js', "formatUiSafetyText('safety.take.purge'"),
            ('app-core.js', "formatUiSafetyText('safety.core.editor.regenerate-all'"),
            ('app-sound-mixes.js', "formatUiSafetyText('safety.sound.mixes.discard-confirm'"),
            ('app-core.js', "formatUiSafetyText('safety.core.export.preset.delete'"),
        )
        for name, needle in expectations:
            with self.subTest(file=name, needle=needle):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
