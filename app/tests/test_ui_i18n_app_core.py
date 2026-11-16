"""The app-core voice card and Script-area cast list render in every locale.

P9a keyed the voice card (createVoiceCard, its candidate list, the trait badge,
the ensemble members and the voice-states editor), the Script-area strings P5
left (recovery panel, batch and review status, saved-script rows, nickname
aliases, the cast list) and the shared defaults (showConfirm, copy/toast glue,
confirmIfRemote, the task-label map, the field validators).

These tests run the real module code against the existing dependency-free Node
DOM harness, so they verify what a reader actually sees rather than that a
function returned a string:

  * the real createVoiceCard paints translated radio labels, the Ready switch,
    the version select and the alias label, and restores the authored English;
  * the real getVoiceCandidateMarkup and the real getTraitBadgeHtml translate;
  * a missing key still falls back to English (the reason a partial catalog can
    ship at all);
  * the real getCastListRowHtml translates its placeholder and aria-labels;
  * the cast-list delete confirm keeps the authored English beneath the
    translation (formatUiSafetyText), and showConfirm's own defaults are keyed.

Every test references a key the P8 tree does not have, so all of them fail on
origin/feat/ui-i18n-p8.
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

# The real createVoiceCard calls these; each is either loaded from the module or
# stubbed with its documented contract.
VOICE_STUBS = r'''
context.AVAILABLE_VOICES=['Aiden','Dylan'];
context.ensureVoiceRosterCache=()=>({book:'BOOK'});
context.renderInitialAliasOptions=()=>'<option value="">ALIAS ROW</option>';
context.renderStyleTimeline=()=>'';
context.getSavedAdapterOption=()=>'';
context._voiceCardsBookToken='token';
context._voiceSaveSnapshot={book_token:'token'};
context.window=context;
'''


class LocaleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = json.loads((I18N / 'ui.en.json').read_text(encoding='utf-8'))
        cls.catalogs = {locale: json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))
                        for locale in LOCALES}

    def run_node(self, body, setup=''):
        source = DOM_FIXTURE + PREAMBLE + setup
        source += f"vm.runInContext(fs.readFileSync({js(str(STATIC / 'js' / 'app-i18n.js'))},'utf8'),context);\n"
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

    def assert_translated(self, key):
        for locale in LOCALES:
            with self.subTest(locale=locale, key=key):
                self.assertNotEqual(self.catalogs[locale][key], self.english[key])

    def slice_source(self, name, start, end):
        text = (STATIC / 'js' / name).read_text(encoding='utf-8')
        first = text.index(start)
        return text[first:text.index(end, first)]

    def load_core(self):
        """The real voice-card and cast-list functions, nothing re-implemented."""
        return ('vm.runInContext(' + js(self.slice_source('app-core.js',
                                                          'function escapeHtml(',
                                                          '// Parse a numeric input')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'function getVoiceCandidateMarkup(',
                                                            'function getVoiceCardMetadata(')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'function getStateVoiceCardsMarkup(',
                                                            '// Suggest members for a compound name')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'function ensembleMembersMarkup(',
                                                            'function getSavedAdapterOption(')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'function getCastListRowHtml(',
                                                            'async function loadCastList(')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'function getEffectiveVoiceAlias(',
                                                            'function renderInitialAliasOptions(')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'function getVoiceStateDefault(',
                                                            'async function voiceChangesHere(')) + ',context);\n'
                + 'vm.runInContext(' + js(self.slice_source('app-core.js',
                                                            'const TASK_LABELS = {',
                                                            '// The one label map for the task-status vocabulary')) + ',context);\n')


class BatchDLocaleStabilityTests(LocaleTestCase):
    def test_runtime_estimates_and_integrity_badges_repaint_without_reload(self):
        functions = self.slice_source('app-core.js', 'function invalidateEditorIntegrity(', 'async function ensureMergeIntegrityApproval(')
        self.run_node(self.catalogs_js() + '\nvm.runInContext(' + js('let editorIntegrityView=0;\n' + functions) + ',context);\n' + r'''
const badge=addNode({id:'editor-integrity-badge'},'');const estimate=addNode({},'');
for(const locale of ['en','zh-CN','ja','es-419','qps-ploc']){
 await context.applyUiLocale(locale);
 context.invalidateEditorIntegrity('Edits awaiting save');
 assert.equal(badge.textContent,context.t('editor.integrity.unsaved','Edits awaiting save'));
 context.renderRuntimeEstimate(estimate,{basis:'fallback',total_seconds:5400});
 assert(estimate.textContent.includes(context.formatRuntimeDuration(5400)));
 context.renderRuntimeEstimate(estimate,{basis:'sampled',total_seconds:5400,low_seconds:4800,high_seconds:6000,mp3_bytes:1048576,samples:2});
 assert(estimate.textContent.includes(context.formatRuntimeDuration(5400)));assert(!estimate.textContent.includes('{'));
}
context.applyUiLocale('en');context.renderRuntimeEstimate(estimate,{basis:'fallback',total_seconds:5400});
await context.applyUiLocale('ja');assert(estimate.textContent.includes(context.formatRuntimeDuration(5400)));
assert(!estimate.textContent.includes('rough'));assert(!badge.textContent.includes('Edits awaiting save'));
''')

    def test_theme_cycles_and_live_locale_switch_repaint_all_six_actions(self):
        theme = self.slice_source('app-core.js', 'const THEMES = [', '// Sync button label/icon')
        settings = self.slice_source('app-settings.js', 'let setupSearchStatus', '// Setup discovery')
        self.run_node(self.catalogs_js() + '\n' + r'''
for(const id of ['theme-label','theme-icon','theme-toggle','setup-settings-search-status']){addNode({id},'');}
context.localStorage={setItem(){}};
context.applySystemStatusText=()=>{};
''' + 'vm.runInContext(' + js(theme + settings) + ',context);\n' + r'''
for(const locale of ['en','zh-CN','ja','es-419','qps-ploc']){
 await context.applyUiLocaleAndSearchStatus(locale);
 for(const key of ['light','night','super-night','cyberpunk','bread','leek']){
  context.applyTheme(key);
  const label=document.getElementById('theme-label').textContent;
  assert.equal(label,context.t('shell.theme.current-label','Theme: {theme}',{theme:context.getThemeLabel(key)}));
  const button=document.getElementById('theme-toggle');
  assert.equal(button.title,button.getAttribute('aria-label'));
  assert(button.title.includes(context.getThemeLabel(key)));
  context.cycleTheme();
  assert.notEqual(document.getElementById('theme-label').textContent,label);
 }
}
context.applyTheme('night');
await context.applyUiLocaleAndSearchStatus('ja');
assert.equal(document.getElementById('theme-label').textContent,context.t('shell.theme.current-label','Theme: {theme}',{theme:context.getThemeLabel('night')}));
''')

    def test_saved_review_enums_and_unknown_support_value_survive_live_switch(self):
        self.run_node(self.catalogs_js() + '\n' + self.load_core() + VOICE_STUBS + r'''
for(const locale of ['en','zh-CN','ja','es-419','qps-ploc']){
 await context.applyUiLocale(locale);
 for(const status of ['unreviewed','unassigned','assigned','generated','reviewed','approved','rejected']){
  const label=context.getVoiceReviewStatusLabel(status);
  if(locale!=='en'){assert.notEqual(label,status,locale+' '+status);}
 }
 const markup=context.createVoiceCard({name:'SYNTHETIC',config:{persona_status:'future-state',voice_status:'approved'}},0);
 assert(markup.includes(context.t('voice.review.unknown','Unknown status')));
 assert(markup.includes('future-state'));
 const review=addNode({'class':'voice-review-labels','data-persona-status':'unreviewed','data-voice-status':'unassigned'},'unreviewed');
 context.renderVoiceLocaleChrome();
 assert(review.textContent.includes(context.getVoiceReviewStatusLabel('unreviewed')));
}
''')

    def test_real_voice_cards_pluralize_one_two_and_state_counts_in_each_locale(self):
        self.run_node(self.catalogs_js() + '\n' + self.load_core() + VOICE_STUBS + r'''
for(const locale of ['en','zh-CN','ja','es-419','qps-ploc']){
 await context.applyUiLocale(locale);
 for(const count of [0,1,2,1000000]){
  context._lineCounts={SYNTHETIC:count};
  const markup=context.createVoiceCard({name:'SYNTHETIC',config:{}},0);
  const labels=context.getVoiceLineCountText(count,false);
  assert(markup.includes(labels.text));assert(markup.includes(labels.title));
  const state=context.createVoiceCard({name:'SYNTHETIC',version_id:'v1',state_line_count:count,config:{}},0);
  assert(state.includes(context.getVoiceLineCountText(count,true).text));
  if(locale==='en' && count===1){assert(markup.includes('1 line<'));assert(!markup.includes('1 lines'));}
 }
}
''')


class VoiceCardRenderTests(LocaleTestCase):
    def test_the_real_voice_card_paints_each_locale_and_restores_english(self):
        keys = ('voice.card.type.custom', 'voice.card.type.builtin-lora', 'voice.card.ready',
                'voice.card.alias-label', 'voice.card.version-empty')
        for key in keys:
            self.assert_translated(key)
        result = self.run_node(
            self.catalogs_js() + '\n' + self.load_core() + VOICE_STUBS
            + "\nconst card={name:'ALICE',config:{type:'custom',voice:'Aiden',ready:true}};\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=context.createVoiceCard(card,0);\n"
            + "}\n"
            + "console.log(JSON.stringify(seen));")
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertIn(self.catalogs[locale][key], seen[locale])
            self.assertIn(self.catalogs[locale]['voice.card.alias-target-aria'].replace('{name}', 'ALICE'),
                          seen[locale])
        for key in keys:
            self.assertIn(self.english[key], seen['en'])
        self.assertIn('Custom voice for ALICE', seen['en'])

    def test_the_real_candidate_list_and_trait_badge_translate(self):
        self.assert_translated('voice.candidate.saved-title')
        self.assert_translated('voice.trait.label')
        result = self.run_node(
            self.catalogs_js() + '\n' + self.load_core() + VOICE_STUBS
            + "\nconst seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]={candidates:context.getVoiceCandidateMarkup([{candidate_id:'design-1'}],null),"
            + " trait:context.getTraitBadgeHtml({gender:'female',age_group:'adult',lines:3})};\n"
            + "}\n"
            + "console.log(JSON.stringify(seen));")
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        for locale in LOCALES:
            with self.subTest(locale=locale):
                self.assertIn(self.catalogs[locale]['voice.candidate.saved-title'], seen[locale]['candidates'])
                self.assertIn(self.catalogs[locale]['voice.trait.label'].replace('{traits}', 'female · adult'),
                              seen[locale]['trait'])
        self.assertIn('Script estimate: female · adult', seen['en']['trait'])

    def test_the_real_ensemble_members_and_state_editor_translate(self):
        self.assert_translated('voice.ensemble.none')
        self.assert_translated('voice.states.main')
        result = self.run_node(
            self.catalogs_js() + '\n' + self.load_core() + VOICE_STUBS
            + "\nconst seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " context._voicesNames=['ALICE'];\n"
            + " seen[locale]={empty:context.ensembleMembersMarkup('ALICE',[]),"
            + " rows:context.renderVoiceStateRows({states:[{gender:'male',age_group:'adult',from_index:4}"
            + ",{gender:'male',age_group:'elderly',from_index:null}],applied:[]},'ALICE')};\n"
            + "}\n"
            + "console.log(JSON.stringify(seen));")
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        for locale in LOCALES:
            with self.subTest(locale=locale):
                self.assertIn(self.catalogs[locale]['voice.ensemble.none'], seen[locale]['empty'])
                self.assertIn(self.catalogs[locale]['voice.states.main'], seen[locale]['rows'])
                self.assertIn(self.catalogs[locale]['voice.states.from-line'].replace('{line}', '5'),
                              seen[locale]['rows'])
        self.assertIn('No other characters to combine.', seen['en']['empty'])

    def test_a_missing_voice_card_key_keeps_its_english(self):
        key = 'voice.card.type.custom'
        self.assert_translated(key)
        result = self.run_node(
            self.catalogs_js(drop=(key,)) + '\n' + self.load_core() + VOICE_STUBS
            + "\ncontext.applyUiLocale('ja');\n"
            + "console.log(context.createVoiceCard({name:'ALICE',config:{type:'custom',voice:'Aiden'}},0));")
        self.assertIn(self.english[key], result.stdout)


class CastListRenderTests(LocaleTestCase):
    def test_the_real_cast_list_row_translates_its_placeholder_and_names(self):
        keys = ('script.cast_list.name-placeholder', 'script.cast_list.aliases-placeholder',
                'script.cast_list.remove-aria')
        for key in keys:
            self.assert_translated(key)
        result = self.run_node(
            self.catalogs_js() + '\n' + self.load_core()
            + "\nconst seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=context.getCastListRowHtml({name:'ALICE',aliases:['AL']});\n"
            + "}\n"
            + "console.log(JSON.stringify(seen));")
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    value = self.catalogs[locale][key].replace('{name}', 'ALICE')
                    self.assertIn(value, seen[locale])
        self.assertIn('NAME or THE DESCRIPTION', seen['en'])

    def test_the_cast_list_delete_confirm_keeps_english_beneath_the_translation(self):
        key = 'safety.script.cast_list.delete'
        self.assert_translated(key)
        english = self.english[key]
        expected = {locale: self.catalogs[locale][key] + '\n' + english for locale in LOCALES}
        expected['en'] = english
        self.run_node(
            self.catalogs_js() + '\nconst seen={};\n'
            + "for(const locale of ['zh-CN','ja','es-419','en']){context.applyUiLocale(locale);\n"
            + f"seen[locale]=context.formatUiSafetyText({js(key)},{js(english)});}}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});")

    def test_show_confirm_defaults_and_recovery_copy_are_keyed(self):
        keys = ('core.confirm.title', 'core.confirm.continue', 'script.recovery.title',
                'script.cast_list.saved', 'core.task-label.script')
        for key in keys:
            self.assert_translated(key)
        source = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        for key in keys:
            with self.subTest(key=key):
                self.assertIn(f"'{key}'", source)
        # The task-label English the notification uses matches the map that
        # app-workspace.js still reads (Rule 15: one label per task).
        for task, label in (('script', 'Script generation'), ('batch_review', 'Batch review'),
                            ('cast_list', 'Cast list'), ('m4b_export', 'M4B export')):
            with self.subTest(task=task):
                self.assertIn(f"case '{task}': return t('core.task-label.", source)
        result = self.run_node(
            self.catalogs_js() + '\n' + self.load_core()
            + "\nconst labels={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " labels[locale]=context.getTaskLabel('script');\n"
            + " context.applyUiLocale('en');\n"
            + "}\n"
            + "console.log(JSON.stringify(labels));")
        labels = json.loads(result.stdout.strip().splitlines()[-1])
        for locale in LOCALES:
            self.assertEqual(self.catalogs[locale]['core.task-label.script'], labels[locale])
        self.assertEqual('Script generation', labels['en'])


if __name__ == '__main__':
    unittest.main()
