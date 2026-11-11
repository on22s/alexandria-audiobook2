"""Help text follows the UI locale, and the setting finder indexes both languages.

WHAT THIS VERIFIES.
- Every help map entry has a catalog key derived from its stable id, the English
  harvest in ui.en.json is exactly those keys, and all three locales translate
  every one (so "it shows in Chinese" is a fact about the shipped catalogs, not
  about a fixture).
- The runtime reader really swaps text: each of the six map kinds and the inline
  item fallbacks returns the selected locale's value, and an entry with no
  translation falls back to the authored English.
- Switching locale repaints an existing tooltip, so a changed preference cannot
  leave the old language on screen.
- The setting finder matches a translated query and the English one, including
  full-width Japanese input (NFKC), while the UI is in another language.
- Rule 21: the Python map reader is compared against the maps as JavaScript
  evaluates them, and the JavaScript key derivation is compared against the
  catalog, so a drifted reader or key function fails instead of producing a
  plausible but wrong catalog.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

from tests.test_ui_help_js import DOM_FIXTURE, HELP_FIXTURE
from tests.test_ui_i18n_js import SETUP

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC = Path(__file__).resolve().parent.parent / 'static'
HELP_JS = STATIC / 'js' / 'app-help.js'
I18N_JS = STATIC / 'js' / 'app-i18n.js'
SETTINGS_JS = STATIC / 'js' / 'app-settings.js'
I18N = STATIC / 'i18n'
LOCALES = ('zh-CN', 'ja', 'es-419')


def load_extractor():
    spec = importlib.util.spec_from_file_location(
        'extract_ui_catalog', ROOT / 'tools' / 'i18n' / 'extract_ui_catalog.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXTRACT = load_extractor()


def map_keys():
    """Keys for the map entries only (not the literal inline fallbacks).

    Resolved per call so the module imports on a tree without the P3 harvester
    and each test reports its own failure instead of the module failing to load.
    """
    return set(EXTRACT.help_messages(EXTRACT.read_help_maps(HELP_JS)))


def catalog_of(locale):
    payload = json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))
    return {key: value for key, value in payload.items() if key != '_meta'}


def run_help_js(code, settings=False):
    arguments = ['node', '-e', "eval(require('fs').readFileSync(0,'utf8'));", str(HELP_JS)]
    if settings:
        arguments.append(str(SETTINGS_JS))
    # SETUP's load() runs app-help.js; t()/getUiLocale() live in app-i18n.js and
    # are only called later, so loading the locale module after it is enough.
    prelude = "vm.runInContext(fs.readFileSync(%s,'utf8'),context);\n" % json.dumps(str(I18N_JS))
    return subprocess.run(arguments, input=DOM_FIXTURE + SETUP + HELP_FIXTURE + prelude + '\n(async()=>{\n' + code
                          + '\n})().catch(error=>{console.error(error);process.exitCode=1;});',
                          capture_output=True, text=True, encoding='utf-8', timeout=30)


class HelpParityTests(unittest.TestCase):
    """The catalogs, the maps and the two key derivations agree exactly."""

    @classmethod
    def setUpClass(cls):
        cls.english = {key: value for key, value in
                       json.loads((I18N / 'ui.en.json').read_text(encoding='utf-8')).items()
                       if key.startswith('help.')}
        cls.catalogs = {locale: catalog_of(locale) for locale in LOCALES}

    def test_every_map_entry_has_a_key_and_the_catalog_has_no_extra_help_key(self):
        self.assertGreater(len(map_keys()), 700)
        harvested = {}
        EXTRACT.harvest_help(HELP_JS, harvested)
        self.assertEqual(set(self.english), set(harvested),
                         'ui.en.json help keys differ from a fresh harvest; run tools/i18n/extract_ui_catalog.py')
        self.assertTrue(map_keys().issubset(set(self.english)))

    def test_every_locale_translates_every_help_key(self):
        self.assertGreater(len(self.english), 700)
        for locale, messages in self.catalogs.items():
            translated = {key: value for key, value in messages.items() if key.startswith('help.')}
            with self.subTest(locale=locale):
                self.assertEqual(set(self.english), set(translated))
                for key, english in self.english.items():
                    with self.subTest(key=key):
                        value = translated[key]
                        self.assertTrue(value.strip())
                        if not key.endswith('.source'):
                            self.assertNotEqual(english, value,
                                                f'{key} is still English in {locale}')

    def test_the_python_map_reader_matches_the_javascript_maps(self):
        code = r'''
const src=fs.readFileSync(process.argv[1],'utf8');
vm.runInContext(src+';this.__maps={FIELD_HELP,ACTION_HELP,ITEM_HELP,NAV_HELP,DYNAMIC_FIELD_HELP,CHANGED_FIELD_HELP};',context);
process.stdout.write(JSON.stringify(context.__maps));
'''
        result = subprocess.run(['node', '-e', "eval(require('fs').readFileSync(0,'utf8'));", str(HELP_JS)],
                                input=DOM_FIXTURE + code, capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(EXTRACT.read_help_maps(HELP_JS), json.loads(result.stdout),
                         'the Python help-map reader disagrees with JavaScript; the catalog would be wrong')

    def test_the_javascript_key_derivation_matches_the_catalog(self):
        code = r'''
const src=fs.readFileSync(process.argv[1],'utf8');
vm.runInContext(src+';this.__maps={FIELD_HELP,ACTION_HELP,ITEM_HELP,NAV_HELP,DYNAMIC_FIELD_HELP,CHANGED_FIELD_HELP};',context);
const keys=[];
for(const [name,entries] of Object.entries(context.__maps)){
 if(name==='CHANGED_FIELD_HELP'){
  for(const [handler,fields] of Object.entries(entries)){for(const field of Object.keys(fields)){keys.push(context.getHelpCatalogKey(name,handler,field));}}
  continue;
 }
 for(const [id,entry] of Object.entries(entries)){
  keys.push(context.getHelpCatalogKey(name,id,name==='FIELD_HELP'?'what':null));
  if(name==='FIELD_HELP'){for(const field of ['recommend','source']){if(field in entry){keys.push(context.getHelpCatalogKey(name,id,field));}}}
 }
}
process.stdout.write(JSON.stringify(keys.sort()));
'''
        result = subprocess.run(['node', '-e', "eval(require('fs').readFileSync(0,'utf8'));", str(HELP_JS)],
                                input=DOM_FIXTURE + code, capture_output=True, text=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(sorted(map_keys()), json.loads(result.stdout),
                         'getHelpCatalogKey() in app-help.js disagrees with the harvested catalog keys')


class HelpLocaleTests(unittest.TestCase):
    def run_js(self, code, settings=False):
        result = run_help_js(code, settings)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_each_map_kind_and_the_item_fallbacks_show_the_selected_locale(self):
        self.run_js(r'''
const controls={
 field:new Element('input',{id:'llm-context-length'}),
 item:new Element('input',{id:'confirmModalOk'}),
 action:new Element('button',{'data-help-key':'saveScript'}),
 nav:new Element('a',{class:'nav-link','data-tab':'setup'}),
 dynamic:new Element('input',{class:'voice-seed'}),
 changed:new Element('textarea',{onchange:"updateChunk(2, 'text', this.value)"}),
 fallback:new Element('a',{class:'navbar-brand'}),
};
const keys={
 field:'help.field.llm-context-length.what', item:'help.item.confirmmodalok',
 action:'help.action.savescript', nav:'help.nav.setup',
 dynamic:'help.dynamic.voice-seed', changed:'help.changed.updatechunk.text',
 fallback:'help.item.brand',
};
for(const locale of ['zh-CN','ja','es-419']){
 const messages={};for(const key of Object.values(keys)){messages[key]=locale+':'+key;}
 setUiCatalog(locale,catalog(messages));
 context.applyUiLocale(locale);
 for(const [kind,control] of Object.entries(controls)){
  assert.strictEqual(context.getItemHelpText(control),locale+':'+keys[kind],locale+' '+kind);
 }
}
''')

    def test_an_entry_without_a_translation_falls_back_to_the_authored_english(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({'help.field.llm-context-length.what':'中文说明。'}));
context.applyUiLocale('zh-CN');
assert.strictEqual(context.getItemHelpText(new Element('input',{id:'llm-context-length'})),'中文说明。');
assert.strictEqual(context.getItemHelpText(new Element('input',{id:'tts-url'})),
 'Address of the external Qwen3-TTS Gradio server.');
''')

    def test_a_field_recommendation_frames_the_translated_parts(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({
 'help.field.compile-codec.what':'用 torch.compile 编译音频编解码器。',
 'help.field.compile-codec.recommend':'保持关闭：它目前对渲染速度没有影响。',
 'help.field.compile-codec.source':'#1113 / #1120 measurement',
 'help.field.recommended':'推荐：{recommend}（来源：{source}）',
}));
context.applyUiLocale('zh-CN');
const text=context.getItemHelpText(new Element('input',{id:'compile-codec'}));
assert.strictEqual(text,'用 torch.compile 编译音频编解码器。 推荐：保持关闭：它目前对渲染速度没有影响。（来源：#1113 / #1120 measurement）');
''')

    def test_the_next_tooltip_after_a_locale_change_uses_the_new_language(self):
        self.run_js(r'''
const a=body.appendChild(new Element('input',{id:'tts-url'}));
setUiCatalog('zh-CN',catalog({'help.field.tts-url.what':'外部 Qwen3-TTS Gradio 服务器的地址。'}));
context.renderHelpTips();
assert(a.getAttribute('data-bs-title').startsWith('Address of the external'));
context.applyUiLocale('zh-CN');
assert.strictEqual(a.getAttribute('data-bs-title'),'Address of the external Qwen3-TTS Gradio server.');
a.fire('pointerover');tick(500);
assert.strictEqual(shown.at(-1),a);
assert.strictEqual(a.getAttribute('data-bs-title'),'外部 Qwen3-TTS Gradio 服务器的地址。');
''')


class SettingFinderTests(unittest.TestCase):
    """The finder indexes the displayed help and the English help together."""

    def finder(self, query, catalog_entries, locale='zh-CN'):
        code = r'''
const form=body.appendChild(new Element('form',{id:'config-form'}));
const section=form.appendChild(new Element('div',{'data-settings-section':'Setup'}));
const label=section.appendChild(new Element('label',{for:'llm-key'}));
label.textContent='API Key';
const input=section.appendChild(new Element('input',{id:'llm-key'}));
input.labels=[label];
context.getComputedStyle=()=>({display:'block'});
document.querySelectorAll=selector=>selector==='#config-form input, #config-form select, #config-form textarea'
 ? [input] : body.querySelectorAll(selector);
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
setUiCatalog('zh-CN',catalog(CATALOG));
context.applyUiLocale('zh-CN');
assert(context.getItemHelpText(input).includes('环境变量'),'the UI must actually be translated for this check');
const ids=query=>context.getSetupSettingsMatches(query).map(match=>match.id);
for(const [query,expected] of QUERIES){assert.strictEqual(JSON.stringify(ids(query)),JSON.stringify(expected),query);}
'''.replace('CATALOG', json.dumps(catalog_entries, ensure_ascii=False)) \
   .replace('QUERIES', json.dumps([[query, ['llm-key']]], ensure_ascii=False))
        return run_help_js(code, settings=True)

    def test_matches_the_translated_help_and_the_english_help(self):
        entries = {'help.field.llm-key.what': '随每个请求发送的 API 密钥。使用环境变量或 env:NAME 读取。'}
        result = self.finder('环境变量', entries)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_matches_english_text_while_the_ui_is_translated(self):
        entries = {'help.field.llm-key.what': '随每个请求发送的 API 密钥。使用环境变量或 env:NAME 读取。'}
        result = self.finder('environment variable', entries)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_full_width_input_matches_after_nfkc_normalisation(self):
        entries = {'help.field.llm-key.what': '随每个请求发送的 API 密钥。使用环境变量或 env:NAME 读取。'}
        result = self.finder('ＥＮＶ:ＮＡＭＥ', entries)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class HelpKeyReaderTests(unittest.TestCase):
    """Rule 21: the derivation helpers are checked on cases with known answers."""

    def test_slug_matches_the_runtime_rule_for_known_ids(self):
        cases = {
            'roster-consent': 'roster-consent',
            'audiobookshelfApiDocs': 'audiobookshelfapidocs',
            '.voice-seed': 'voice-seed',
            '[data-voice-roster="speaker"]': 'data-voice-roster-speaker',
            '[data-apply]': 'data-apply',
        }
        for identity, expected in cases.items():
            with self.subTest(identity=identity):
                self.assertEqual(expected, EXTRACT.help_slug(identity))

    def test_the_flat_key_shape_is_exact(self):
        maps = {
            'FIELD_HELP': {'x': {'what': 'Explain it.'}},
            'ACTION_HELP': {'saveIt': 'Save it.'},
            'ITEM_HELP': {'ok': 'Confirm it.'},
            'NAV_HELP': {'setup': 'Open it.'},
            'DYNAMIC_FIELD_HELP': {'.row': 'This row.'},
            'CHANGED_FIELD_HELP': {'updateChunk': {'text': 'Change it.'}},
        }
        self.assertEqual({
            'help.field.x.what': 'Explain it.',
            'help.action.saveit': 'Save it.',
            'help.item.ok': 'Confirm it.',
            'help.nav.setup': 'Open it.',
            'help.dynamic.row': 'This row.',
            'help.changed.updatechunk.text': 'Change it.',
        }, EXTRACT.help_messages(maps))

    def test_an_entry_without_a_what_fails_loudly(self):
        maps = {
            'FIELD_HELP': {'x': {'recommend': 'Do it.'}},
            'ACTION_HELP': {'saveIt': 'Save it.'},
            'ITEM_HELP': {'ok': 'Confirm it.'},
            'NAV_HELP': {'setup': 'Open it.'},
            'DYNAMIC_FIELD_HELP': {'.row': 'This row.'},
            'CHANGED_FIELD_HELP': {'updateChunk': {'text': 'Change it.'}},
        }
        with self.assertRaises(EXTRACT.HarvestError):
            EXTRACT.help_messages(maps)


if __name__ == '__main__':
    unittest.main()
