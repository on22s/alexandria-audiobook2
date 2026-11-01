'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const directory = __dirname;
const implementation = fs.readFileSync(path.join(directory, 'interface.js'), 'utf8');
function createHarness(code) {
  const stops = code.slice(code.indexOf('  function stopRegion()'), code.indexOf('  function input('));
  const play = code.slice(code.indexOf('  async function playRegion()'), code.indexOf('  function render()'));
  assert(stops && play);
  const resumes = [], sources = [], buffers = [], copies = [], listeners = {};
  const signal = {channels:2,frames:64,sampleRate:8000,samples:[new Float32Array(64).fill(.25),new Float32Array(64).fill(-.5)]};
  const state = {source:null,playbackEpoch:0,current:{signal},context:{sampleRate:48000,destination:{},resume:()=>new Promise(resolve=>resumes.push(resolve)),createBuffer:(...args)=>{buffers.push(args);return {copyToChannel:(samples,channel)=>copies.push({samples,channel})};},createBufferSource:()=>{const source={started:0,stopped:0,disconnected:0,connect(){},start(...args){this.started++;this.startArguments=args;},stop(){this.stopped++;},disconnect(){this.disconnected++;}};sources.push(source);return source;}}};
  const sandbox = {state,element:id=>({pause(){},addEventListener:(event,callback)=>{listeners[id+':'+event]=callback;}}),region:()=>({startSeconds:.002,durationSeconds:.003,endSeconds:.005}),number:value=>value.toFixed(3),status(){},window:{},listening:{resetDraft(){}},refreshRegion(){}};
  vm.runInNewContext(stops + '\n' + play, sandbox);
  const regionListener=code.slice(code.indexOf("  for(const id of ['region-start','region-end'])"),code.indexOf("  element('region-play').addEventListener"));
  assert(regionListener,'Production region-input listener must be exercised');
  vm.runInNewContext(regionListener,sandbox);
  return {sandbox,state,resumes,sources,buffers,copies,listeners};
}
async function exercise(code) {
  const outcomes = [];
  for (const order of [[0,1],[1,0]]) {
    const harness = createHarness(code);
    const pending = [harness.sandbox.playRegion(),harness.sandbox.playRegion()];
    for(const index of order){harness.resumes[index]();await pending[index];}
    assert.equal(harness.sources.length,1,'Only the newest pending playback may start');
    assert.equal(harness.sources[0].started,1);
    assert.deepEqual(harness.buffers,[[2,64,8000]],'Playback buffer uses both source channels and the source clock');
    assert.deepEqual(harness.sources[0].startArguments,[0,.002,.003],'Playback uses the selected offset and duration');
    assert.equal(harness.copies.length,2);
    harness.copies.forEach((copy,index)=>{assert.equal(copy.channel,index);assert.equal(copy.samples,harness.state.current.signal.samples[index]);});
    harness.sandbox.stop();
    assert.equal(harness.sources[0].stopped,1);
    assert.equal(harness.sources[0].disconnected,1);
    assert.equal(harness.state.source,null);
    outcomes.push({scenario:'double play; resume order '+order.join(','),sourcesStarted:1,allSourcesStopped:true});
  }
  for(const method of ['stop','stopRegion','region-start','region-end']){
    const harness=createHarness(code),pending=harness.sandbox.playRegion();
    if(method.startsWith('region-')){harness.listeners[method+':input']();}else{harness.sandbox[method]();}harness.resumes[0]();await pending;
    assert.equal(harness.sources.length,0,'Stop must invalidate pending playback');
    outcomes.push({scenario:method+' before resume resolves',sourcesStarted:0});
  }
  return outcomes;
}
(async()=>{
  const scenarios=await exercise(implementation);
  let mutationRejected=false;
  try{await exercise(implementation.replace('if(epoch!==state.playbackEpoch) { return; }',''));}
  catch(error){assert.match(error.message,/newest pending playback/);mutationRejected=true;}
  assert(mutationRejected,'Removing the stale-request guard must fail');
  const html=fs.readFileSync(path.join(directory,'listening-lab.html'),'utf8');
  assert.equal(html.match(/<script id="interface">([\s\S]*?)<\/script>/)[1],implementation);
  console.log(JSON.stringify({schema:'listening-lab-playback-race-probe/v1',interfaceSha256:crypto.createHash('sha256').update(implementation).digest('hex'),scenarios,mutationRejected,scope:'Extracted production playback functions with a deterministic AudioContext stub; not browser/hardware playback verification'},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
