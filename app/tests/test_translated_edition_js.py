"""Translation preview panel: labelled proposal, safe text, stale and cancel paths."""
from pathlib import Path
import subprocess
import unittest
from tests.test_ui_help_js import DOM_FIXTURE

SOURCE = Path(__file__).resolve().parents[1] / 'static/js/app-translated-edition.js'
CORE = Path(__file__).resolve().parents[1] / 'static/js/app-core.js'
SETUP = r'''
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Element.prototype.replaceChildren=function(...nodes){for(const child of [...this.children]){child.remove();}for(const node of nodes){this.appendChild(node);}};
const text=node=>node.textContent+node.children.map(text).join('');
for(const id of ['panel','pair','create','refresh','cancel','feedback','review']){
 body.appendChild(new Element('div',{id:'translated-edition-'+id}));}
const el=id=>document.getElementById('translated-edition-'+id);el('panel').open=true;el('pair').value='';
const calls=[];let unavailable=false,stale=false,failCreate=false,releaseCreate=null;
const proposal={proposal_id:'a'.repeat(32),label:'proposal, not approved',status:'partial',stale,
 source_script_sha256:'b'.repeat(64),target_language:'fr',
 excerpt:{row_count:2},counts:{translated:1,flagged:1,unsupported:0,failed:1},
 rows:[{index:0,speaker:'<script>',source_text:'<img src=x>',translated_text:'FR text',status:'translated',flags:['number_changed'],error:null},
 {index:1,speaker:'BOB',source_text:'Plain row.',translated_text:'',status:'failed',flags:[],error:'synthetic translator failure'}]};
context.window=context;context._existingUploadSelectionRequest={};
context.API={get:async path=>{calls.push(['GET',path]);
 if(path.includes('/proposals/')){return {...proposal,stale};}
 return unavailable?{available:false,reason:'unavailable',pairs:[]}
 :{available:true,reason:'ready',pairs:[{from_code:'en',to_code:'fr',from_name:'English',to_name:'French',package_version:'1.0'}]};},
 post:async(path,payload)=>{calls.push(['POST',path,payload]);
 if(path.includes('/cancel')){return {cancelled:true,reason:'cancel_requested'};}
 if(failCreate){throw Error('Language pair is not installed: en -> de');}
 if(releaseCreate){await releaseCreate.promise;}
 return proposal;}};
vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
'''


class TranslatedEditionJSTests(unittest.TestCase):
    def run_js(self, script):
        script = "let finished=false;process.on('beforeExit',()=>assert(finished,'Test did not finish'));\n" + script.replace('})().catch', '})().then(()=>{finished=true;}).catch')
        result = subprocess.run(['node', '-e', DOM_FIXTURE + SETUP + script, str(SOURCE)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_pairs_create_and_render_labelled_proposal_safely(self):
        self.run_js(r'''
(async()=>{
 await context.createTranslatedEditionProposal();assert.equal(calls.filter(c=>c[0]==='POST').length,0);
 await context.refreshTranslatedEditionPairs();
 assert(el('pair').children.length>=1);el('pair').value='en->fr';
 await context.createTranslatedEditionProposal();
 const posted=calls.find(c=>c[0]==='POST');
 assert.equal(posted[2].source_language,'en');assert.equal(posted[2].target_language,'fr');
 assert.match(posted[2].cancel_token,/^[0-9a-f]{32}$/);
 assert(text(el('review')).includes('Proposal, not approved'));
 assert(text(el('review')).includes('<img src=x>'));
 assert.equal(el('review').querySelectorAll('img').length,0);
 assert(text(el('review')).includes('number_changed'));
 assert(text(el('review')).includes('partial'));
 assert(text(el('review')).includes('synthetic translator failure'));
 assert(el('feedback').textContent.includes('proposal, not an approved translation'));
 assert(el('cancel').disabled);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_unavailable_and_create_failure_stay_visible(self):
        self.run_js(r'''
(async()=>{
 unavailable=true;await context.refreshTranslatedEditionPairs();
 assert(el('feedback').textContent.includes('unavailable'));assert(el('create').disabled);
 unavailable=false;await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 failCreate=true;await context.createTranslatedEditionProposal();
 assert(el('feedback').textContent.includes('not installed'));
 assert.equal(el('review').children.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_stale_reply_and_book_change_guard(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 let resolveCreate;
 releaseCreate={promise:new Promise(r=>{resolveCreate=r;})};
 const pending=context.createTranslatedEditionProposal();
 await new Promise(setImmediate);
 assert(!el('cancel').disabled);
 context.resetTranslatedEditionBook();
 resolveCreate();await pending;
 assert.equal(el('review').children.length,0);
 assert(el('cancel').disabled);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_rechecking_a_saved_proposal_surfaces_the_stale_flag(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 await context.createTranslatedEditionProposal();
 assert(!text(el('review')).includes('Stale'));
 stale=true;
 await context.refreshTranslatedEditionProposal();
 const gets=calls.filter(c=>c[0]==='GET'&&c[1].includes('/proposals/'));
 assert.equal(gets.length,1);
 assert(text(el('review')).includes('Stale'));
 assert(el('feedback').textContent.includes('stale'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_cancel_button_requests_a_stop_for_the_running_preview(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 let resolveCreate;
 releaseCreate={promise:new Promise(r=>{resolveCreate=r;})};
 const pending=context.createTranslatedEditionProposal();
 await new Promise(setImmediate);
 await context.cancelTranslatedEditionProposal();
 const cancel=calls.find(c=>c[0]==='POST'&&c[1].includes('/cancel'));
 assert(cancel,'no cancel request was sent');
 assert(cancel[1].includes('/proposals/')&&cancel[1].endsWith('/cancel'));
 assert(el('feedback').textContent.includes('Cancel requested'));
 resolveCreate();await pending;
 assert(text(el('review')).includes('partial'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_book_change_reset_is_wired_in_the_core_script(self):
        core = CORE.read_text(encoding='utf-8')
        start = core.index('function applyCurrentBookFilename(')
        body = core[start:core.index('function getCurrentBookName(')]
        self.assertIn('window.resetTranslatedEditionBook?.();', body)


if __name__ == '__main__':
    unittest.main()
