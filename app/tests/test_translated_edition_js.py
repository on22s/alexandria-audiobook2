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
const calls=[];let unavailable=false,stale=false,failCreate=false,releaseCreate=null,latestProposal=null,malformedLatest=[],releasePairs=null;
const proposal={proposal_id:'a'.repeat(32),label:'proposal, not approved',status:'partial',stale,
 source_script_sha256:'b'.repeat(64),target_language:'fr',
 excerpt:{row_count:2},counts:{translated:1,flagged:1,unsupported:0,failed:1},
 rows:[{index:0,speaker:'<script>',source_text:'<img src=x>',translated_text:'FR text',status:'translated',flags:['number_changed'],error:null},
 {index:1,speaker:'BOB',source_text:'Plain row.',translated_text:'',status:'failed',flags:[],error:'synthetic translator failure'}]};
context.window=context;context._existingUploadSelectionRequest={};
context.API={get:async path=>{calls.push(['GET',path]);
 if(path.includes('latest-proposal')){return {proposal:latestProposal,malformed_proposals:malformedLatest};}
 if(path.includes('/proposals/')){return {...proposal,stale};}
 if(path.includes('/pairs')&&releasePairs){await releasePairs.promise;}
 return unavailable?{available:false,reason:'unavailable',pairs:[]}
 :{available:true,reason:'ready',pairs:[{from_code:'en',to_code:'fr',from_name:'English',to_name:'French',package_version:'1.0'}]};},
 post:async(path,payload)=>{calls.push(['POST',path,payload]);
 if(path.includes('/cancel')){return {cancelled:true,reason:'cancel_requested'};}
 if(releaseCreate){await releaseCreate.promise;}
 if(failCreate){throw Error('Language pair is not installed: en -> de');}
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

    def test_book_change_discards_an_in_flight_pairs_refresh(self):
        self.run_js(r'''
(async()=>{
 let resolvePairs;releasePairs={promise:new Promise(r=>{resolvePairs=r;})};
 context.onTranslatedEditionPanelToggle();   // open: pairs refresh starts
 await new Promise(setImmediate);
 context.resetTranslatedEditionBook();       // a book switch while it is in flight
 resolvePairs();
 await new Promise(setImmediate);await new Promise(setImmediate);
 assert.equal(el('pair').children.length,0,'old pairs painted for the new book');
 assert(!el('feedback').textContent.includes('installed pairs listed'));
 assert(el('feedback').textContent.includes('No saved translation preview'));
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

    def test_toggling_the_panel_during_a_build_keeps_its_result(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 let resolveCreate;
 releaseCreate={promise:new Promise(r=>{resolveCreate=r;})};
 const pending=context.createTranslatedEditionProposal();
 await new Promise(setImmediate);
 assert(el('feedback').textContent.includes('Building translation preview'));
 context.onTranslatedEditionPanelToggle();
 assert(el('feedback').textContent.includes('Building translation preview'));
 resolveCreate();await pending;
 assert(text(el('review')).includes('Proposal, not approved'));
 assert(!el('feedback').textContent.includes('Building translation preview'));
 assert(el('feedback').textContent.includes('proposal, not an approved translation'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_create_failure_after_a_selection_change_stays_hidden(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 let resolveCreate;
 releaseCreate={promise:new Promise(r=>{resolveCreate=r;})};
 failCreate=true;
 const pending=context.createTranslatedEditionProposal();
 await new Promise(setImmediate);
 context._existingUploadSelectionRequest={};
 resolveCreate();await pending;
 assert(!el('feedback').textContent.includes('not installed'));
 assert.equal(el('review').children.length,0);
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_opening_the_panel_loads_the_saved_latest_proposal(self):
        self.run_js(r'''
(async()=>{
 latestProposal={...proposal,stale:true};
 el('panel').open=true;
 context.onTranslatedEditionPanelToggle();
 await new Promise(setImmediate);
 const get=calls.find(c=>c[0]==='GET'&&c[1].includes('latest-proposal'));
 assert(get,'latest proposal was not requested');
 assert(text(el('review')).includes('Proposal, not approved'));
 assert(text(el('review')).includes('Stale'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_opening_the_panel_during_a_pairs_refresh_loads_the_saved_preview(self):
        # A manual Refresh in flight must not make the toggle drop the
        # saved proposal: re-opening waits for the refresh, then loads it.
        self.run_js(r'''
(async()=>{
  latestProposal={...proposal,stale:false};
  let resolvePairs;releasePairs={promise:new Promise(r=>{resolvePairs=r;})};
  const manual=context.refreshTranslatedEditionPairs();      // user clicked Refresh
  await new Promise(setImmediate);                           // now in flight (busy)
  el('panel').open=true;
  context.onTranslatedEditionPanelToggle();                  // user opens the panel
  await new Promise(setImmediate);
  const latestGets=()=>calls.filter(c=>c[0]==='GET'&&c[1].includes('latest-proposal')).length;
  assert.equal(latestGets(),0,'nothing loaded while the refresh is in flight');
  resolvePairs();  // refresh completes now
  await manual;
  await new Promise(setImmediate);await new Promise(setImmediate);
  assert.equal(latestGets(),1,'saved preview was not loaded after the refresh finished');
  assert(text(el('review')).includes('Proposal, not approved'));
  assert(el('feedback').textContent.includes('still matches'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_unavailable_pairs_and_a_saved_preview_are_both_shown(self):
        self.run_js(r'''
(async()=>{
  unavailable=true;latestProposal={...proposal,stale:true};
  el('panel').open=true;
  await context.onTranslatedEditionPanelToggle();
  await new Promise(setImmediate);await new Promise(setImmediate);
  assert(el('feedback').textContent.includes('unavailable'),
         'pair unavailability was overwritten by the preview message');
  assert(el('feedback').textContent.includes('stale'),
         'the saved preview message was lost behind the pair check');
  assert(text(el('review')).includes('Proposal, not approved'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_failed_pairs_check_and_a_saved_preview_are_both_shown(self):
        self.run_js(r'''
(async()=>{
  latestProposal={...proposal,stale:false};
  const originalGet=context.API.get;
  context.API.get=async path=>{calls.push(['GET',path]);
    if(path.includes('latest-proposal'))return {proposal:latestProposal,malformed_proposals:[]};
    if(path.includes('/proposals/'))return {...proposal,stale:false};
    throw Error('pairs endpoint down');};
  el('panel').open=true;
  await context.onTranslatedEditionPanelToggle();
  await new Promise(setImmediate);await new Promise(setImmediate);
  assert(el('feedback').textContent.includes('pairs endpoint down'),
         'the pairs failure was overwritten by the preview message');
  assert(el('feedback').textContent.includes('still matches'),
         'the saved preview was not reached when pairs failed');
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_a_partial_preview_says_why_it_is_not_complete(self):
        self.run_js(r'''
(async()=>{
  await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
  proposal.status='partial';proposal.counts={translated:2,flagged:1,unsupported:0,failed:0,skipped:0,cancelled:0};
  proposal.rows=[{index:0,speaker:null,source_text:'Room 3 was quiet.',translated_text:'Raum war still.',status:'translated',flags:['number_changed'],error:null},
    {index:1,speaker:'BOB',source_text:'Plain row.',translated_text:'FR: Plain row.',status:'translated',flags:[],error:null}];
  await context.createTranslatedEditionProposal();
  assert(text(el('review')).includes('Why this is not complete: 1 flagged for review.'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_reopening_the_panel_during_a_rebuild_keeps_the_build_feedback(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 await context.createTranslatedEditionProposal();
 assert(text(el('review')).includes('Proposal, not approved'));
 let resolveCreate;
 releaseCreate={promise:new Promise(r=>{resolveCreate=r;})};
 const pending=context.createTranslatedEditionProposal();
 await new Promise(setImmediate);
 const proposalGets=()=>calls.filter(c=>c[0]==='GET'&&c[1].includes('/proposals/')).length;
 const before=proposalGets();
 assert(el('feedback').textContent.includes('Building translation preview'));
 el('panel').open=false;el('panel').open=true;
 context.onTranslatedEditionPanelToggle();
 await new Promise(setImmediate);
 assert.equal(proposalGets(),before,'re-checked the old proposal during a build');
 assert(el('feedback').textContent.includes('Building translation preview'));
 assert(!el('feedback').textContent.includes('still matches'));
 resolveCreate();await pending;
 assert(text(el('review')).includes('Proposal, not approved'));
 assert(el('feedback').textContent.includes('proposal, not an approved translation'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_a_slow_pair_list_does_not_overwrite_the_saved_preview_message(self):
        self.run_js(r'''
(async()=>{
 latestProposal={...proposal,stale:true};malformedLatest=['c'.repeat(32)+'.json'];
 let resolvePairs;releasePairs={promise:new Promise(r=>{resolvePairs=r;})};
 el('panel').open=true;
 context.onTranslatedEditionPanelToggle();
 await new Promise(setImmediate);
 resolvePairs();
 await new Promise(setImmediate);await new Promise(setImmediate);
 assert(el('feedback').textContent.includes('stale'),'pair list overwrote the preview message');
 assert(el('feedback').textContent.includes('could not be read and were skipped'));
 assert(!el('feedback').textContent.includes('installed pairs listed'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_panel_survives_a_book_with_no_readable_saved_preview(self):
        self.run_js(r'''
(async()=>{
 latestProposal=null;malformedLatest=['c'.repeat(32)+'.json'];
 el('panel').open=true;
 context.onTranslatedEditionPanelToggle();
 await new Promise(setImmediate);
 assert.equal(el('review').children.length,0);
 assert(el('feedback').textContent.includes('could not be read and were skipped'));
 assert(el('feedback').textContent.includes('No readable saved preview'));
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
 assert(el('feedback').textContent.includes('already finished'));
 assert(!el('feedback').textContent.includes('is saved as cancelled.'));
 resolveCreate();await pending;
 assert(text(el('review')).includes('partial'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_book_change_says_what_the_panel_can_show(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 await context.createTranslatedEditionProposal();
 context.resetTranslatedEditionBook();
 assert.equal(el('review').children.length,0);
 assert(el('feedback').textContent.includes('never shown for this book'));
 assert(!el('feedback').textContent.includes('was removed'));
 assert(!el('feedback').textContent.includes('Checking installed language pairs'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_panel_text_says_where_a_proposal_lives(self):
        html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text(
            encoding='utf-8')
        start = html.index('id="translated-edition-panel"')
        panel = html[start:html.index('</details>', start)]
        self.assertIn('re-opening this panel shows the most recent saved preview', panel)
        self.assertIn('a preview from another book is never shown for this book', panel)
        self.assertIn('selecting an uploaded source leaves old files on disk', panel)
        self.assertIn('does not copy proposals into the saved book', panel)
        # The final behaviour: no download happens, and a run that would
        # need one is refused before it starts, not disclosed as possible.
        self.assertIn('Nothing is downloaded and no text leaves this machine', panel)
        self.assertIn('refuses to start rather than let Argos fetch a sentence-splitting model', panel)
        self.assertIn('the refusal names the file', panel)
        self.assertNotIn('may fetch', panel)
        self.assertNotIn('disclos', panel)


    def test_a_build_starting_while_the_toggle_loads_keeps_its_feedback(self):
        # The pair check finishes, the toggle is awaiting the saved-proposal
        # reply, and a build starts in that window. Its "Building" feedback
        # must survive the stale reply (the epoch guard in the loader).
        self.run_js(r'''
(async()=>{
  latestProposal={...proposal,stale:false};
  let releaseLatest=null;let holdLatest=true;
  context.API.get=async path=>{calls.push(['GET',path]);
    if(path.includes('latest-proposal')){
      if(holdLatest){await new Promise(r=>{releaseLatest=r;});}
      return {proposal:latestProposal,malformed_proposals:[]};}
    if(path.includes('/proposals/'))return {...proposal,stale:false};
    return {available:true,reason:'ready',pairs:[{from_code:'en',to_code:'fr',from_name:'English',to_name:'French'}]};};
  el('panel').open=true;
  const toggle=context.onTranslatedEditionPanelToggle();
  await new Promise(setImmediate);await new Promise(setImmediate);
  el('pair').value='en->fr';
  let resolveCreate;releaseCreate={promise:new Promise(r=>{resolveCreate=r;})};
  const build=context.createTranslatedEditionProposal();
  await new Promise(setImmediate);
  assert(el('feedback').textContent.includes('Building translation preview'));
  holdLatest=false;releaseLatest();
  await toggle;
  await new Promise(setImmediate);
  assert(el('feedback').textContent.includes('Building translation preview'),
         'the stale proposal reply overwrote the build feedback: '+el('feedback').textContent);
  releaseCreate=null;resolveCreate();await build;
  assert(text(el('review')).includes('Proposal, not approved'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')


    def test_excerpt_only_note_names_the_untranslated_remainder(self):
        self.run_js(r'''
(async()=>{
 await context.refreshTranslatedEditionPairs();el('pair').value='en->fr';
 proposal.status='excerpt_complete';proposal.excerpt.total_rows=40;proposal.excerpt.row_count=25;
 proposal.counts={translated:25,flagged:0,unsupported:0,failed:0};
 await context.createTranslatedEditionProposal();
 assert(text(el('review')).includes('excerpt complete'));
 assert(text(el('review')).includes('Excerpt only: the first 25 of 40 Script rows'));
 assert(text(el('review')).includes('rest of the book was not translated'));
})().catch(e=>{console.error(e);process.exitCode=1;});
''')

    def test_book_change_reset_is_wired_in_the_core_script(self):
        core = CORE.read_text(encoding='utf-8')
        start = core.index('function applyCurrentBookFilename(')
        body = core[start:core.index('function getCurrentBookName(')]
        self.assertIn('window.resetTranslatedEditionBook?.();', body)


if __name__ == '__main__':
    unittest.main()
