"""Known-control help lookup and dynamic DOM behavior; actual hover is separate."""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest

STATIC = Path(__file__).resolve().parent.parent / 'static'
DOM_FIXTURE = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
function matches(node, selector) {
 return selector.split(',').some(raw=>{
  let part=raw.trim();
  if(part===':disabled'){
   if(node.disabled){return true;}
   for(let p=node.parentElement;p;p=p.parentElement){
    if(p.tagName==='FIELDSET'&&p.disabled&&!p.children.find(c=>c.tagName==='LEGEND')?.contains(node)){return true;}
   }
   return false;
  }
  if(part.includes(':hover')&&!node.hovered){return false;}
  if(part.includes(':focus-within')&&!node.focused){return false;}
  if(part.includes(':not([type="hidden"])')&&node.type==='hidden'){return false;}
  part=part.replace(':not([type="hidden"])','');
  const tag=/^[a-z]+/i.exec(part);if(tag&&node.tagName!==tag[0].toUpperCase()){return false;}
  const id=/#([\w-]+)/.exec(part);if(id&&node.id!==id[1]){return false;}
  for(const m of part.matchAll(/\.([\w-]+)/g)){if(!node.classList.contains(m[1])){return false;}}
  for(const m of part.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)){
   if(!node.hasAttribute(m[1])||(m[2]!==undefined&&node.getAttribute(m[1])!==m[2])){return false;}
  }
  return true;
 });
}
class Element {
 constructor(tag,attrs={}){this.nodeType=1;this.tagName=tag.toUpperCase();this.attrs={...attrs};this.children=[];this.parentElement=null;this.listeners={};this.style={display:/display\s*:\s*none/.test(attrs.style||'')?'none':''};this.labels=[];this.textContent='';}
 get id(){return this.attrs.id||'';} get type(){return this.attrs.type||(this.tagName==='INPUT'?'text':'');}
 get className(){return this.attrs.class||'';}set className(value){this.attrs.class=value;}
 get classList(){return {contains:value=>this.className.split(/\s+/).includes(value)};}
 get dataset(){const data={};for(const [key,value] of Object.entries(this.attrs)){if(key.startsWith('data-')){data[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=value;}}return data;}
 get hidden(){return this.hasAttribute('hidden');}set hidden(value){if(value){this.setAttribute('hidden','');}else{this.removeAttribute('hidden');}}
 get disabled(){return this.hasAttribute('disabled');}
 get isConnected(){return this===body||!!this.parentElement?.isConnected;}
 get previousElementSibling(){if(!this.parentElement){return null;}const items=this.parentElement.children;return items[items.indexOf(this)-1]||null;}
 setAttribute(key,value){this.attrs[key]=String(value);}getAttribute(key){return this.hasAttribute(key)?this.attrs[key]??'':null;}hasAttribute(key){return Object.hasOwn(this.attrs,key);}removeAttribute(key){delete this.attrs[key];}
 matches(selector){return matches(this,selector);}closest(selector){for(let n=this;n;n=n.parentElement){if(n.matches(selector)){return n;}}return null;}
 contains(other){for(let node=other;node;node=node.parentElement){if(node===this){return true;}}return false;}
 querySelectorAll(selector){return this.children.flatMap(c=>[...(c.matches(selector)?[c]:[]),...c.querySelectorAll(selector)]);}
 appendChild(child){child.remove();child.parentElement=this;this.children.push(child);return child;}
 insertAdjacentElement(position,child){assert.strictEqual(position,'afterend');assert(this.parentElement);const parent=this.parentElement;child.remove();child.parentElement=parent;parent.children.splice(parent.children.indexOf(this)+1,0,child);return child;}
 remove(){if(this.parentElement){const items=this.parentElement.children;items.splice(items.indexOf(this),1);this.parentElement=null;}}
 addEventListener(name,callback){(this.listeners[name]??=[]).push(callback);}
 fire(name,event={}){event.target??=this;event.prevented=false;event.stopped=false;event.preventDefault=()=>{event.prevented=true;};event.stopPropagation=()=>{event.stopped=true;};for(let n=this;n&&!event.stopped;n=n.parentElement){for(const callback of n.listeners[name]||[]){callback(event);}}return event;}
}
const body=new Element('body');const observers=[],tooltipCalls=[],instances=new WeakMap();
const document={nodeType:9,body,createElement:tag=>new Element(tag),getElementById:id=>body.querySelectorAll('[id]').find(n=>n.id===id)||null,querySelectorAll:selector=>body.querySelectorAll(selector)};
function Tooltip(target,options){tooltipCalls.push({target,options});}Tooltip.getInstance=node=>instances.get(node)||null;
const context={window:{},document,bootstrap:{Tooltip},MutationObserver:class {constructor(callback){this.callback=callback;observers.push(this);}observe(target,options){this.target=target;this.options=options;}}};
context.window.listeners={};
context.window.addEventListener=(name,callback)=>{(context.window.listeners[name]??=[]).push(callback);};
document.listeners={};
document.addEventListener=(name,callback)=>{(document.listeners[name]??=[]).push(callback);};

vm.createContext(context);
function load(){vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);}
function helpFor(control){return context.getItemHelpText(control);}
function mutation(record){observers[0].callback([record]);}
'''

HELP_FIXTURE = r'''
let now=0,serial=0;const timers=new Map(),shown=[],windowListeners={};
context.setTimeout=(fn,ms)=>{timers.set(++serial,{fn,at:now+ms});return serial;};
context.clearTimeout=id=>timers.delete(id);
function tick(ms){now+=ms;for(const [id,t] of [...timers]){if(t.at<=now){timers.delete(id);t.fn();}}}
context.window.addEventListener=(n,fn)=>{(windowListeners[n]??=[]).push(fn);};
function windowEvent(n){for(const fn of windowListeners[n]||[]){fn();}}
document.addEventListener=()=>{};
Element.prototype.getClientRects=function(){return this.isConnected&&!this.hidden&&this.style.display!=='none'&&!this.closest('.d-none')?[{}]:[];};
const popup=body.appendChild(new Element('div',{id:'popup'}));
Tooltip.getOrCreateInstance=(target,options)=>{
 assert.strictEqual(options.trigger,'manual');assert.strictEqual(options.html,false);
 const instance={show(){shown.push(target);target.setAttribute('aria-describedby','popup');},
 dispose(){instances.delete(target);target.removeAttribute('aria-describedby');}};
 instances.set(target,instance);return instance;
};

