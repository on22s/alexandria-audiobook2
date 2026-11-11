const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
const snapshot = JSON.parse(fs.readFileSync(0, 'utf8'));
const voice = snapshot.voices.find(row => row.name === 'ARTHUR');
const configs = [['', voice.config], ...Object.entries(voice.config.versions)];
const cards = configs.map(([version, data]) => {
    const fields = {
        '.voice-type:checked': {value: 'clone'}, '.alias-select': {value: ''},
        '.ref-text': {value: data.ref_text || ''}, '.ref-audio': {value: data.ref_audio},
        '.persona-description': {value: data.description}, '.voice-ready': {checked: false},
        '.voice-seed': {value: process.argv.includes('--empty-seed') ? '' : String(data.seed)}
    };
    return {dataset: {voice: 'ARTHUR', version}, querySelector: selector => fields[selector] || null};
});
const context = {window: null, document: {querySelectorAll: () => cards}, getLoraModelsById: () => new Map()};
context.window = context;
context._voicesByName = {ARTHUR: voice};
(vm.createContext(context),context.t=context.t||((key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split(String.fromCharCode(123)+name+String.fromCharCode(125)).join(String(values[name])),String(english))),context.formatUiSafetyText=context.formatUiSafetyText||((key,english,values=Object())=>context.t(key,english,values)),context.joinUiSentences=context.joinUiSentences||((parts,locale)=>(Array.isArray(parts)?parts:[]).filter(part=>typeof part==='string'&&part.trim()!=='').map(part=>part.trim()).join(' ')),context.tSafety=context.tSafety||((key,english,values=Object())=>Object.fromEntries([['primary',context.t(key,english,values)],['secondary',null]])),context);
for (const [start, end] of [
    ['function getVoiceCardMetadata(', 'async function postVoiceTarget('],
    ['function isPersonaStateCurrent(', 'function getStateVoiceCardsMarkup('],
    ['function collectVoiceConfig()', 'function onVoiceReadyChange(']
]) {
    const first = source.indexOf(start);
    vm.runInContext(source.slice(first, source.indexOf(end, first)), context);
}
console.log(JSON.stringify(context.collectVoiceConfig()));
