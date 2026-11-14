"""Locale behaviour and safety rendering, run against the real module in Node.

These tests discriminate the failure modes the design lists as dangerous:
English is returned when a catalog is absent (so a phased merge is safe), a
placeholder mismatch falls back to English AND records a miss, switching back to
English restores the authored text instead of keeping a translation, the picker's
"(draft)" marker comes from catalog metadata, and a safety message keeps the
authored English under the translation.
"""
import subprocess
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'

SETUP = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
document.documentElement=new Element('html');
load();
function addNode(attrs,text){const node=body.appendChild(new Element('div',attrs));node.textContent=text;return node;}
function catalog(messages,review={all:'machine-draft',safety:'not-reviewed'}){return {meta:{review_status:review},messages};}
function setUiCatalog(locale,payload){vm.runInContext('uiCatalogs.set('+JSON.stringify(locale)+','+JSON.stringify(payload)+')',context);}
'''


class UiI18nJsTests(unittest.TestCase):
    def test_followup_count_and_ingest_facts_survive_locale_round_trip(self):
        self.run_js(r'''
const root=require('path').dirname(process.argv[1]);
const core=fs.readFileSync(root+'/app-core.js','utf8');
vm.runInContext(core.slice(core.indexOf('function isChunkPendingRender('),core.indexOf('window.renderAll =')),context);
context.window=context;context.currentBookFilename='book';context.chunkSnapshotBook='book';context.cachedChunks=[{status:'pending',text:'Synthetic'}];
vm.runInContext(fs.readFileSync(root+'/app-ui-followups.js','utf8'),context);
const label=addNode({id:'render-pending-label','data-i18n':'editor.toolbar.render-pending'},'Render Pending');
for(const name of ['source','cast','script']){addNode({id:'script-ingest-'+name+'-state','data-i18n':'script.ingest.unknown'},'Not checked');}
const metadata=addNode({id:'script-ingest-metadata'},'');addNode({id:'script-ingest-feedback'},'');
await context.applyUiLocale('en');
context.renderPendingCount(context.cachedChunks);context.renderScriptIngestFacts({source:{source_name:'book',total_chars:10,word_count:2,chapter_count:0,estimated_minutes:.1},cast:{status:'not_built'},script:{status:'verified'}});
setUiCatalog('es-419',catalog({'editor.toolbar.render-pending-count':'Pendientes ({count})','script.ingest.unknown':'Sin comprobar',
 'script.ingest.accepted':'Fuente verificada','script.ingest.cast-missing':'Sin crear','script.ingest.script-verified':'Texto coincide',
 'script.ingest.metadata':'{name}: {chars} caracteres, {words} palabras, {chapters} capítulos, {minutes} minutos'}));
context.renderSetupSearchStatus=()=>{};
const settings=fs.readFileSync(root+'/app-settings.js','utf8');
vm.runInContext(settings.slice(settings.indexOf('function renderUiLocaleChrome('),settings.indexOf('function renderSetupSearchStatus(')),context);
await context.applyUiLocaleAndSearchStatus('es-419');
assert.equal(label.textContent,'Pendientes (1)');assert.equal(document.getElementById('script-ingest-source-state').textContent,'Fuente verificada');
assert.equal(document.getElementById('script-ingest-script-state').textContent,'Texto coincide');assert(metadata.textContent.includes('palabras'));
await context.applyUiLocaleAndSearchStatus('en');assert.equal(label.textContent,'Render Pending (1)');
assert.equal(document.getElementById('script-ingest-source-state').textContent,'Selected source verified');
''')
    def run_js(self, code):
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + SETUP + "\n(async()=>{\n" + code
             + "\n})().catch(error=>{console.error(error);process.exitCode=1;});",
             str(STATIC / 'js' / 'app-i18n.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_english_is_the_fallback_when_no_catalog_is_loaded(self):
        self.run_js(r'''
