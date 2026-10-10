const fs = require('fs');
const assert = require('node:assert/strict');
// Minimal dependency-free DOM for handler/lifecycle tests. This is not browser QA.
const vm = require('node:vm');
class Element {
 constructor(tag,doc){this.tagName=tag.toLowerCase();this.ownerDocument=doc;this.children=[];this.parentElement=null;this.attrs={};this.dataset={};this.style={};this.events={};this._text='';this._value=undefined;this.checked=false;this.disabled=false;this.hidden=false;this.open=false;}
 get id(){return this.attrs.id||'';}set id(v){this.attrs.id=String(v);}
 get type(){return this.attrs.type||'';}set type(v){this.attrs.type=v;}
 get value(){if(this._value!==undefined)return this._value;return this.tagName==='select'?(this.children.find(c=>!c.disabled)?.value||''):this.attrs.value||'';}set value(v){this._value=String(v);}
 get firstChild(){return this.children[0]||null;}
 get textContent(){return this._text+this.children.map(c=>c.textContent).join('');}set textContent(v){this.replaceChildren();this._text=String(v);}
 get className(){return this.attrs.class||'';}set className(v){this.attrs.class=v;}
 setAttribute(k,v){this.attrs[k]=String(v);if(['hidden','disabled','checked'].includes(k))this[k]=true;if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=String(v);}
 append(...nodes){for(const child of nodes){child.parentElement=this;this.children.push(child);}}
 replaceChildren(...nodes){for(const c of this.children)c.parentElement=null;this.children=[];this._text='';this._value=this.tagName==='select'?undefined:this._value;this.append(...nodes);}
 addEventListener(name,callback){(this.events[name]||=[]).push(callback);}
 dispatchEvent(event){event.target=this;for(const callback of this.events[event.type]||[])callback(event);return true;}
 click(){if(this.disabled)return;if(this.tagName==='summary'&&this.parentElement)this.parentElement.open=!this.parentElement.open;this.dispatchEvent({type:'click',target:this});}
 focus(){this.ownerDocument.activeElement=this;}
 querySelectorAll(selector){return query(this,selector);}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
function match(element,selector){
 if(selector.startsWith('#'))return element.id===selector.slice(1);
 const attr=selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
 if(attr){let val=attr[1].startsWith('data-')?element.dataset[attr[1].slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:element.attrs[attr[1]];return val!==undefined&&(attr[2]===undefined||String(val)===attr[2]);}
 return element.tagName===selector.toLowerCase();
}
function query(root,selector){
 if(selector.includes(' > ')){const [parent,child]=selector.split(' > ');return query(root,parent).flatMap(e=>e.children.filter(c=>match(c,child)));}
 if(selector.includes(' ')){const [first,...rest]=selector.split(' ');return query(root,first).flatMap(e=>query(e,rest.join(' ')));}
 const all=[];for(const child of root.children){if(match(child,selector))all.push(child);all.push(...query(child,selector));}return all;
}
function makeWindow(html){
 const doc={activeElement:null,createElement:tag=>new Element(tag,doc)};
 const root=new Element('document',doc);const stack=[root];
 for(const token of html.match(/<!--[\s\S]*?-->|<[^>]+>|[^<]+/g)||[]){
  if(token.startsWith('<!--'))continue;
  if(token.startsWith('</')){const tag=token.match(/^<\/([\w-]+)/)?.[1];if(stack.at(-1).tagName===tag)stack.pop();continue;}
  if(token.startsWith('<')){const tag=token.match(/^<([\w-]+)/)?.[1];if(!tag)continue;const el=doc.createElement(tag);for(const a of token.slice(tag.length+1).matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)')?/g))el.setAttribute(a[1],a[2]??a[3]??'');stack.at(-1).append(el);if(!['input','br','hr','img','meta','link'].includes(tag))stack.push(el);}
  else {const el=doc.createElement('#text');el.textContent=token;stack.at(-1).append(el);}
 }
 doc.getElementById=id=>query(root,'#'+id)[0]||null;doc.querySelector=s=>query(root,s)[0]||null;doc.querySelectorAll=s=>query(root,s);
 const timers=new Map();let next=0;
 const w={document:doc,console,Event:class{constructor(type){this.type=type;}},setTimeout:fn=>{timers.set(++next,fn);return next;},clearTimeout:id=>timers.delete(id),close:()=>timers.clear()};
 w.window=w;vm.createContext(w);w.eval=code=>vm.runInContext(code,w);return w;
}


const root=process.argv[2] || require('path').resolve(__dirname, '../..');
const html=fs.readFileSync(root+'/app/static/index.html','utf8');
let panel=html.slice(html.indexOf('            <div class="card mb-3" id="speaker-review-card">'),html.indexOf('                    <h5 class="m-0">Saved Scripts</h5>'));
panel=panel.slice(0,panel.lastIndexOf('            <div class="card">'));
const core=fs.readFileSync(root+'/app/static/js/app-core.js','utf8');
const book=core.slice(core.indexOf("        let currentBookFilename = '';"),core.indexOf('        function getCurrentBookName('));
const script=fs.readFileSync(root+'/app/static/js/app-speaker-review.js','utf8');
const setup="\nwindow.calls=[];window.confirmed=true;window.confirmations=[];window.clearedAliases=0;\nfunction showConfirm(message, options){confirmations.push({message, options});return window.holdConfirm?new Promise(r=>window.resolveConfirm=r):Promise.resolve(confirmed);}\nfunction clearCharacterAliases(){clearedAliases++;}\nfunction getActionErrorMessage(action,error,recovery){return `${action}. ${recovery} Details: ${error.message}`;}\nconst clone=v=>JSON.parse(JSON.stringify(v));\nwindow.mockOptions={book_id:'book-token-a',sources:[{id:'current',label:'Current script',available:true,entries:5,total:5,complete:true},{id:'checkpoint',label:'Attribution checkpoint',available:true,entries:3,total:5,complete:false}],references:[{name:'Reference <img src=x onerror=window.xss=true>'}],profile:{model:'test-model',endpoint:'https://model.example/v1',available:true},limits:{max_pairs:20,max_attempts:40,modes:['none','low']}};\nwindow.mockPreview={snapshot:'snapshot-one',book_id:'book-token-a',source:'current',reference_name:mockOptions.references[0].name,phase:'reconciled',warning:'Provisional reference.',candidates:[{id:'pair-one',entry_index:0,labels:['Al','Alice'],prediction_label:'Al',reference_label:'Alice',context:[{text:'<img src=x onerror=window.xss=true> Hello',type:'SPOKEN',speaker:'Al'}],reference_context:[{text:'Hello',type:'SPOKEN',speaker:'Alice'}],can_apply:false}],skipped:[],call_count:2,profile:mockOptions.profile,limits:mockOptions.limits};\nwindow.mockReport={...clone(mockPreview),run_id:'run-one',status:'completed',attempts:2,reviews:[{candidate_id:'pair-one',entry_index:0,mode:'none',verdict:{same_identity:true,reason:'Same evidence <script>window.xss=true</script>'}},{candidate_id:'pair-one',entry_index:0,mode:'low',verdict:{same_identity:true,reason:'Supported by the scene'}}],applied:[],stale:false};\nmockReport.candidates[0].can_apply=true;\nmockReport.candidates[0].apply_directions=[{direction:'forward',alias:'Al',canonical:'Alice',can_apply:true,apply_refusal:null},{direction:'reverse',alias:'Alice',canonical:'Al',can_apply:true,apply_refusal:null}];\nwindow.errorResponse=null;\nconst API={get:async url=>{calls.push({method:'GET',url});if(window.errorResponse?.url===url)throw Object.assign(new Error(errorResponse.message),{status:errorResponse.status});if(url.endsWith('/options')){if(window.holdOptions)await new Promise(r=>window.resolveOptions=r);return clone(mockOptions);}if(window.holdReport)await new Promise(r=>window.resolveReport=r);return window.lastReport=clone(mockReport);},post:async(url,body)=>{calls.push({method:'POST',url,body});if(window.errorResponse?.url===url)throw Object.assign(new Error(errorResponse.message),{status:errorResponse.status});if(url.endsWith('/preview')){if(window.holdPreview)await new Promise(r=>window.resolvePreview=r);return clone(mockPreview);}if(url.endsWith('/start')){if(window.holdStart)await new Promise(r=>window.resolveStart=r);return {run_id:'run-one',status:'running'};}if(url.endsWith('/cancel')){mockReport.status='cancelled';return {status:'cancelling'};}if(url.endsWith('/apply')){return {status:'saved',alias:body.alias,canonical:body.canonical,run_id:'run-one',applied:['pair-one']};}throw Error('Unexpected URL '+url);}};\n";
let assertions=0;const passed=[],errors=[];
const settle=async()=>{for(let i=0;i<4;i++)await new Promise(r=>setImmediate(r));};
function fresh(extra=''){
 const w=makeWindow(panel);w.eval(book+setup+extra+script);w.applyCurrentBookFilename('book-a.json');
 w.document.querySelector('summary').click();return w;
}
const $=(w,id)=>w.document.querySelector(id);
async function click(w,id){$(w,id).click();await settle();}
async function change(w,id,value){const e=$(w,id);if(e.type==='checkbox')e.checked=value;else e.value=value;e.dispatchEvent(new w.Event('change',{bubbles:true}));await settle();}
async function load(w){await click(w,'#speaker-review-load');}
async function select(w){await change(w,'#speaker-review-reference',w.mockOptions.references[0].name);}
async function preview(w){await click(w,'#speaker-review-preview');}
async function start(w){await change(w,'#speaker-review-consent',true);await click(w,'#speaker-review-start');}
function check(w,condition,label){assert.equal(!!w.eval(condition),true,label);assertions++;passed.push(label);}
(async()=>{
 let w=fresh();check(w,'calls.length===0','Opening UI makes no requests');
 await load(w);await select(w);
 check(w,"calls.length===1&&calls[0].method==='GET'",'Options only reads metadata');
 check(w,"!window.xss&&!document.querySelector('#speaker-review-reference img')",'Reference filename safely escaped');
 await preview(w);
 check(w,"calls.length===2&&calls[1].url.endsWith('/preview')&&!calls.some(c=>c.url.endsWith('/start'))",'Preview never starts model calls');
 check(w,"document.querySelector('#speaker-review-start').disabled&&document.querySelector('#speaker-review-profile').textContent.includes('https://model.example/v1')&&document.querySelector('#speaker-review-budget').textContent.includes('40 attempts')",'Endpoint and budget displayed; consent unchecked');
 check(w,"!document.querySelector('#speaker-review-results img')&&!window.xss",'Source context safely text-rendered');
 await change(w,'#speaker-review-max-pairs',21);
 check(w,"document.querySelector('#speaker-review-preview').disabled&&document.querySelector('#speaker-review-consent-panel').hidden",'Invalid pair budget disables preview and invalidates consent');
 await change(w,'#speaker-review-max-pairs',2);await preview(w);await change(w,'#speaker-review-consent',true);
 w.eval("document.querySelector('#speaker-review-start').click();document.querySelector('#speaker-review-start').click()");await settle();
 check(w,"calls.filter(c=>c.url.endsWith('/start')).length===1&&calls.find(c=>c.url.endsWith('/start')).body.allow_network===true&&calls.find(c=>c.url.endsWith('/start')).body.snapshot==='snapshot-one'",'Repeated Start deduplicated and consent bound to snapshot');
 check(w,"document.querySelectorAll('[data-apply]').length===1&&!window.xss&&!document.querySelector('#speaker-review-results script')",'Both true judgments show one Apply; model reason escaped');
 w.confirmed=false;await click(w,'[data-apply]');
 check(w,"!calls.some(c=>c.url.endsWith('/apply'))",'Declined confirmation does not Apply');
 w.confirmed=true;await change(w,'[data-direction]','reverse');await click(w,'[data-apply]');
 check(w,"calls.find(c=>c.url.endsWith('/apply')).body.alias==='Alice'&&calls.find(c=>c.url.endsWith('/apply')).body.canonical==='Al'&&calls.find(c=>c.url.endsWith('/apply')).body.candidate_id==='pair-one'&&calls.find(c=>c.url.endsWith('/apply')).body.snapshot==='snapshot-one'",'Reverse Apply uses exact reviewed candidate and snapshot');
 check(w,"document.querySelectorAll('[data-apply]').length===0&&clearedAliases===1&&confirmations[1].message.includes('shared character registry')",'One Apply invalidates remaining report and clears aliases');w.close();
 for(const [verdict,error] of [[false,false],[null,false],[true,true]]){
  w=fresh(`mockReport.reviews[1].verdict.same_identity=${JSON.stringify(verdict)};`+(error?"mockReport.reviews[1].error='model failed';":''));
  await load(w);await select(w);await preview(w);await start(w);
  check(w,"document.querySelectorAll('[data-apply]').length===0",`Unsafe low judgment cannot Apply (${verdict}, error=${error})`);w.close();
 }
 w=fresh();await load(w);await select(w);await preview(w);await start(w);
 w.errorResponse={url:'/api/speaker_review/run-one/apply',status:409,message:'Registry changed'};
 await click(w,'[data-apply]');
 check(w,"document.querySelectorAll('[data-apply]').length===0&&document.querySelector('#speaker-review-status').textContent.includes('Edit aliases')",'409 Apply fails closed with recovery');w.close();
 for(const stage of ['Options','Preview','Start']){
  w=fresh();if(stage!=='Options'){await load(w);await select(w);}if(stage==='Start'){await preview(w);await change(w,'#speaker-review-consent',true);}
  w['hold'+stage]=true;await click(w,'#speaker-review-'+stage.toLowerCase().replace('options','load'));
  assert.equal(typeof w['resolve'+stage],'function');
  w.applyCurrentBookFilename('book-b.json');w['resolve'+stage]();await settle();
  check(w,"document.querySelector('#speaker-review-options').hidden&&document.querySelector('#speaker-review-results').children.length===0&&document.querySelector('#speaker-review-consent-panel').hidden",`Late ${stage} response ignored after book switch`);w.close();
 }
 w=fresh();await load(w);await select(w);await preview(w);await start(w);
 w.mockReport.book_id='other-book';await click(w,'#speaker-review-refresh');
 check(w,"document.querySelector('[data-apply]').disabled",'Wrong-book status disables Apply');w.close();
 w=fresh();await load(w);await select(w);await preview(w);await start(w);
 w.errorResponse={url:'/api/speaker_review/run-one',message:'network failed'};await click(w,'#speaker-review-refresh');
 check(w,"document.querySelector('[data-apply]').disabled",'Failed status disables Apply');w.close();
 w=fresh("mockOptions.recent_run={run_id:'run-one',book_id:'book-token-a',status:'completed'};");await load(w);
 check(w,"calls.length===2&&calls.every(c=>c.method==='GET')&&document.querySelectorAll('[data-apply]').length===1",'Persisted review recovered through read-only calls');w.close();
 w=fresh("mockOptions.active_run={run_id:'run-one',book_id:'book-token-a',status:'running'};mockReport.status='running';");await load(w);await click(w,'#speaker-review-cancel');
 check(w,"calls.filter(c=>c.method==='POST').length===1&&calls.find(c=>c.method==='POST').url.endsWith('/cancel')&&document.querySelector('#speaker-review-status').textContent.includes('Review cancelled')",'Recovered active run explicitly cancellable');w.close();
 w=fresh("mockOptions.active_run={run_id:'previous-run',book_id:'other-book',status:'running'};");await load(w);await click(w,'#speaker-review-cancel-previous');
 check(w,"calls.find(c=>c.method==='POST').url==='/api/speaker_review/previous-run/cancel'",'Previous-book Cancel targets captured run');w.close();
 w=fresh("mockOptions.profile.available=false;mockOptions.profile.reason='Manual mode unsupported';");await load(w);await select(w);await preview(w);await change(w,'#speaker-review-consent',true);
 check(w,"document.querySelector('#speaker-review-start').disabled&&!document.querySelector('#speaker-review-consent-panel').hidden",'Unavailable model allows preview but blocks Start');w.close();
 w=fresh("mockPreview.candidates=[];mockPreview.call_count=0;");await load(w);await select(w);await preview(w);await change(w,'#speaker-review-consent',true);
 check(w,"document.querySelector('#speaker-review-start').disabled&&document.querySelector('#speaker-review-status').textContent.includes('No eligible')",'No eligible pairs cannot start');w.close();
 w=fresh("mockPreview.phase='provisional';mockReport.phase='provisional';mockReport.attempts=40;mockReport.reviews[1].error='Budget reached';mockReport.skipped=[{entry_index:1,reason:'ambiguous'}];");await load(w);await select(w);await preview(w);await start(w);
 check(w,"document.querySelector('#speaker-review-status').textContent.includes('Partial checkpoint')&&document.querySelector('#speaker-review-status').textContent.includes('budget is exhausted')&&document.querySelector('#speaker-review-results').textContent.includes('ambiguous')",'Partial, budget, skipped and review errors visible');w.close();
 w=fresh();await load(w);await select(w);await preview(w);await start(w);w.holdConfirm=true;await click(w,'[data-apply]');
 w.applyCurrentBookFilename('book-b.json');w.resolveConfirm(true);await settle();
 check(w,"!calls.some(c=>c.url.endsWith('/apply'))",'Book switch during Apply confirmation cancels write');w.close();
 w=fresh();await load(w);await select(w);await preview(w);await click(w,'#speaker-review-panel > summary');await click(w,'#speaker-review-panel > summary');
 check(w,"calls.length===2",'Close and reopen does not start or cancel calls');w.applyCurrentBookFilename('book-a.json');
 check(w,"document.querySelector('#speaker-review-results').children.length===0&&document.querySelector('#speaker-review-consent-panel').hidden",'Reload of same filename resets evidence');w.close();
 w=fresh();await load(w);await select(w);await preview(w);w.errorResponse={url:'/api/speaker_review/start',message:'Lost reply'};await start(w);
 check(w,"document.querySelector('#speaker-review-start').disabled&&!document.querySelector('#speaker-review-load').disabled",'Lost Start blocks repeats and permits recovery');
 w.errorResponse=null;w.mockOptions.recent_run={run_id:'run-one',book_id:'book-token-a',status:'completed'};await load(w);
 check(w,"calls.filter(c=>c.url.endsWith('/start')).length===1&&document.querySelectorAll('[data-apply]').length===1",'Lost Start recovered without duplicate');w.close();
 for(const stage of ['Options','Preview','Report']){
  w=fresh();if(stage!=='Options'){await load(w);await select(w);}if(stage==='Report'){await preview(w);await start(w);}
  w['hold'+stage]=true;await click(w,'#speaker-review-'+({Options:'load',Preview:'preview',Report:'refresh'}[stage]));
  w._existingUploadSelectionRequest={}; // Selection intent changed; canceled before it touches active data.
  w['resolve'+stage]();await settle();
  check(w,"!document.querySelector('#speaker-review-load').disabled&&!document.querySelector('#speaker-review-preview').disabled===!!document.querySelector('#speaker-review-reference').value",`Canceled selection during ${stage} leaves controls recoverable`);w.close();
 }
 w=fresh();await load(w);await select(w);await preview(w);await start(w);w.holdConfirm=true;await click(w,'[data-apply]');
 w._existingUploadSelectionPending=new Promise(()=>{});w.resolveConfirm(true);await settle();
 check(w,"!calls.some(c=>c.url.endsWith('/apply'))",'Apply rechecks pending book selection after confirmation');w.close();
 w=fresh();w._existingUploadSelectionRequest={};w.eval("window.selectionPromise=enqueueBookSelection(window._existingUploadSelectionRequest,async()=>{})");await w.selectionPromise;await settle();
 check(w,"window._existingUploadSelectionPending===null",'Successful queued selection clears global pending promise');
 w._existingUploadSelectionRequest={};w.eval("window.selectionPromise=enqueueBookSelection(window._existingUploadSelectionRequest,async()=>{throw Error('synthetic failure')})");try{await w.selectionPromise;}catch{}await settle();
 check(w,"window._existingUploadSelectionPending===null",'Failed queued selection clears global pending promise');await load(w);
 check(w,"calls.length===1",'Review options remain available after settled selection');w.close();

 w=fresh();await load(w);await select(w);await preview(w);await start(w);w.holdReport=true;await click(w,'#speaker-review-refresh');
 check(w,"document.querySelector('[data-apply]').disabled",'Apply disabled while refreshing report');
 await change(w,'#speaker-review-max-pairs',3);w.resolveReport();await settle();w.holdReport=false;await preview(w);await start(w);
 check(w,"document.querySelector('#speaker-review-status').textContent.includes('Review finished')&&calls.filter(c=>c.url.endsWith('/start')).length===2",'Selection change during terminal refresh does not strand polling');w.close();

 w=fresh("mockOptions.active_run={run_id:'run-one',book_id:'book-token-a',status:'running'};mockReport.status='running';mockReport.cancel_requested=true;");await load(w);
 check(w,"document.querySelector('#speaker-review-cancel').disabled&&document.querySelector('#speaker-review-status').textContent.includes('Cancellation requested')",'Cross-process cancellation marker keeps Cancel disabled and request notice visible');w.close();
 w=fresh("mockReport.skipped=[{entry_index:2,reason:'context_limit'}];mockReport.skipped_count=900;");await load(w);await select(w);await preview(w);await start(w);
 check(w,"document.querySelector('#speaker-review-results').textContent.includes('900 entries skipped')&&document.querySelector('#speaker-review-results').textContent.includes('Showing the first 1')",'Capped skipped list reports actual total');w.close();
 w=fresh();await load(w);await select(w);await preview(w);await start(w);w.holdReport=true;await click(w,'#speaker-review-refresh');
 w.applyCurrentBookFilename('book-b.json');w.resolveReport();await settle();
 check(w,"document.querySelector('#speaker-review-results').children.length===0&&!document.querySelector('#speaker-review-load').disabled",'Late status response cannot repopulate switched book');w.close();
 w=fresh("mockOptions.sources.forEach(s=>s.available=false);mockOptions.references=[];");await load(w);
 check(w,"document.querySelector('#speaker-review-preview').disabled&&document.querySelector('#speaker-review-status').textContent.includes('No saved reference')",'Missing sources and references fail safely');w.close();
 w=fresh();await load(w);await select(w);await preview(w);await start(w);await click(w,'#speaker-review-refresh');await click(w,'[data-apply]');
 check(w,"calls.filter(c=>c.url.endsWith('/apply')).length===1&&calls.find(c=>c.url.endsWith('/apply')).body.alias==='Al'",'Unchanged status refresh retains a usable eligible Apply');w.close();
 // Registry eligibility is directional even when both judgments agree.
 for(const blocked of ['forward','reverse']){
  const allowed=blocked==='forward'?'reverse':'forward';
  w=fresh(`const candidate=mockReport.candidates[0];candidate.prediction_label='Ally';candidate.labels=['Ally','Alice'];candidate.apply_directions=[{direction:'forward',alias:'Ally',canonical:'Alice',can_apply:${blocked!=='forward'},apply_refusal:${blocked==='forward'?"'Ally already aliases Alicia'":'null'}},{direction:'reverse',alias:'Alice',canonical:'Ally',can_apply:${blocked!=='reverse'},apply_refusal:${blocked==='reverse'?"'Alice already aliases Alicia'":'null'}}];`);
  await load(w);await select(w);await preview(w);await start(w);
  check(w,`document.querySelectorAll('[data-direction] option').length===1&&document.querySelector('[data-direction]').value==='${allowed}'&&!document.querySelector('[data-apply]').disabled`,`${allowed}-only registry eligibility renders only the available direction`);
  await change(w,'[data-direction]',blocked);await click(w,'[data-apply]');
  check(w,"confirmations.length===0&&!calls.some(c=>c.url.endsWith('/apply'))",`Forged ${blocked} selection cannot confirm or Apply`);
  await change(w,'[data-direction]',allowed);await click(w,'[data-apply]');
  const alias=allowed==='forward'?'Ally':'Alice',canonical=allowed==='forward'?'Alice':'Ally';
  check(w,`calls.filter(c=>c.url.endsWith('/apply')).length===1&&calls.find(c=>c.url.endsWith('/apply')).body.alias==='${alias}'&&calls.find(c=>c.url.endsWith('/apply')).body.canonical==='${canonical}'&&confirmations[0].message.includes('“${alias}” → “${canonical}”')`,`${allowed}-only Apply confirms and submits the exact eligible alias`);w.close();
 }
 for(const [mutation,label] of [
  ['delete mockReport.candidates[0].apply_directions','Missing directions'],
  ['mockReport.candidates[0].apply_directions=[]','Empty directions'],
  ['mockReport.candidates[0].apply_directions={}','Malformed directions'],
  ['mockReport.candidates[0].apply_directions.forEach(d=>d.can_apply=false)','Both directions blocked'],
  ['mockReport.candidates[0].apply_directions.forEach(d=>delete d.can_apply)','Missing direction eligibility'],
  ["mockReport.candidates[0].apply_directions.forEach(d=>d.alias='Unreviewed')",'Unreviewed direction labels'],
  ['mockReport.candidates[0].can_apply=false','Candidate eligibility blocked'],
  ['mockReport.stale=true','Stale report'],
  ["mockReport.status='stale'",'Stale report status'],
  ["mockReport.status='failed'",'Failed report with optimistic eligibility'],
  ["mockReport.status='interrupted'",'Interrupted report with optimistic eligibility'],
  ["mockReport.status='cancelled'",'Cancelled report with optimistic eligibility'],
  ["mockReport.reviews=mockReport.reviews.filter(r=>r.mode!=='none')",'Missing none judgment'],
  ["mockReport.reviews=mockReport.reviews.filter(r=>r.mode!=='low')",'Missing low judgment']
 ]){
  w=fresh(mutation+';');await load(w);await select(w);await preview(w);await start(w);
  check(w,"document.querySelectorAll('[data-apply]').length===0&&document.querySelectorAll('[data-direction]').length===0",`${label} fails closed without direction controls`);w.close();
 }
 w=fresh();await load(w);await select(w);await preview(w);await start(w);await change(w,'[data-direction]','reverse');
 w.mockReport.attempts=3;await click(w,'#speaker-review-refresh');
 check(w,"document.querySelector('[data-direction]').value==='reverse'",'Still-eligible direction survives report refresh');
 w.oldApply=$(w,'[data-apply]');w.mockReport.candidates[0].apply_directions[1].can_apply=false;await click(w,'#speaker-review-refresh');
 check(w,"document.querySelectorAll('[data-direction] option').length===1&&document.querySelector('[data-direction]').value==='forward'",'Unavailable previous direction is discarded on refresh');
 w.oldApply.click();await settle();
 check(w,"confirmations.length===0&&!calls.some(c=>c.url.endsWith('/apply'))",'Detached prior report control cannot Apply a revoked direction');
 await click(w,'[data-apply]');
 check(w,"calls.find(c=>c.url.endsWith('/apply')).body.alias==='Al'&&calls.find(c=>c.url.endsWith('/apply')).body.canonical==='Alice'",'Fallback after unavailable selection applies the remaining eligible direction');w.close();
 for(const [mutation,label] of [
  ['lastReport.candidates[0].apply_directions[0].can_apply=false','Chosen direction revoked'],
  ['delete lastReport.candidates[0].apply_directions','Directions removed'],
  ['lastReport.reviews.pop()','Judgment removed'],
  ['lastReport.candidates=[]','Candidate removed'],
  ['lastReport.stale=true','Report made stale'],
  ["lastReport.status='stale'",'Report status made stale'],
  ["lastReport.candidates[0].prediction_label='Changed';lastReport.candidates[0].apply_directions[0].alias='Changed'",'Eligible direction target changed']
 ]){
  w=fresh();await load(w);await select(w);await preview(w);await start(w);w.holdConfirm=true;await click(w,'[data-apply]');
  w.eval(mutation);w.resolveConfirm(true);await settle();
  check(w,"confirmations.length===1&&!calls.some(c=>c.url.endsWith('/apply'))",`${label} during confirmation prevents Apply`);w.close();
 }
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({harness:'dependency-free Node DOM fixture, not a real browser',assertions,passed,errors},null,2));
})().catch(e=>{console.error(e);process.exitCode=1;});
