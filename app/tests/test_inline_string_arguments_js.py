"""HTML-decode real inline handlers, then compile/invoke them as JavaScript."""
from html.parser import HTMLParser
import json
from pathlib import Path
import subprocess
import unittest

SOURCE=Path(__file__).resolve().parent.parent/'static/js/app-core.js'


class HandlerParser(HTMLParser):
    def __init__(self,markup):
        super().__init__(convert_charrefs=True);self.handlers=[];self.feed(markup)

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if 'onclick' in attrs:self.handlers.append(attrs['onclick'])


class InlineStringArgumentsJsTests(unittest.TestCase):
    def run_js(self,code,payload):
        script=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8'),payload=JSON.parse(process.argv[2]);
const context={currentBookFilename:'fixture',_voiceCardsBookToken:null,showConfirm:async()=>true,URLSearchParams};(vm.createContext(context),context.t=context.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english))),context.formatUiSafetyText=context.formatUiSafetyText||((key,english,values=Object())=>context.t(key,english,values)),context.tSafety=context.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',context.t(key,english,values)],['secondary',null]])),context);
const guard=source.indexOf('async function applyConfirmedVoiceRemoval(');vm.runInContext(source.slice(guard,source.indexOf('window.deleteVoiceCandidate =',guard)),context);
vm.runInContext(source.slice(source.indexOf('function escapeHtml('),source.indexOf('// Parse a numeric input')),context);
const a=source.indexOf('function getVoiceCandidateMarkup('),b=source.indexOf('function getLibraryVoiceReference(',a);vm.runInContext(source.slice(a,b),context);
const c=source.indexOf('function renderStyleTimeline('),d=source.indexOf('// Editor: from this line on',c);vm.runInContext(source.slice(c,d),context);
(async()=>{
'''+code+r'''
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        res=subprocess.run(['node','-e',script,str(SOURCE),json.dumps(payload)],capture_output=True,text=True,timeout=10)
        self.assertEqual(0,res.returncode,res.stderr);return json.loads(res.stdout)

    def test_character_names_and_candidate_ids_do_not_execute_after_html_decoding(self):
        names=["');globalThis.compromised=true;//", "O'Brien", 'double " quote', 'slash\\end', 'line\nbreak', '</script>&#39;é🙂', '\u2028\u2029']
        markup=self.run_js("console.log(JSON.stringify(payload.map(name=>({candidate:context.getVoiceCandidateMarkup([{candidate_id:name,favorite:false,rank:1}]),style:context.renderStyleTimeline(name,{style_timeline:[{from_index:7,character_style:'older'}]})}))));",names)
        decoded=[]
        for name,row in zip(names,markup):
            candidate=HandlerParser(row['candidate']).handlers;style=HandlerParser(row['style']).handlers
            self.assertEqual(3,len(candidate));self.assertEqual(1,len(style));decoded.append({'name':name,'handlers':candidate+style})
        result=self.run_js(r'''
const output=payload.map(row=>{
 const errors=[],calls=[],element={tag:'button'};globalThis.compromised=false;
 globalThis.favoriteVoiceCandidate=(...args)=>calls.push({type:'favorite',self:args[0]===element,args:args.slice(1)});
 globalThis.selectVoiceCandidate=(...args)=>calls.push({type:'select',self:args[0]===element,args:args.slice(1)});
 globalThis.deleteVoiceCandidate=(...args)=>calls.push({type:'delete',self:args[0]===element,args:args.slice(1)});
 globalThis.removeStylePoint=(...args)=>calls.push({type:'style',args});
 for(const handler of row.handlers){try{new Function(handler).call(element);}catch(error){errors.push(String(error));}}
 return {compromised:globalThis.compromised,calls,errors};
});console.log(JSON.stringify(output));
''',decoded)
        for name,row in zip(names,result):
            self.assertFalse(row['compromised'],'HTML entity decoding must not enable JavaScript injection')
            self.assertEqual([],row['errors'])
            self.assertEqual([{'type':'favorite','self':True,'args':[name,True]},{'type':'select','self':True,'args':[name]},{'type':'delete','self':True,'args':[name]},{'type':'style','args':[name,'7',{'tag':'button'},None,'7']}],row['calls'])

    def test_real_style_remove_handler_uses_exact_character_name_in_api_route(self):
        name="O'Brien \" & / café"
        markup=self.run_js("console.log(JSON.stringify(context.renderStyleTimeline(payload,{style_timeline:[{from_index:3,character_style:'older'}]})));",name)
        handler=HandlerParser(markup).handlers[0]
        output=self.run_js(r'''
const calls=[];let refreshed=0;context.API={del:async path=>calls.push(path)};context.loadVoices=async()=>{refreshed++;};context.showToast=()=>{throw Error('unexpected failure');};
const result=vm.runInContext('(function(){'+payload.handler+'})',context)();assert.strictEqual(result,false);await new Promise(resolve=>setImmediate(resolve));
const request=new URL(calls[0],'http://test');
assert.strictEqual(request.pathname,'/api/voices/'+encodeURIComponent(payload.name)+'/style_timeline/3');
assert.strictEqual(request.searchParams.get('legacy_only'),'true');
assert.strictEqual(refreshed,1);console.log(JSON.stringify(calls));
''',{'name':name,'handler':handler})
        self.assertEqual(1,len(output))

    def test_numeric_string_badge_click_deletes_decimal_line(self):
        points = [{'from_index': 8, 'character_style': 'eight'},
                  {'from_index': '010', 'character_style': 'ten'}]
        markup = self.run_js(
            "console.log(JSON.stringify(context.renderStyleTimeline('A',{style_timeline:payload})));",
            points)
        handlers = HandlerParser(markup).handlers
        self.assertEqual(2, len(handlers))
        output = self.run_js(r'''
const calls=[];context.API={del:async path=>calls.push(path)};context.loadVoices=async()=>{};context.showToast=()=>{};
vm.runInContext('(function(){'+payload.handler+'})',context)();
await new Promise(resolve=>setImmediate(resolve));console.log(JSON.stringify(calls));
''', {'handler': handlers[1]})
        self.assertEqual(['/api/voices/A/style_timeline/10?legacy_only=true'], output)

    def test_unrepresentable_style_indices_have_no_delete_handler_or_request(self):
        points = [{'from_index': value, 'character_style': f'style-{index}'}
                  for index, value in enumerate(('9007199254740993', '١٠', '9007199254740991', 8))]
        markup = self.run_js(
            "console.log(JSON.stringify(context.renderStyleTimeline('A',{style_timeline:payload})));",
            points)
        self.assertIn('style-0', markup)
        self.assertIn('style-1', markup)
        self.assertNotIn('NaN', markup)
        handlers = HandlerParser(markup).handlers
        self.assertEqual(2, len(handlers))
        output = self.run_js(r'''
const calls=[];context.API={del:async path=>calls.push(path)};context.loadVoices=async()=>{};
for (const index of ['9007199254740993','١٠',9007199254740992]) {
 await context.removeStylePoint('A',index);
}
for (const handler of payload) {
 vm.runInContext('(function(){'+handler+'})',context)();
 await new Promise(resolve=>setImmediate(resolve));
}
console.log(JSON.stringify(calls));
''', handlers)
        self.assertEqual(['/api/voices/A/style_timeline/9007199254740991?legacy_only=true',
                          '/api/voices/A/style_timeline/8?legacy_only=true'], output)

    def test_editor_style_dialog_submits_the_uid_and_scope_of_the_selected_row(self):
        result = self.run_js(r'''
const start=source.indexOf('async function voiceChangesHere(');
vm.runInContext(source.slice(start,source.indexOf('function collectVoiceConfig(', start)),context);
const row={dataset:{uid:'before',bookToken:'book-A'},querySelector:()=>({value:'A'})};
context.document={querySelector:()=>row};context.window={};context.loadVoices=async()=>{};
context.showToast=()=>{};context.showActionError=error=>{throw Error(error);};
const posts=[];context.API={post:async(url, body)=>posts.push({url,body})};
context.showPresetEditor=async()=>({name:'aged'});
await context.voiceChangesHere(3);
context.showPresetEditor=async()=>{row.dataset.uid='replacement';return {name:'aged'};};
await context.voiceChangesHere(3);
console.log(JSON.stringify(posts));
''', None)
        self.assertEqual([{'url': '/api/voices/A/style_timeline', 'body': {
            'from_index': 3, 'from_uid': 'before', 'expected_book_token': 'book-A',
            'character_style': 'aged'}}], result)