assert.strictEqual(context.getUiLocale(),'en');
assert.strictEqual(context.t('setup.finder.label','Find a setting'),'Find a setting');
assert.strictEqual(context.t('setup.finder.one','{count} matching setting.',{count:7}),'7 matching setting.');
assert.strictEqual(JSON.stringify(context.getUiCatalogMisses()),'[]');
assert.throws(()=>context.t('setup.finder.label'),/Missing English text/);
''')

    def test_a_present_translation_wins_and_a_placeholder_mismatch_falls_back_with_a_miss(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({
 'setup.finder.label':'查找设置',
 'setup.finder.one':'匹配到 {count} 项设置。',
 'setup.finder.many':'匹配到许多项设置。',
}));
assert.strictEqual(context.t('setup.finder.label','Find a setting',{},'zh-CN'),'查找设置');
assert.strictEqual(context.t('setup.finder.one','{count} matching setting.',{count:2},'zh-CN'),'匹配到 2 项设置。');
assert.strictEqual(context.t('setup.finder.many','{count} matching settings.',{count:2},'zh-CN'),'2 matching settings.');
assert.strictEqual(JSON.stringify(context.getUiCatalogMisses()),'[{"key":"setup.finder.many","locale":"zh-CN"}]');
assert.strictEqual(context.t('setup.finder.opened','Showing the original setting.',{},'zh-CN'),'Showing the original setting.');
assert.strictEqual(context.getUiCatalogMisses().length,1);
vm.runInContext("uiCatalogs.set('ja',{meta:{review_status:{all:'machine-draft',safety:'not-reviewed'}},messages:Object.create({toString:'poisoned inherited value'})})",context);
vm.runInContext("uiCatalogs.get('ja').messages['setup.finder.label']='查找设置'",context);
assert.strictEqual(context.t('toString','Authored English fallback',{},'ja'),'Authored English fallback');
''')

    def test_plural_form_for_zero_one_two_and_large_counts(self):
        self.run_js(r'''
for(const locale of ['en','es-419']){
 for(const [count,form] of [[0,'many'],[1,'one'],[2,'many'],[500,'many']]){
  assert.strictEqual(context.pluralForm(count,locale),form,locale+' '+count);
 }
}
for(const locale of ['zh-CN','ja']){
 for(const count of [0,1,2,500]){assert.strictEqual(context.pluralForm(count,locale),'many',locale+' '+count);}
}
''')

    def test_apply_catalog_is_idempotent_and_restores_authored_english(self):
        self.run_js(r'''
const label=addNode({'data-i18n':'setup.finder.label'},'Find a setting');
const unkeyed=body.appendChild(new Element('span'));unkeyed.textContent='Unkeyed English';
setUiCatalog('zh-CN',catalog({'setup.finder.label':'查找设置'}));
context.applyUiLocale('zh-CN');
assert.strictEqual(label.textContent,'查找设置');
const frozen=JSON.stringify([label.textContent,unkeyed.textContent,label.attrs]);
context.applyUiCatalog();context.applyUiCatalog();
assert.strictEqual(JSON.stringify([label.textContent,unkeyed.textContent,label.attrs]),frozen);
context.applyUiLocale('en');
assert.strictEqual(label.textContent,'Find a setting');
assert.strictEqual(unkeyed.textContent,'Unkeyed English');
''')

    def test_apply_locale_sets_lang_snaps_unsupported_codes_and_leaves_dir_alone(self):
        self.run_js(r'''
const selector=body.appendChild(new Element('select',{id:'setup-ui-locale'}));
for(const value of ['en','zh-CN','ja','es-419','qps-ploc']){selector.appendChild(new Element('option',{value}));}
context.applyUiLocale('ja');
assert.strictEqual(document.documentElement.getAttribute('lang'),'ja');
assert.strictEqual(selector.value,'ja');
body.setAttribute('dir','ltr');
for(const [requested,expected] of [['ar','en'],['he','en'],['zh-TW','en'],[null,'en'],['unsupported','en']]){
 context.applyUiLocale(requested);
 assert.strictEqual(selector.value,expected);assert.strictEqual(context.getUiLocale(),expected);
 assert.strictEqual(document.documentElement.getAttribute('lang'),expected);
 assert.strictEqual(body.getAttribute('dir'),'ltr');
 assert.strictEqual(document.documentElement.getAttribute('dir'),null);
}
context.applyUiLocale('qps-ploc');
assert.strictEqual(document.documentElement.getAttribute('lang'),'qps-ploc');
''')

    def test_pseudo_locale_expands_every_string_and_keeps_placeholders(self):
        self.run_js(r'''
context.applyUiLocale('qps-ploc');
assert.strictEqual(context.t('setup.finder.one','{count} matching setting.',{count:3}),'[3 maatchiing seettiing.]');
const label=addNode({'data-i18n':'setup.finder.label'},'Find a setting');
context.applyUiCatalog();
assert.strictEqual(label.textContent,'[Fiind aa seettiing]');
assert(!label.textContent.includes('{'));
''')

    def test_safety_messages_show_the_translation_with_the_english_below_it(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({'safety.delete_take':'删除 {count} 条录音？此操作无法撤销。'},
                             {all:'machine-draft',safety:'human-reviewed'}));
