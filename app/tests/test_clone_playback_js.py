"""Voice card reference playback: errors reported, and Play/Stop like candidates (#1933).

What these verify, running the real playCloneVoice from app-scripts.js and the
real shared Voices-tab player from app-core.js:
- a refused or throwing play is reported as a warning, never an unhandled
  rejection, and a card without a reference starts nothing;
- pressing the reference button while it plays stops it instead of starting a
  second, overlapping recording (the reported bug), and the button shows Stop
  while playing and Play again after Stop or the end of the audio;
- a reference preview and a saved-candidate preview never play at once.
"""
from pathlib import Path
import subprocess
import unittest

APP = Path(__file__).resolve().parent.parent
SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const scripts=fs.readFileSync(process.argv[1],'utf8'),core=fs.readFileSync(process.argv[2],'utf8');
const warnings=[],played=[],unhandled=[];let mode='resolve';
class Audio{constructor(url){this.url=url;this.paused=true;this.listeners={};played.push(this);}
 addEventListener(name,fn){this.listeners[name]=fn;}
 play(){if(mode==='throw'){throw Error('media refused');}this.paused=false;return mode==='reject'?Promise.reject(Error('media unavailable')):Promise.resolve();}
 pause(){this.paused=true;}}
const context={window:{},Date,Audio,getLocalAudioUrl:(path)=>'/'+path,showToast:(...args)=>warnings.push(args)};
vm.createContext(context);context.t=(key,english,values={})=>Object.keys(values).reduce((text,name)=>text.split('{'+name+'}').join(String(values[name])),String(english));
const slice=(source,from,to)=>{const start=source.indexOf(from);assert(start>=0,from);return source.slice(start,source.indexOf(to,start));};
vm.runInContext(slice(core,'window.setVoicePreviewButton =','function setCandidatePlayButton('),context);
vm.runInContext(slice(core,'window.toggleVoicePreviewPlayback =','window.toggleCandidatePlayback ='),context);
vm.runInContext(slice(scripts,'function setClonePlayButton(','window.deleteCloneVoice ='),context);
const button=(value,label='Hero')=>{const classes=new Set(['btn-outline-secondary']);return {value,innerHTML:'<i class="fas fa-play"></i>',attrs:{},dataset:{label},
 setAttribute(k,v){this.attrs[k]=v;},classList:{toggle:(c,on)=>{if(on){classes.add(c);}else{classes.delete(c);}},has:c=>classes.has(c)},
 closest(){return {querySelector:()=>({value})};}};};
const listener=error=>unhandled.push(error);process.on('unhandledRejection',listener);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
let finished=false;process.on('beforeExit',()=>assert(finished,'scenario did not finish'));
'''


class ClonePlaybackTests(unittest.TestCase):
    def run_js(self, body):
        script = SETUP + '(async()=>{try{' + body + '\nfinished=true;}finally{process.removeListener(\'unhandledRejection\',listener);}})().catch(error=>{console.error(error);process.exitCode=1;});'
        result = subprocess.run(['node', '-e', script, str(APP / 'static/js/app-scripts.js'),
                                 str(APP / 'static/js/app-core.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_play_failures_are_reported_and_an_empty_reference_starts_nothing(self):
        self.run_js(r'''
for(const scenario of ['reject','throw']){mode=scenario;const b=button('clone_voices/synthetic.wav');const count=warnings.length;
 await context.window.playCloneVoice(b);await tick();
 assert.equal(unhandled.length,0);assert.equal(warnings.length,count+1);assert.match(warnings.at(-1)[0],/Could not play/);assert.equal(warnings.at(-1)[1],'warning');
 assert(b.innerHTML.includes('fa-play'),'a failed play leaves the button on Play');assert.equal(context.window._voicePreviewPlayback,null);}
mode='resolve';const count=played.length;await context.window.playCloneVoice(button(''));assert.equal(played.length,count);
''')

    def test_pressing_play_again_stops_instead_of_overlapping(self):
        self.run_js(r'''
const b=button('clone_voices/hero.wav');
await context.window.playCloneVoice(b);
assert.equal(played.length,1);assert.equal(played[0].url,'/clone_voices/hero.wav');assert(!played[0].paused);
assert(b.innerHTML.includes('fa-stop'));assert.equal(b.attrs['aria-label'],'Stop reference audio for Hero');assert(b.classList.has('btn-secondary'));
await context.window.playCloneVoice(b);
assert.equal(played.length,1,'a second press must not start another recording');assert(played[0].paused,'it stops the playing one');
assert(b.innerHTML.includes('fa-play'));assert.equal(b.attrs['aria-label'],'Play reference audio for Hero');assert(b.classList.has('btn-outline-secondary'));
await context.window.playCloneVoice(b);assert.equal(played.length,2);played[1].listeners.ended();
assert(b.innerHTML.includes('fa-play'),'the end of the audio resets the button');assert.equal(context.window._voicePreviewPlayback,null);
''')

    def test_reference_and_candidate_previews_never_overlap(self):
        self.run_js(r'''
const reference=button('clone_voices/hero.wav');const candidate=button('');
const drawn=[];const renderCandidate=(btn,playing)=>drawn.push(playing);
await context.window.playCloneVoice(reference);
await context.window.toggleVoicePreviewPlayback(candidate,()=>'/designed_voices/a.wav',renderCandidate,'failed');
assert(played[0].paused,'starting a candidate stops the reference');assert(reference.innerHTML.includes('fa-play'));
assert(!played[1].paused);assert.deepEqual(drawn,[true]);
await context.window.playCloneVoice(reference);
assert(played[1].paused,'starting the reference stops the candidate');assert.deepEqual(drawn,[true,false]);assert(!played[2].paused);
''')


if __name__ == '__main__':
    unittest.main()