'''


def run_help_js(body):
    return subprocess.run(
        ['node', '-e', "eval(require('fs').readFileSync(0,'utf8'));",
         str(STATIC / 'js' / 'app-help.js')],
        input=DOM_FIXTURE + HELP_FIXTURE + body, capture_output=True, text=True,
        encoding='utf-8', timeout=15)


class InteractiveInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if (tag in {'input', 'select', 'textarea', 'button', 'summary'}
                or tag == 'a' and 'href' in attrs
                or tag in {'audio', 'video'} and 'controls' in attrs
                or attrs.get('role') == 'button' or 'onclick' in attrs):
            if attrs.get('type') == 'hidden':
                return
            if attrs.get('type') == 'file' and ('hidden' in attrs or 'display:none' in attrs.get('style', '').replace(' ', '')):
                return
            self.items.append({'tag': tag, 'attrs': attrs})


class UiHelpTests(unittest.TestCase):
    def run_js(self, body):
        result = run_help_js(body)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_all_static_interactive_items_have_explanations(self):
        inventory = InteractiveInventory()
        inventory.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        self.assertGreater(len(inventory.items), 400)
        self.run_js('load();const items=' + json.dumps(inventory.items) + r''';
const missing=items.filter(item=>!helpFor(new Element(item.tag,item.attrs)));
assert.deepStrictEqual(missing,[],'undocumented static items');
''')

    def test_cleanup_help_distinguishes_preview_accept_and_discard(self):
        self.run_js(r'''