const target=body.appendChild(new Element('div'));
context.applyUiLocale('zh-CN');
context.renderUiSafety(target,'safety.delete_take','Delete {count} takes? This cannot be undone.',{count:2});
assert.strictEqual(target.children.length,2);
assert.strictEqual(target.children[0].className,'ui-safety-primary');
assert.strictEqual(target.children[0].textContent,'删除 2 条录音？此操作无法撤销。');
assert.strictEqual(target.children[1].className,'ui-safety-secondary');
assert.strictEqual(target.children[1].getAttribute('lang'),'en');
assert.strictEqual(target.children[1].textContent,'Delete 2 takes? This cannot be undone.');
assert.strictEqual(JSON.stringify(context.tSafety('safety.delete_take','Delete {count} takes? This cannot be undone.',{count:2})),
                   JSON.stringify({primary:'删除 2 条录音？此操作无法撤销。',secondary:'Delete 2 takes? This cannot be undone.'}));
context.applyUiLocale('en');
context.renderUiSafety(target,'safety.delete_take','Delete {count} takes? This cannot be undone.',{count:2});
assert.strictEqual(target.children.length,1);
assert.strictEqual(target.children[0].textContent,'Delete 2 takes? This cannot be undone.');
assert.strictEqual(JSON.stringify(context.tSafety('safety.delete_take','Delete {count} takes? This cannot be undone.',{count:2})),
                   JSON.stringify({primary:'Delete 2 takes? This cannot be undone.',secondary:null}));
''')

    def test_a_missing_safety_translation_still_shows_english_once(self):
        self.run_js(r'''
const target=body.appendChild(new Element('div'));
context.applyUiLocale('ja');
context.renderUiSafety(target,'safety.delete_take','Delete {count} takes?',{count:1});
assert.strictEqual(target.children.length,1);
assert.strictEqual(target.children[0].textContent,'Delete 1 takes?');
assert.strictEqual(target.children[0].className,'ui-safety-primary');
''')

    def test_numbers_and_dates_follow_the_ui_locale(self):
        self.run_js(r'''
context.applyUiLocale('en');
assert.strictEqual(context.formatUiNumber(0),'0');
assert.strictEqual(context.formatUiNumber(1),'1');
assert.strictEqual(context.formatUiNumber(500),'500');
vm.runInContext("globalThis.__seen=[];globalThis.__NumberFormat=Intl.NumberFormat;globalThis.__DateTimeFormat=Intl.DateTimeFormat;"
 +"Intl.NumberFormat=function(locale){__seen.push('number:'+locale);return new __NumberFormat(locale);};"
 +"Intl.DateTimeFormat=function(locale){__seen.push('date:'+locale);return new __DateTimeFormat(locale);};",context);
context.applyUiLocale('es-419');
assert.strictEqual(context.formatUiNumber(1500),vm.runInContext("new __NumberFormat('es-419').format(1500)",context));
assert.strictEqual(context.formatUiDate('2026-11-10T12:00:00'),
                   vm.runInContext("new __DateTimeFormat('es-419').format(new Date('2026-11-10T12:00:00'))",context));
context.applyUiLocale('qps-ploc');
context.formatUiNumber(1500);context.formatUiDate('2026-11-10T12:00:00');
assert.strictEqual(JSON.stringify(context.__seen),'["number:es-419","date:es-419","number:en","date:en"]');
assert.strictEqual(context.formatUiDate('not a date'),'not a date');
context.applyUiLocale('en');
assert.strictEqual(context.formatUiDate('2026-11-10T12:00:00'),'11/10/2026');
context.applyUiLocale('es-419');
assert.strictEqual(context.formatUiDate('2026-11-10T12:00:00'),'10/11/2026');
''')

    def test_the_draft_label_is_driven_by_each_catalog_review_status(self):
        self.run_js(r'''
