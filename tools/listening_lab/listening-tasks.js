'use strict';
(() => {
  const version = 'labelled-ear-tasks/1.0.3';
  const definitions = [
    {id:'pause-background',issue:1441,title:'Pause and background continuity — measured follow-up',status:'Public issue closed; current broader-goal false-positive/intent follow-up remains open.',requiresProductAudio:false,prompt:'Listen to the speech before the pause, the pause itself, and the speech after it. Does the background suddenly disappear or change? Does the pause feel too long or too short? You can save your answer in the notes below. The built-in clips are demonstrations: both the pause length and the background level can affect what you hear.',findings:['Background drops abruptly','Background changes audibly','Pause feels too long','Pause feels too short','Expected authored pause (my source/plan knowledge)','Unexplained gap (my judgment)','No concern heard in this region','Unsure']},
    {id:'reply-delivery',issue:1258,title:'Reply-aware delivery — actual render required',status:'Open. No eligible product render is bundled.',requiresProductAudio:true,prompt:'Load existing, authorized baseline and directed WAV excerpts of the same passage and voice. Hear the preceding line and its reply. Does the reply express the intended skepticism, interruption, question or continuation? Use the same comparison label; report one finding for each labelled rendition. Word recognition alone is not a delivery measurement.',findings:['Delivery fits the intended exchange','Delivery does not fit the intended exchange','No clear difference to my ear','Unsure']},
    {id:'speaker-consistency',issue:1258,title:'Same-character voice consistency — actual render required',status:'Open. Preserve the earlier adverse drift result; this is separate listening evidence.',requiresProductAudio:true,prompt:'Load existing, authorized excerpts containing the same character before and after the change. Label the rendition and comparison. Note the exact interval where identity seems to change, rather than giving the clip an invented objective quality score.',findings:['Same character sounds consistent to me','Character identity changes audibly to me','No clear difference to my ear','Unsure']},
    {id:'context-edges',issue:1258,title:'Context cropping and authored-word edges — actual render required',status:'Open. Listening does not replace exact alignment, word-coverage or crop-integrity checks.',requiresProductAudio:true,prompt:'Load an existing, authorized context-cropped WAV. Read the known intended words locally. Hear the first and last authored sounds and any extra/repeated speech. Mark the smallest useful event interval. Do not paste copyrighted text into results intended for public sharing.',findings:['First authored sound seems missing','Last authored sound seems missing','Unexpected context words are audible','Unintended repetition seems audible','Authored edges sound intact to me','Unsure']}
  ];
  function create(options) {
    const {element,make,getClip,getRegion,getSettings,refreshReport} = options;
    const measurements = [], observations = [];
    let nextId = 1;
    for (const task of definitions) {
      const option = make('option', '#' + task.issue + ' — ' + task.title);
      option.value = task.id;
      element('ear-task').append(option);
    }
    element('ear-task').value = definitions[0].id;
    function task() { return definitions.find(entry => entry.id === element('ear-task').value); }
    function resetDraft() {
      element('ear-heard').checked = false;
      element('ear-finding').value = '';
      element('ear-detail').value = '';
      element('ear-save-status').textContent = 'Selection changed. Confirm listening again before saving; existing saved findings are retained.';
    }
    function updateTask() {
      resetDraft();
      const selected = task();
      element('ear-prompt').textContent = selected.prompt;
      element('ear-scope').textContent = selected.status;
      const choices = element('ear-finding');
      choices.replaceChildren();
      const empty = make('option','Choose what you heard'); empty.value = ''; choices.append(empty);
      for (const finding of selected.findings) { const option = make('option',finding); option.value = finding; choices.append(option); }
      choices.value = '';
      element('ear-save-status').textContent = 'No new response recorded. Existing saved findings are retained when you switch tasks.';
      refreshReport();
    }
    function record() {
      try {
        const clip = getClip(), selected = task();
        if (!clip) { throw Error('Load a WAV before recording a finding.'); }
        if (!element('ear-heard').checked) { throw Error('Confirm that you listened to the selected region; playback is not inferred.'); }
        const finding = element('ear-finding').value;
        if (!selected.findings.includes(finding)) { throw Error('Choose a finding, including Unsure if needed.'); }
        const declaredOrigin = element('ear-origin').value;
        if (!['unknown','existing-product-render','other-authorized-audio'].includes(declaredOrigin)) { throw Error('Choose an audio-origin declaration.'); }
        if (clip.construction && selected.requiresProductAudio) { throw Error('Built-in demonstrations are not product renders. Load an existing authorized WAV for this task.'); }
        if (selected.requiresProductAudio && declaredOrigin !== 'existing-product-render') { throw Error('This task requires an existing product render. Its origin remains your declaration, not machine-verified provenance.'); }
        const comparisonLabel = element('ear-comparison').value.trim();
        if (!comparisonLabel || comparisonLabel.length > 120) { throw Error('Enter a comparison label of 1–120 characters.'); }
        const rendition = element('ear-rendition').value;
        if (!['reference','baseline','directed','context-cropped','other'].includes(rendition)) { throw Error('Choose the labelled rendition.'); }
        const regionRole = element('ear-region-role').value;
        if (!['event','before','gap','after'].includes(regionRole)) { throw Error('Choose the region role.'); }
        const notes = element('ear-detail').value.trim();
        if (notes.length > 4000) { throw Error('Keep each finding note within 4000 characters.'); }
        if (measurements.length >= 100) { throw Error('100 findings retained. Download your results before starting another session.'); }
        const measured = getRegion(), settings = getSettings();
        const id = 'ear-' + nextId++;
        measurements.push(JSON.parse(JSON.stringify({id,clipId:clip.id,sourceSha256:clip.sha256,sourceBytes:clip.bytes,taskId:selected.id,issue:selected.issue,recordedAt:new Date().toISOString(),sourceSampleRate:clip.signal.sampleRate,sourceChannels:clip.signal.channels,region:measured,analysisInputs:settings})));
        observations.push({measurementId:id,comparisonLabel,rendition,regionRole,heardDeclared:true,audioOrigin:clip.construction?'built-in-demonstration':declaredOrigin,originVerification:'User declaration only; built-ins are known demonstrations.',finding,notes});
        element('ear-heard').checked = false;
        element('ear-finding').value = '';
        element('ear-detail').value = '';
        element('ear-save-status').textContent = 'Saved your note for ' + measured.startSeconds.toFixed(3) + '–' + measured.endSeconds.toFixed(3) + ' seconds. ' + measurements.length + ' notes saved in this page. Copy or download them before closing.';
        renderSaved(); refreshReport();
      } catch (error) { element('ear-save-status').textContent = 'Not saved: ' + error.message; }
    }
    function renderSaved() {
      const rows = element('ear-saved'); rows.replaceChildren();
      measurements.forEach((measurement,index) => {
        const observation = observations[index], row = make('tr');
        row.append(make('td',measurement.id),make('td','#' + measurement.issue + ' / ' + observation.rendition),make('td',measurement.region.startSeconds.toFixed(3) + '–' + measurement.region.endSeconds.toFixed(3) + ' s / ' + observation.regionRole),make('td',observation.finding));
        const action = make('td'), remove = make('button','Remove ' + measurement.id);
        remove.addEventListener('click',() => { measurements.splice(index,1); observations.splice(index,1); renderSaved(); refreshReport(); });
        action.append(remove); row.append(action); rows.append(row);
      });
    }
    function levelTransitions() {
      const groups = new Map();
      measurements.forEach((measurement,index) => {
        const observation = observations[index];
        if (measurement.taskId !== 'pause-background') { return; }
        const key = JSON.stringify([measurement.clipId,observation.comparisonLabel,observation.rendition]);
        if (!groups.has(key)) { groups.set(key,[]); }
        groups.get(key).push({measurement,observation});
      });
      const transitions = [], diagnostics = [];
      for (const entries of groups.values()) {
        const regions = ['before','gap','after'].map(role => entries.filter(entry => entry.observation.regionRole === role));
        const group = {clipId:entries[0].measurement.clipId,comparisonLabel:entries[0].observation.comparisonLabel,rendition:entries[0].observation.rendition,measurementIds:entries.map(entry=>entry.measurement.id)};
        if (regions.some(matches => matches.length !== 1)) { diagnostics.push({...group,status:'unavailable',reason:'Exactly one before, gap and after region is required.',roleCounts:Object.fromEntries(['before','gap','after'].map((role,index)=>[role,regions[index].length]))});continue; }
        const [before,gap,after] = regions.map(matches => matches[0].measurement);
        if (before.region.endFrameExclusive > gap.region.startFrame || gap.region.endFrameExclusive > after.region.startFrame) { diagnostics.push({...group,status:'unavailable',reason:'Regions overlap or are not ordered before, gap, after.'});continue; }
        diagnostics.push({...group,status:'measured'});
        transitions.push({clipId:before.clipId,measurementIds:[before.id,gap.id,after.id],channels:gap.region.channels.map((channel,index) => {const prior=before.region.channels[index].rmsDbfs,next=after.region.channels[index].rmsDbfs;return {channel:channel.channel,beforeRmsDbfs:prior,gapRmsDbfs:channel.rmsDbfs,afterRmsDbfs:next,stepIntoGapDb:prior===null||channel.rmsDbfs===null?null:channel.rmsDbfs-prior,stepOutOfGapDb:next===null||channel.rmsDbfs===null?null:next-channel.rmsDbfs};}),definition:'Differences of selected-region RMS dBFS. Digital zero produces null deltas. Not a noise-only, perceived-loudness or naturalness measurement.'});
      }
      return {transitions,diagnostics};
    }
    function report() {
      const levels = levelTransitions();
      return JSON.parse(JSON.stringify({version,selectedTaskId:task().id,definitions,measurements,humanObservations:observations,levelTransitions:levels.transitions,levelTransitionDiagnostics:levels.diagnostics,limits:['Task labels and audio origin are declarations, not verified TTS provenance.','A checked listening box is self-report, not proof of playback or attention.','Saved regions/settings are snapshots; changing current settings does not rewrite them.','No scores, accuracy rates, default-policy decision, pilot admission or capture authorization.','Labels and notes are included in exports; do not publicly share private or copyrighted material.']}));
    }
    element('ear-task').addEventListener('change',updateTask);
    element('ear-save').addEventListener('click',record);
    element('ear-prompt').textContent = definitions[0].prompt;
    element('ear-scope').textContent = definitions[0].status;
    const empty = make('option','Choose what you heard'); empty.value=''; element('ear-finding').append(empty);
    for (const finding of definitions[0].findings) { const option=make('option',finding); option.value=finding; element('ear-finding').append(option); }
    element('ear-finding').value='';
    return {report,resetDraft};
  }
  globalThis.AudioListeningTasks = {version,definitions,create};
})();
