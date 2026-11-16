"""Server guidance stays keyed through real workspace and collection projections."""
import json
from pathlib import Path
import tempfile
import unittest
import ast

from book_workspace import get_book_workspace
from collection_overview import get_collection_overview
from workspace_guidance import WORKSPACE_MESSAGES, get_workspace_message, with_workspace_guidance


class WorkspaceGuidanceTests(unittest.TestCase):
    def test_guidance_producers_have_no_unkeyed_english_sentences(self):
        root = Path(__file__).resolve().parent.parent
        for filename in ('book_workspace.py', 'collection_overview.py'):
            tree = ast.parse((root / filename).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value in ('label', 'reason', 'evidence', 'message'):
                            for literal in ast.walk(value):
                                if isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                                    self.assertNotIn(' ', literal.value, (filename, literal.lineno, literal.value))

    def test_every_template_matches_all_catalogs_and_retains_english(self):
        catalogs = Path(__file__).resolve().parent.parent / 'static/i18n'
        for locale in ('en', 'zh-CN', 'ja', 'es-419'):
            messages = json.loads((catalogs / f'ui.{locale}.json').read_text(encoding='utf-8'))
            for key, template in WORKSPACE_MESSAGES.items():
                with self.subTest(locale=locale, key=key):
                    self.assertIn(key, messages)
                    if locale == 'en':
                        self.assertEqual(template, messages[key])

    def test_projection_retains_dynamic_values_when_evidence_becomes_reason(self):
        message = get_workspace_message('workspace.next.voices-pending', count=1)
        @with_workspace_guidance
        def project():
            return {'next': {'reason': message}, 'stage': {'evidence': message}}
        payload = json.loads(json.dumps(project()))
        for field, row in [('reason', payload['next']), ('evidence', payload['stage'])]:
            self.assertEqual(row[field], '1 characters need voice preparation.')
            self.assertEqual(row[field + '_key'], 'workspace.next.voices-pending')
            self.assertEqual(row[field + '_values'], {'count': 1})
            self.assertEqual(row[field + '_template'], '{count} characters need voice preparation.')

    def test_empty_workspace_and_unreadable_collection_are_keyed_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = get_book_workspace(directory, {}, lambda entries, voices: [], lambda row: None)
            self.assertEqual(workspace['name_key'], 'workspace.name.no-book-selected')
            self.assertIn('message_key', workspace['audio'])
            self.assertIn('label_key', workspace['next'])
            self.assertIn('reason_key', workspace['next'])
            scripts = root / 'scripts'
            scripts.mkdir()
            (scripts / 'Broken.json').write_text('{', encoding='utf-8')
            rows = get_collection_overview(scripts, ['Broken'], lambda entries, voices: [])
            for stage in ('meta', 'script', 'review', 'voices', 'render'):
                self.assertIn('evidence_key', rows[0][stage])
            self.assertEqual(rows[0]['script']['evidence_values'], {'error_type': 'ValueError'})


class WorkspaceGuidanceJsTests(unittest.TestCase):
    from tests.test_ui_batch_a_js import UiBatchAJsTests
    run_js = UiBatchAJsTests.run_js

    def test_manifest_status_interpolation_is_keyed_and_translated(self):
        payload = with_workspace_guidance(lambda: {
            'evidence': get_workspace_message('workspace.evidence.manifest-state',
                                              status=get_workspace_message('workspace.manifest.incomplete'))})()
        self.assertEqual(payload['evidence'], 'The three-pass manifest records a incomplete script run.')
        self.assertEqual(payload['evidence_values']['status_key'], 'workspace.manifest.incomplete')
        self.run_js('const payload=' + json.dumps(payload) + ';\n' + r'''
run(source('app-i18n.js'));run(source('app-workspace.js'));
for(const locale of ['en','ja','es-419','zh-CN']){
 run('uiCatalogs.set('+JSON.stringify(locale)+',{messages:'+fs.readFileSync(require('path').join(process.argv[1],'../i18n/ui.'+locale+'.json'),'utf8')+'});');
}
for(const locale of ['en','qps-ploc','ja','es-419','zh-CN']){
 await context.applyUiLocale(locale);
 const status=context.t('workspace.manifest.incomplete','incomplete');
 assert.strictEqual(context.getWorkspaceGuidance(payload,'evidence'),context.t('workspace.evidence.manifest-state','The three-pass manifest records a {status} script run.',{status}));
 if(locale!=='en'){assert(!context.getWorkspaceGuidance(payload,'evidence').includes(' incomplete '));}
}
''')

    def test_eta_uses_the_task_dispatcher_and_repaints_without_polling(self):
        self.run_js(r'''
run(source('app-i18n.js'));run(section('app-core.js','function getTaskLabel(','function taskStatusLabel('));
add('sys-eta');add('sys-eta-val');
run(section('app-workbench.js','function applySystemStatusText(','let _systemStatsPending'));
run(section('app-workbench.js','function formatDuration(','function pollLmStudioStatus('));
let requests=0;context.API={get:async()=>{requests++;return {running:true,task:'audio',label:'Audio generation',eta_seconds:30};}};
for(const locale of ['en','ja','es-419','zh-CN']){
 run('uiCatalogs.set('+JSON.stringify(locale)+',{messages:'+fs.readFileSync(require('path').join(process.argv[1],'../i18n/ui.'+locale+'.json'),'utf8')+'});');
}
await context.updateEtaStatus();
for(const locale of ['qps-ploc','ja','es-419','zh-CN','en']){
 await context.applyUiLocale(locale);context.renderEtaLocaleChrome();
 const expected=context.t('core.eta.with-remaining','{label} (ETA {remaining})',{label:context.getTaskLabel('audio'),remaining:context.formatDuration(30)});
 assert.strictEqual(document.getElementById('sys-eta-val').textContent,expected);
}
assert.strictEqual(requests,1);
''')

    def test_saved_library_repaints_without_replacing_selection_or_repeating_load(self):
        self.run_js(r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
run(source('app-i18n.js'));run(source('app-workspace.js'));add('workspace-library');
for(const locale of ['en','ja','es-419','zh-CN']){
 run('uiCatalogs.set('+JSON.stringify(locale)+',{messages:'+fs.readFileSync(require('path').join(process.argv[1],'../i18n/ui.'+locale+'.json'),'utf8')+'});');
}
context.renderWorkspaceLibrary([{name:'SYNTHETIC',has_voice_config:true}]);
const library=document.getElementById('workspace-library'),button=library.querySelectorAll('button')[0],checkbox=library.querySelectorAll('input')[0];checkbox.checked=true;
for(const locale of ['qps-ploc','ja','es-419','zh-CN','en']){
 await context.applyUiLocale(locale);
 assert.strictEqual(library.querySelectorAll('button')[0],button);assert.strictEqual(library.querySelectorAll('input')[0],checkbox);assert(checkbox.checked);
 assert.strictEqual(button.textContent,context.t('home.workspace.library.load','Load saved book…'));
}
let calls=0,release;context.loadScript=()=>{calls++;return new Promise(resolve=>{release=resolve;});};context.refreshBookWorkspace=async()=>null;
const pending=context.openWorkspaceSavedBook('SYNTHETIC',button);assert(button.disabled);
await context.applyUiLocale('ja');await context.openWorkspaceSavedBook('SYNTHETIC',button);assert.strictEqual(calls,1);assert(button.disabled);
release();await pending;assert.strictEqual(button.disabled,false);
''')

    def test_server_guidance_uses_locale_plurals_and_live_switch_without_fetch(self):
        payload = with_workspace_guidance(lambda: {'reason': get_workspace_message('workspace.next.voices-pending', count=1)})()
        self.run_js('const payload=' + json.dumps(payload) + ';\n' + r'''
run(source('app-i18n.js'));run(source('app-workspace.js'));
for(const locale of ['en','ja','es-419','zh-CN']){
 run('uiCatalogs.set('+JSON.stringify(locale)+',{messages:'+fs.readFileSync(require('path').join(process.argv[1],'../i18n/ui.'+locale+'.json'),'utf8')+'});');
}
const expected={'en':'1 character needs voice preparation.','es-419':'1 personaje necesita preparación de voz.','ja':'1 人の登場人物に音声の準備が必要です。','zh-CN':'1 个角色需要准备声音。'};
for(const locale of ['en','ja','es-419','zh-CN','qps-ploc','es-419']){
 await context.applyUiLocale(locale);
 const text=context.getWorkspaceGuidance(payload,'reason');
 if(locale==='qps-ploc'){assert.notStrictEqual(text,expected.en);}
 else{assert.strictEqual(text,expected[locale]);}
}
context.API={get(){throw Error('must not fetch on locale switch');},post(){throw Error('must not fetch on locale switch');}};
run('workspaceBookSnapshot={retained:true};collectionOverviewSnapshot=[{retained:true}];');
let books=0,collections=0;context.renderBookWorkspace=()=>books++;context.renderCollectionOverview=()=>collections++;
context.renderWorkspaceLocaleChrome();assert.strictEqual(books,1);assert.strictEqual(collections,1);
''')

    def test_safety_retains_english_but_feedback_repaints_without_english_secondary(self):
        self.run_js(r'''
run(source('app-i18n.js'));
const scope=add('source-scope-feedback'),translation=add('translated-edition-feedback');
const discard=add('source-scope-discard','button');discard.textContent='Discard undo; keep current scope';discard.setAttribute('data-i18n','safety.script.scope.discard');
const roster=add('roster-discard','button');roster.textContent='Discard saved undo';roster.setAttribute('data-i18n','safety.script.roster.discard-undo');
context.updateSourceScopeControls=()=>{};context.invalidateSourceScopePreview=()=>{};context.updateTranslatedEditionControls=()=>{};
for(const id of ['source-scope-entry','source-scope-undo-status','source-scope-panel','translated-edition-review','translated-edition-pair']){add(id);}
run('let sourceScopeUndo=null,sourceScopeNextOffset=null,sourceScopeBusy=false;const sourceScopeElement=id=>document.getElementById("source-scope-"+id);let translatedEditionEpoch=0,translatedEditionProposal=null,translatedEditionCancelToken=null,translatedEditionBusy=false;const translatedEditionElement=id=>document.getElementById("translated-edition-"+id);');
run(section('app-project-scope.js','function resetSourceScopeBook(','function onSourceScopePanelToggle('));
run(section('app-translated-edition.js','function resetTranslatedEditionBook(','async function onTranslatedEditionPanelToggle('));
context.resetSourceScopeBook();context.resetTranslatedEditionBook();
for(const locale of ['en','ja','es-419','zh-CN']){
 run('uiCatalogs.set('+JSON.stringify(locale)+',{messages:'+fs.readFileSync(require('path').join(process.argv[1],'../i18n/ui.'+locale+'.json'),'utf8')+'});');
}
for(const locale of ['qps-ploc','ja','es-419','zh-CN','en']){
 await context.applyUiLocale(locale);
 for(const node of [scope,translation]){assert.strictEqual(node.children.length,0);assert.strictEqual(node.textContent,context.t(node.getAttribute('data-i18n'),node===scope?'Book changed. Refresh the saved entries and undo status.':'Panel cleared. Re-opening the panel shows the most recent saved preview for the current book, if one is still on disk; a preview from another book is never shown for this book.'));}
 for(const node of [discard,roster]){assert.strictEqual(node.children.length,locale==='en'?1:2);if(locale!=='en'){assert.strictEqual(node.children[1].getAttribute('lang'),'en');assert.notStrictEqual(node.children[0].textContent,node.children[1].textContent);}}
}
context.renderUiMessage(scope,'New server refusal');await context.applyUiLocale('ja');assert.strictEqual(scope.textContent,'New server refusal');
''')