const selector=body.appendChild(new Element('select',{id:'setup-ui-locale'}));
for(const [value,text] of [['en','English'],['zh-CN','简体中文'],['ja','日本語'],
                           ['es-419','Español (Latinoamérica)'],['qps-ploc','Pseudo-English (layout test, not a translation)']]){
 const option=selector.appendChild(new Element('option',{value}));option.textContent=text;
}
const labels=()=>JSON.stringify(selector.children.map(option=>option.textContent));
context.renderUiLocaleOptions();
assert.strictEqual(labels(),JSON.stringify(['English','简体中文 (draft)','日本語 (draft)','Español (Latinoamérica) (draft)',
                                'Pseudo-English (layout test, not a translation)']));
setUiCatalog('zh-CN',catalog({}));
setUiCatalog('ja',catalog({},{all:'human-reviewed',safety:'human-reviewed'}));
setUiCatalog('es-419',catalog({}));
context.renderUiLocaleOptions();
assert.strictEqual(labels(),JSON.stringify(['English','简体中文 (draft)','日本語','Español (Latinoamérica) (draft)',
                                'Pseudo-English (layout test, not a translation)']));
context.renderUiLocaleOptions();
assert.strictEqual(labels(),JSON.stringify(['English','简体中文 (draft)','日本語','Español (Latinoamérica) (draft)',
                                'Pseudo-English (layout test, not a translation)']));
''')

    def test_untranslated_locales_are_never_marked_draft(self):
        self.run_js(r'''
const selector=body.appendChild(new Element('select',{id:'setup-ui-locale'}));
for(const [value,text] of [['en','English'],['qps-ploc','Pseudo-English (layout test, not a translation)']]){
 const option=selector.appendChild(new Element('option',{value}));option.textContent=text;
}
context.renderUiLocaleOptions();
assert.strictEqual(JSON.stringify(selector.children.map(option=>option.textContent)),
                   JSON.stringify(['English','Pseudo-English (layout test, not a translation)']));
''')

    def test_the_attribute_sweep_uses_the_authored_value_and_never_html(self):
        self.run_js(r'''
for(const property of ['innerHTML','outerHTML']){Object.defineProperty(Element.prototype,property,{set:()=>assert.fail('HTML sink is forbidden')});}
Element.prototype.insertAdjacentHTML=()=>assert.fail('HTML sink is forbidden');
const input=body.appendChild(new Element('input',{'data-i18n-attr':'placeholder:setup.finder.placeholder,title:setup.finder.title'}));
input.setAttribute('placeholder','Try context, memory, language or retries');
input.setAttribute('title','Find a setting');
setUiCatalog('zh-CN',catalog({'setup.finder.placeholder':'试试输入上下文、内存、语言或重试次数'}));
context.applyUiLocale('zh-CN');
assert.strictEqual(input.getAttribute('placeholder'),'试试输入上下文、内存、语言或重试次数');
assert.strictEqual(input.getAttribute('title'),'Find a setting');
context.applyUiLocale('en');
assert.strictEqual(input.getAttribute('placeholder'),'Try context, memory, language or retries');
''')

    def test_markup_counts_reach_the_translation_and_follow_later_changes(self):
        self.run_js(r'''
const summary=addNode({'data-i18n':'setup.preview.defaults','data-i18n-count':'5'},
                      '{count} missing settings filled with defaults');
setUiCatalog('zh-CN',catalog({'setup.preview.defaults':'已用默认值补全 {count} 项缺失设置'}));
context.applyUiLocale('zh-CN');
assert.strictEqual(summary.textContent,'已用默认值补全 5 项缺失设置');
summary.setAttribute('data-i18n-count','9');
context.applyUiCatalog();
assert.strictEqual(summary.textContent,'已用默认值补全 9 项缺失设置');
summary.removeAttribute('data-i18n-count');
assert.throws(()=>context.applyUiCatalog(),/Missing or invalid/);
''')

    def test_a_catalog_is_fetched_once_and_a_malformed_one_is_refused(self):
        self.run_js(r'''
