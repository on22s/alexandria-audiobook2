"""Calibre foundation UI: config, browse, cross-origin review, pure preview."""
from html.parser import HTMLParser
from pathlib import Path
import json
import subprocess
import unittest

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
CALIBRE = STATIC / 'js' / 'app-calibre.js'
INDEX = STATIC / 'index.html'
CORE = STATIC / 'js' / 'app-core.js'
HELP = STATIC / 'js' / 'app-help.js'

CALIBRE_IDS = [
    'calibre-root-url', 'calibre-username', 'calibre-password',
    'calibre-clear-password', 'calibre-load-config', 'calibre-save-config',
    'calibre-status', 'calibre-page-title', 'calibre-page-info',
    'calibre-entries', 'calibre-browse-root', 'calibre-next-page',
    'calibre-start-page', 'calibre-review-panel', 'calibre-review-url',
    'calibre-review-origin', 'calibre-review-operation',
    'calibre-review-approve', 'calibre-review-cancel',
    'calibre-preview-panel', 'calibre-preview-out',
    'calibre-capture-start', 'calibre-confirmation-panel',
    'calibre-confirmation-provider', 'calibre-confirmation-stable-id',
    'calibre-confirmation-source', 'calibre-confirmation-final',
    'calibre-confirmation-sha256', 'calibre-confirmation-size',
    'calibre-import-confirm', 'calibre-discard-capture',
    'calibre-job-panel', 'calibre-job-status', 'calibre-job-out',
    'calibre-import-receipt', 'calibre-check-status',
    'calibre-cancel-capture', 'calibre-job-discard',
]

PAGE = {
    'review_required': False, 'title': 'Root <script>bad</script>',
    'feed_url': 'https://catalog.example/opds',
    'next_token': 'b' * 32, 'next_loop_refused': False,
    'start_token': 'c' * 32, 'config_revision': 3,
    'pages_fetched': 1, 'entries_total': 3,
    'entries': [
        {'stable_id': 'id1', 'title': 'Good <img onerror=boom>',
         'authors': ['Ann Author'], 'series': 'Saga', 'series_index': 1,
         'subtitle': '', 'format': 'epub', 'has_acquisition': True,
         'rejected_reason': None, 'source_url': 'https://catalog.example/book1',
         'preview_token': 'd' * 32,
         'navigation': [{'title': 'Fiction', 'url': 'https://catalog.example/fiction',
                         'nav_token': 'e' * 32, 'loop_refused': False}]},
        {'stable_id': 'id2', 'title': 'Blocked', 'authors': [], 'series': None,
         'series_index': None, 'subtitle': '', 'format': '',
         'has_acquisition': False, 'rejected_reason': 'Protected by publisher',
         'source_url': None, 'preview_token': None, 'navigation': []},
        {'stable_id': 'id3', 'title': 'Loop Cat', 'authors': [], 'series': None,
         'series_index': None, 'subtitle': '', 'format': '',
         'has_acquisition': False, 'rejected_reason': None,
         'source_url': None, 'preview_token': None,
         'navigation': [{'title': 'Loop', 'url': 'https://catalog.example/loop',
                         'nav_token': None, 'loop_refused': True}]},
    ],
}
PREVIEW = {
    'preview_token': 'd' * 32, 'config_revision': 3,
    'provider': {'title': 'Good <img onerror=boom>', 'authors': ['Ann Author'],
                 'series': 'Saga', 'series_index': 1, 'subtitle': 'Sub',
                 'format': 'epub'},
    'user_editable': {'title': 'Good <img onerror=boom>',
                      'authors': ['Ann Author'], 'series': 'Saga'},
    'stable_id': 'id1', 'feed_url': 'https://catalog.example/opds',
    'source_url': 'https://catalog.example/book1',
}
REVIEW = {'review_required': True, 'review_token': 'f' * 32,
          'destination_origin': 'https://other.example',
          'destination_url': 'https://other.example/opds',
          'operation': 'root', 'config_revision': 3,
          'session_id': 's1', 'nav_token': '1' * 32}
SHA = 'b' * 64
CONFIRM = 'c' * 32
JOB = 'a' * 32
JOB2 = '9' * 32
CAPTURED = {
    'job_token': JOB, 'status': 'captured', 'error': '',
    'confirmation_token': CONFIRM, 'review_token': None,
    'review_destination_url': None, 'review_operation': None,
    'provider': {'title': 'Good <img onerror=boom>', 'authors': ['Ann Author'],
                 'series': 'Saga', 'series_index': 1, 'subtitle': 'Sub',
                 'format': 'epub'},
    'source_identity': {'stable_id': 'id1',
                        'source_url': 'https://catalog.example/book1',
                        'final_url': 'https://catalog.example/book1?download=1'},
    'sha256': SHA, 'size_bytes': 12345, 'config_revision': 3,
}
IMPORTED = {
    'upload': {'stored_filename': 'calibre-abc.txt', 'extraction_report': {'ok': True},
               'book_identity_token': None},
    'provider': {'title': 'Good <img onerror=boom>', 'authors': ['Ann Author'],
                 'series': 'Saga', 'series_index': 1, 'subtitle': 'Sub',
                 'format': 'epub'},
    'source_identity': {'stable_id': 'id1',
                        'source_url': 'https://catalog.example/book1',
                        'final_url': 'https://catalog.example/book1?download=1'},
    'sha256': SHA, 'size_bytes': 12345,
}


class CalibreTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') in CALIBRE_IDS:
            self.tags[attrs['id']] = {'tag': tag, 'attrs': attrs}


def calibre_tags():
    parser = CalibreTags()
    parser.feed(INDEX.read_text(encoding='utf-8'))
    return parser.tags


