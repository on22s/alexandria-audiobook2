from pathlib import Path
import subprocess
import unittest


class ClonePlaybackTests(unittest.TestCase):
    def test_native_play_rejection_and_throw_are_reported_without_unhandled_promises(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-scripts.js'
        script = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert'),source=fs.readFileSync(process.argv[1],'utf8');
const warnings=[],urls=[],unhandled=[];let mode='reject';const context={window:{},Date,
Audio:class {constructor(url){urls.push(url);}play(){if(mode==='throw'){throw Error('media refused');}return mode==='reject'?Promise.reject(Error('media unavailable')):Promise.resolve();}},
getLocalAudioUrl:(path)=>'/'+path,showToast:(...args)=>warnings.push(args)};
vm.createContext(context);const start=source.indexOf('window.playCloneVoice =');vm.runInContext(source.slice(start,source.indexOf('window.deleteCloneVoice =',start)),context);
const listener=error=>unhandled.push(error);process.on('unhandledRejection',listener);
const button=value=>({closest:()=>({querySelector:()=>({value})})});
(async()=>{try{for(const scenario of ['reject','throw','resolve']){mode=scenario;const count=warnings.length;await context.window.playCloneVoice(button('clone_voices/synthetic.wav'));await new Promise(resolve=>setImmediate(resolve));assert.equal(unhandled.length,0);assert.equal(warnings.length,count+(scenario==='resolve'?0:1));if(scenario!=='resolve'){assert.match(warnings.at(-1)[0],/Could not play/);assert.equal(warnings.at(-1)[1],'warning');}}
assert.equal(urls.length,3);await context.window.playCloneVoice(button(''));assert.equal(urls.length,3);
}finally{process.removeListener('unhandledRejection',listener);}})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', script, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