let calls=0;
context.fetch=(url,options)=>{calls++;assert.strictEqual(options.cache,'no-store');
 return Promise.resolve({ok:true,json:()=>Promise.resolve({_meta:{locale:'es-419',review_status:{all:'machine-draft'}},
  'setup.finder.label':'Buscar un ajuste'})});};
const first=await context.loadUiCatalog('es-419');
const second=await context.loadUiCatalog('es-419');
assert.strictEqual(calls,1);
assert.strictEqual(first.messages['setup.finder.label'],'Buscar un ajuste');
assert.strictEqual(first.meta.locale,'es-419');
assert.strictEqual(first,second);
assert.strictEqual(context.t('setup.finder.label','Find a setting',{},'es-419'),'Buscar un ajuste');
context.fetch=()=>Promise.resolve({ok:true,json:()=>Promise.resolve({'setup.finder.label':'无元数据'})});
const broken=await context.loadUiCatalog('ja');
assert.strictEqual(broken,null);
assert.strictEqual(context.t('setup.finder.label','Find a setting',{},'ja'),'Find a setting');
assert.strictEqual(await context.loadUiCatalog('en'),null);
''')

    def test_a_failed_load_is_not_cached_and_a_retry_can_succeed(self):
        self.run_js(r'''
let calls=0;
context.fetch=()=>{calls++;return Promise.resolve({ok:false,status:404,json:()=>Promise.resolve({})});};
assert.strictEqual(await context.loadUiCatalog('zh-CN'),null);
context.fetch=()=>{calls++;return Promise.resolve({ok:true,json:()=>Promise.resolve({_meta:{locale:'zh-CN'},'setup.finder.label':'查找设置'})});};
assert.strictEqual((await context.loadUiCatalog('zh-CN')).messages['setup.finder.label'],'查找设置');
assert.strictEqual(calls,2);
''')

    def test_apply_ui_locale_repaints_after_its_catalog_loads(self):
        self.run_js(r'''
const label=addNode({'data-i18n':'setup.finder.label'},'Find a setting');
let release;
context.fetch=()=>new Promise(resolve=>{release=resolve;});
const pending=context.applyUiLocale('ja');
assert.strictEqual(label.textContent,'Find a setting');
release({ok:true,json:()=>Promise.resolve({_meta:{locale:'ja',review_status:{all:'machine-draft'}},
                                          'setup.finder.label':'設定を検索'})});
await pending;
assert.strictEqual(label.textContent,'設定を検索');
''')

    def test_sentences_join_without_a_space_after_cjk_punctuation(self):
        self.run_js(r'''
context.applyUiLocale('zh-CN');
assert.strictEqual(context.joinUiSentences(['已保存。','重试导出。']),'已保存。重试导出。');
assert.strictEqual(context.joinUiSentences(['说明','（可选）']),'说明（可选）');
assert.strictEqual(context.joinUiSentences(['标题','后文']),'标题 后文');
assert.strictEqual(context.joinUiSentences(['','已保存。','']),'已保存。');
context.applyUiLocale('ja');
assert.strictEqual(context.joinUiSentences(['保存しました。','もう一度お試しください。']),'保存しました。もう一度お試しください。');
assert.strictEqual(context.joinUiSentences(['タイトル','本文']),'タイトル 本文');
for(const locale of ['es-419','en']){
 assert.strictEqual(context.joinUiSentences(['Guardado.','Vuelve a intentarlo.'],locale),'Guardado. Vuelve a intentarlo.');
 assert.strictEqual(context.joinUiSentences(['Saved.','Try again.'],locale),'Saved. Try again.');
}
''')

    def test_a_server_english_fragment_after_a_cjk_sentence_is_also_tight(self):
        """The full-width stop already ends the sentence, so no ASCII space is
        inserted before the English fragment; English keeps its own space."""
        self.run_js(r'''
context.applyUiLocale('zh-CN');
assert.strictEqual(context.joinUiSentences(['已保存。','Retry the export.']),'已保存。Retry the export.');
context.applyUiLocale('ja');
assert.strictEqual(context.joinUiSentences(['保存しました。','Retry the export.']),'保存しました。Retry the export.');
context.applyUiLocale('en');
assert.strictEqual(context.joinUiSentences(['Saved.','Retry the export.']),'Saved. Retry the export.');
''')

    def test_markup_sentence_joins_collapse_only_for_cjk_and_restore(self):
        self.run_js(r'''