load();
const gain=helpFor(new Element('input',{id:'clone-cleanup-gain'})).toLowerCase();
assert(gain.includes('0.25')&&gain.includes('4.0')&&gain.includes('full-length'));
assert(helpFor(new Element('input',{id:'clone-cleanup-remove-dc'})).includes('quality'));
for(const name of ['original','candidate']){
 const text=helpFor(new Element('input',{id:'clone-cleanup-listened-'+name}));
 assert(text.includes('listened')&&text.includes('not automated'));
}
assert(helpFor(new Element('button',{id:'clone-cleanup-prepare'})).includes('does not import'));
assert(helpFor(new Element('button',{id:'clone-cleanup-accept'})).includes('new immutable reference'));
assert(helpFor(new Element('button',{id:'clone-cleanup-reject'})).includes('accepted voices remain'));
''')

    def test_lookups_reject_prototype_names_and_unknown_field_saves(self):
        self.run_js(r'''
load();for(const id of ['constructor','__proto__','toString']){assert.strictEqual(helpFor(new Element('input',{id})), '');}
assert.strictEqual(helpFor(new Element('input',{onchange:'saveVoicesDebounced()'})), '');
assert(helpFor(new Element('textarea',{onchange:"updateChunk(2, 'text', this.value)"})).includes('Words spoken'));
assert(helpFor(new Element('input',{onchange:"dsbUpdateRow(2, 'seed', this.value)"})).includes('project seed'));
''')

    def test_generated_cards_recovery_and_programmatic_actions_have_specific_help(self):
        self.run_js(r'''
load();
for(const cls of ['lora-adapter-select','lora-character-style','voice-presentation','nick-alias','nick-canonical','script-batch-upload-check','review-batch-check','cast-bulk-check']){
 assert(helpFor(new Element('input',{class:cls})).length>25,cls);
}
assert(helpFor(new Element('input',{id:'script-batch-narrator-3'})).includes('narrator'));
assert(helpFor(new Element('input',{onchange:"updateChunk(2, 'pause_after', this.value)"})).includes('milliseconds'));
assert(helpFor(new Element('button',{'data-apply':'true'})).includes('does not rewrite'));
assert(helpFor(new Element('select',{'data-direction':'true'})).includes('canonical'));
assert(helpFor(new Element('button',{'data-bs-dismiss':'toast'})).includes('Dismiss'));
const action=body.appendChild(new Element('div',{class:'chunk-actions'}));assert(helpFor(action.appendChild(new Element('button'))).includes('this script line'));
const recovery=body.appendChild(new Element('div',{id:'script-recovery-panel'}));assert(helpFor(recovery.appendChild(new Element('textarea',{readonly:''}))).includes('failed chunk'));
const retained=body.appendChild(new Element('div',{id:'manual-llm-retained-replies'}));assert(helpFor(retained.appendChild(new Element('textarea',{readonly:''}))).includes('earlier model reply'));
assert(helpFor(new Element('button',{disabled:'','aria-label':'Candidate example is in use'})).includes('already assigned'));
assert.strictEqual(helpFor(new Element('input',{class:'unknown-future-field'})),'');
''')


    def test_summary_retains_authored_help_and_observes_details_open(self):
        self.run_js(r'''
const summary=body.appendChild(new Element('summary',{title:'Inspect immutable source provenance before accepting this import.'}));
load();
assert.strictEqual(summary.getAttribute('data-bs-title'),'Inspect immutable source provenance before accepting this import.');
context.renderHelpTips();
assert.strictEqual(helpFor(summary),'Inspect immutable source provenance before accepting this import.');
assert(observers[0].options.attributeFilter.includes('open'));
''')

    def test_disabled_label_lifecycle(self):
        self.run_js(r'''
