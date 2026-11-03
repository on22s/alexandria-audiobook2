"""Search must reveal original controls without reading values or submitting.

Node controls discriminate hidden backends from collapsed UI, secret values
from public descriptions, and field identity/focus from a copied input.
Actual browser visibility and CSS states are checked separately.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'

SETUP = r'''
load();
Element.prototype.replaceChildren=function(){for(const child of [...this.children]){child.remove();}};
Element.prototype.querySelector=function(selector){return (selector==='h5'?this.children.find(node=>node.tagName==='H5'):this.querySelectorAll(selector)[0])||null;};
Element.prototype.focus=function(){document.activeElement=this;};
Element.prototype.scrollIntoView=function(){this.scrolled=true;};
context.getComputedStyle=node=>({display:node.style.display||'block'});
const form=body.appendChild(new Element('form',{id:'config-form'}));
const overview=body.appendChild(new Element('div'));
const search=overview.appendChild(new Element('input',{id:'setup-settings-search'}));search.value='';
const results=overview.appendChild(new Element('div',{id:'setup-settings-results',hidden:''}));
const category=overview.appendChild(new Element('select',{id:'setup-settings-category'}));
const status=overview.appendChild(new Element('div',{id:'setup-settings-search-status'}));
overview.appendChild(new Element('button',{id:'setup-settings-clear'}));
const section=form.appendChild(new Element('section',{'data-settings-section':'Language model',id:'settings-llm'}));
section.appendChild(new Element('h5'));
const details=section.appendChild(new Element('details'));details.open=false;
function field(id,label,parent=details){
 const node=parent.appendChild(new Element('input',{id}));node.labels=[{textContent:label}];
 Object.defineProperty(node,'value',{get(){throw Error('Search must never read configuration values');},set(){throw Error('Locale must never write configuration values');}});
 return node;
}
const budget=field('llm-context-length','Total context budget (tokens)');
const key=field('llm-key','API Key',section);
const queryAll=document.querySelectorAll.bind(document);
document.querySelectorAll=selector=>selector==='#config-form [data-settings-section]'?form.querySelectorAll('[data-settings-section]'):selector.startsWith('#config-form ')?form.querySelectorAll('input,select,textarea'):queryAll(selector);
let revealed=null;context.openConfigField=node=>{revealed=node;details.open=true;node.focus();};
for(const [filename,name] of [[process.argv[3],'setLocalStorageValue'],[process.argv[4],'applySystemStatusText']]){
 const source=fs.readFileSync(filename,'utf8');
 const start=source.indexOf('function '+name+'(');assert(start>=0);
 const end=source.indexOf('\n        }',start);assert(end>start);
 vm.runInContext(source.slice(start,end+10),context);
}
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
'''


class SettingsDiscoveryTests(unittest.TestCase):
    def run_js(self, script, locale_setup=''):
        self.assertEqual(SETUP.count("vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);"), 1)
        setup = SETUP.replace("vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);",
                              locale_setup+"vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);")
        result = subprocess.run(['node', '-e', DOM_FIXTURE+setup+script,
                                 str(STATIC/'js/app-help.js'), str(STATIC/'js/app-settings.js'),
                                 str(STATIC/'js/app-core.js'), str(STATIC/'js/app-workbench.js')],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_message_fallback_placeholders_and_plain_text_values(self):
        self.run_js(r'''
assert.equal(context.getSetupUiMessage('one',{count:0},'unavailable-locale'),'0 matching setting.');
assert.equal(context.getSetupUiMessage('missing-key'), 'missing-key');
assert.equal(context.getSetupUiMessage('toString'), 'toString');
assert.equal(context.formatUiMessage('Hello {name}',undefined,{name:'<img onerror=bad()>'}),'Hello <img onerror=bad()>');
assert.equal(context.formatUiMessage('Hello {name}','Wrong {other}',{name:'Safe'}),'Hello Safe');
assert.equal(context.formatUiMessage('Hello {name}','',{name:'Safe'}),'Hello Safe');
assert.equal(context.formatUiMessage('Hello {name}','{name} repeats {name}',{name:'{other}'}),'{other} repeats {other}');
assert.throws(()=>context.getSetupUiMessage('one',{}),/Missing or invalid/);
assert.throws(()=>context.getSetupUiMessage('one',{count:NaN}),/Missing or invalid/);
const pseudo=context.getSetupUiMessage('none',{},'qps-ploc');
assert(pseudo.startsWith('[')&&pseudo.length>context.getSetupUiMessage('none').length);
assert.equal(context.getSetupUiMessage('many',{count:12},'qps-ploc'),'[12 maatchiing seettiings.]');
''')

    def test_unknown_markup_keeps_english_fallback_and_unsupported_rtl_stays_english(self):
        self.run_js(r'''
const fallback=overview.appendChild(new Element('div',{'data-setup-message':'future-message'}));
fallback.textContent='Future English fallback <img onerror=bad()>';
const placeholder=overview.appendChild(new Element('input',{'data-setup-message':'future-placeholder'}));
placeholder.placeholder='Future English placeholder';
const inherited=overview.appendChild(new Element('div',{'data-setup-message':'toString'}));inherited.textContent='English inherited-key fallback';
const label=overview.appendChild(new Element('label',{'data-setup-message':'label'}));
const selector=overview.appendChild(new Element('select',{id:'setup-ui-locale'}));
for(const node of [fallback,placeholder,inherited]){
 const text=node.textContent;Object.defineProperty(node,'textContent',{get:()=>text,set:()=>assert.fail('Unknown message must retain authored text')});
}
Object.defineProperty(placeholder,'placeholder',{get:()=>'Future English placeholder',set:()=>assert.fail('Unknown message must retain authored placeholder')});
for(const property of ['innerHTML','outerHTML']){Object.defineProperty(Element.prototype,property,{set:()=>assert.fail('HTML sink is forbidden')});}
Element.prototype.insertAdjacentHTML=()=>assert.fail('HTML sink is forbidden');
document.documentElement=new Element('html');
Object.defineProperty(Element.prototype,'dir',{get(){return this.getAttribute('dir')||'';},set:()=>assert.fail('Locale must not change direction')});
for(const node of [body,document.documentElement]){Object.defineProperty(node.style,'direction',{get:()=>'',set:()=>assert.fail('Locale must not change direction')});}
const original=JSON.stringify(form.querySelectorAll('[id]').map(node=>node.attrs));
for(const locale of ['qps-ploc','ar','he','unsupported',null,'en']){
 context.applySetupUiLocale(locale);
 assert.equal(fallback.textContent,'Future English fallback <img onerror=bad()>');
 assert.equal(placeholder.placeholder,'Future English placeholder');
 assert.equal(inherited.textContent,'English inherited-key fallback');
 assert.equal(label.textContent,locale==='qps-ploc'?'[Fiind aa seettiing]':'Find a setting');
 assert.equal(selector.value,locale==='qps-ploc'?'qps-ploc':'en');
 assert.equal(JSON.stringify(form.querySelectorAll('[id]').map(node=>node.attrs)),original);
 assert.equal(body.getAttribute('dir'),null);
 assert.equal(document.documentElement.getAttribute('dir'),null);
}
''')

    def test_locale_restoration_storage_events_and_configuration_isolation(self):
        self.run_js(r'''
assert.equal(selector.value,'qps-ploc');assert(label.textContent.startsWith('['));
assert(search.placeholder.startsWith('['));
search.value='memory';context.renderSetupSettingsSearch();assert(status.textContent.startsWith('[1 '));
assert.strictEqual(document.getElementById('llm-context-length'),budget);
const fingerprint=()=>JSON.stringify(form.querySelectorAll('[id]').map(node=>[node.attrs,node.textContent,node.style]));
const original=fingerprint();context.changeSetupUiLocale('en');
assert.equal(saved.get('alex-ui-locale'),'en');assert.equal(label.textContent,'Find a setting');
assert.equal(fingerprint(),original);assert.equal(search.value,'memory');
saved.set('alex-ui-locale','qps-ploc');assert.equal(selector.value,'en');
storageEvent({key:'unrelated',newValue:'qps-ploc',storageArea:store});assert.equal(selector.value,'en');
storageEvent({key:'alex-ui-locale',newValue:'qps-ploc',storageArea:{}});assert.equal(selector.value,'en');
const row=results.children[0];const hit=row.children[0];hit.focus();
storageEvent({key:'alex-ui-locale',newValue:'qps-ploc',storageArea:store});assert.equal(selector.value,'qps-ploc');
assert.strictEqual(results.children[0],row);assert.strictEqual(document.activeElement,hit);
assert(status.textContent.startsWith('[1 '));
context.openSetupSetting('llm-context-length');assert(results.hidden);assert.equal(results.children.length,0);
context.changeSetupUiLocale('en');assert(results.hidden);assert.equal(results.children.length,0);
assert.equal(status.textContent,'Showing the original setting. Save Configuration to apply any edits.');
storageEvent({key:'alex-ui-locale',newValue:'unsupported',storageArea:store});assert.equal(selector.value,'en');
context.changeSetupUiLocale('qps-ploc');
storageEvent({key:null,newValue:null,storageArea:store});assert.equal(selector.value,'en');
assert(results.hidden);assert(status.textContent.includes('Save Configuration'));
context.clearSetupSettingsSearch();context.changeSetupUiLocale('qps-ploc');assert.equal(status.textContent,'');
assert.equal(fingerprint(),original);
''', r'''
const selector=overview.appendChild(new Element('select',{id:'setup-ui-locale'}));
const label=overview.appendChild(new Element('label',{'data-setup-message':'label'}));
search.setAttribute('data-setup-message','placeholder');
overview.appendChild(new Element('div',{id:'setup-ui-locale-status'}));
const saved=new Map([['alex-ui-locale','qps-ploc']]);let storageEvent;
const store={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value)};context.localStorage=store;
context.window.addEventListener=(type,callback)=>{assert.equal(type,'storage');storageEvent=callback;};
''')

    def test_blocked_locale_storage_is_visible_and_preference_remains_tab_only(self):
        self.run_js(r'''
assert.equal(selector.value,'en');assert(storageStatus.textContent.includes('only to this tab'));context.changeSetupUiLocale('qps-ploc');
assert.equal(selector.value,'qps-ploc');assert(storageStatus.textContent.includes('only to this tab'));
search.value='api key';context.renderSetupSettingsSearch();assert(status.textContent.startsWith('[1 '));
assert.equal(results.children[0].children[0].textContent,'API Key · Language model');
''', r'''
const selector=overview.appendChild(new Element('select',{id:'setup-ui-locale'}));
const storageStatus=overview.appendChild(new Element('div',{id:'setup-ui-locale-status'}));
context.localStorage={getItem(){throw Error('blocked');},setItem(){throw Error('blocked');}};
context.window.addEventListener=()=>{};
''')

    def test_description_matching_and_no_value_or_secret_indexing(self):
        self.run_js(r'''
assert.deepStrictEqual(Array.from(context.getSetupSettingsMatches('memory context'),m=>m.id),['llm-context-length']);
assert.equal(context.getSetupSettingsMatches('   ').length,0);
assert.equal(context.getSetupSettingsMatches('sk-secret-not-a-label').length,0);
assert.equal(context.getSetupSettingsMatches('nonexistent-setting').length,0);
assert.equal(context.getSetupSettingsMatches('api key')[0].id,'llm-key');
''')

    def test_hidden_backend_and_disabled_controls_refused_but_collapses_searchable(self):
        self.run_js(r'''
const panel=section.appendChild(new Element('div',{class:'collapse'}));panel.style.display='none';
const tokens=field('llm-max-output-tokens','Output ceiling',panel);
assert.equal(context.getSetupSettingsMatches('output ceiling').length,1);
panel.setAttribute('hidden','');assert.equal(context.getSetupSettingsMatches('output ceiling').length,0);
panel.removeAttribute('hidden');panel.className='backend-only';assert.equal(context.getSetupSettingsMatches('output ceiling').length,0);
panel.style.display='block';tokens.setAttribute('disabled','');assert.equal(context.getSetupSettingsMatches('output ceiling').length,0);
tokens.removeAttribute('disabled');tokens.setAttribute('type','hidden');assert.equal(context.getSetupSettingsMatches('output ceiling').length,0);
tokens.removeAttribute('type');panel.className='collapsing';assert.equal(context.getSetupSettingsMatches('output ceiling').length,0);
assert.equal(context.isSetupSettingAvailable(search),false);
''')

    def test_match_reveals_original_field_and_rechecks_changed_backend(self):
        self.run_js(r'''
search.value='memory';context.renderSetupSettingsSearch();assert.equal(results.children.length,1);
results.children[0].children[0].fire('click');assert.strictEqual(revealed,budget);assert.strictEqual(document.activeElement,budget);assert(details.open);assert(results.hidden);
revealed=null;details.style.display='none';context.openSetupSetting(budget.id);assert.strictEqual(revealed,null);
context.openSetupSetting('missing');assert.strictEqual(revealed,null);
''')

    def test_plain_text_bounded_results_clear_and_escape(self):
        self.run_js(r'''
for(let i=0;i<12;i++){field('extra-'+i,'Matching <img onerror=bad()> '+i,section);}
search.value='matching';context.renderSetupSettingsSearch();assert.equal(results.children.length,8);assert(status.textContent.includes('12 matching'));assert(status.textContent.includes('first 8'));
assert(results.children.every(row=>row.children[0].children.length===0&&row.children[0].textContent.includes('<img')));
assert(results.children.every(row=>row.children[0].getAttribute('data-help-key')==='openSetupSetting'));
const event={key:'Escape',preventDefault(){this.prevented=true;}};context.onSetupSettingsSearchKeydown(event);assert(event.prevented);assert(results.hidden);assert.equal(search.value,'');assert.strictEqual(document.activeElement,search);
search.value='keep';context.onSetupSettingsSearchKeydown({key:'Tab',preventDefault(){throw Error('Tab intercepted');}});assert.equal(search.value,'keep');
''')

    def test_category_navigation_keeps_original_controls_and_searches_inactive_panels(self):
        self.run_js(r'''
const other=form.appendChild(new Element('section',{'data-settings-section':'Speech engine',id:'settings-tts',hidden:''}));
other.appendChild(new Element('h5'));
const language=field('tts-language','TTS Language',other);
const buttons=['settings-llm','settings-tts'].map(id=>overview.appendChild(new Element('button',{'data-settings-target':id})));
assert.equal(context.getSetupSettingsMatches('tts language')[0].id,'tts-language');
context.openSetupSettingsSection('settings-tts');assert(section.hidden);assert(!other.hidden);assert.equal(category.value,'settings-tts');
assert.equal(buttons[0].getAttribute('aria-current'),'false');assert.equal(buttons[1].getAttribute('aria-current'),'true');
assert.strictEqual(document.activeElement,other.querySelector('h5'));assert.strictEqual(document.getElementById('tts-language'),language);
context.openSetupSettingsSection('settings-llm',false);assert(!section.hidden);assert(other.hidden);assert.strictEqual(document.getElementById('llm-context-length'),budget);
context.openSetupSettingsSection('not-a-panel');assert(!section.hidden);assert(other.hidden);
const conditional=other.appendChild(new Element('div',{hidden:''}));field('server','Backend only',conditional);
assert.equal(context.getSetupSettingsMatches('backend only').length,0);
''')

    def test_native_validation_reveals_first_invalid_only(self):
        self.run_js(r'''
form.querySelector=selector=>{assert.equal(selector,'input:invalid, select:invalid, textarea:invalid');return budget;};
context.onSetupSettingsInvalid({target:budget});assert.strictEqual(revealed,budget);
revealed=null;context.onSetupSettingsInvalid({target:key});assert.strictEqual(revealed,null);
''')

    def test_search_outside_save_form_and_classic_script_order(self):
        class Inventory(HTMLParser):
            def __init__(self):
                super().__init__();self.form=False;self.search_inside=None;self.locale_inside=None;self.locale_attrs={};self.scripts=[];self.ids=[]
            def handle_starttag(self, tag, attrs):
                attrs=dict(attrs)
                if tag=='form' and attrs.get('id')=='config-form':self.form=True
                if attrs.get('id')=='setup-settings-search':self.search_inside=self.form
                if attrs.get('id')=='setup-ui-locale':self.locale_inside=self.form;self.locale_attrs=attrs
                if attrs.get('id'):self.ids.append(attrs['id'])
                if tag=='script' and attrs.get('src'):self.scripts.append(attrs)
            def handle_endtag(self,tag):
                if tag=='form':self.form=False
        inventory=Inventory();inventory.feed((STATIC/'index.html').read_text())
        self.assertIs(inventory.search_inside,False)
        self.assertIs(inventory.locale_inside,False)
        self.assertNotIn('name',inventory.locale_attrs)
        self.assertEqual(len(inventory.ids),len(set(inventory.ids)))
        paths=[s['src'].split('?')[0] for s in inventory.scripts]
        self.assertLess(paths.index('/static/js/app-help.js'),paths.index('/static/js/app-settings.js'))
        self.assertTrue(all(s.get('type')!='module' for s in inventory.scripts))

    def test_markup_message_defaults_and_locale_options_match_catalog(self):
        class Messages(HTMLParser):
            def __init__(self):
                super().__init__()
                self.messages = {}
                self.values = {}
                self.current = None
                self.locale = False
                self.options = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'select':
                    self.locale = attrs.get('id') == 'setup-ui-locale'
                if tag == 'option' and self.locale:
                    self.options.append(attrs['value'])
                if 'data-setup-message' in attrs:
                    key = attrs['data-setup-message']
                    if 'data-setup-count' in attrs:
                        self.values[key] = {'count': int(attrs['data-setup-count'])}
                    if tag == 'input':
                        self.messages[key] = attrs['placeholder']
                    else:
                        self.current = key
                        self.messages[key] = ''

            def handle_data(self, data):
                if self.current is not None:
                    self.messages[self.current] += data

            def handle_endtag(self, tag):
                self.current = None
                if tag == 'select':
                    self.locale = False

        markup = Messages()
        markup.feed((STATIC/'index.html').read_text(encoding='utf-8'))
        self.assertEqual(markup.options, ['en', 'qps-ploc'])
        self.assertEqual(set(markup.messages), {'label', 'placeholder', 'clear', 'hint',
                                               'previewAction', 'previewTitle', 'previewClose', 'previewDefaults'})
        self.run_js('for(const [key,text] of Object.entries('+json.dumps(markup.messages)+')) {'
                    'assert.equal(context.getSetupUiMessage(key,('+json.dumps(markup.values)+')[key]||{}),text);'
                    'assert(!/[{}]/.test(text));}')


def contrast(a,b):
    def luminance(color):
        channels=[int(color[i:i+2],16)/255 for i in (1,3,5)]
        linear=[c/12.92 if c<=0.04045 else ((c+0.055)/1.055)**2.4 for c in channels]
        return sum(c*w for c,w in zip(linear,(0.2126,0.7152,0.0722)))
    bright,dark=sorted((luminance(a),luminance(b)),reverse=True)
    return (bright+0.05)/(dark+0.05)


class DarkThemeContrastTests(unittest.TestCase):
    def test_rejects_old_white_on_light_blue_and_accepts_fills_and_text(self):
        self.assertLess(contrast('#ffffff','#8ab4f8'),4.5)
        self.assertAlmostEqual(contrast('#000000','#ffffff'),21)
        html=(STATIC/'index.html').read_text()
        for theme in ['night','super-night']:
            block=re.search(r'html\[data-theme="'+theme+r'"\] \{(.*?)\n        \}',html,re.S).group(1)
            tokens=dict(re.findall(r'(--[\w-]+):\s*(#[0-9a-fA-F]{6})',block))
            for foreground,background in [('#ffffff',tokens['--accent-fill']),('#ffffff',tokens['--accent-fill-strong']),
                                          (tokens['--text-primary'],tokens['--bg-card']),(tokens['--text-muted'],tokens['--bg-card-header'])]:
                with self.subTest(theme=theme,foreground=foreground):self.assertGreaterEqual(contrast(foreground,background),4.5)
            self.assertGreaterEqual(contrast(tokens['--border-input'],tokens['--bg-input']),3)