const wrap=body.appendChild(new Element('div'));
const first=wrap.appendChild(new Element('span',{'data-i18n':'setup.finder.label'}));
first.textContent='Find a setting';
const gap={nodeType:3,textContent:' ',nextSibling:null};
const second=wrap.appendChild(new Element('span',{'data-i18n':'setup.finder.opened'}));
second.textContent='Showing the original setting.';
first.nextSibling=gap;gap.nextSibling=second;
const control=wrap.appendChild(new Element('span',{'data-i18n':'setup.finder.label'}));
control.textContent='Find a setting';
const controlGap={nodeType:3,textContent:' ',nextSibling:null};
const button=wrap.appendChild(new Element('button',{'data-i18n':'setup.finder.clear'}));
button.textContent='Clear';
control.nextSibling=controlGap;controlGap.nextSibling=button;
const label=wrap.appendChild(new Element('strong',{'data-i18n':'stage.label'}));
label.textContent='Quality';
const labelGap={nodeType:3,textContent:' ',nextSibling:null};
const help=wrap.appendChild(new Element('span',{'data-i18n':'stage.help'}));
help.textContent='warning-only clip health';
label.nextSibling=labelGap;labelGap.nextSibling=help;
const missingLeft=wrap.appendChild(new Element('span',{'data-i18n':'missing.left'}));
missingLeft.textContent='Missing left sentence.';
const missingGap={nodeType:3,textContent:' ',nextSibling:null};
const missingRight=wrap.appendChild(new Element('span',{'data-i18n':'missing.right'}));
missingRight.textContent='Missing right sentence.';
missingLeft.nextSibling=missingGap;missingGap.nextSibling=missingRight;
setUiCatalog('zh-CN',catalog({'setup.finder.label':'查找设置','setup.finder.opened':'已定位到原始设置。','setup.finder.clear':'清除','stage.label':'质检','stage.help':'——仅警告的检查'}));
context.applyUiLocale('zh-CN');
assert.strictEqual(gap.textContent,'');
assert.strictEqual(labelGap.textContent,'');
assert.strictEqual(missingGap.textContent,' ');
assert.strictEqual(controlGap.textContent,' ');
assert.strictEqual(first.textContent,'查找设置');
setUiCatalog('ja',catalog({'setup.finder.label':'設定を検索','setup.finder.opened':'元の設定を表示しています。','setup.finder.clear':'クリア','stage.label':'品質チェック','stage.help':'— 警告のみのチェック'}));
context.applyUiLocale('ja');
assert.strictEqual(gap.textContent,'');
assert.strictEqual(labelGap.textContent,'');
context.applyUiLocale('es-419');
assert.strictEqual(gap.textContent,' ');
context.applyUiLocale('en');
assert.strictEqual(gap.textContent,' ');
assert.strictEqual(first.textContent,'Find a setting');
''')

    def test_the_locale_storage_key_is_read_in_exactly_one_module(self):
        readers = []
        for path in sorted((STATIC / 'js').glob('*.js')):
            source = path.read_text(encoding='utf-8')
            if "alex-ui-locale" in source:
                readers.append(path.name)
        self.assertEqual(['app-i18n.js'], readers,
                         'getUiLocale/readUiLocalePreference must be the one reader of the stored locale')
        source = (STATIC / 'js' / 'app-i18n.js').read_text(encoding='utf-8')
        self.assertIn('localStorage.getItem(UI_LOCALE_STORAGE_KEY)', source)
        self.assertLess(source.index('function readUiLocalePreference'),
                        source.index('localStorage.getItem(UI_LOCALE_STORAGE_KEY)'))

    def test_the_removed_setup_catalog_names_no_longer_exist_anywhere(self):
        removed = ('setupUiEnglish', 'setupUiLocale', 'getSetupUiMessage',
                   'applySetupUiLocale', 'changeSetupUiLocale', 'data-setup-message')
        for path in sorted((STATIC / 'js').glob('*.js')):
            source = path.read_text(encoding='utf-8')
            for name in removed:
                self.assertNotIn(name, source, f'{path.name} still defines or uses {name}')
        self.assertNotIn('data-setup-message', (STATIC / 'index.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
