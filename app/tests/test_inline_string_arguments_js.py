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
const context={};vm.createContext(context);
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
            self.assertEqual([{'type':'favorite','self':True,'args':[name,True]},{'type':'select','self':True,'args':[name]},{'type':'delete','self':True,'args':[name]},{'type':'style','args':[name,7]}],row['calls'])

    def test_real_style_remove_handler_uses_exact_character_name_in_api_route(self):
        name="O'Brien \" & / café"
        markup=self.run_js("console.log(JSON.stringify(context.renderStyleTimeline(payload,{style_timeline:[{from_index:3,character_style:'older'}]})));",name)
        handler=HandlerParser(markup).handlers[0]
        output=self.run_js(r'''
const calls=[];let refreshed=0;context.API={del:async path=>calls.push(path)};context.loadVoices=async()=>{refreshed++;};context.showToast=()=>{throw Error('unexpected failure');};
const result=vm.runInContext('(function(){'+payload.handler+'})',context)();assert.strictEqual(result,false);await new Promise(resolve=>setImmediate(resolve));
assert.strictEqual(calls[0],'/api/voices/'+encodeURIComponent(payload.name)+'/style_timeline/3');assert.strictEqual(refreshed,1);console.log(JSON.stringify(calls));
''',{'name':name,'handler':handler})
        self.assertEqual(1,len(output))
