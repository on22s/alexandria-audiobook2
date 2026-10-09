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
vm.createContext(context);
for (const [start, end] of [
    ['function getVoiceCardMetadata(', 'async function postVoiceTarget('],
    ['function isPersonaStateCurrent(', 'function getStateVoiceCardsMarkup('],
    ['function collectVoiceConfig()', 'function onVoiceReadyChange(']
]) {
    const first = source.indexOf(start);
    vm.runInContext(source.slice(first, source.indexOf(end, first)), context);
}
console.log(JSON.stringify(context.collectVoiceConfig()));