SETUP = r'''
const path=require('path');
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(...nodes){for(const c of [...this.children]){c.parentElement=null;}this.children=[];for(const n of nodes){this.appendChild(n);}};
const tags=__TAGS__;
for(const [id,info] of Object.entries(tags)){body.appendChild(new Element(info.tag,info.attrs));}
const el=id=>document.getElementById(id);
const calls=[];let deferred=[];
function defer(){const d={};d.promise=new Promise((res,rej)=>{d.res=res;d.rej=rej;});deferred.push(d);return d;}
context.API={
 get:async url=>{calls.push(['GET',url]);const d=deferred.shift();if(d&&d.url===url){}return d?d.promise:apiGet(url);},
 post:async (url,body)=>{calls.push(['POST',url,body]);return apiPost(url,body);}};
let apiGet=async url=>{throw new Error('unexpected GET '+url);};
let apiPost=async (url,body)=>{throw new Error('unexpected POST '+url);};
let timerNow=0,timerSerial=0;const timers=new Map();
context.setTimeout=(fn,ms)=>{timers.set(++timerSerial,{fn,at:timerNow+ms});return timerSerial;};
context.clearTimeout=id=>timers.delete(id);
context.setInterval=(fn,ms)=>{const id=++timerSerial;timers.set(id,{fn,at:timerNow+ms,interval:ms});return id;};
context.clearInterval=id=>timers.delete(id);
function tick(ms){timerNow+=ms;for(const [id,t] of [...timers]){if(t.at<=timerNow){if(t.interval){t.at=timerNow+t.interval;}else{timers.delete(id);}t.fn();}}}
let uploadsRefreshCalls=0,extractionReports=[];
context.loadExistingScriptUploads=async()=>{uploadsRefreshCalls++;const sel=el('existing-upload-select');if(sel){const opt=document.createElement('option');opt.value='calibre-abc.txt';opt.textContent='calibre-abc.txt (12 KB)';sel.replaceChildren();sel.appendChild(opt);sel.value='';}};
context.renderEpubExtractionReport=r=>{extractionReports.push(r);};
context.window.loadExistingScriptUploads=context.loadExistingScriptUploads;
context.window.renderEpubExtractionReport=context.renderEpubExtractionReport;
const settle=()=>new Promise(r=>setImmediate(r));
function drive(id,attr){const h=el(id).getAttribute(attr);assert(h,id+' missing '+attr);return vm.runInContext(h,context);}
function change(id){return drive(id,'onchange');}
function click(id){return drive(id,'onclick');}
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
context.window.resetCalibreSourceView=context.resetCalibreSourceView;
async function readyConfig(){apiGet=async()=>({root_url:'https://catalog.example/opds',username:'reader',configured:true,revision:3});await click('calibre-load-config');assert.strictEqual(el('calibre-browse-root').disabled,false);}
async function readyPreview(page,preview){apiPost=async(url)=>{if(url==='/api/calibre/browse-root'){return page;}throw new Error(url);};await click('calibre-browse-root');apiGet=async url=>{assert.ok(url.startsWith('/api/calibre/preview?preview_token='));return preview;};el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata').fire('click');await settle();assert.strictEqual(el('calibre-preview-panel').hidden,false);}
'''


def run(body):
    code = DOM_FIXTURE + SETUP.replace('__TAGS__', json.dumps(calibre_tags())) + body
    return subprocess.run(['node', '-e', code, str(CALIBRE)],
                          capture_output=True, text=True, timeout=20)


