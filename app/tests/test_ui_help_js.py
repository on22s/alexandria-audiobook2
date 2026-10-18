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
 querySelectorAll(selector){return this.children.flatMap(c=>[...(c.matches(selector)?[c]:[]),...c.querySelectorAll(selector)]);}
 appendChild(child){child.remove();child.parentElement=this;this.children.push(child);return child;}
 insertAdjacentElement(position,child){assert.strictEqual(position,'afterend');assert(this.parentElement);const parent=this.parentElement;child.remove();child.parentElement=parent;parent.children.splice(parent.children.indexOf(this)+1,0,child);return child;}
 remove(){if(this.parentElement){const items=this.parentElement.children;items.splice(items.indexOf(this),1);this.parentElement=null;}}
 addEventListener(name,callback){(this.listeners[name]??=[]).push(callback);}
 fire(name,event={}){event.prevented=false;event.stopped=false;event.preventDefault=()=>{event.prevented=true;};event.stopPropagation=()=>{event.stopped=true;};for(let n=this;n&&!event.stopped;n=n.parentElement){for(const callback of n.listeners[name]||[]){callback(event);}}return event;}
}
const body=new Element('body');const observers=[],tooltipCalls=[],instances=new WeakMap();
const document={nodeType:9,body,createElement:tag=>new Element(tag),getElementById:id=>body.querySelectorAll('[id]').find(n=>n.id===id)||null,querySelectorAll:selector=>body.querySelectorAll(selector)};
function Tooltip(target,options){tooltipCalls.push({target,options});}Tooltip.getInstance=node=>instances.get(node)||null;
const context={window:{},document,bootstrap:{Tooltip},MutationObserver:class {constructor(callback){this.callback=callback;observers.push(this);}observe(target,options){this.target=target;this.options=options;}}};
vm.createContext(context);
function load(){vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);}
function helpFor(control){return context.getItemHelpText(control);}
function tipFor(control){return vm.runInContext('helpTipNodes',context).get(control);}
function mutation(record){observers[0].callback([record]);}
'''


def run_help_js(body):
    return subprocess.run(['node', '-e', DOM_FIXTURE + body,
                           str(STATIC / 'js' / 'app-help.js')],
                          capture_output=True, text=True, timeout=15)


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

    def test_lookups_reject_prototype_names_and_unknown_field_saves(self):
        self.run_js(r'''
load();for(const id of ['constructor','__proto__','toString']){assert.strictEqual(helpFor(new Element('input',{id})), '');}
assert.strictEqual(helpFor(new Element('input',{onchange:'saveVoicesDebounced()'})), '');
assert(helpFor(new Element('textarea',{onchange:"updateChunk(2, 'text', this.value)"})).includes('Words spoken'));
assert(helpFor(new Element('input',{onchange:"dsbUpdateRow(2, 'seed', this.value)"})).includes('project seed'));
''')

    def test_disabled_help_stays_reachable_and_does_not_activate_ancestors(self):
        self.run_js(r'''
const group=body.appendChild(new Element('div'));let actions=0;group.addEventListener('click',()=>actions++);
const button=group.appendChild(new Element('button',{id:'btn-gen-script',disabled:''}));
load();const tip=tipFor(button);assert(tip);assert.strictEqual(tip.tagName,'SPAN');assert.strictEqual(tip.tabIndex,0);assert.strictEqual(tip.getAttribute('role'),'img');assert.strictEqual(tip.parentElement,button.parentElement);assert(!tip.hidden);
const click=tip.fire('click');assert(click.prevented&&click.stopped);assert.strictEqual(actions,0);
for(const key of ['Enter',' ']){const event=tip.fire('keydown',{key});assert(event.prevented&&event.stopped);}
assert(!tip.fire('keydown',{key:'Tab'}).prevented,'keyboard focus must remain escapable');
assert.strictEqual(tooltipCalls.length,1);assert.strictEqual(tooltipCalls[0].options.trigger,'hover focus');assert.strictEqual(tooltipCalls[0].options.html,false);
assert.strictEqual(tooltipCalls[0].options.customClass,'help-tooltip');
''')

    def test_dynamic_insert_rerender_removal_and_tooltip_cleanup(self):
        self.run_js(r'''
