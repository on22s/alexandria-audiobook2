"""describeApiError: translate when the server sent a key, never lose English.

The server's English ``detail`` is a rendered sentence, so a dynamic message's
translation is checked against ``message_values`` (the canonical placeholder
set) rather than against the rendered text. Missing, mismatched and no-catalog
cases all fall back to the English sentence, never to a raw key. ``safety.*``
keeps the English beneath the translation.

The slices are the real app-core.js regions, run against the real app-i18n.js,
so the wiring inside showActionError and API._handleError is exercised rather
than re-implemented.
"""
import subprocess
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'

SETUP = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
document.documentElement=new Element('html');
load();
function catalog(messages,review={all:'machine-draft',safety:'not-reviewed'}){return {meta:{review_status:review},messages};}
function setUiCatalog(locale,payload){vm.runInContext('uiCatalogs.set('+JSON.stringify(locale)+','+JSON.stringify(payload)+')',context);}
const core=fs.readFileSync(process.argv[2],'utf8');
vm.runInContext(core.slice(core.indexOf('function showActionError('),core.indexOf('function showConfirm(')),context);
const apiStart=core.indexOf('const API = {');
vm.runInContext(core.slice(apiStart,core.indexOf('get: async (url',apiStart))+'};',context);
vm.runInContext('globalThis.API = API;',context);
'''


class ApiErrorTranslationJsTests(unittest.TestCase):
    def run_js(self, code):
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + SETUP + "\n(async()=>{\n" + code
             + "\n})().catch(error=>{console.error(error);process.exitCode=1;});",
             str(STATIC / 'js' / 'app-i18n.js'), str(STATIC / 'js' / 'app-core.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def test_english_is_used_when_no_key_or_no_catalog_is_available(self):
        self.run_js(r'''
context.applyUiLocale('zh-CN');
assert.strictEqual(context.describeApiError(new Error('Plain refusal.')),'Plain refusal.');
const missing=new Error('Book or voices are busy; please try again.');
missing.messageKey='error.voice.busy';
assert.strictEqual(context.describeApiError(missing),'Book or voices are busy; please try again.');
assert.strictEqual(context.describeApiError({messageKey:'error.voice.busy'}),'[object Object]');
''')

    def test_a_present_key_wins_and_message_values_are_substituted(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({
 'error.task.already_running':'{task} 已在运行。',
 'error.voice.config_busy':'声音配置正忙（被另一个操作锁定）；请重试。',
}));
context.applyUiLocale('zh-CN');
const busy=new Error('Voice config is busy (locked by another operation); please try again.');
busy.messageKey='error.voice.config_busy';
assert.strictEqual(context.describeApiError(busy),'声音配置正忙（被另一个操作锁定）；请重试。');
const running=new Error('Script is already running.');
running.messageKey='error.task.already_running';running.messageValues={task:'Script'};
assert.strictEqual(context.describeApiError(running),'Script 已在运行。');
context.applyUiLocale('en');
assert.strictEqual(context.describeApiError(running),'Script is already running.');
''')

    def test_a_placeholder_mismatch_falls_back_to_english_and_records_a_miss(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({'error.task.already_running':'{task} 已在运行。'}));
context.applyUiLocale('zh-CN');
const running=new Error('Script is already running.');
running.messageKey='error.task.already_running';
assert.strictEqual(context.describeApiError(running),'Script is already running.');
assert(context.getUiCatalogMisses().some(miss=>miss.key==='error.task.already_running'));
''')

    def test_safety_messages_keep_the_english_beneath_the_translation(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({'safety.voice.book_changed':'当前书已更改；请重新加载声音。'}));
context.applyUiLocale('zh-CN');
const changed=new Error('Active book changed; reload voices');
changed.messageKey='safety.voice.book_changed';
assert.strictEqual(context.describeApiError(changed),
                   '当前书已更改；请重新加载声音。\nActive book changed; reload voices');
context.applyUiLocale('en');
assert.strictEqual(context.describeApiError(changed),'Active book changed; reload voices');
''')

    def test_show_action_error_displays_the_translation_without_losing_the_detail(self):
        self.run_js(r'''
setUiCatalog('zh-CN',catalog({'error.voice.config_busy':'声音配置正忙（被另一个操作锁定）；请重试。'}));
context.applyUiLocale('zh-CN');
const toasts=[];context.showToast=(...args)=>toasts.push(args);
const busy=new Error('Voice config is busy (locked by another operation); please try again.');
busy.messageKey='error.voice.config_busy';busy.status=503;
context.showActionError('Save refused',busy,'Reload the book.');
assert(toasts[0][0].includes('声音配置正忙'));
assert(toasts[0][0].includes('Details: '));
context.showActionError('Save refused',new Error('Plain refusal.'),'Reload the book.');
assert(toasts[1][0].includes('Details: Plain refusal.'));
''')

    def test_the_api_wrapper_carries_the_key_and_values_onto_the_error(self):
        self.run_js(r'''
const response={ok:false,status:409,statusText:'Conflict',json:async()=>({
 detail:'Active book changed; reload voices before saving',
 message_key:'safety.voice.book_changed_before_save',
 message_values:{book:'A'}})};
try{await context.API._handleError(response);assert.fail('expected a refusal');}
catch(error){
 assert.strictEqual(error.message,'Active book changed; reload voices before saving');
 assert.strictEqual(error.detail,'Active book changed; reload voices before saving');
 assert.strictEqual(error.messageKey,'safety.voice.book_changed_before_save');
 assert.deepStrictEqual(error.messageValues,{book:'A'});
 assert.strictEqual(error.status,409);
}
const plain={ok:false,status:400,statusText:'Bad Request',json:async()=>({detail:'No input file selected'})};
try{await context.API._handleError(plain);}
catch(error){assert.strictEqual(error.messageKey,undefined);assert.strictEqual(error.detail,'No input file selected');}
''')


if __name__ == '__main__':
    unittest.main()
