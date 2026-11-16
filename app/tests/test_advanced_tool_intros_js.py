import json
import unittest

from tests.test_ui_i18n_voices_casting import LocaleTestCase, STATIC, js


TOOLS = ('designer', 'casting', 'preparer', 'dataset', 'training', 'voicelab', 'reports')
MODULES = tuple(STATIC / 'js' / name for name in
                ('app-i18n.js', 'app-advanced-intros.js', 'app-casting-labels.js'))


class AdvancedToolIntrosTests(LocaleTestCase):
    def hosts(self):
        return ''.join(f"addNode({{'data-advanced-intro':{js(tool)}}},'');\n" for tool in TOOLS)

    def test_intro_catalogs_and_live_switch(self):
        script = self.catalogs_js() + self.hosts() + """
context.renderAdvancedToolIntros();
const results={};
for(const locale of ['en','zh-CN','ja','es-419','qps-ploc','en']){
 await context.applyUiLocale(locale);
 results[locale]=body.children.map(host=>({
  heading:host.children[0].children[0].textContent,
  body:host.children[1].children[0].textContent,
  needs:host.children[1].children[2].children.map(item=>item.textContent),
  expanded:host.children[0].children[0].getAttribute('aria-expanded')
 }));
}
console.log(JSON.stringify(results));
"""
        results = json.loads(self.run_node(script, modules=MODULES).stdout)
        for locale in ('en', *self.catalogs):
            catalog = self.english if locale == 'en' else self.catalogs[locale]
            for tool, rendered in zip(TOOLS, results[locale]):
                with self.subTest(locale=locale, tool=tool):
                    prefix = f'advanced.intro.{tool}.'
                    self.assertEqual(catalog[prefix + 'heading'], rendered['heading'])
                    self.assertEqual(catalog[prefix + 'body'], rendered['body'])
                    self.assertEqual([catalog[prefix + 'need1'], catalog[prefix + 'need2']], rendered['needs'])
                    self.assertEqual('true', rendered['expanded'])
                    if locale != 'en':
                        self.assertNotEqual(self.english[prefix + 'body'], rendered['body'])
        for tool, rendered in zip(TOOLS, results['qps-ploc']):
            self.assertNotEqual(self.english[f'advanced.intro.{tool}.body'], rendered['body'])

    def test_persistence_and_storage_failure(self):
        self.run_node(self.hosts() + """
const saved=new Map();context.window.localStorage={getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)};
context.window.location={hash:'#reports'};
context.renderAdvancedToolIntros();
body.children[0].children[0].children[0].fire('click');
assert.strictEqual(body.children[0].children[1].hidden,true);
context.renderAdvancedToolIntros();
assert.strictEqual(body.children[0].children[0].children[0].getAttribute('aria-expanded'),'false');
assert.strictEqual(body.children[1].children[0].children[0].getAttribute('aria-expanded'),'true');
assert.strictEqual(context.window.location.hash,'#reports');
for(const storage of [null,{getItem(){throw Error('read');},setItem(){throw Error('write');}}]){
 Object.defineProperty(context.window,'localStorage',{get(){if(!storage){throw Error('blocked');}return storage;},configurable:true});
 context.renderAdvancedToolIntros();
 for(const host of body.children){
  const control=host.children[0].children[0];
  assert.strictEqual(control.type,'button');assert.strictEqual(control.getAttribute('aria-expanded'),'true');
  control.fire('click');assert.strictEqual(host.children[1].hidden,true);
  control.fire('click');assert.strictEqual(host.children[1].hidden,false);
 }
}
""", modules=MODULES)

    def test_taxonomy_display_only_and_unknown_fallback(self):
        from casting_tags import AGE_ORDER, GENDER_ORDER, TONE_VOCAB, TEXTURE_VOCAB
        values = sorted(set((*AGE_ORDER, *GENDER_ORDER, *TONE_VOCAB, *TEXTURE_VOCAB)))
        script = self.catalogs_js() + f'const raw={js(values)};const before=JSON.stringify(raw);\n' + """
const results={};
for(const locale of ['en','zh-CN','ja','es-419','qps-ploc']){
 await context.applyUiLocale(locale);
 results[locale]=raw.map(value=>context.getCastingTaxonomyLabel(value));
 const unknown=context.getCastingTaxonomyLabel('<unknown>');
 assert(unknown.includes('<unknown>'));assert.notStrictEqual(unknown,'<unknown>');
}
assert.strictEqual(JSON.stringify(raw),before);console.log(JSON.stringify(results));
"""
        results = json.loads(self.run_node(script, modules=MODULES).stdout)
        self.assertEqual(values, results['en'])
        for locale in (*self.catalogs, 'qps-ploc'):
            for raw, label in zip(values, results[locale]):
                with self.subTest(locale=locale, value=raw):
                    self.assertNotEqual(raw, label)

    def test_badge_live_translation_preserves_modes(self):
        markup = (STATIC / 'index.html').read_text(encoding='utf-8')
        self.assertIn('data-i18n="setup.llm.active.loading" id="llm-active-mode-badge"', markup)
        self.assertNotIn('>Active: Local</span>', markup)
        source = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        start = source.index('        function renderActiveLlmModeBadge()')
        end = source.index('        function onLlmModeChange(', start)
        script = self.catalogs_js() + """
const badge=addNode({id:'llm-active-mode-badge'},'');
vm.runInContext("let savedLlmMode='local',currentLlmMode='remote';",context);
""" + f'vm.runInContext({js(source[start:end])},context);\n' + """
context.renderActiveLlmModeBadge();
for(const locale of ['zh-CN','ja','es-419','qps-ploc']){
 await context.applyUiLocale(locale);
 assert(!badge.textContent.includes('Active:'));assert(!badge.textContent.includes('Save to apply'));
 assert.strictEqual(vm.runInContext('savedLlmMode',context),'local');
}
await context.applyUiLocale('en');assert(badge.textContent.includes('Active: Local'));
"""
        self.run_node(script, modules=MODULES)


if __name__ == '__main__':
    unittest.main()