load();const f=body.appendChild(new Element('fieldset',{disabled:''}));
const legend=f.appendChild(new Element('legend'));const exempt=legend.appendChild(new Element('button',{onclick:'loadReports()'}));
const label=f.appendChild(new Element('label',{for:'save-script-name',tabindex:'-1','aria-describedby':'label-note',title:'Name'}));label.textContent='Name';
const input=f.appendChild(new Element('input',{id:'save-script-name','aria-describedby':'author-note'}));input.labels=[label];label.control=input;
context.renderHelpTips();assert.strictEqual(input.parentElement,f);assert.strictEqual(input.previousElementSibling,label);assert.strictEqual(exempt.parentElement,legend);
assert.strictEqual(label.tabIndex,0);assert.strictEqual(label.getAttribute('role'),'group');assert.strictEqual(label.getAttribute('aria-describedby'),'label-note author-note');
label.fire('focusin');tick(500);assert.strictEqual(label.getAttribute('aria-describedby'),'label-note author-note popup');label.fire('keydown',{key:'Escape'});
input.className='d-none';mutation({type:'attributes',target:input});assert.strictEqual(label.tabIndex,-1);
input.className='';mutation({type:'attributes',target:input});assert.strictEqual(label.tabIndex,0);
f.removeAttribute('disabled');mutation({type:'attributes',target:f});assert.strictEqual(label.getAttribute('tabindex'),'-1');assert.strictEqual(label.getAttribute('role'),null);assert.strictEqual(label.getAttribute('aria-describedby'),'label-note');assert.strictEqual(label.getAttribute('title'),'Name');
f.setAttribute('disabled','');mutation({type:'attributes',target:f});input.remove();mutation({type:'childList',target:f,removedNodes:[input],addedNodes:[]});assert(!label.hasAttribute('data-help-tip'));assert.strictEqual(label.getAttribute('role'),null);
''')

    def test_dynamic_disabled_child_replacement(self):
        self.run_js(r'''
load();const c=body.appendChild(new Element('button',{onclick:'loadReports()',disabled:'','aria-describedby':'reason'}));c.textContent='Reports';
mutation({type:'childList',target:body,addedNodes:[c],removedNodes:[]});const w=c.parentElement;assert.strictEqual(w.getAttribute('role'),'group');assert.strictEqual(w.getAttribute('aria-describedby'),'reason');
c.textContent='New reports';mutation({type:'childList',target:c,addedNodes:[],removedNodes:[]});assert.strictEqual(w.getAttribute('aria-label'),'New reports');
c.removeAttribute('disabled');mutation({type:'attributes',target:c});assert.strictEqual(c.parentElement,body);assert(!w.isConnected);
''')

    def test_pending_cancellation_preserves_existing_description(self):
        self.run_js(r'''
load();
const control=body.appendChild(new Element('button',{onclick:'loadReports()','aria-describedby':'existing'}));
context.renderHelpTips();control.fire('focusin');tick(499);
control.fire('keydown',{key:'Escape'});tick(1);
assert.strictEqual(shown.length,0);
assert.strictEqual(control.getAttribute('aria-describedby'),'existing');
const disabled=body.appendChild(new Element('button',{onclick:'loadReports()',disabled:''}));
context.renderHelpTips();const wrapper=disabled.parentElement;wrapper.remove();
mutation({type:'childList',target:body,removedNodes:[wrapper],addedNodes:[]});
assert(!disabled.hasAttribute('data-help-covered'));
''')

    def test_pointer_exit_never_revives_old_focus(self):
        self.run_js(r'''
