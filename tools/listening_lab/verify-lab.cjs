'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const directory = __dirname;
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const html = fs.readFileSync(path.join(directory, 'listening-lab.html'), 'utf8');
const instrument = fs.readFileSync(path.join(directory, 'instrument.js'), 'utf8');
const controls = fs.readFileSync(path.join(directory, 'interface.js'), 'utf8');
const listeningTasks = fs.readFileSync(path.join(directory, 'listening-tasks.js'), 'utf8');
const inlineSources = ['instrument', 'interface', 'listening-tasks'].map(id => {
  const match = html.match(new RegExp('<script id="' + id + '">([\\s\\S]*?)<\\/script>'));
  assert(match, 'Missing inline module ' + id);
  return match[1];
});
assert.deepEqual(inlineSources, [instrument, controls, listeningTasks], 'Inline modules must be byte-identical to their files');
for (const source of inlineSources) {
  assert(!/\b(?:fetch|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\s*\(/.test(source), 'Inline modules must not use network APIs');
}
assert(!/<script[^>]+\bsrc=/.test(html), 'No external script sources');
new vm.Script(controls);
new vm.Script(listeningTasks);
const sandbox = {ArrayBuffer, Uint8Array, Float32Array, Float64Array, DataView};
vm.runInNewContext(instrument, sandbox);
const api = sandbox.AudioMeasurementInstrument;
const calibration = api.calibration();
assert(calibration.passed);
const checks = [];
function check(name, callback) { callback(); checks.push({name, passed:true}); }
function buffer(bytes) { const copy = new ArrayBuffer(bytes.length);new Uint8Array(copy).set(bytes);return copy; }
function wav(values, bits=16, encoding=1, rate=8000, channels=1) {
  const size = values.length * bits / 8;
  const bytes = new ArrayBuffer(44 + size + size % 2), view = new DataView(bytes);
  const word = (offset,text) => [...text].forEach((character,index)=>view.setUint8(offset+index,character.charCodeAt(0)));
  word(0,'RIFF');view.setUint32(4,bytes.byteLength-8,true);word(8,'WAVE');word(12,'fmt ');view.setUint32(16,16,true);
  view.setUint16(20,encoding,true);view.setUint16(22,channels,true);view.setUint32(24,rate,true);view.setUint32(28,rate*channels*bits/8,true);view.setUint16(32,channels*bits/8,true);view.setUint16(34,bits,true);word(36,'data');view.setUint32(40,size,true);
  values.forEach((value,index)=>{const offset=44+index*bits/8;if(encoding===3){view.setFloat32(offset,value,true);}else if(bits===16){view.setInt16(offset,value,true);}else if(bits===32){view.setInt32(offset,value,true);}else {view.setUint8(offset,value&255);view.setUint8(offset+1,(value>>8)&255);view.setUint8(offset+2,(value>>16)&255);}});
  return bytes;
}
for(const bits of [16,24,32]) {
  const denominator=2**(bits-1), values=[-denominator,-1,0,1,denominator-1];
  check('PCM'+bits+' signed extrema / odd padded data',()=>{
    const signal=api.parseWav(wav(values,bits));assert.equal(signal.frames,values.length);
    for(let index=0;index<values.length;index++){assert.equal(signal.samples[0][index],Math.fround(values[index]/denominator));}
  });
}
check('Stereo channel ordering and opposite phase',()=>{const signal=api.parseWav(wav([16384,-16384,8192,-8192],16,1,8000,2));assert.equal(signal.channels,2);assert.equal(signal.frames,2);assert.equal(signal.samples[0][0],.5);assert.equal(signal.samples[1][0],-.5);});
check('Float32 samples remain signed',()=>{const signal=api.parseWav(wav([-.5,0,.5],32,3));assert.equal(signal.samples[0][0],-.5);assert.equal(signal.samples[0][2],.5);});
for(const value of [NaN,Infinity,-Infinity]) {check('Nonfinite Float32 refused: '+value,()=>assert.throws(()=>api.parseWav(wav([value],32,3)),/Non-finite/));}
check('Empty data refused',()=>assert.throws(()=>api.parseWav(wav([])),/nonempty/));
check('Over-duration header refused before allocation',()=>{const bytes=wav(new Array(8000*60+1).fill(0));assert.throws(()=>api.parseWav(bytes),/60 seconds/);});
check('RIFF size drift refused',()=>{const bytes=wav([0]);new DataView(bytes).setUint32(4,0,true);assert.throws(()=>api.parseWav(bytes),/complete RIFF/);});
check('Truncated data chunk refused',()=>{const bytes=wav([0]);new DataView(bytes).setUint32(40,20,true);assert.throws(()=>api.parseWav(bytes),/bounds/);});
check('Invalid alignment refused',()=>{const bytes=wav([0]);new DataView(bytes).setUint16(32,4,true);assert.throws(()=>api.parseWav(bytes),/alignment/);});
check('Unsupported compressed WAV refused',()=>{const bytes=wav([0]);new DataView(bytes).setUint16(20,6,true);assert.throws(()=>api.parseWav(bytes),/PCM16/);});
check('Three-channel file refused',()=>assert.throws(()=>api.parseWav(wav([0,0,0],16,1,8000,3)),/channel/));
for(const frames of [1,79,80,81,159,160,161,7999,8000,8001]) {
  check('Exact quiet coverage / tail, '+frames+' frames',()=>{const signal=api.parseWav(wav(new Array(frames).fill(0)));const blocks=api.buildBlocks(signal);const result=api.findQuiet(signal,blocks,-50,.01);assert.equal(blocks.analyzedFrames+blocks.droppedTailFrames,frames);assert.equal(blocks.droppedTailFrames,frames%80);assert.equal(result.totalSeconds,Math.floor(frames/80)*80/8000);});
}
const small=api.parseWav(wav([0,0,0]));
for(const bounds of [[-1,2],[0,4],[1,1],[1.5,2]]){check('Invalid region refused '+bounds,()=>assert.throws(()=>api.regionStats(small,...bounds),/Region/));}
for(const threshold of [NaN,Infinity,-101,1]){check('Invalid quiet threshold refused '+threshold,()=>assert.throws(()=>api.findQuiet(small,api.buildBlocks(small),threshold,.5),/Threshold/));}
const fixtures=JSON.parse(html.match(/<script id="fixture-data" type="application\/json">([\s\S]*?)<\/script>/)[1]);
const measured=[];
for(const fixture of fixtures){
  const bytes=Buffer.from(fixture.base64.join(''),'base64');assert.equal(digest(bytes),fixture.expectedSha256);
  assert.equal(fixture.construction.wavSha256,digest(bytes),fixture.id+' construction.wavSha256 must be the embedded WAV hash');
  assert.equal(fixture.construction.wavBytes,bytes.length,fixture.id+' construction.wavBytes must be the embedded WAV length');
  assert.equal(fixture.construction.sourceSha256,undefined,fixture.id+' must not use ambiguous sourceSha256');
  assert.equal(fixture.construction.sourceBytes,undefined,fixture.id+' must not use ambiguous sourceBytes');
  assert.match(fixture.construction.sourceMp3Sha256,/^[0-9a-f]{64}$/,fixture.id+' sourceMp3Sha256 format');
  assert.notEqual(fixture.construction.sourceMp3Sha256,fixture.construction.wavSha256,fixture.id+' source MP3 and fixture WAV hashes must stay distinct');
  const signal=api.parseWav(buffer(bytes)),blocks=api.buildBlocks(signal),whole=api.regionStats(signal),quiet=api.findQuiet(signal,blocks,-50,.5);
  const result={id:fixture.id,sourceSha256:digest(bytes),sourceBytes:bytes.length,sampleRate:signal.sampleRate,frames:signal.frames,channels:signal.channels,whole,quiet};
  if(fixture.construction.pause){const pause=fixture.construction.pause;result.constructedInterval=api.regionStats(signal,pause.startFrame,pause.endFrameExclusive);assert.equal(result.constructedInterval.durationSeconds,pause.durationSeconds);assert.equal(quiet.count,1);assert.equal(quiet.intervals[0].startFrame,pause.startFrame);assert.equal(quiet.intervals[0].endFrameExclusive,pause.endFrameExclusive);assert(Math.abs(result.constructedInterval.channels[0].rmsDbfs-fixture.construction.insertRmsDbfs)<.000001);}
  else {assert.equal(quiet.count,0);}
  measured.push(result);
}
assert.equal(new Set(fixtures.map(fixture=>fixture.construction.sourceMp3Sha256)).size,1,'All fixtures must name one public source MP3');
assert.equal(fixtures[0].construction.sourceMp3Sha256,'ad060a5cbd1ea90024967af226f6bc8e5d0d9905660a5a506720604f13a1d759','Recorded public source MP3 hash');
const independentToleranceDb = 1e-9;
function compareReference(measurements, reference) {
  assert.equal(reference.fixtures.length, measurements.length, 'Independent fixture count');
  assert.equal(new Set(reference.fixtures.map(entry => entry.id)).size, measurements.length, 'Independent fixture IDs');
  let maximumLevelDeltaDb = 0, levelComparisons = 0;
  function compareLevels(actual, expected, label) {
    assert.equal(actual.startFrame, expected.startFrame, label + ' start frame');
    assert.equal(actual.endFrameExclusive, expected.endFrameExclusive, label + ' end frame');
    for (const metric of ['rmsDbfs', 'peakDbfs']) {
      const observed = actual.channels[0][metric], target = expected[metric];
      if (target === null) { assert.equal(observed, null, label + ' ' + metric); }
      else {
        assert(Number.isFinite(observed) && Number.isFinite(target), label + ' finite ' + metric);
        const delta = Math.abs(observed - target);
        assert(delta <= independentToleranceDb, label + ' ' + metric + ' exceeds independent tolerance');
        maximumLevelDeltaDb = Math.max(maximumLevelDeltaDb, delta);
      }
      levelComparisons++;
    }
  }
  for (const measurement of measurements) {
    const expected = reference.fixtures.find(entry => entry.id === measurement.id);
    assert(expected, 'Missing independent fixture ' + measurement.id);
    assert.equal(measurement.sourceSha256, expected.sourceSha256, measurement.id + ' source hash');
    assert.equal(measurement.sourceBytes, expected.sourceBytes, measurement.id + ' source bytes');
    assert.equal(measurement.sampleRate, expected.rate, measurement.id + ' sample rate');
    assert.equal(measurement.frames, expected.frames, measurement.id + ' frames');
    assert.equal(measurement.channels, 1, 'Reference scope is mono PCM24');
    compareLevels(measurement.whole, expected.whole, measurement.id + ' whole');
    assert.equal(JSON.stringify(measurement.quiet.intervals.map(entry => [entry.startFrame, entry.endFrameExclusive])), JSON.stringify(expected.quiet), measurement.id + ' quiet boundaries');
    assert.equal(Boolean(measurement.constructedInterval), Boolean(expected.region), measurement.id + ' constructed interval presence');
    if (expected.region) { compareLevels(measurement.constructedInterval, expected.region, measurement.id + ' constructed'); }
  }
  return {toleranceDb: independentToleranceDb, maximumLevelDeltaDb, levelComparisons, fixtureComparisons: measurements.length, enforced: true};
}
const referencePath = path.join(directory, 'reference_measurements.py');
const expectedReferenceSha256 = 'fbe85bcdcde46af9b3297535f9f9dd82ee35bbe470aa1d31d99343c68a16cbb6';
function requirePinnedReference(source) {
  assert.equal(digest(source), expectedReferenceSha256, 'Independent Python reference source hash mismatch');
}
const referenceSource = fs.readFileSync(referencePath);
requirePinnedReference(referenceSource);
check('Pinned independent reference accepts exact bytes', () => requirePinnedReference(referenceSource));
check('Changed independent reference is refused', () => assert.throws(() => requirePinnedReference(Buffer.concat([referenceSource, Buffer.from('\n')])), /reference source hash mismatch/));
check('Empty independent reference is refused', () => assert.throws(() => requirePinnedReference(Buffer.alloc(0)), /reference source hash mismatch/));
const referenceDirectory = fs.mkdtempSync(path.join(require('node:os').tmpdir(), 'aa2-listening-reference-'));
let independentReference;
try {
  const inputs = fixtures.map((fixture, index) => {
    const filename = path.join(referenceDirectory, 'fixture-' + index + '.wav');
    fs.writeFileSync(filename, Buffer.from(fixture.base64.join(''), 'base64'), {flag: 'wx'});
    return {id: fixture.id, path: filename, pause: fixture.construction.pause};
  });
  const verifiedProgram = '__file__ = ' + JSON.stringify(referencePath) + '\nexec(compile(' + JSON.stringify(referenceSource.toString('utf8')) + ', __file__, "exec"))';
  const execution = require('node:child_process').spawnSync(process.env.PYTHON || 'python', ['-B', '-c', verifiedProgram], {input: JSON.stringify(inputs), encoding: 'utf8', timeout: 30000, maxBuffer: 1024 * 1024});
  if (execution.error) { throw execution.error; }
  assert.equal(execution.status, 0, 'Independent Python reference failed: ' + execution.stderr.slice(0, 2000));
  independentReference = JSON.parse(execution.stdout);
  assert.equal(independentReference.referenceScriptSha256, expectedReferenceSha256, 'Independent reference runtime source identity mismatch');
} finally {
  assert.equal(path.dirname(path.resolve(referenceDirectory)), path.resolve(require('node:os').tmpdir()), 'Reference cleanup must remain inside the OS temp directory');
  fs.rmSync(referenceDirectory, {recursive: true, force: true});
}
const independentCrosscheck = compareReference(measured, independentReference);
check('Independent level mismatch fails closed', () => {
  const changed = JSON.parse(JSON.stringify(independentReference));
  changed.fixtures[0].whole.rmsDbfs += .01;
  assert.throws(() => compareReference(measured, changed), /exceeds independent tolerance/);
});
check('Independent boundary mismatch fails closed', () => {
  const changed = JSON.parse(JSON.stringify(independentReference));
  changed.fixtures.find(entry => entry.quiet.length).quiet[0][0]++;
  assert.throws(() => compareReference(measured, changed), /quiet boundaries/);
});
check('Independent frame mismatch fails closed', () => {
  const changed = JSON.parse(JSON.stringify(independentReference));
  changed.fixtures[0].frames++;
  assert.throws(() => compareReference(measured, changed), /frames/);
});
assert(html.includes("connect-src 'none'"));assert(!/\b(fetch|XMLHttpRequest|WebSocket)\s*\(/.test(controls));
console.log(JSON.stringify({schema:'browser-listening-lab-verification/v2',runtime:process.version,html:{bytes:Buffer.byteLength(html),sha256:digest(Buffer.from(html))},instrument:{sha256:digest(Buffer.from(instrument)),calibration},additionalChecks:checks,fixtures:measured,independentReference,independentCrosscheckToleranceDb:independentToleranceDb,independentCrosscheck,browserInteraction:'not exercised by this verifier; separate browser evidence is scoped to its own source pins',limits:['Not a browser playback, clipboard, download, UI-layout or user-listening result.','Synthetic checks establish instrument properties, not narration accuracy or TTS acceptance.']},null,2));
