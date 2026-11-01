
'use strict';
(() => {
  const api = globalThis.AudioMeasurementInstrument;
  const fixtures = JSON.parse(document.getElementById('fixture-data').textContent);
  const element = id => document.getElementById(id);
  const make = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) { node.textContent = text; } return node; };
  const state = {items: [], current: null, selectedInterval: null, selectionEpoch: 0, context: null, source: null, playbackEpoch: 0, calibration: api.calibration(), startedAt: new Date().toISOString()};
  const feedbackGroups = [{key:'pauseLength',title:'Pause length',choices:[['pause-short','Too short'],['pause-right','About right'],['pause-long','Too long'],['pause-unsure','Not sure'],['pause-none','There is no pause to judge']]},{key:'backgroundSound',title:'Background sound',choices:[['background-steady','It stayed consistent'],['background-drops','It suddenly became much quieter or disappeared'],['background-changes','It changed in another noticeable way'],['background-unsure','Not sure'],['background-none','I did not hear background sound']]}];
  const number = value => Number(value).toFixed(3);
  const db = value => value === null ? 'digital zero' : value.toFixed(2) + ' dBFS';
  const status = (message, error = false) => { element('status').textContent = message;element('status').className = error ? 'notice error' : 'notice'; };
  const listening = globalThis.AudioListeningTasks.create({element,make,getClip:()=>state.current,getRegion:region,getSettings:()=>({thresholdDbfs:input('threshold'),minimumSeconds:input('minimum')}),refreshReport});
  function stopRegion() { state.playbackEpoch++;if (state.source) { state.source.onended = null;state.source.stop();state.source.disconnect();state.source = null; } }
  function stop() { stopRegion();element('player').pause(); }
  function input(id) { const text = element(id).value.trim(); if (!text) { throw Error('Enter a value for ' + id + '.'); } const value = Number(text);if (!Number.isFinite(value)) { throw Error('Enter a finite value for ' + id + '.'); } return value; }
  function region() {
    if (!state.current) { throw Error('Load a clip before selecting a section.'); }
    const signal = state.current.signal, start = input('region-start'), end = input('region-end');
    if (start < 0 || end <= start || end > signal.durationSeconds + 0.000001) { throw Error('Region end must be after start and within the clip.'); }
    const interval = state.selectedInterval;
    if (interval && interval.clipId === state.current.id && start === interval.startSeconds && end === interval.endSeconds) { return api.regionStats(signal, interval.startFrame, interval.endFrameExclusive); }
    return api.regionStats(signal, Math.floor(start * signal.sampleRate), Math.min(signal.frames, Math.ceil(end * signal.sampleRate)));
  }
  const quiet = item => api.findQuiet(item.signal, item.blocks, input('threshold'), input('minimum'));
  function fixtureLevels(item) {
    const pause = item.construction.pause;
    if (!pause) { return {status:'not-applicable',reason:'No constructed pause.'}; }
    const signal = item.signal, windowFrames = signal.sampleRate;
    const before = api.regionStats(signal, Math.max(0,pause.startFrame-windowFrames), pause.startFrame);
    const gap = api.regionStats(signal, pause.startFrame, pause.endFrameExclusive);
    const after = api.regionStats(signal, pause.endFrameExclusive, Math.min(signal.frames,pause.endFrameExclusive+windowFrames));
    return {status:'measured',sourceSha256:item.sha256,windowSeconds:1,before,gap,after,
      channels:gap.channels.map((channel,index)=>({channel:channel.channel,
        stepIntoGapDb:channel.rmsDbfs===null||before.channels[index].rmsDbfs===null?null:channel.rmsDbfs-before.channels[index].rmsDbfs,
        stepOutOfGapDb:channel.rmsDbfs===null||after.channels[index].rmsDbfs===null?null:after.channels[index].rmsDbfs-channel.rmsDbfs})),
      definition:'Source-frame RMS for the full constructed pause and up to one second immediately before/after. Deltas are gap minus before and after minus gap. Digital zero yields null. Speech is included; not noise-only or perceived loudness. Pause duration and background level both vary; not a duration-only test.'};
  }
  function report() {
    if (element('notes').value.length > 4000) { throw Error('Keep quick notes within 4000 characters. Your previous saved note is unchanged.'); }
    return {schema:'audiobook-listening-measurement/v1', analyzerVersion:api.version, interfaceVersion:'pcm-window-lab-ui/1.2.5', sessionStartedAt:state.startedAt, exportedAt:new Date().toISOString(),
      environment:{userAgent:null,exportPrivacy:'Browser user-agent and local filenames omitted; audio SHA-256 fingerprints, byte lengths, format, duration, labels and listener notes are included. Review before sharing.',decodePath:'Direct RIFF PCM parser to Float32; source-clock analysis, no analysis resampling'},
      definitions:{rms:'20 log10(sqrt(mean(sample^2))); per channel, null for digital zero',peak:'20 log10(max(abs(sample))); sample peak, not true peak',quiet:'Maximum per-channel block RMS power <= 10^(thresholdDbfs/10); consecutive runs filtered by duration',blocks:'round(source rate * .010) frames, anchored at frame 0; trailing partial block dropped from quiet analysis only',region:'Half-open source-frame interval. Quiet-search selections keep exact source-frame bounds; manually entered times use floor(start*rate) to ceil(end*rate), evaluated as JavaScript numbers.',nearFullScale:{thresholdDbfs:-0.1,amplitude:api.nearFullScaleAmplitude,comparison:'abs(sample) >= threshold; not a clipping diagnosis'},overFullScale:'Count of represented Float32 values with abs(sample) > 1. Integer PCM cannot encode such values; zero does not establish absence of clipping. PCM32 values may round to full scale.'},
      telemetry:state.items.map(item=>({id:item.id,label:item.construction?item.label:item.id,sourceName:item.construction?item.name:null,sourceBytes:item.bytes,sourceSha256:item.sha256,sourceFormat:item.signal.format,sourceFrames:item.signal.frames,sourceSampleRate:item.signal.sampleRate,sourceChannels:item.signal.channels,durationSeconds:item.signal.durationSeconds,analyzedRepresentation:'Float32; integer PCM16/24 exact, PCM32 amplitudes can round',wholeClip:item.whole,quietAnalysis:{...quiet(item),blockFrames:item.blocks.blockFrames,blockSeconds:item.blocks.blockSeconds,analyzedFrames:item.blocks.analyzedFrames,droppedTailFrames:item.blocks.droppedTailFrames}})),
      selectedRegion:{clipId:state.current.id,...region()},fixtureConstruction:state.items.filter(item=>item.construction).map(item=>({clipId:item.id,...item.construction,measuredLevels:fixtureLevels(item)})),
      humanObservations:state.items.map(item=>({clipId:item.id,observation:item.observation||'not recorded',answers:item.listeningAnswers||{},notes:item.notes||''})),instrumentChecks:state.calibration,listeningTasks:listening.report(),
      limits:['Built-in pause inserts have a large level transition relative to surrounding narration; this is not a duration-only preference test.','Quietness does not establish an unintended pause.','No LUFS, true peak, word accuracy, pronunciation, perceptual quality or false-positive/miss rate is measured.','No model/pilot acceptance, capture authorization or publication clearance.','Short-clip in-memory tool; not a production or long-book memory validator. The 64 MiB session cap counts retained decoded samples only, not source blobs, in-flight decoding, playback copies or total browser memory.','No normalization, EQ, silence removal or gain changes to sample data. Playback/browser/hardware may resample and change listening volume. File metrics are not sound-pressure measurements.','Notes are lost on reload unless copied or downloaded.']};
  }
  function refreshReport() {
    if(!state.current){element('results').value='Load a clip before copying or downloading measurements.';element('copy').disabled=true;element('download').disabled=true;return;}
    try { element('results').value = JSON.stringify(report(),null,2);element('copy').disabled = false;element('download').disabled = false; }
    catch(error) { element('results').value = 'Cannot prepare export: ' + error.message;element('copy').disabled = true;element('download').disabled = true; }
  }
  function draw() {
    const canvas = element('waveform'), context = canvas.getContext('2d'), signal = state.current.signal;
    context.fillStyle='#101a2b';context.fillRect(0,0,canvas.width,canvas.height);
    const step = Math.ceil(signal.frames / canvas.width);context.strokeStyle='#72d9d3';context.beginPath();
    for(let column=0;column<canvas.width;column++) { let low=0,high=0;for(const samples of signal.samples) { for(let frame=column*step;frame<Math.min(signal.frames,(column+1)*step);frame++) { low=Math.min(low,samples[frame]);high=Math.max(high,samples[frame]); } } context.moveTo(column,80-Math.min(1,high)*73);context.lineTo(column,80-Math.max(-1,low)*73); }
    context.stroke();
    try { const chosen = region();context.fillStyle='rgba(255,209,102,.18)';context.fillRect(chosen.startFrame/signal.frames*canvas.width,0,chosen.frames/signal.frames*canvas.width,160); } catch(error) { }
    context.strokeStyle='#5b6a80';context.beginPath();context.moveTo(0,80);context.lineTo(canvas.width,80);context.stroke();
  }
  function refreshRegion() {
    try { const measured=region();element('region-result').textContent=number(measured.durationSeconds)+' seconds selected · Average signal level: '+measured.channels.map(channel=>db(channel.rmsDbfs)).join(' / ')+' · Loudest sample: '+measured.channels.map(channel=>db(channel.peakDbfs)).join(' / ');element('region-play').disabled=false; }
    catch(error) { element('region-result').textContent=error.message;element('region-play').disabled=true; }
    draw();refreshReport();
  }
  function metric(label,value,detail) { const node=make('div');node.className='metric';node.append(make('span',label),make('strong',value),make('small',detail));return node; }
  async function playRegion() {
    try {
      stop();const epoch=state.playbackEpoch,measured=region(),signal=state.current.signal;
      if(!state.context) { state.context=new (window.AudioContext||window.webkitAudioContext)(); } await state.context.resume();
      if(epoch!==state.playbackEpoch) { return; }
      const buffer=state.context.createBuffer(signal.channels,signal.frames,signal.sampleRate);signal.samples.forEach((samples,channel)=>buffer.copyToChannel(samples,channel));
      const source=state.context.createBufferSource();source.buffer=buffer;source.connect(state.context.destination);state.source=source;
      source.onended=()=>{source.disconnect();if(state.source===source){state.source=null;status('Selected section finished. Your playback volume does not change the file measurements.');}};
      source.start(0,measured.startSeconds,measured.durationSeconds);
      status('Playing the selected section: '+number(measured.startSeconds)+'–'+number(measured.endSeconds)+' seconds.');
    } catch(error) { status('Playback failed: '+error.message,true); }
  }
  function render() {
    const item=state.current,signal=item.signal,measured=quiet(item);
    element('clip-title').textContent=item.label;element('clock').textContent=signal.sampleRate+' Hz · '+signal.channels+' channel(s) · '+signal.frames.toLocaleString()+' source frames · '+signal.format.bits+'-bit WAV';
    element('construction').textContent=item.construction?item.construction.description+' Designed boundaries are separate from measured intervals.':'User-selected audio: no intended pauses or source rights inferred.';
    element('fingerprint').textContent='Source SHA-256: '+item.sha256;
    element('metrics').replaceChildren(metric('Clip duration',number(signal.durationSeconds)+' s','Length of this audio file'),metric('Average signal level (RMS)',item.whole.channels.map(channel=>db(channel.rmsDbfs)).join(' / '),'More negative = quieter signal. Includes speech and background.'),metric('Loudest sample',item.whole.channels.map(channel=>db(channel.peakDbfs)).join(' / '),'0 dBFS is the usual digital ceiling. This is not a distortion test.'),metric('Quiet sections found',String(measured.count),number(measured.totalSeconds)+' seconds total with the settings below'));
    element('near-full').textContent='Samples close to the digital ceiling (−0.1 dBFS or higher): '+item.whole.channels.map(channel=>'ch '+channel.channel+': '+channel.nearFullScaleSamples).join(' · ')+'. A count here does not by itself prove audible distortion.';
    element('coverage').textContent=item.blocks.blockFrames+' source frames per block ('+number(item.blocks.blockSeconds*1000)+' ms). Excluded partial tail: '+item.blocks.droppedTailFrames+' frame(s). Whole/region levels include every selected frame.';
    const rows=element('quiet-rows');rows.replaceChildren();
    for(const interval of measured.intervals.slice(0,100)){const row=make('tr');row.append(make('td',number(interval.startSeconds)),make('td',number(interval.endSeconds)),make('td',number(interval.durationSeconds)));const cell=make('td'),button=make('button','Select + listen');button.className='quiet-select';button.addEventListener('click',()=>{listening.resetDraft();state.selectedInterval={...interval,clipId:state.current.id};element('region-start').value=interval.startSeconds;element('region-end').value=interval.endSeconds;refreshRegion();playRegion();});cell.append(button);row.append(cell);rows.append(row);}
    if(!measured.count){const row=make('tr'),cell=make('td','No sections are quiet enough for long enough at these settings. You may still hear pauses.');cell.colSpan=4;row.append(cell);rows.append(row);}
    element('row-limit').textContent=measured.count>100?'First 100 runs shown; report includes all runs.':'';refreshRegion();
  }
  function select(id) {
    const item=state.items.find(entry=>entry.id===id);quiet(item);
    listening.resetDraft(true);
    stop();state.selectionEpoch++;state.current=item;state.selectedInterval=null;element('clips').value=id;element('player').src=item.url;
    element('region-start').value=item.construction&&item.construction.pause?item.construction.pause.startSeconds:0;
    element('region-end').value=item.construction&&item.construction.pause?item.construction.pause.endSeconds:Math.min(5,item.signal.durationSeconds);
    element('notes').value=item.notes||'';for(const group of feedbackGroups){for(const [id,label] of group.choices){element(id).checked=item.listeningAnswers?.[group.key]===label;}}element('workspace').hidden=false;render();
  }
  async function add(bytes,metadata) {
    const selectionEpoch=state.selectionEpoch;
    const signal=api.parseWav(bytes);
    if(!crypto.subtle) { throw Error('WebCrypto is required. Open this file in a current Chromium or Firefox browser.'); }
    const digest=await crypto.subtle.digest('SHA-256',bytes),sha256=[...new Uint8Array(digest)].map(value=>value.toString(16).padStart(2,'0')).join('');
    if(metadata.expectedSha256&&sha256!==metadata.expectedSha256){throw Error('Sample hash mismatch. Analysis refused.');}
    const blocks=api.buildBlocks(signal);quiet({signal,blocks});
    const id=metadata.id||'local-'+sha256.slice(0,16),duplicate=state.items.find(item=>item.sha256===sha256);if(duplicate){if(selectionEpoch===state.selectionEpoch){select(duplicate.id);}status('Identical source bytes already loaded; reusing '+duplicate.label+'. No second observation record was created.');return 'reused';}if(state.items.some(item=>item.id===id)){throw Error('Clip ID collision with different source bytes; analysis refused.');}
    const storedFrames=state.items.reduce((sum,item)=>sum+item.signal.frames*item.signal.channels,0);
    if(state.items.length>=8||(storedFrames+signal.frames*signal.channels)*4>64*1024*1024){throw Error('Session limit: 8 clips / 64 MiB decoded samples. Download your notes and reload.');}
    const item={...metadata,id,sha256,bytes:bytes.byteLength,signal,blocks,whole:api.regionStats(signal),url:URL.createObjectURL(new Blob([bytes],{type:'audio/wav'}))};state.items.push(item);
    const option=make('option',item.label);option.value=id;element('clips').append(option);if(selectionEpoch===state.selectionEpoch){select(id);}else{refreshReport();}return 'added';
  }
  for(const fixture of fixtures){const button=make('button',fixture.label);button.className='sample-button';button.addEventListener('click',async()=>{
    const buttons=[...element('sample-buttons').querySelectorAll('button')];buttons.forEach(node=>node.disabled=true);
    try{status('Verifying sample hash and measuring source PCM…');await new Promise(resolve=>requestAnimationFrame(resolve));const binary=atob(fixture.base64.join('')),bytes=Uint8Array.from(binary,character=>character.charCodeAt(0)).buffer;const outcome=await add(bytes,fixture);if(outcome==='added'){status('Sample hash verified. Measurements ready. Press play when ready; nothing autoplays.');}}
    catch(error){status(error.message,true);}finally{buttons.forEach(node=>node.disabled=false);}
  });element('sample-buttons').append(button);}
  element('files').addEventListener('change',async event=>{let loaded=0,reused=0;const refused=[];try{for(const file of event.target.files){try{if(file.size>api.limits.bytes){throw Error('exceeds 20 MiB');}const outcome=await add(await file.arrayBuffer(),{name:file.name,label:file.name});if(outcome==='reused'){reused++;}else{loaded++;}}catch(error){refused.push(file.name+': '+error.message);}}status(loaded+' loaded, '+reused+' identical sources reused, '+refused.length+' refused.'+(refused.length?' '+refused.join(' | '):' No upload or normalization.'),refused.length>0);}finally{event.target.value='';}});
  element('clips').addEventListener('change',event=>{try{select(event.target.value);}catch(error){element('clips').value=state.current?state.current.id:'';status('Clip switch refused: '+error.message,true);element('copy').disabled=true;element('download').disabled=true;}});
  for(const id of ['threshold','minimum']){element(id).addEventListener('change',()=>{stop();try{render();status('Settings changed; all report intervals use current settings.');}catch(error){status(error.message,true);element('copy').disabled=true;element('download').disabled=true;}});}
  for(const id of ['region-start','region-end']){element(id).addEventListener('input',()=>{stop();state.selectedInterval=null;listening.resetDraft();refreshRegion();});}
  element('region-play').addEventListener('click',playRegion);element('stop').addEventListener('click',stop);
  element('player').addEventListener('play',stopRegion);
  element('notes').addEventListener('input',()=>{if(state.current){if(element('notes').value.length<=4000){state.current.notes=element('notes').value;}refreshReport();}});
  element('feedback-clear').addEventListener('click',()=>{if(!state.current){return;}state.current.listeningAnswers={};state.current.observation='';for(const group of feedbackGroups){for(const [id] of group.choices){element(id).checked=false;}}refreshReport();});
  for(const group of feedbackGroups){for(const [id,label] of group.choices){element(id).addEventListener('change',()=>{if(!state.current||!element(id).checked){return;}for(const [otherId] of group.choices){element(otherId).checked=otherId===id;}state.current.listeningAnswers={...state.current.listeningAnswers,[group.key]:label};state.current.observation=feedbackGroups.filter(entry=>state.current.listeningAnswers[entry.key]).map(entry=>entry.title+': '+state.current.listeningAnswers[entry.key]).join('; ');refreshReport();});}}
  element('copy').addEventListener('click',async()=>{refreshReport();if(element('copy').disabled){return;}try{await navigator.clipboard.writeText(element('results').value);status('Results copied. Browser details and local filenames omitted; your notes are included. Review before sharing.');}catch(error){element('results').focus();element('results').select();let copied=false;try{copied=document.execCommand('copy');}catch(failure){copied=false;}status(copied?'Results copied using the local fallback. Browser details and local filenames omitted; your notes are included.':'Results selected. Press Ctrl+C, or Download JSON.');}});
  element('download').addEventListener('click',()=>{refreshReport();if(element('download').disabled){return;}let url,anchor;try{url=URL.createObjectURL(new Blob([element('results').value+'\n'],{type:'application/json'}));anchor=make('a');anchor.href=url;anchor.download='audiobook-measurements-'+new Date().toISOString().replace(/[:.]/g,'-')+'.json';document.body.append(anchor);anchor.click();status('Download requested, not confirmed saved. Check your downloads; if absent, copy the results. Local filenames/browser details omitted; notes included.');}catch(error){status('Download could not be requested: '+error.message+'. Copy the results instead.',true);}finally{if(anchor){anchor.remove();}if(url){setTimeout(()=>URL.revokeObjectURL(url),10000);}}});
  for(const check of state.calibration.results){element('checks').append(make('li',(check.passed?'PASS · ':'FAIL · ')+check.name+' | observed '+JSON.stringify(check.observed)+' | expected '+JSON.stringify(check.expected)+' | tolerance '+check.tolerance));}
  element('calibration-summary').textContent=(state.calibration.passed?'PASS':'FAIL')+' — '+state.calibration.count+' measurement self-checks (expand for details)';
  if(!state.calibration.passed){[...document.querySelectorAll('button,input,select')].forEach(node=>node.disabled=true);status('Instrument calibration failed; do not use measurements.',true);}
  element('region-play').disabled = !state.current;
  refreshReport();
  window.addEventListener('beforeunload',()=>{for(const item of state.items){URL.revokeObjectURL(item.url);}});
})();