load();const row=new Element('div');const field=row.appendChild(new Element('input',{class:'voice-seed'}));body.appendChild(row);
mutation({type:'childList',addedNodes:[row],removedNodes:[]});const tip=tipFor(field);assert(tip&&tip.isConnected);assert(tip.getAttribute('data-bs-title').includes('Seed'));
context.renderHelpTips();context.renderHelpTips();assert.strictEqual(body.querySelectorAll('.help-tip').length,1);
let disposed=0;instances.set(tip,{dispose(){disposed++;instances.delete(tip);}});
row.remove();mutation({type:'childList',addedNodes:[],removedNodes:[row]});assert(!tip.isConnected);assert.strictEqual(disposed,1);assert.strictEqual(tipFor(field),undefined);
''')

    def test_individual_hidden_action_and_removed_glyph_recover(self):
        self.run_js(r'''
const button=body.appendChild(new Element('button',{id:'btn-gen-script',style:'display:none'}));load();const tip=tipFor(button);assert(tip.hidden);
button.style.display='';mutation({type:'attributes',target:button});assert(!tip.hidden);
button.hidden=true;mutation({type:'attributes',target:button});assert(tip.hidden);
button.hidden=false;mutation({type:'attributes',target:button});assert(!tip.hidden);
tip.remove();mutation({type:'childList',addedNodes:[],removedNodes:[tip]});assert(tip.isConnected);assert.strictEqual(body.querySelectorAll('.help-tip').length,1);
''')

    def test_reused_control_refreshes_hint_and_disposes_stale_tooltip(self):
        self.run_js(r'''
const button=body.appendChild(new Element('button',{onclick:'loadReports()'}));load();const tip=tipFor(button);let disposed=0;instances.set(tip,{dispose(){disposed++;instances.delete(tip);}});
button.setAttribute('onclick','loadRunHistory()');mutation({type:'attributes',target:button});assert(tip.getAttribute('data-bs-title').includes('task runs'));assert.strictEqual(disposed,1);
button.setAttribute('onclick','unknownAction()');mutation({type:'attributes',target:button});assert(!tip.isConnected);assert(!button.hasAttribute('data-help-covered'));
''')

    def test_spaced_flex_actions_keep_their_own_help_and_move_safely(self):
        self.run_js(r'''
const header=body.appendChild(new Element('div',{class:'justify-content-between'}));
const button=header.appendChild(new Element('button',{onclick:'loadReports()',disabled:''}));load();
const group=button.parentElement,tip=tipFor(button);assert.strictEqual(group.className,'help-action-group');assert.strictEqual(tip.parentElement,group);assert(button.hasAttribute('disabled'));assert.strictEqual(tip.tabIndex,0);
context.renderHelpTips();assert.strictEqual(header.querySelectorAll('.help-action-group').length,1);
body.appendChild(button);context.renderHelpTips();assert(!group.isConnected);assert.strictEqual(tip.parentElement,body);
button.setAttribute('onclick','unknownAction()');context.renderHelpTips();assert.strictEqual(button.parentElement,body);assert(!tip.isConnected);
const second=header.appendChild(new Element('button',{onclick:'loadReports()'}));context.renderHelpTips();second.setAttribute('onclick','unknownAction()');context.renderHelpTips();assert.strictEqual(second.parentElement,header);assert.strictEqual(header.querySelectorAll('.help-action-group').length,0);
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

    def test_navigation_keeps_original_layout_and_actions_with_direct_help(self):
        self.run_js(r'''
const nav=body.appendChild(new Element('nav',{class:'navbar'}));
const link=nav.appendChild(new Element('a',{class:'nav-link',href:'#designer','data-tab':'designer'}));let navigations=0;link.addEventListener('click',()=>navigations++);
load();assert.strictEqual(nav.children.length,1);assert.strictEqual(link.parentElement,nav);assert.strictEqual(tipFor(link),undefined);assert(link.hasAttribute('data-help-tip'));assert(link.getAttribute('data-bs-title').includes('reusable voice'));assert.strictEqual(link.getAttribute('aria-label'),null);
assert(!link.fire('click').prevented);assert.strictEqual(navigations,1);
context.renderHelpTips();assert.strictEqual(nav.children.length,1);
body.appendChild(link);context.renderHelpTips();assert(!link.hasAttribute('data-help-tip'));assert(tipFor(link));assert.strictEqual(link.parentElement,body);
nav.appendChild(link);context.renderHelpTips();assert.strictEqual(nav.children.length,1);assert.strictEqual(tipFor(link),undefined);assert(link.hasAttribute('data-help-tip'));
link.remove();mutation({type:'childList',removedNodes:[link],addedNodes:[]});assert(!link.hasAttribute('data-help-tip'));
''')


if __name__ == '__main__':
    unittest.main()
