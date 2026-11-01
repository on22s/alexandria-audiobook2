'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const directory = __dirname;
const implementation = fs.readFileSync(path.join(directory, 'interface.js'), 'utf8');
const instrument = fs.readFileSync(path.join(directory, 'instrument.js'), 'utf8');
const listeningTasks = fs.readFileSync(path.join(directory, 'listening-tasks.js'), 'utf8');
const html = fs.readFileSync(path.join(directory, 'listening-lab.html'), 'utf8');
const fixtures = JSON.parse(html.match(/<script id="fixture-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
for (const [id, source] of [['instrument', instrument], ['interface', implementation], ['listening-tasks', listeningTasks]]) {
  const match = html.match(new RegExp('<script id="' + id + '">([\\s\\S]*?)<\\/script>'));
  assert(match, 'Missing inline module ' + id);
  assert.equal(match[1], source, 'Inline module ' + id + ' must be byte-identical to ' + id + '.js');
  assert(!/\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\s*\(/.test(source), id + ' must not use network APIs');
}
const nodes = new Map(), anchors = [], copies = [], results = [];
let failDownload = false;
class Element {
  get value() { return this._value || ''; }
  set value(value) { this._value = String(value); }
  constructor(tag = 'div') { this.tag = tag; this.value = ''; this.textContent = ''; this.disabled = false; this.children = []; this.listeners = {}; this.width = 960; this.height = 160; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  addEventListener(event, listener) { this.listeners[event] = listener; }
  querySelectorAll() { return this.children.filter(child => child.tag === 'button'); }
  getContext() { return {fillRect(){},beginPath(){},moveTo(){},lineTo(){},stroke(){}}; }
  pause() {}
  focus() {}
  select() {}
  click() { if (this.tag === 'a') { assert(this.parent, 'Download anchor must be attached'); anchors.push({attached:true,filename:this.download}); if (failDownload) { throw Error('blocked download request'); } } }
  remove() { if (this.parent) { this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; } }
}
const body = new Element('body');
const get = id => { if (!nodes.has(id)) { nodes.set(id, new Element()); } return nodes.get(id); };
get('fixture-data').textContent = JSON.stringify(fixtures);
get('threshold').value = '-50'; get('minimum').value = '.5';
get('ear-comparison').value = 'comparison-1'; get('ear-rendition').value = 'reference';
get('ear-region-role').value = 'event'; get('ear-origin').value = 'unknown';
get('ear-heard').checked = false;
const sandbox = {atob,ArrayBuffer,Uint8Array,Float32Array,Float64Array,DataView,Blob,console,
  document:{getElementById:get,createElement:tag=>new Element(tag),body,querySelectorAll:()=>[...nodes.values()],execCommand:()=>true},
  window:{addEventListener(){}},navigator:{userAgent:'PRIVATE-DEVICE-DETAIL',clipboard:{writeText:async text=>copies.push(text)}},
  crypto:crypto.webcrypto,URL:{createObjectURL:()=> 'blob:test',revokeObjectURL(){}},setTimeout:callback=>callback(),requestAnimationFrame:callback=>callback()};
vm.runInNewContext(instrument, sandbox);
vm.runInNewContext(listeningTasks, sandbox);
vm.runInNewContext(implementation, sandbox);
function check(name, observed, expected) { assert.deepEqual(observed, expected, name); results.push({name,observed,expected,passed:true}); }
function source(index, name) { const bytes = Buffer.from(fixtures[index].base64.join(''), 'base64'); return {name,size:bytes.length,arrayBuffer:async()=>bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.length)}; }
async function load(files) { const target = {files,value:'selected'}; await get('files').listeners.change({target}); assert.equal(target.value, ''); }
(async () => {
  check('No-clip exports disabled', [get('copy').disabled,get('download').disabled], [true,true]);
  check('No-clip section playback disabled', get('region-play').disabled, true);
  await get('region-play').listeners.click();
  check('Programmatic no-clip playback gives useful instruction', get('status').textContent, 'Playback failed: Load a clip before selecting a section.');
  check('No-clip message is actionable', get('results').value, 'Load a clip before copying or downloading measurements.');
  await get('copy').listeners.click();
  check('No-clip copy does not dispatch', copies.length, 0);
  await load([source(0,'PRIVATE-CLIENT-A.wav'),{name:'bad.wav',size:3,arrayBuffer:async()=>new ArrayBuffer(3)},source(1,'PRIVATE-CLIENT-B.wav')]);
  let report = JSON.parse(get('results').value);
  check('Batch continues after refusal', report.telemetry.length, 2);
  check('Batch reports loaded and refused counts', /^2 loaded, 0 identical sources reused, 1 refused\./.test(get('status').textContent), true);
  check('Browser identity omitted', report.environment.userAgent, null);
  check('Local names and labels sanitized', report.telemetry.every(item=>item.sourceName===null && item.label===item.id), true);
  get('notes').value = 'Audible background step'; get('notes').listeners.input();
  await load([source(1,'RENAMED-PRIVATE-COPY.wav')]);
  report = JSON.parse(get('results').value);
  check('Duplicate does not create a second observation', [report.telemetry.length,report.humanObservations.length], [2,2]);
  check('Duplicate reports reuse', /^0 loaded, 1 identical sources reused, 0 refused\./.test(get('status').textContent), true);
  check('Duplicate preserves notes', report.humanObservations[1].notes, 'Audible background step');
  check('Quick feedback starts unanswered', report.humanObservations[1].answers, {});
  get('pause-right').checked = true; get('pause-right').listeners.change();
  get('background-drops').checked = true; get('background-drops').listeners.change();
  report = JSON.parse(get('results').value);
  check('Pause and background answers are independent', report.humanObservations[1].answers, {pauseLength:'About right',backgroundSound:'It suddenly became much quieter or disappeared'});
  get('clips').listeners.change({target:{value:report.telemetry[0].id}});
  check('Other clips do not inherit feedback selections', [get('pause-right').checked,get('background-drops').checked], [false,false]);
  get('clips').listeners.change({target:{value:report.telemetry[1].id}});
  check('Returning to a clip restores both answers', [get('pause-right').checked,get('background-drops').checked], [true,true]);
  get('feedback-clear').listeners.click();
  check('Clear answers preserves written notes', [JSON.parse(get('results').value).humanObservations[1].answers,get('notes').value], [{},'Audible background step']);
  get('pause-right').checked = true; get('pause-right').listeners.change();
  get('background-drops').checked = true; get('background-drops').listeners.change();
  get('ear-finding').value = 'Background drops abruptly';
  get('ear-save').listeners.click();
  check('Listening is not inferred from loading a clip', JSON.parse(get('results').value).listeningTasks.measurements.length, 0);
  get('ear-heard').checked = true;
  get('ear-detail').value = 'The background disappears during the pause.';
  get('ear-save').listeners.click();
  report = JSON.parse(get('results').value);
  check('Saved listening note retains exact measured interval', report.listeningTasks.measurements[0].region, report.selectedRegion.region || Object.fromEntries(Object.entries(report.selectedRegion).filter(([key]) => key !== 'clipId')));
  check('Human finding stays separate from measured levels', report.listeningTasks.humanObservations[0].finding, 'Background drops abruptly');
  const savedRegion = JSON.stringify(report.listeningTasks.measurements[0].region);
  get('region-start').value = '1'; get('region-start').listeners.input();
  check('Changing current selection preserves saved measurements', JSON.stringify(JSON.parse(get('results').value).listeningTasks.measurements[0].region), savedRegion);
  await get('copy').listeners.click();
  const copied = copies[copies.length-1];
  check('Copied results include both listening answers', JSON.parse(copied).humanObservations[1].answers, {pauseLength:'About right',backgroundSound:'It suddenly became much quieter or disappeared'});
  check('Copied payload excludes local names and device detail', /PRIVATE-CLIENT|RENAMED-PRIVATE|PRIVATE-DEVICE/.test(copied), false);
  check('Copy confirmation discloses notes', get('status').textContent.includes('your notes are included'), true);
  check('Counter scope explicitly excludes integer-PCM clipping diagnosis', JSON.parse(copied).definitions.overFullScale.includes('Integer PCM cannot encode such values'), true);
  get('download').listeners.click();
  check('Download anchor attached before dispatch', anchors.length, 1);
  check('Download does not claim confirmed save', get('status').textContent.includes('not confirmed saved'), true);
  check('Download anchor removed', body.children.length, 0);
  failDownload = true; get('download').listeners.click();
  check('Download failure names copy fallback', get('status').textContent.includes('Copy the results instead'), true);
  check('Failed download anchor removed', body.children.length, 0);
  const savedCount = JSON.parse(get('results').value).listeningTasks.measurements.length;
  function draft() { get('ear-heard').checked = true; get('ear-finding').value = 'Background drops abruptly'; get('ear-detail').value = 'Previous selection only'; }
  function checkDraftReset(name) { check(name, [get('ear-heard').checked,get('ear-finding').value,get('ear-detail').value], [false,'','']); }
  draft();
  get('clips').listeners.change({target:{value:report.telemetry[0].id}});
  checkDraftReset('Clip switch clears listening confirmation and unsaved details');
  get('ear-finding').value = 'Background drops abruptly'; get('ear-save').listeners.click();
  check('New clip cannot reuse old listening confirmation', JSON.parse(get('results').value).listeningTasks.measurements.length, savedCount);
  draft(); get('ear-task').value = 'reply-delivery'; get('ear-task').listeners.change();
  checkDraftReset('Task switch clears listening confirmation and unsaved details');
  get('ear-task').value = 'pause-background'; get('ear-task').listeners.change();
  draft(); get('region-start').value = '.5'; get('region-start').listeners.input();
  checkDraftReset('Region start change clears listening confirmation and unsaved details');
  draft(); get('region-end').value = '4'; get('region-end').listeners.input();
  checkDraftReset('Region end change clears listening confirmation and unsaved details');
  get('clips').listeners.change({target:{value:report.telemetry[1].id}});
  get('region-start').value = '0'; get('region-end').value = '5'; get('region-start').listeners.input();
  draft();
  const quietButton = get('quiet-rows').children[0].children[3].children[0];
  quietButton.listeners.click();
  checkDraftReset('Quiet-row selection clears listening confirmation and unsaved details');
  check('Quiet-row selection changes the measured interval', [get('region-start').value,get('region-end').value], ['10','12.5']);
  get('ear-finding').value = 'Background drops abruptly'; get('ear-save').listeners.click();
  check('Quiet-row selection cannot reuse old listening confirmation', JSON.parse(get('results').value).listeningTasks.measurements.length, savedCount);
  check('Selection changes preserve saved findings', JSON.parse(get('results').value).listeningTasks.measurements.length, savedCount);
  for (const [setting,invalid] of [['threshold','1'],['minimum','0']]) {
    const previous = JSON.parse(get('results').value), selected = get('clips').value, options = get('clips').children.length;
    const valid = get(setting).value; get(setting).value = invalid;
    await load([source(2,'new-rejected-source.wav')]);
    check('Invalid '+setting+' refuses load explicitly', /^0 loaded, 0 identical sources reused, 1 refused\./.test(get('status').textContent), true);
    check('Invalid '+setting+' preserves clip selection and options', [get('clips').value,get('clips').children.length], [selected,options]);
    get(setting).value = valid; get(setting).listeners.change();
    const after = JSON.parse(get('results').value);
    check('Invalid '+setting+' retains no refused clip', after.telemetry.map(item=>item.id), previous.telemetry.map(item=>item.id));
    check('Invalid '+setting+' preserves selected measurement', after.selectedRegion.clipId, previous.selectedRegion.clipId);
  }
  for (const [setting,invalid] of [['threshold','1'],['minimum','0']]) {
    const previous = JSON.parse(get('results').value), selected = get('clips').value;
    const title = get('clip-title').textContent, player = get('player').src;
    const valid = get(setting).value; get(setting).value = invalid;
    const other = previous.telemetry.find(item=>item.id!==selected).id;
    get('clips').value = other;
    get('clips').listeners.change({target:get('clips')});
    check('Invalid '+setting+' refuses switching without mixed clip state', [get('clips').value,get('clip-title').textContent,get('player').src,get('results').value===JSON.stringify(previous,null,2)], [selected,title,player,true]);
    check('Invalid '+setting+' disables export after refused switch', [get('copy').disabled,get('download').disabled], [true,true]);
    check('Invalid '+setting+' reports switch refusal', get('status').className, 'notice error');
    get(setting).value = valid; get(setting).listeners.change();
    get('clips').value = other; get('clips').listeners.change({target:get('clips')});
    check('Corrected '+setting+' permits consistent clip switch', [get('clips').value,JSON.parse(get('results').value).selectedRegion.clipId], [other,other]);
  }
  get('notes').value = 'x'.repeat(4000); get('notes').listeners.input();
  check('Quick note accepts the 4000-character boundary', JSON.parse(get('results').value).humanObservations.find(item=>item.clipId===get('clips').value).notes.length, 4000);
  get('notes').value += 'x'; get('notes').listeners.input();
  check('Over-length quick note blocks exports', [get('copy').disabled,get('download').disabled], [true,true]);
  check('Note error is labelled as an export problem', get('results').value.startsWith('Cannot prepare export: '), true);
  check('Over-length note reports the limit instead of truncating', get('results').value.includes('4000 characters'), true);
  get('threshold').listeners.change();
  check('Other report refresh cannot bypass note limit', get('copy').disabled, true);
  get('notes').value = 'Corrected note'; get('notes').listeners.input();
  check('Corrected note restores export', get('copy').disabled, false);
  await get('sample-buttons').children[0].listeners.click();
  check('Sample reuse preserves the duplicate identity message', get('status').textContent.includes('Identical source bytes already loaded; reusing'), true);
  get('ear-task').value = 'pause-background'; get('ear-task').listeners.change();
  get('ear-comparison').value = 'transition-controls';
  function saveRegion(role,start,end) {
    get('region-start').value=start; get('region-end').value=end;
    get('ear-region-role').value=role; get('ear-heard').checked=true;
    get('ear-finding').value='Unsure'; get('ear-save').listeners.click();
    return JSON.parse(get('results').value).listeningTasks;
  }
  function diagnostic(tasks) { return tasks.levelTransitionDiagnostics.find(item=>item.comparisonLabel==='transition-controls'); }
  let transitionTasks=saveRegion('before',0,1);
  check('Incomplete transition exports an explicit diagnostic', diagnostic(transitionTasks).status, 'unavailable');
  check('Incomplete transition names missing roles', diagnostic(transitionTasks).roleCounts, {before:1,gap:0,after:0});
  saveRegion('gap',1,2); transitionTasks=saveRegion('after',2,3);
  check('Complete adjacent regions produce one transition', transitionTasks.levelTransitions.filter(item=>diagnostic(transitionTasks).measurementIds.includes(item.measurementIds[0])).length, 1);
  check('Complete transition is marked measured', diagnostic(transitionTasks).status, 'measured');
  transitionTasks=saveRegion('before',0,.5);
  check('Duplicate role is unavailable, not silently skipped', diagnostic(transitionTasks).roleCounts.before, 2);
  get('ear-comparison').value='overlap-control';
  saveRegion('before',0,2);saveRegion('gap',1,3);transitionTasks=saveRegion('after',3,4);
  check('Overlapping regions explain why no transition is available', transitionTasks.levelTransitionDiagnostics.find(item=>item.comparisonLabel==='overlap-control').reason.includes('overlap'), true);
  check('Audio control has an accessible name', /<audio[^>]*aria-label="Current clip playback"/.test(html), true);
  check('Task prompt changes use a polite live region', /id="ear-prompt" aria-live="polite"/.test(html), true);
  check('Quick-note markup carries the same limit', /<textarea maxlength="4000" id="notes"/.test(html), true);
  await get('sample-buttons').children[2].listeners.click();
  const fixtureReport=JSON.parse(get('results').value);
  const fixtureLevels=fixtureReport.fixtureConstruction.find(item=>item.clipId===fixtures[2].id).measuredLevels;
  const fixtureBytes=Buffer.from(fixtures[2].base64.join(''),'base64');
  const fixtureSignal=sandbox.AudioMeasurementInstrument.parseWav(fixtureBytes.buffer.slice(fixtureBytes.byteOffset,fixtureBytes.byteOffset+fixtureBytes.byteLength));
  const pause=fixtures[2].construction.pause;
  check('Fixture levels bind the source hash', fixtureLevels.sourceSha256, fixtures[2].expectedSha256);
  check('Fixture level windows bind exact source frames', [fixtureLevels.before.startFrame,fixtureLevels.before.endFrameExclusive,fixtureLevels.gap.startFrame,fixtureLevels.gap.endFrameExclusive,fixtureLevels.after.startFrame,fixtureLevels.after.endFrameExclusive], [pause.startFrame-fixtureSignal.sampleRate,pause.startFrame,pause.startFrame,pause.endFrameExclusive,pause.endFrameExclusive,pause.endFrameExclusive+fixtureSignal.sampleRate]);
  for (const role of ['before','gap','after']) {
    const measured=fixtureLevels[role];
    let power=0;
    for (let frame=measured.startFrame;frame<measured.endFrameExclusive;frame++) { power+=fixtureSignal.samples[0][frame]**2; }
    const rms=10*Math.log10(power/measured.frames);
    check('Fixture '+role+' RMS matches independent sample accumulation', Math.abs(measured.channels[0].rmsDbfs-rms)<1e-9, true);
  }
  check('Fixture step arithmetic is explicit', fixtureLevels.channels[0].stepIntoGapDb, fixtureLevels.gap.channels[0].rmsDbfs-fixtureLevels.before.channels[0].rmsDbfs);
  check('Fixture levels disclose the duration confound', fixtureLevels.definition.includes('not a duration-only test'), true);
  check('Progress copy does not claim full sound-design closure', html.includes('#1444 sound-design criteria are closed'), false);
  check('Progress copy discloses unproven acceptance', html.includes('full acceptance is not established'), true);
  console.log(JSON.stringify({schema:'listening-lab-interface-probe/v1',interfaceSha256:crypto.createHash('sha256').update(implementation).digest('hex'),checks:results,scope:'Real interface and instrument evaluated against a deterministic DOM/clipboard/download stub; not browser layout, permission-policy, filesystem-save, or audio-hardware verification.'},null,2));
})().catch(error=>{console.error(error);process.exitCode=1;});
