const fs = require('fs'), vm = require('vm'), assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
class Element {
    constructor(className, dataset = {}) { this.className = className; this.dataset = dataset; this.children = []; this.style = {}; }
    appendChild(element) {
        if (element.parentElement) { const old = element.parentElement.children; old.splice(old.indexOf(element), 1); }
        element.parentElement = this; this.children.push(element);
    }
    querySelectorAll(selector) {
        return this.children.flatMap(element => [...(selector === '.' + element.className ? [element] : []), ...element.querySelectorAll(selector)]);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    closest(selector) { return selector === '.' + this.className ? this : this.parentElement?.closest(selector) || null; }
}
const container = new Element('voices-list'), group = new Element('voice-character-group', {voice: 'ARTHUR'});
const details = new Element('voice-base-details'); details.open = false;
const base = new Element('voice-card', {voice: 'ARTHUR', version: '', hasStates: '1'});
const state = new Element('voice-card', {voice: 'ARTHUR', version: 'state_adult', hasStates: '1'});
const other = new Element('voice-card', {voice: 'OTHER', version: '', hasStates: '0'});
for (const card of [base, state, other]) { card.appendChild(new Element('card-body')); }
details.appendChild(base); group.appendChild(details); group.appendChild(state);
container.appendChild(group); container.appendChild(other);
let filter = 'all';
const c = {window: null, document: {getElementById: id => id === 'voices-list' ? container : id === 'voices-state-filter' ? {value: filter} : {checked: false},
    querySelectorAll: selector => container.querySelectorAll(selector), createElement: () => new Element('')},
    currentBookFilename: 'book', getVoiceListFocusSnapshot: () => null, restoreVoiceListFocus() {}, escapeHtml: String};
c.window = c; c._voiceSuggestions = {ARTHUR: {type: 'lora', line_count: 20}, OTHER: {type: 'lora', line_count: 40}};
c._lineCounts = {ARTHUR: 20, OTHER: 40}; vm.createContext(c);
for (const [start, end] of [['function renderVoiceSuggestions()', 'function applySuggestionToCard('], ['function onToggleHideReady()', 'let _voiceStatusClearTimer']]) {
    vm.runInContext(source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))), c);
}
c.renderVoiceSuggestions();
assert.equal(base.parentElement, details); assert.equal(details.children.length, 1); assert.equal(details.open, false);
assert.equal(group.parentElement, container); assert.deepEqual(container.children, [other, group]);
filter = 'single'; c.onToggleHideReady(); assert.equal(group.style.display, 'none'); assert.equal(other.style.display, '');
filter = 'states'; c.onToggleHideReady(); assert.equal(group.style.display, ''); assert.equal(other.style.display, 'none');
c.renderVoiceSuggestions(); assert.equal(base.parentElement, details); assert.equal(state.parentElement, group);
console.log('Real suggestion renderer preserves closed base sections and filters whole character groups.');