class CalibreJsTests(unittest.TestCase):
    def run_js(self, body):
        result = run(body)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)

    def test_real_index_controls_startup_no_network_and_help(self):
        tags = calibre_tags()
        self.assertEqual(set(CALIBRE_IDS), set(tags))
        help_text = HELP.read_text(encoding='utf-8')
        for field in ['calibre-root-url', 'calibre-username',
                      'calibre-password', 'calibre-clear-password']:
            self.assertIn(f"'{field}'", help_text)
        for action in ['loadCalibreConfig', 'saveCalibreConfig',
                       'browseCalibreRoot', 'browseCalibreNextPage',
                       'browseCalibreStartPage', 'openCalibreNav',
                       'approveCalibreReview', 'cancelCalibreReview',
                       'previewCalibreBook', 'startCalibreCapture',
                       'checkCalibreCaptureStatus', 'cancelCalibreCapture',
                       'discardCalibreCapture', 'importCalibreCapture']:
            self.assertIn(f"'{action}'", help_text)
        self.assertIn('/static/js/app-calibre.js?v=__APP_BUILD__',
                      INDEX.read_text(encoding='utf-8'))
        self.run_js(r'''
assert.strictEqual(calls.length,0,'no network on script startup');
assert.strictEqual(el('calibre-save-config').getAttribute('onclick'),'saveCalibreConfig()');
assert.strictEqual(el('calibre-root-url').getAttribute('onchange'),'onCalibreConfigChange()');
assert.strictEqual(el('calibre-review-approve').getAttribute('onclick'),'approveCalibreReview()');
assert.strictEqual(el('calibre-capture-start').getAttribute('onclick'),'startCalibreCapture()');
assert.strictEqual(el('calibre-import-confirm').getAttribute('onclick'),'importCalibreCapture()');
assert.strictEqual(el('calibre-check-status').getAttribute('onclick'),'checkCalibreCaptureStatus()');
assert.strictEqual(el('calibre-cancel-capture').getAttribute('onclick'),'cancelCalibreCapture()');
assert.strictEqual(el('calibre-discard-capture').getAttribute('onclick'),'discardCalibreCapture()');
assert.strictEqual(el('calibre-capture-start').disabled,true,'capture disabled before preview');
assert.strictEqual(el('calibre-import-confirm').disabled,true,'import disabled before capture');
assert.strictEqual(el('calibre-browse-root').disabled,true,'browse disabled before save/load');
console.log('ok');
''')

    def test_save_keep_clear_and_password_cleared_on_all_outcomes(self):
        self.run_js(r'''
(async()=>{
let resolveSave;apiPost=()=>new Promise(r=>{resolveSave=r;});
el('calibre-root-url').value='https://catalog.example/opds';
el('calibre-username').value='reader';
el('calibre-password').value='secret';
const saving=click('calibre-save-config');
assert.strictEqual(el('calibre-password').value,'','password cleared immediately on submit');
assert.strictEqual(calls.length,1);
assert.strictEqual(calls[0][2].password,'secret','string stores');
resolveSave({root_url:'https://catalog.example/opds',username:'reader',configured:true,revision:4});
await saving;
el('calibre-password').value='';el('calibre-clear-password').checked=false;
apiPost=async (u,b)=>b;
await click('calibre-save-config');
assert.strictEqual(calls.at(-1)[2].password,null,'blank means keep');
el('calibre-clear-password').checked=true;
await click('calibre-save-config');
assert.strictEqual(calls.at(-1)[2].password,'','explicit clear sends empty');
assert.strictEqual(el('calibre-clear-password').checked,false);
el('calibre-password').value='secret2';
apiPost=async()=>{throw Object.assign(new Error('store unavailable'),{status:503});};
await click('calibre-save-config');
assert.strictEqual(el('calibre-password').value,'','password cleared on error too');
assert.match(el('calibre-status').textContent,/store unavailable/);
assert.ok(!el('calibre-status').textContent.includes('secret2'));
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
''')

    def test_load_recovery_and_stale_load_cannot_clobber_edits(self):
        self.run_js(r'''
(async()=>{
let resolveLoad;apiGet=()=>new Promise(r=>{resolveLoad=r;});
const loading=click('calibre-load-config');
el('calibre-root-url').value='https://new.example/opds';
await change('calibre-root-url');
resolveLoad({root_url:'https://old.example',username:'old',configured:true});
await loading;
assert.strictEqual(el('calibre-root-url').value,'https://new.example/opds','stale load must not clobber edit');
assert.match(el('calibre-status').textContent,/not saved/);
apiGet=async()=>({root_url:'https://new.example/opds',username:'new',configured:true,recovery_required:true});
await click('calibre-load-config');
assert.match(el('calibre-status').textContent,/Recovery required/);
assert.strictEqual(el('calibre-browse-root').disabled,true,'recovery blocks browse until save');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
''')

    def test_browse_entries_nav_preview_and_no_forbidden_calls(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'apiPost=async(url,body)=>{if(url===\'/api/calibre/browse-root\'){return '
        body += json.dumps(PAGE) + '; } throw new Error(url);};'
        body += r'''
await click('calibre-browse-root');
assert.ok(calls.some(c=>c[1]==='/api/calibre/browse-root'&&JSON.stringify(c[2])==='{}'));
const rows=el('calibre-entries').children;
assert.strictEqual(rows.length,3);
const good=rows[0].children;
assert.ok(good[0].textContent.includes('<img onerror=boom>'),'provider text literal via textContent');
const navBtn=good.find(b=>b.textContent.startsWith('Open category'));
assert.strictEqual(navBtn.disabled,false);
const loopBtn=rows[2].children.find(b=>b.textContent.startsWith('Open category'));
assert.strictEqual(loopBtn.disabled,true,'null nav token disabled');
const blockedPreview=rows[1].children.find(b=>b.textContent==='Preview metadata');
assert.strictEqual(blockedPreview.disabled,true,'unsupported preview disabled');
assert.ok(rows[1].children.some(c=>c.textContent.includes('Protected by publisher')));
let replayed=null;apiPost=async(url,body)=>{replayed=body;return {review_required:false,title:'Cat',feed_url:'https://catalog.example/cat',entries:[],next_token:null,next_loop_refused:false,start_token:null,config_revision:3,pages_fetched:2,entries_total:3};};
navBtn.fire('click');await settle();await settle();
assert.strictEqual(replayed.token,'e'.repeat(32),'category uses opaque token never display URL');
apiPost=async(url,body)=>{if(url==='/api/calibre/browse-root'){return ''' + json.dumps(PAGE) + r''';} throw new Error(url);};
await click('calibre-browse-root');
const previewBtn=el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata');
apiGet=async url=>{assert.ok(url.startsWith('/api/calibre/preview?preview_token='));return ''' + json.dumps(PREVIEW) + r''';};
previewBtn.fire('click');await settle();
assert.strictEqual(el('calibre-preview-panel').hidden,false);
assert.ok(el('calibre-preview-out').textContent.includes('Good <img onerror=boom>'));
assert.ok(el('calibre-preview-out').textContent.includes('Stable ID: id1'));
assert.ok(!calls.some(c=>/capture|import|uploads\/select|synthesis|tts/.test(String(c[1]))),'no forbidden calls');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
'''
        self.run_js(body)

    def test_new_browse_actions_clear_prior_preview_before_reply(self):
        for action in ('next', 'start', 'nav'):
            with self.subTest(action=action):
                self.run_js('(async()=>{\nawait readyConfig();\n'
                    + 'apiPost=async()=>(' + json.dumps(PAGE) + ');'
                    + "await click('calibre-browse-root');"
                    + 'apiGet=async()=>(' + json.dumps(PREVIEW) + ');'
                    + r"""
const row=el('calibre-entries').children[0];
row.children.find(b=>b.textContent==='Preview metadata').fire('click');await settle();
assert.strictEqual(el('calibre-preview-panel').hidden,false);
let finish;apiPost=()=>new Promise(r=>{finish=r;});
""" + ("row.children.find(b=>b.textContent.startsWith('Open category')).fire('click');"
         if action == 'nav' else "const pending=click('calibre-" + ('next-page' if action == 'next' else 'start-page') + "');")
                    + r"""
assert.strictEqual(el('calibre-preview-panel').hidden,true,'old preview must retire at action start');
assert.strictEqual(el('calibre-preview-out').textContent,'');
finish({review_required:true,review_token:'f'.repeat(32),destination_url:'https://other.example/opds',destination_origin:'https://other.example',operation:'nav'});
await settle();await settle();
assert.strictEqual(el('calibre-review-panel').hidden,false);
assert.strictEqual(el('calibre-next-page').disabled,true,'prior page cannot keep stale navigation beside review');
assert.strictEqual(el('calibre-start-page').disabled,true);
let finishRoot;apiPost=()=>new Promise(r=>{finishRoot=r;});
const rootPending=click('calibre-browse-root');
assert.strictEqual(el('calibre-review-panel').hidden,true);
assert.strictEqual(el('calibre-review-approve').disabled,true);
finishRoot({entries:[]});await rootPending;
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_failed_second_preview_cannot_show_prior_book(self):
        self.run_js('(async()=>{await readyConfig();'
            + 'apiPost=async()=>(' + json.dumps(PAGE) + ');'
            + "await click('calibre-browse-root');"
            + 'apiGet=async()=>(' + json.dumps(PREVIEW) + ');'
            + r"""
const button=el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata');
button.fire('click');await settle();
assert.strictEqual(el('calibre-preview-panel').hidden,false);
let rejectPreview;apiGet=()=>new Promise((r,j)=>{rejectPreview=j;});
button.fire('click');
assert.strictEqual(el('calibre-preview-panel').hidden,true);
assert.strictEqual(el('calibre-preview-out').textContent,'');
rejectPreview(new Error('Preview unavailable'));await settle();
assert.strictEqual(el('calibre-preview-panel').hidden,true);
assert.match(el('calibre-status').textContent,/Preview unavailable/);
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_loading_settings_retires_page_and_unconfigured_blocks_browse(self):
        self.run_js('(async()=>{await readyConfig();'
            + 'apiPost=async()=>(' + json.dumps(PAGE) + ');'
            + "await click('calibre-browse-root');"
            + r"""
let finish;apiGet=()=>new Promise(r=>{finish=r;});
const loading=click('calibre-load-config');
assert.strictEqual(el('calibre-entries').children.length,0,'load retires old page immediately');
assert.strictEqual(el('calibre-page-title').textContent,'');
finish({root_url:'',username:'',configured:false,revision:0});await loading;
assert.strictEqual(el('calibre-browse-root').disabled,true);
const before=calls.length;await click('calibre-browse-root');
assert.strictEqual(calls.length,before,'unconfigured browse must not dispatch');
apiPost=async()=>({root_url:'',username:'',configured:false,revision:1});
await click('calibre-save-config');
assert.strictEqual(el('calibre-browse-root').disabled,true,'disabled config save also blocks browse');
})().catch(e=>{console.error(e);process.exit(1);});
""")

    def test_cross_origin_approve_replay_cancel_and_multihop(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'apiPost=async()=>(' + json.dumps(REVIEW) + ');'
        body += r'''
await click('calibre-browse-root');
assert.strictEqual(el('calibre-review-panel').hidden,false);
assert.strictEqual(el('calibre-review-url').textContent,'https://other.example/opds');
assert.strictEqual(el('calibre-review-origin').textContent,'https://other.example');
assert.ok(!calls.some(c=>c[1]==='/api/calibre/review/approve'),'no automatic approval');
let approveCalls=0,replays=0;
apiPost=async(url,body)=>{if(url==='/api/calibre/review/approve'){approveCalls++;assert.strictEqual(body.token,'f'.repeat(32));return {approved:true,nav_token:'1'.repeat(32),destination_origin:'https://other.example',destination_url:'https://other.example/opds',operation:'root',config_revision:3,session_id:'s1'};} if(url==='/api/calibre/browse-next'){replays++;return ''' + json.dumps(REVIEW) + r''';} throw new Error(url);};
await click('calibre-review-approve');
assert.strictEqual(approveCalls,1,'one approve per click');
assert.strictEqual(replays,1,'approved action replays browse-next once');
assert.strictEqual(el('calibre-review-panel').hidden,false,'second hop needs its own review');
await click('calibre-review-cancel');
assert.strictEqual(el('calibre-review-panel').hidden,true);
await click('calibre-review-approve');
assert.strictEqual(approveCalls,1,'cancelled review cannot be approved');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
'''
        self.run_js(body)

    def test_stale_replies_after_core_reset_and_core_hook_sites(self):
        core = CORE.read_text(encoding='utf-8')
        self.assertGreaterEqual(core.count('window.resetCalibreSourceView?.()'), 3,
                                'apply book, existing selection, and file change hooks')
        body = '(async()=>{\nawait readyConfig();\n'
        body += r'''
const coreSource=fs.readFileSync(path.join(path.dirname(process.argv[1]),'app-core.js'),'utf8');
vm.runInContext("let currentBookFilename='prior.txt';"+coreSource.slice(coreSource.indexOf('function applyCurrentBookFilename('),coreSource.indexOf('function getCurrentBookName(')),context);
let resolveBrowse;apiPost=()=>new Promise(r=>{resolveBrowse=r;});
const browsing=click('calibre-browse-root');
context.applyCurrentBookFilename('other.txt');
el('calibre-status').textContent='after-reset';
resolveBrowse(''' + json.dumps(PAGE) + r''');
await browsing;
assert.strictEqual(el('calibre-entries').children.length,0,'stale browse cannot restore entries');
assert.strictEqual(el('calibre-status').textContent,'after-reset');
let resolvePreview;apiGet=()=>new Promise(r=>{resolvePreview=r;});
const previewing=context.previewCalibreBook('d'.repeat(32));
context.applyCurrentBookFilename('other2.txt');
el('calibre-status').textContent='edit-state';
resolvePreview(''' + json.dumps(PREVIEW) + r''');
await previewing;
assert.strictEqual(el('calibre-preview-panel').hidden,true,'stale preview hidden');
assert.strictEqual(el('calibre-status').textContent,'edit-state');
apiPost=async(url)=>{if(url==='/api/calibre/browse-root'){return ''' + json.dumps(REVIEW) + r''';} throw new Error(url);};
await click('calibre-browse-root');
assert.strictEqual(el('calibre-review-panel').hidden,false);
let resolveApprove;let replayed=false;
apiPost=async(url)=>{if(url==='/api/calibre/review/approve'){return new Promise(r=>{resolveApprove=r;});} if(url==='/api/calibre/browse-next'){replayed=true;return ''' + json.dumps(PAGE) + r''';} throw new Error(url);};
const approving=click('calibre-review-approve');
context.applyCurrentBookFilename('other3.txt');
el('calibre-status').textContent='after-approve-reset';
resolveApprove({approved:true,nav_token:'1'.repeat(32)});
await approving;
assert.strictEqual(replayed,false,'stale approval cannot replay browse');
assert.strictEqual(el('calibre-status').textContent,'after-approve-reset');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
'''
        self.run_js(body)

    def test_foundation_flows_have_no_acquisition_or_import_calls(self):
        text = CALIBRE.read_text(encoding='utf-8')
        for forbidden in ['/api/uploads/select', 'fetch(', 'innerHTML']:
            self.assertNotIn(forbidden, text)
        # Capture/import endpoints exist only behind explicit bound handlers.
        for allowed in ['/api/calibre/capture', '/api/calibre/import',
                        '/api/calibre/capture/status',
                        '/api/calibre/capture/cancel',
                        '/api/calibre/capture/discard']:
            self.assertIn(allowed, text)
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'apiPost=async()=>(' + json.dumps(PAGE) + ');'
        body += "await click('calibre-browse-root');"
        body += 'apiGet=async()=>(' + json.dumps(PREVIEW) + ');'
        body += r"""
el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata').fire('click');await settle();
assert.strictEqual(el('calibre-capture-start').disabled,false,'capture enabled only after pure preview');
assert.ok(!calls.some(c=>/calibre\/(capture|import)/.test(String(c[1]))),'startup/load/browse/pure preview must not acquire or import');
assert.ok(!calls.some(c=>/uploads\/select|synthesis|tts/.test(String(c[1]))),'no select/synthesis during foundation flows');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_explicit_capture_poll_captured_import_refresh_without_selection(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'body.appendChild(new Element("select",{id:"existing-upload-select"}));\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
assert.strictEqual(el('calibre-import-confirm').disabled,true);
apiPost=async(url,body)=>{if(url==='/api/calibre/capture'){assert.strictEqual(body.token,'d'.repeat(32),'capture binds preview token, never display URL');return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/import'){assert.strictEqual(body.confirmation_token,'""" + CONFIRM + r"""');assert.strictEqual(body.sha256,'""" + SHA + r"""');return """ + json.dumps(IMPORTED) + r""";} throw new Error(url);};
let statuses=[{job_token:'""" + JOB + r"""',status:'capturing',error:'',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null},""" + json.dumps(CAPTURED) + r"""];
apiGet=async url=>{assert.ok(url.includes('job_token='));return statuses.shift();};
await click('calibre-capture-start');
assert.ok(calls.some(c=>c[1]==='/api/calibre/capture'),'explicit capture POST');
tick(1000);await settle();
assert.strictEqual(el('calibre-job-panel').hidden,false);
assert.strictEqual(el('calibre-import-confirm').disabled,true,'import disabled while capturing');
tick(1000);await settle();
assert.strictEqual(el('calibre-confirmation-panel').hidden,false);
assert.ok(el('calibre-confirmation-provider').textContent.includes('Good <img onerror=boom>'));
assert.ok(el('calibre-confirmation-provider').textContent.includes('Ann Author'),'provider is OPDS origin, not OPF');
assert.ok(el('calibre-confirmation-stable-id').textContent.includes('id1'));
assert.ok(el('calibre-confirmation-source').textContent.includes('https://catalog.example/book1'));
assert.ok(el('calibre-confirmation-final').textContent.includes('download=1'));
assert.ok(el('calibre-confirmation-sha256').textContent.includes('""" + SHA + r"""'));
assert.ok(el('calibre-confirmation-size').textContent.includes('12345'));
assert.strictEqual(el('calibre-import-confirm').disabled,false);
const importCallsBefore=calls.filter(c=>c[1]==='/api/calibre/import').length;
await click('calibre-import-confirm');
assert.strictEqual(calls.filter(c=>c[1]==='/api/calibre/import').length,importCallsBefore+1,'exactly one import dispatch');
assert.strictEqual(uploadsRefreshCalls,1,'existing uploads refreshed via core loader');
assert.deepStrictEqual(extractionReports,[{ok:true}],'extraction report rendered for current view');
assert.strictEqual(el('existing-upload-select').value,'','import must not set the existing-upload dropdown');
assert.ok(el('existing-upload-select').children.some(o=>o.value==='calibre-abc.txt'),'refreshed inventory lists imported file for manual selection');
assert.ok(el('calibre-import-receipt').textContent.includes('calibre-abc.txt'));
assert.ok(el('calibre-import-receipt').textContent.includes('did not select'));
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'confirmation consumed after import');
await click('calibre-import-confirm');
assert.strictEqual(calls.filter(c=>c[1]==='/api/calibre/import').length,importCallsBefore+1,'second import click cannot redispatch consumed confirmation');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_capture_and_import_guards_before_captured(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
let capturePosts=0;
apiPost=async(url)=>{if(url==='/api/calibre/capture'){capturePosts++;return {job_token:'""" + JOB + r"""',status:'capturing'};} throw new Error(url);};
apiGet=async()=>({job_token:'""" + JOB + r"""',status:'capturing',error:'',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null});
await click('calibre-capture-start');
await click('calibre-capture-start');
assert.strictEqual(capturePosts,1,'duplicate capture cannot start while slot uncertain');
await click('calibre-import-confirm');
assert.ok(!calls.some(c=>c[1]==='/api/calibre/import'),'no import before captured');
await click('calibre-discard-capture');
assert.ok(!calls.some(c=>c[1]==='/api/calibre/capture/discard'),'no discard without confirmation');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_capture_redirect_chain_needs_each_explicit_approval(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
let captureStep=0,approveStep=0;
apiPost=async(url,body)=>{
 if(url==='/api/calibre/review/approve'){approveStep++;const toks=['f'.repeat(32),'1'.repeat(32),'2'.repeat(32)];assert.strictEqual(body.token,toks[approveStep-1],'approves server-held token, never display URL');return {approved:true,preview_token:'d'.repeat(32),operation:'capture',destination_origin:'https://other.example',destination_url:'https://other.example/epub',config_revision:3};}
 if(url==='/api/calibre/capture'){captureStep++;assert.strictEqual(body.token,'d'.repeat(32));if(captureStep===1){return {review_required:true,review_token:'f'.repeat(32),destination_url:'https://other.example/epub',destination_origin:'https://other.example',operation:'capture',config_revision:3};} if(captureStep===2){return {review_required:true,review_token:'1'.repeat(32),destination_url:'https://cdn.example/epub',destination_origin:'https://cdn.example',operation:'capture',config_revision:3};} if(captureStep===3){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(captureStep===4){return {job_token:'""" + JOB2 + r"""',status:'capturing'};} throw new Error('extra capture');}
 throw new Error(url);};
let statusStep=0;
apiGet=async()=>{statusStep++;if(statusStep===1){return {job_token:'""" + JOB + r"""',status:'review_required',error:'',confirmation_token:null,review_token:'2'.repeat(32),review_destination_url:'https://final.example/epub',review_operation:'capture'};} return {job_token:'""" + JOB2 + r"""',status:'captured',error:'',confirmation_token:'""" + CONFIRM + r"""',review_token:null,review_destination_url:null,review_operation:null,provider:""" + json.dumps(CAPTURED['provider']) + r""",source_identity:""" + json.dumps(CAPTURED['source_identity']) + r""",sha256:'""" + SHA + r"""',size_bytes:12345,config_revision:3};};
await click('calibre-capture-start');
assert.strictEqual(el('calibre-review-panel').hidden,false,'initial capture review shown');
assert.strictEqual(el('calibre-review-url').textContent,'https://other.example/epub');
assert.strictEqual(approveStep,0,'no automatic approval');
await click('calibre-review-approve');
assert.strictEqual(captureStep,2,'approval replays capture once');
assert.strictEqual(el('calibre-review-url').textContent,'https://cdn.example/epub','second redirect needs its own review');
await click('calibre-review-approve');
assert.strictEqual(captureStep,3);
tick(1000);await settle();
assert.strictEqual(el('calibre-review-url').textContent,'https://final.example/epub','worker redirect normalized into same review panel');
assert.strictEqual(el('calibre-job-panel').hidden,false);
await click('calibre-review-approve');
assert.strictEqual(captureStep,4,'worker redirect approval replays capture once for same preview');
tick(1000);await settle();
assert.strictEqual(el('calibre-confirmation-panel').hidden,false);
assert.strictEqual(el('calibre-import-confirm').disabled,false);
assert.strictEqual(approveStep,3,'one approval per explicit click across A-B-C chain');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_cancel_start_pending_late_token(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
let resolveCapture;apiPost=async(url)=>{if(url==='/api/calibre/capture'){return new Promise(r=>{resolveCapture=r;});} if(url==='/api/calibre/capture/cancel'){return {job_token:'""" + JOB + r"""',status:'capturing',cancelling:true};} throw new Error(url);};
apiGet=async()=>({job_token:'""" + JOB + r"""',status:'cancelled',error:'Capture cancelled.',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null});
const pending=click('calibre-capture-start');
context.resetCalibreSourceView();
assert.strictEqual(el('calibre-capture-start').disabled,true,'reset retires candidate immediately');
assert.ok(el('calibre-job-status').textContent.includes('View reset during capture start'),'job pane retains pending start');
resolveCapture({job_token:'""" + JOB + r"""',status:'capturing'});
await pending;await settle();
assert.ok(calls.some(c=>c[1]==='/api/calibre/capture/cancel'&&c[2].token==='""" + JOB + r"""'),'late token binds original start and sends cancel');
assert.ok(calls.some(c=>String(c[1]).includes('/api/calibre/capture/status')),'late token checks status');
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'late job cannot restore import for new view');
assert.strictEqual(el('calibre-capture-start').disabled,true,'late job cannot re-enable capture for new view');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_cancel_download_cancelling_vs_cancelled_and_race_discard(self):
        for scenario in ('cancelling', 'race'):
            with self.subTest(scenario=scenario):
                body = '(async()=>{\nawait readyConfig();\n'
                body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
                if scenario == 'cancelling':
                    body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/cancel'){return {job_token:'""" + JOB + r"""',status:'capturing',cancelling:true};} throw new Error(url);};
let n=0;apiGet=async()=>{n++;return n===1?{job_token:'""" + JOB + r"""',status:'capturing',error:'',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null}:{job_token:'""" + JOB + r"""',status:'cancelled',error:'Capture cancelled.',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null};};
await click('calibre-capture-start');tick(1000);await settle();
await click('calibre-cancel-capture');
assert.ok(el('calibre-job-status').textContent.includes('cancelling'),'accepted cancel is not terminal cancelled');
assert.ok(!/status cancelled/i.test(el('calibre-job-out').textContent),'must not claim cancelled from cancelling');
tick(1000);await settle();
assert.ok(el('calibre-job-out').textContent.includes('cancelled'),'terminal cancelled only from status');
const gets=n;tick(2000);await settle();assert.strictEqual(n,gets,'poll stops on terminal cancelled');
"""
                else:
                    body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/cancel'){throw Object.assign(new Error('Capture already completed; discard it instead.'),{status:409});} if(url==='/api/calibre/capture/discard'){return {discarded:true,confirmation_token:'""" + CONFIRM + r"""'};} throw new Error(url);};
apiGet=async()=>({job_token:'""" + JOB + r"""',status:'captured',error:'',confirmation_token:'""" + CONFIRM + r"""',review_token:null,review_destination_url:null,review_operation:null,provider:""" + json.dumps(CAPTURED['provider']) + r""",source_identity:""" + json.dumps(CAPTURED['source_identity']) + r""",sha256:'""" + SHA + r"""',size_bytes:12345,config_revision:3});
await click('calibre-capture-start');
await click('calibre-cancel-capture');await settle();await settle();
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'completion race captured after cancel cannot enable import');
assert.strictEqual(el('calibre-import-confirm').disabled,true,'race captured import disabled');
assert.ok(calls.some(c=>c[1]==='/api/calibre/capture/discard'&&c[2].token==='""" + CONFIRM + r"""'),'completion race captured is discarded, not imported');
assert.ok(el('calibre-job-out').textContent.includes('discarded'),'race discard confirmed in job pane');
"""
                body += r"""
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
                self.run_js(body)

    def test_status_error_keeps_manual_check_and_cancel(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
let capturePosts=0;
apiPost=async(url)=>{if(url==='/api/calibre/capture'){capturePosts++;return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/cancel'){return {job_token:'""" + JOB + r"""',status:'capturing',cancelling:true};} throw new Error(url);};
let fail=true;let gets=0;
apiGet=async()=>{gets++;if(fail){throw new Error('status unavailable');} return {job_token:'""" + JOB + r"""',status:'capturing',error:'',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null};};
await click('calibre-capture-start');
tick(1000);await settle();
assert.ok(el('calibre-job-status').textContent.includes('Status check failed'));
assert.ok(el('calibre-job-out').textContent.includes('""" + JOB + r"""'),'job token kept after status error');
assert.strictEqual(el('calibre-check-status').disabled,false,'manual check remains available');
assert.strictEqual(el('calibre-cancel-capture').disabled,false,'manual cancel remains available');
const before=gets;tick(3000);await settle();assert.strictEqual(gets,before,'no infinite rapid retry after status error');
fail=false;await click('calibre-check-status');
assert.strictEqual(capturePosts,1,'manual check does not recapture');
await click('calibre-cancel-capture');
assert.strictEqual(capturePosts,1,'manual cancel does not recapture');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_resets_during_captured_and_importing(self):
        for scenario in ('captured', 'importing', 'capturing'):
            with self.subTest(scenario=scenario):
                body = '(async()=>{\nawait readyConfig();\n'
                body += 'body.appendChild(new Element("select",{id:"existing-upload-select"}));\n'
                body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
                if scenario == 'captured':
                    body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/discard'){return {discarded:true,confirmation_token:'""" + CONFIRM + r"""'};} throw new Error(url);};
apiGet=async()=>(""" + json.dumps(CAPTURED) + r""");
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-confirmation-panel').hidden,false);
context.resetCalibreSourceView();
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'reset retires confirmation');
assert.strictEqual(el('calibre-import-confirm').disabled,true,'captured after reset cannot restore import');
await settle();
assert.ok(calls.some(c=>c[1]==='/api/calibre/capture/discard'&&c[2].token==='""" + CONFIRM + r"""'),'reset discards owned confirmation');
assert.ok(el('calibre-job-out').textContent.includes('discarded'),'cleanup confirmed in job pane');
"""
                elif scenario == 'importing':
                    body += r"""
let resolveImport;apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/import'){return new Promise(r=>{resolveImport=r;});} throw new Error(url);};
apiGet=async()=>(""" + json.dumps(CAPTURED) + r""");
await click('calibre-capture-start');tick(1000);await settle();
const importing=click('calibre-import-confirm');
assert.strictEqual(el('calibre-cancel-capture').disabled,true,'importing disables cancel');
assert.strictEqual(el('calibre-discard-capture').disabled,true,'importing disables discard');
context.resetCalibreSourceView();
assert.strictEqual(el('calibre-confirmation-panel').hidden,true);
resolveImport(""" + json.dumps(IMPORTED) + r""");
await importing;await settle();
assert.ok(el('calibre-import-receipt').textContent.includes('calibre-abc.txt'),'stale import receipt preserved in job pane');
assert.deepStrictEqual(extractionReports,[],'stale import must not render extraction report for new book');
assert.strictEqual(el('existing-upload-select').value,'','stale import must not set dropdown');
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'stale import does not restore confirmation');
"""
                else:
                    body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/cancel'){return {job_token:'""" + JOB + r"""',status:'capturing',cancelling:true};} throw new Error(url);};
apiGet=async()=>({job_token:'""" + JOB + r"""',status:'cancelled',error:'Capture cancelled.',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null});
await click('calibre-capture-start');
context.resetCalibreSourceView();
assert.ok(calls.some(c=>c[1]==='/api/calibre/capture/cancel'),'reset during capturing requests cancel');
tick(1000);await settle();
assert.ok(el('calibre-job-out').textContent.includes('cancelled'),'job controls/status remain after reset until terminal');
assert.strictEqual(el('calibre-job-panel').hidden,false,'job pane kept after reset');
"""
                body += r"""
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
                self.run_js(body)

    def test_tampered_capture_status_rejects_import(self):
        cases = {
            'bad_sha': {**CAPTURED, 'sha256': 'not-a-sha'},
            'wrong_provider': {**CAPTURED, 'provider': {**CAPTURED['provider'], 'title': 'Other Book'}},
            'wrong_stable': {**CAPTURED, 'source_identity': {**CAPTURED['source_identity'], 'stable_id': 'other'}},
            'token_mismatch': {**CAPTURED, 'job_token': '8' * 32},
        }
        for name, captured in cases.items():
            with self.subTest(case=name):
                body = '(async()=>{\nawait readyConfig();\n'
                body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
                body += "apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'" + JOB + "',status:'capturing'};} throw new Error(url);};"
                body += 'apiGet=async()=>(' + json.dumps(captured) + ');'
                body += r"""
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'tampered status cannot enable import');
assert.strictEqual(el('calibre-import-confirm').disabled,true);
await click('calibre-import-confirm');
assert.ok(!calls.some(c=>c[1]==='/api/calibre/import'),'tampered import never dispatched');
assert.strictEqual(el('calibre-check-status').disabled,false,'manual check stays available for tampered status');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
                self.run_js(body)

    def test_import_uses_actual_core_upload_refresh_slice(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'body.appendChild(new Element("select",{id:"existing-upload-select"}));\n'
        body += 'body.appendChild(new Element("div",{id:"existing-uploads-status"}));\n'
        body += 'body.appendChild(new Element("div",{id:"epub-extraction-report-panel",hidden:""}));\n'
        body += 'body.appendChild(new Element("pre",{id:"epub-extraction-report-json"}));\n'
        body += r"""
const coreSource=fs.readFileSync(path.join(path.dirname(process.argv[1]),'app-core.js'),'utf8');
const renderStart=coreSource.indexOf('function renderEpubExtractionReport');
vm.runInContext("let scriptBatchUploads=[];function renderScriptBatchUploads(){};function escapeHtml(s){return String(s);};"+coreSource.slice(coreSource.indexOf('let existingUploadsLoadRequest'),coreSource.indexOf('window.selectExistingScriptUpload'))+coreSource.slice(renderStart,coreSource.indexOf('window.renderEpubExtractionReport =',renderStart)+66),context);
"""
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/import'){return """ + json.dumps(IMPORTED) + r""";} throw new Error(url);};
apiGet=async url=>{if(url==='/api/uploads'){return [{filename:'calibre-abc.txt',size:12345}];} return """ + json.dumps(CAPTURED) + r""";};
await click('calibre-capture-start');tick(1000);await settle();
await click('calibre-import-confirm');await settle();
const sel=el('existing-upload-select');
assert.ok(String(sel.innerHTML).includes('calibre-abc.txt'),'actual core loadExistingScriptUploads populated inventory');
assert.strictEqual(sel.value||'','', 'actual core refresh does not select the imported file');
assert.strictEqual(el('epub-extraction-report-panel').hidden,false,'actual core renderEpubExtractionReport showed report for current view');
assert.ok(el('epub-extraction-report-json').textContent.includes('"ok": true'));
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)


    def test_p1_import_refresh_reset_preserves_new_view(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'body.appendChild(new Element("select",{id:"existing-upload-select"}));\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/import'){return """ + json.dumps(IMPORTED) + r""";} throw new Error(url);};
apiGet=async()=>(""" + json.dumps(CAPTURED) + r""");
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-import-confirm').disabled,false);
let resolveRefresh;
context.loadExistingScriptUploads=()=>new Promise(r=>{resolveRefresh=r;});
context.window.loadExistingScriptUploads=context.loadExistingScriptUploads;
const importing=click('calibre-import-confirm');
await settle();await settle();
context.resetCalibreSourceView();
apiPost=async(url)=>{if(url==='/api/calibre/browse-root'){return """ + json.dumps(PAGE) + r""";} if(url==='/api/calibre/capture'){return {job_token:'""" + JOB2 + r"""',status:'capturing'};} throw new Error(url);};
await click('calibre-browse-root');
apiGet=async()=>(""" + json.dumps(PREVIEW) + r""");
el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata').fire('click');await settle();
assert.strictEqual(el('calibre-preview-panel').hidden,false,'new preview shown');
const capturesBefore=calls.filter(c=>c[1]==='/api/calibre/capture').length;
await click('calibre-capture-start');
assert.strictEqual(calls.filter(c=>c[1]==='/api/calibre/capture').length,capturesBefore,'new capture cannot dispatch while old import refresh is pending');
resolveRefresh();
await importing;await settle();await settle();
assert.deepStrictEqual(extractionReports,[],'deferred old import must not render extraction report for new view');
assert.ok(!el('calibre-status').textContent.includes('Imported calibre-abc.txt'),'old import must not overwrite new view status');
assert.ok(el('calibre-import-receipt').textContent.includes('calibre-abc.txt'),'old receipt stays truthful on its own job pane');
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'old import must not clear/restore new view confirmation');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_p2_replay_pending_reset_cancels_late_token_b(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/cancel'){return {job_token:'""" + JOB + r"""',status:'capturing',cancelling:true};} throw new Error(url);};
apiGet=async url=>{if(url.includes('""" + JOB2 + r"""')){return {job_token:'""" + JOB2 + r"""',status:'cancelled',error:'Capture cancelled.',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null};} return {job_token:'""" + JOB + r"""',status:'review_required',error:'',confirmation_token:null,review_token:'f'.repeat(32),review_destination_url:'https://cdn.example/epub',review_operation:'capture'};};
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-review-panel').hidden,false,'worker redirect review shown');
let resolveReplay;
apiPost=async(url,body)=>{
 if(url==='/api/calibre/review/approve'){return {approved:true,preview_token:'d'.repeat(32),operation:'capture',destination_origin:'https://cdn.example',destination_url:'https://cdn.example/epub',config_revision:3};}
 if(url==='/api/calibre/capture'){return new Promise(r=>{resolveReplay=r;});}
 if(url==='/api/calibre/capture/cancel'){return {job_token:body.token,status:'capturing',cancelling:true};}
 throw new Error(url);};
const approving=click('calibre-review-approve');
await settle();await settle();
context.resetCalibreSourceView();
resolveReplay({job_token:'""" + JOB2 + r"""',status:'capturing'});
await approving;await settle();await settle();
const cancels=calls.filter(c=>c[1]==='/api/calibre/capture/cancel').map(c=>c[2]&&c[2].token);
assert.ok(cancels.includes('""" + JOB + r"""'),'original token A cancelled');
assert.ok(cancels.includes('""" + JOB2 + r"""'),'late replay token B must be cancelled even though A was already cancelled');
assert.strictEqual(el('calibre-confirmation-panel').hidden,true,'late B cannot restore import');
assert.strictEqual(el('calibre-import-confirm').disabled,true);
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_p3_worker_review_cancel_frees_capture_after_repreview(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
apiPost=async(url,body)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/capture/cancel'){return {job_token:body.token,status:'cancelled',cancelling:false};} throw new Error(url);};
apiGet=async()=>({job_token:'""" + JOB + r"""',status:'review_required',error:'',confirmation_token:null,review_token:'f'.repeat(32),review_destination_url:'https://cdn.example/epub',review_operation:'capture'});
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-review-panel').hidden,false);
await click('calibre-review-cancel');await settle();await settle();
assert.ok(el('calibre-job-out').textContent.includes('cancelled'),'terminal cancel result frees local ownership truthfully');
apiPost=async(url)=>{if(url==='/api/calibre/browse-root'){return """ + json.dumps(PAGE) + r""";} if(url==='/api/calibre/capture'){return {job_token:'""" + JOB2 + r"""',status:'capturing'};} throw new Error(url);};
await click('calibre-browse-root');
apiGet=async()=>(""" + json.dumps(PREVIEW) + r""");
el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata').fire('click');await settle();
assert.strictEqual(el('calibre-capture-start').disabled,false,'capture enabled after cancelled worker review and repreview');
const before=calls.filter(c=>c[1]==='/api/calibre/capture').length;
await click('calibre-capture-start');
assert.strictEqual(calls.filter(c=>c[1]==='/api/calibre/capture').length,before+1,'subsequent capture dispatched');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_p4_unknown_capture_start_keeps_ownership_and_config_save_recovers(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
let capturePosts=0;
apiPost=async(url)=>{if(url==='/api/calibre/capture'){capturePosts++;throw new Error('network lost');} if(url==='/api/calibre/config'){return {root_url:'https://catalog.example/opds',username:'reader',configured:true,revision:4};} throw new Error(url);};
await click('calibre-capture-start');
assert.ok(el('calibre-job-out').textContent.includes('UNKNOWN'),'lost capture start response is UNKNOWN, not failed');
assert.ok(!el('calibre-job-out').textContent.includes('Status: failed'),'unknown start must not claim failed');
assert.strictEqual(el('calibre-capture-start').disabled,true,'unknown start keeps ownership; duplicate capture disabled');
tick(2000);await settle();
assert.strictEqual(capturePosts,1,'no auto resend of unknown capture start');
context.resetCalibreSourceView();
assert.ok(el('calibre-job-out').textContent.includes('UNKNOWN'),'reset must not silently free unknown start');
assert.strictEqual(el('calibre-capture-start').disabled,true);
apiPost=async(url)=>{if(url==='/api/calibre/browse-root'){return """ + json.dumps(PAGE) + r""";} if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} throw new Error(url);};
await click('calibre-browse-root');
apiGet=async()=>(""" + json.dumps(PREVIEW) + r""");
el('calibre-entries').children[0].children.find(b=>b.textContent==='Preview metadata').fire('click');await settle();
assert.strictEqual(el('calibre-capture-start').disabled,true,'repreview alone must not free unknown start');
apiPost=async(url)=>{if(url==='/api/calibre/config'){return {root_url:'https://catalog.example/opds',username:'reader',configured:true,revision:5};} throw new Error(url);};
await click('calibre-save-config');await settle();
assert.ok(el('calibre-job-out').textContent.includes('UNKNOWN'),'config-save retirement keeps unknown truthful');
assert.ok(el('calibre-job-out').textContent.includes('does not claim'),'config save must not claim the capture never ran or was cancelled');
assert.ok(!/Status: cancelled/.test(el('calibre-job-out').textContent),'config save must not claim the unknown capture was cancelled');
assert.ok(el('calibre-job-out').textContent.includes('stale'),'explicit successful config save locally retires unknown start');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_unknown_replay_retires_old_job_token_and_pending_status(self):
        for replay_outcome in ['lost', 'missing_token', 'invalid_token']:
            with self.subTest(replay_outcome=replay_outcome):
                body = '(async()=>{\nawait readyConfig();\n'
                body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
                body += 'const outcome=' + json.dumps(replay_outcome) + ';\n'
                body += r"""
const oldToken='a'.repeat(32);
const reviewStatus={job_token:oldToken,status:'review_required',error:'',confirmation_token:null,review_token:'f'.repeat(32),review_destination_url:'https://cdn.example/epub',review_operation:'capture'};
apiPost=async url=>{assert.strictEqual(url,'/api/calibre/capture');return {job_token:oldToken,status:'capturing'};};
apiGet=async()=>reviewStatus;
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-review-panel').hidden,false);
let finishOldStatus;
apiGet=async()=>new Promise(resolve=>{finishOldStatus=resolve;});
const oldCheck=click('calibre-check-status');await settle();
apiPost=async(url,body)=>{
 if(url==='/api/calibre/review/approve'){return {approved:true,preview_token:'d'.repeat(32),operation:'capture'};}
 if(url==='/api/calibre/capture'){
  if(outcome==='lost'){throw new Error('replay accepted but response lost');}
  return outcome==='missing_token'?{}:{job_token:'invalid',status:'capturing'};
 }
 if(url==='/api/calibre/capture/cancel'){return {job_token:body.token,status:'cancelled',cancelling:false};}
 if(url==='/api/calibre/config'){return {root_url:'https://catalog.example/opds',username:'reader',configured:true,revision:4};}
 throw new Error(url);
};
await click('calibre-review-approve');
assert.ok(el('calibre-job-out').textContent.includes('Status: unknown'),'replay outcome remains UNKNOWN');
assert.strictEqual(el('calibre-check-status').disabled,true,'old A token cannot reconcile unknown replay B');
const before=calls.length;
await click('calibre-check-status');await click('calibre-cancel-capture');
assert.strictEqual(calls.length,before,'neither recovery action may send old A as authority for B');
finishOldStatus(reviewStatus);await oldCheck;await settle();
assert.strictEqual(el('calibre-review-panel').hidden,true,'late old A status cannot restore consumed review');
assert.strictEqual(el('calibre-review-approve').disabled,true);
assert.ok(el('calibre-job-out').textContent.includes('Status: unknown'),'late A status cannot resolve B');
assert.strictEqual(el('calibre-check-status').disabled,true,'A stays unavailable after its pending request settles');
const afterOldStatus=calls.length;
await click('calibre-check-status');await click('calibre-cancel-capture');
assert.strictEqual(calls.length,afterOldStatus,'manual actions cannot resolve B through A after the late reply');
context.resetCalibreSourceView();
await readyPreview(""" + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + r""");
assert.strictEqual(el('calibre-capture-start').disabled,true,'reset and repreview cannot free unknown B');
assert.strictEqual(calls.filter(c=>c[1]==='/api/calibre/capture').length,2,'no automatic replay retry');
assert.strictEqual(calls.filter(c=>c[1]==='/api/calibre/capture/cancel').length,0,'old A never falsely cancelled as B');
apiPost=async url=>{assert.strictEqual(url,'/api/calibre/config');return {root_url:'https://catalog.example/opds',username:'reader',configured:true,revision:4};};
await click('calibre-save-config');await settle();
assert.ok(el('calibre-job-out').textContent.includes('Status: stale'),'successful config save is the recovery fence');
assert.ok(el('calibre-job-out').textContent.includes('does not claim'),'recovery must preserve uncertainty');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
                self.run_js(body)

    def test_p4_definitive_capture_start_rejection_is_failed(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
apiPost=async(url)=>{if(url==='/api/calibre/capture'){throw Object.assign(new Error('Another Calibre capture is already in progress.'),{status:409});} throw new Error(url);};
await click('calibre-capture-start');
assert.ok(el('calibre-job-out').textContent.includes('Status: failed'),'definitive pre-side-effect 409 start rejection may be failed');
assert.strictEqual(el('calibre-capture-start').disabled,false,'definitive rejection frees local start for a later explicit attempt');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)

    def test_p4_unknown_import_keeps_ownership_and_status_reconciles(self):
        body = '(async()=>{\nawait readyConfig();\n'
        body += 'await readyPreview(' + json.dumps(PAGE) + ',' + json.dumps(PREVIEW) + ');\n'
        body += r"""
let importPosts=0;
apiPost=async(url)=>{if(url==='/api/calibre/capture'){return {job_token:'""" + JOB + r"""',status:'capturing'};} if(url==='/api/calibre/import'){importPosts++;throw new Error('response lost');} throw new Error(url);};
apiGet=async()=>(""" + json.dumps(CAPTURED) + r""");
await click('calibre-capture-start');tick(1000);await settle();
assert.strictEqual(el('calibre-import-confirm').disabled,false);
await click('calibre-import-confirm');
assert.ok(el('calibre-job-out').textContent.includes('UNKNOWN'),'lost import response is UNKNOWN, not failed');
assert.ok(!el('calibre-job-out').textContent.includes('Status: failed'),'unknown import must not claim failed');
assert.ok(!el('calibre-job-out').textContent.includes('Status: imported'),'unknown import must not claim imported');
assert.strictEqual(el('calibre-import-confirm').disabled,true,'unknown import cannot redispatch');
assert.strictEqual(el('calibre-discard-capture').disabled,true,'unknown import cannot discard until status is safe');
assert.strictEqual(el('calibre-cancel-capture').disabled,true,'unknown import cannot cancel until status is safe');
assert.strictEqual(el('calibre-capture-start').disabled,true,'unknown import keeps capture ownership');
tick(2000);await settle();
assert.strictEqual(importPosts,1,'no auto resend of unknown import');
context.resetCalibreSourceView();
assert.ok(el('calibre-job-out').textContent.includes('UNKNOWN'),'reset preserves unknown import ownership');
apiGet=async()=>(""" + json.dumps(CAPTURED) + r""");
await click('calibre-check-status');
assert.ok(el('calibre-job-out').textContent.includes('captured'),'server status captured truthfully reconciles unknown import to not-imported');
apiGet=async()=>({job_token:'""" + JOB + r"""',status:'imported',error:'',confirmation_token:null,review_token:null,review_destination_url:null,review_operation:null});
await click('calibre-check-status');
assert.ok(el('calibre-job-out').textContent.includes('Status: imported'),'server status imported truthfully reconciles outcome');
console.log('ok');
})().catch(e=>{console.error(e);process.exit(1);});
"""
        self.run_js(body)


if __name__ == '__main__':
    unittest.main()
