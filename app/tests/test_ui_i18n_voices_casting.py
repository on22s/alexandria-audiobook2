"""The Voices and Casting surfaces render in every locale.

P6 keyed the voices and casting panes, the Casting library module, the research
toolbar, the pronunciation panel's messages and the character roster's messages.
These tests run the real catalogs and real module code against the existing
dependency-free Node DOM harness, so they verify what a reader actually sees:

  * sampled voices/casting markup nodes paint each locale and restore the
    authored English, an authored placeholder follows the locale, and a
    safety.* node keeps the English sentence beneath the translation;
  * the real casting status bar renders translated status and priority labels;
  * the real pronunciation preview renders translated decision labels;
  * the real character-roster combine error carries a translated message;
  * the destructive confirms (cast replace, roster discard, voice discard,
    line-rule discard) keep the English underneath the translation.

Every test references at least one key the P5 tree does not have, so all of
them fail on origin/feat/ui-i18n-p5.
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
    'voices.header.title',
    'voices.intro',
    'voices.narrator.label',
    'voices.personas.generate',
    'voices.cast.title',
    'voices.single.title',
    'voices.logs.title',
    'casting.header.title',
    'casting.find.title',
    'casting.review.title',
    'casting.saved.title',
    'casting.activity.title',
)
PLACEHOLDER_KEY = 'casting.review.notes-placeholder'
SAFETY_MARKUP_KEY = 'safety.casting.similarity_unavailable'
SAFETY_CONFIRMS = (
    ('safety.casting.replace_voices',
     'Replace voices for the selected characters in the active book?'),
    ('safety.roster.discard-edits', 'Discard unsaved character-review edits?'),
    ('safety.voice.discard_all', 'Discard all unsaved voice edits and reload the saved voices?'),
    ('safety.pron.line_discard', 'Discard unsaved line rules and load the chosen line?'),
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


class MarkupRenderTests(LocaleTestCase):
    def test_voices_and_casting_nodes_paint_each_locale_and_restore_english(self):
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

    def test_casting_notes_placeholder_follows_the_locale(self):
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
        key = 'voices.header.title'
        self.assertNotEqual(self.catalogs['ja'][key], self.english[key])
        self.run_node(
            self.catalogs_js(drop=(key,)) + '\n'
            + f"const node=addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n"
            + "context.applyUiLocale('ja');\n"
            + f"assert.strictEqual(node.textContent,{js(self.english[key])});",
            modules=(STATIC / 'js' / 'app-i18n.js',))


class RealRendererTests(LocaleTestCase):
    def slice_source(self, name, start, end):
        text = (STATIC / 'js' / name).read_text(encoding='utf-8')
        first, second = text.index(start), text.index(end, text.index(start))
        return text[first:second]

    def test_casting_status_bar_renders_translated_labels(self):
        keys = ('casting.status.shortlist', 'casting.priority.high')
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        for locale in LOCALES:
            result = self.run_node(
                self.catalogs_js() + '\n'
                + "vm.runInContext(" + js(self.slice_source('app-casting.js', '    const PAGE = 48', '    function playButton(')) + ",context);\n"
                + f"context.applyUiLocale({js(locale)});\n"
                + "const bar=context.statusBar({id:'v1',name:'ABEL'},{status:'shortlist',priority:'high'});\n"
                + "console.log(JSON.stringify(bar.children.map(child=>child.textContent)));",
                modules=(STATIC / 'js' / 'app-i18n.js',))
            labels = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual([self.catalogs[locale]['casting.status.shortlist'],
                              self.catalogs[locale]['casting.priority.high']], labels)

    def test_pronunciation_preview_renders_translated_decisions(self):
        key = 'pron.decision.applied-respelling'
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "for(const id of ['pronunciation-occurrence-text','pronunciation-occurrence-status']){body.appendChild(new Element('div',{id}));}\n"
            + "vm.runInContext(" + js(self.slice_source('app-pronunciation.js', 'function setOccurrencePronunciationStatus(', 'async function loadOccurrencePronunciation(')) + ",context);\n"
            + "context.applyUiLocale('zh-CN');\n"
            + "context.showOccurrencePronunciationPreview({preview:{text:'Line',decisions:[{applied:true,reason:'applied_respelling'}]},context_source:'configured',audio_will_be_detached:false},"
            + js(self.catalogs['zh-CN']['pron.status.line-loaded']) + ");\n"
            + "console.log(document.getElementById('pronunciation-occurrence-status').textContent);",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        text = result.stdout.strip().splitlines()[-1]
        self.assertIn(self.catalogs['zh-CN'][key], text)
        self.assertIn(self.catalogs['zh-CN']['pron.status.line-loaded'], text)

    def test_roster_combine_refusal_carries_a_translated_message(self):
        key = 'roster.error.combine-limits'
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-character-roster.js', '    const copy = ', '    function merge()')) + ",context);\n"
            + "context.applyUiLocale('ja');\n"
            + "const aliases=Array.from({length:33},(_,i)=>'A'+i);\n"
            + "let message='';\n"
            + "try{context.combine([{canonical_name:'X',aliases,evidence:[]}],{canonical_name:'X',aliases:[],evidence:[]});}catch(error){message=error.message;}\n"
            + "console.log(message);",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        self.assertEqual(self.catalogs['ja'][key], result.stdout.strip().splitlines()[-1])


class SafetyConfirmTests(LocaleTestCase):
    def test_each_destructive_confirm_keeps_english_beneath_the_translation(self):
        for key, english in SAFETY_CONFIRMS:
            for locale in LOCALES:
                with self.subTest(key=key, locale=locale):
                    self.assertNotEqual(self.catalogs[locale][key], english)
            expected = {locale: self.catalogs[locale][key] + '\n' + english for locale in LOCALES}
            expected['en'] = english
            calls = '\n'.join(
                f"seen[{js(locale)}]=context.formatUiSafetyText({js(key)},{js(english)});"
                for locale in ('zh-CN', 'ja', 'es-419', 'en'))
            self.run_node(
                self.catalogs_js() + '\nconst seen={};\n'
                + "for(const locale of ['zh-CN','ja','es-419','en']){context.applyUiLocale(locale);\n"
                + f"seen[locale]=context.formatUiSafetyText({js(key)},{js(english)});}}\n"
                + f"assert.deepStrictEqual(seen,{js(expected)});",
                modules=(STATIC / 'js' / 'app-i18n.js',))


class VoicesCastingKeyingTests(unittest.TestCase):
    """Source-level: the P6 surfaces are keyed in markup and in each module."""

    @classmethod
    def setUpClass(cls):
        cls.markup = (STATIC / 'index.html').read_text(encoding='utf-8')

    def test_voices_and_casting_markup_is_keyed(self):
        for key in MARKUP_KEYS:
            with self.subTest(key=key):
                self.assertIn(f'data-i18n="{key}"', self.markup)
        self.assertIn(f'data-i18n-attr="placeholder:{PLACEHOLDER_KEY}"', self.markup)
        self.assertIn(f'data-i18n="{SAFETY_MARKUP_KEY}"', self.markup)

    def test_each_module_keys_its_safety_confirms(self):
        expectations = {
            'app-casting.js': "formatUiSafetyText('safety.casting.use_voice'",
            'app-character-roster.js': "formatUiSafetyText('safety.roster.discard-edits'",
            'app-pronunciation.js': "formatUiSafetyText('safety.pron.line_discard'",
            'app-casting-research.js': "formatUiSafetyText('safety.research.delete_permanently.many'",
            'app-core.js': "formatUiSafetyText('safety.voice.discard_all'",
        }
        for name, needle in expectations.items():
            with self.subTest(file=name):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