load();
const a=body.appendChild(new Element('button',{onclick:'loadReports()'}));
const b=body.appendChild(new Element('button',{onclick:'loadRunHistory()'}));
context.renderHelpTips();a.fire('focusin');a.fire('pointerover');a.fire('click');
a.fire('pointerout',{relatedTarget:body});tick(500);assert.strictEqual(shown.length,0);
b.fire('pointerover');tick(500);assert.strictEqual(shown.at(-1),b);
b.fire('pointerout',{relatedTarget:body});tick(500);assert.strictEqual(shown.length,1);
''')

    def test_title_naming_search_and_updates(self):
        self.run_js(r'''
load();
const a=body.appendChild(new Element('button',{title:'Remove'}));
const b=body.appendChild(new Element('button',{title:'Native','aria-label':'Authored'}));
context.renderHelpTips();assert.strictEqual(a.getAttribute('aria-label'),'Remove');
assert.strictEqual(b.getAttribute('aria-label'),'Authored');assert.strictEqual(helpFor(a),'Remove');
a.setAttribute('title','Delete');mutation({type:'attributes',target:a});
assert.strictEqual(helpFor(a),'Delete');assert.strictEqual(a.getAttribute('aria-label'),'Delete');
''')

    def test_scroll_and_live_description_tokens(self):
        self.run_js(r'''
load();const a=body.appendChild(new Element('button',{onclick:'loadReports()','aria-describedby':'original'}));
context.renderHelpTips();a.fire('focusin');tick(500);
a.setAttribute('aria-describedby','popup replacement');windowEvent('scroll');
assert(instances.get(a));a.fire('keydown',{key:'Escape'});
assert.strictEqual(a.getAttribute('aria-describedby'),'replacement');
''')

    def test_delayed_help_lifecycle(self):
        self.run_js(r'''
load();
const a=body.appendChild(new Element('button',{onclick:'loadReports()',title:'Native title'}));
const b=body.appendChild(new Element('button',{onclick:'loadRunHistory()'}));
context.renderHelpTips();
assert(!a.hasAttribute('title'));
assert.strictEqual(body.querySelectorAll('.help-tip,.help-nav-glyph').length,0);
a.fire('pointerover');tick(499);assert.strictEqual(shown.length,0);
a.fire('pointerout',{relatedTarget:body});tick(1);assert.strictEqual(shown.length,0);
a.fire('pointerover');tick(300);a.fire('pointerout',{relatedTarget:b});tick(499);
assert.strictEqual(shown.length,0);tick(1);assert.strictEqual(shown.at(-1),b);
assert.strictEqual(popup.style.pointerEvents,'none');
b.fire('click');assert.strictEqual(instances.get(b),undefined);
a.fire('focusin');tick(500);assert.strictEqual(shown.at(-1),a);
a.fire('keydown',{key:'Escape'});assert.strictEqual(instances.get(a),undefined);
a.fire('focusout',{relatedTarget:body});a.fire('focusin');a.remove();tick(500);
assert.strictEqual(instances.get(a),undefined);
body.appendChild(a);a.fire('focusin');windowEvent('hashchange');tick(500);
assert.strictEqual(instances.get(a),undefined);
a.fire('focusin');a.setAttribute('onclick','loadRunHistory()');
mutation({type:'attributes',target:a});tick(499);assert.strictEqual(instances.get(a),undefined);
tick(1);assert(instances.get(a));
assert(a.getAttribute('data-bs-title').includes('task runs'));
a.setAttribute('aria-describedby','existing');a.fire('focusin');tick(500);
assert.strictEqual(a.getAttribute('aria-describedby'),'existing popup');
a.fire('keydown',{key:'Escape'});assert.strictEqual(a.getAttribute('aria-describedby'),'existing');
const disabled=body.appendChild(new Element('button',{onclick:'loadReports()',disabled:''}));
context.renderHelpTips();const wrapper=disabled.parentElement;
assert.strictEqual(wrapper.tabIndex,0);assert(disabled.disabled);
wrapper.fire('focusin');tick(500);assert.strictEqual(shown.at(-1),wrapper);
''')


if __name__ == '__main__':
    unittest.main()
