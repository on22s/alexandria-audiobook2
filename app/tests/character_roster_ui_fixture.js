const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
const root = process.argv[2], existing = fs.readFileSync(path.join(root, 'app/tests/speaker_review_ui_fixture.js'), 'utf8');
// Reuse the existing dependency-free test DOM. These checks are not browser QA.
const stop = existing.indexOf('\nconst root=process.argv[2]'); assert(stop > 0);
const {makeWindow, Element} = new Function('require', existing.slice(0, stop) + '\nreturn {makeWindow, Element};')(require);
Element.prototype.prepend = function (...nodes) { for (const n of [...nodes].reverse()) { n.parentElement = this; this.children.unshift(n); } };
const dispatch = Element.prototype.dispatchEvent;
Element.prototype.dispatchEvent = function (event) { dispatch.call(this, event); this['on' + event.type]?.(event); return true; };
const click = Element.prototype.click;
Element.prototype.click = function () { for (let p = this.parentElement; p; p = p.parentElement) { if (p.tagName === 'fieldset' && p.disabled) { return; } } return click.call(this); };
const html = fs.readFileSync(path.join(root, 'app/static/index.html'), 'utf8');
const start = html.indexOf('<details id="character-roster-panel"'); assert(start > 0);
const panel = html.slice(start, html.indexOf('</details>', start) + 10);
const source = fs.readFileSync(path.join(root, 'app/static/js/app-character-roster.js'), 'utf8');
const core = fs.readFileSync(path.join(root, 'app/static/js/app-core.js'), 'utf8');
const book = core.slice(core.indexOf("        let currentBookFilename = '';"), core.indexOf('        function getCurrentBookName('));
const A = 'character_' + 'a'.repeat(20), B = 'character_' + 'b'.repeat(20), C = 'character_' + 'c'.repeat(20);
const entry = (id, name) => ({id, canonical_name: name, aliases: [], evidence: [{source_quote: name, start_char: 0, end_char: name.length}]});
const clone = value => JSON.parse(JSON.stringify(value));
const settle = async () => { for (let i = 0; i < 5; i++) { await new Promise(resolve => setImmediate(resolve)); } };
const get = (w, id) => w.document.getElementById('roster-' + id);
const boxes = w => get(w, 'rows').children;
const text = (w, row, index, value) => { const field = boxes(w)[row].querySelectorAll('textarea')[index]; field.value = value; field.dispatchEvent({type: 'input'}); };
const select = (w, row) => { const field = boxes(w)[row].querySelector('input'); field.checked = true; field.dispatchEvent({type: 'change'}); };
async function press(w, id) { get(w, id).click(); await settle(); }
let assertions = 0;
function check(condition, message) { assertions++; assert(condition, message); }
function fresh() {
    const w = makeWindow(panel); w.crypto = require('crypto').webcrypto; w.URLSearchParams = URLSearchParams;
    w.Blob = Blob; w.URL = {createObjectURL: blob => { w.exported = blob; return 'blob:fixture'; }, revokeObjectURL() {}};
    w.calls = []; w.confirmed = true; w.canApprove = true;
    w.meta = {context_revision: '1'.repeat(64), source_sha256: '2'.repeat(64), book_token: '3'.repeat(64), source_name: 'book-a.txt',
        source_characters: 100, offset_domain: 'prepared_source_unicode_characters_v1', cast: [], approval_current: true,
        roster: {schema: 1, source_sha256: '2'.repeat(64), source_characters: 100, entries: [entry(A, 'ALICE'), entry(B, 'BOB')], retired_ids: []}};
    w.undo = {available: false, can_undo: false, revision: null};
    w.showToast = () => {}; w.showConfirm = async () => w.holdConfirm ? new Promise(resolve => { w.resolveConfirm = resolve; }) : w.confirmed;
    w.API = {
        get: async url => {
            w.calls.push({url, method: 'GET'});
            if (url.endsWith('/undo')) { return clone(w.undo); }
            if (w.holdMeta) { await new Promise(resolve => { w.resolveMeta = resolve; }); } return {...clone(w.meta), undo_revision: Object.hasOwn(w.meta, 'undo_revision') ? w.meta.undo_revision : w.undo.revision};
        },
        post: async (url, body) => {
            w.calls.push({url, method: 'POST', body: clone(body)});
            if (url.endsWith('/source')) { if (w.holdQuote) { await new Promise(resolve => { w.resolveQuote = resolve; }); } const quote = body.quote; return {matches: [{source_quote: quote, start_char: 5, end_char: 5 + [...quote].length, before: '', after: ''}], next_offset: null}; }
            if (url.endsWith('/preview')) {
                if (w.holdPreview) { await new Promise(resolve => { w.resolvePreview = resolve; }); }
                return {context_revision: body.context_revision, revision: '4'.repeat(64), undo_available: false, cast_changed: true,
                    review: {can_approve: w.canApprove, decisions: body.proposal.decisions, import_review: null,
                        evidence: {entries: body.proposal.entries.map(r => ({...r, evidence: r.evidence.map(e => ({...e, status: 'exact'}))}))}}};
            }
            if (url.endsWith('/apply')) {
                if (w.holdApply) { await new Promise(resolve => { w.resolveApply = resolve; }); }
                w.meta.roster = {schema: 1, source_sha256: body.proposal.source_sha256, source_characters: 100, entries: clone(body.proposal.entries), retired_ids: []};
                w.meta.context_revision = '5'.repeat(64); w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)};
                if (w.loseApply) { throw Error('response lost'); } return {approved: true};
            }
            if (url.endsWith('/discard_undo')) { w.undo = {available: false, can_undo: false, revision: null}; w.meta.context_revision = '7'.repeat(64); return {discarded: true}; }
            throw Error('Unexpected fixture POST: ' + url);
        }
    };
    w.eval(book + source); w.applyCurrentBookFilename('book-a.txt'); return w;
}
async function load(w) { await press(w, 'load'); check(!get(w, 'fields').disabled, 'loaded draft is editable'); }
async function consent(w) { await press(w, 'preview'); get(w, 'consent').checked = true; get(w, 'consent').dispatchEvent({type: 'change'}); }
(async () => {
    let w = fresh(); check(w.calls.length === 0, 'opening module makes no request'); await load(w);
    select(w, 0); select(w, 1); get(w, 'survivor').value = A; await press(w, 'merge'); await press(w, 'preview');
    let request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    check(JSON.stringify(request.proposal.decisions) === JSON.stringify([{action: 'merge', from_ids: [A, B], to_ids: [A]}]), 'merge keeps explicitly chosen canonical ID');
    check(request.proposal.entries[0].aliases.includes('BOB'), 'merge exposes other canonical name as alias'); w.close();

    w = fresh(); await load(w); select(w, 0); get(w, 'split-name').value = 'TWIN'; get(w, 'split-new').checked = true; await press(w, 'split'); await press(w, 'preview');
    request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    let d = request.proposal.decisions.find(d => d.action === 'split');
    check(d.from_ids[0] === A && d.to_ids.length === 2 && !d.to_ids.includes(A), 'split can explicitly retire original and create two IDs'); w.close();

    w = fresh(); await load(w); select(w, 1); await press(w, 'retire'); text(w, 0, 0, 'RENAMED ALICE'); await press(w, 'preview');
    request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    check(request.proposal.decisions.some(d => d.action === 'retire' && d.from_ids[0] === B), 'retirement requires explicit decision');
    check(request.proposal.decisions.some(d => d.action === 'update' && d.to_ids[0] === A), 'rename preserves ID'); w.close();

    w = fresh(); await load(w); await consent(w); check(!get(w, 'approve').disabled, 'review and checkbox enable approval');
    text(w, 0, 1, 'AN ALIAS'); check(get(w, 'approve').disabled && !get(w, 'consent').checked, 'editing invalidates preview and consent');
    await press(w, 'approve'); check(!w.calls.some(c => c.url.endsWith('/apply')), 'stale preview cannot write'); w.close();

    w = fresh(); await load(w); w.holdPreview = true; await press(w, 'preview'); text(w, 0, 0, 'EDITED DURING RESPONSE'); w.resolvePreview(); await settle();
    check(get(w, 'approve').disabled && !get(w, 'review').textContent, 'out-of-order preview cannot approve changed draft'); w.close();

    w = fresh(); w.holdMeta = true; await press(w, 'load'); w.applyCurrentBookFilename('book-b.txt'); w.resolveMeta(); await settle();
    check(get(w, 'fields').disabled && !get(w, 'book').textContent, 'old metadata response hidden after book switch'); w.close();

    w = fresh(); await load(w); w.holdQuote = true;
    boxes(w)[0].querySelectorAll('button').find(b => b.textContent === 'Find quote in source').click(); await settle();
    w.applyCurrentBookFilename('book-b.txt'); w.resolveQuote(); await settle();
    check(!boxes(w).length && get(w, 'fields').disabled, 'old quote locations cannot enter another book'); w.close();

    w = fresh(); await load(w); text(w, 0, 0, 'UNSAVED'); w.holdConfirm = true;
    const guard = w.ensureCharacterRosterEditsDiscardable(); await settle(); text(w, 0, 0, 'EDITED AGAIN'); w.resolveConfirm(true);
    check(await guard === false, 'edits during discard confirmation refuse book switch'); w.close();

    w = fresh(); await load(w); await consent(w); w.holdApply = true; await press(w, 'approve');
    check(await w.ensureCharacterRosterEditsDiscardable() === false, 'inflight approval blocks selection');
    w.applyCurrentBookFilename('book-b.txt'); check(await w.ensureCharacterRosterEditsDiscardable() === false, 'reset keeps inflight mutation fence');
    const reads = w.calls.filter(c => c.method === 'GET').length; w.resolveApply(); await settle();
    check(w.calls.filter(c => c.method === 'GET').length === reads, 'old approval response does not reload wrong book'); w.close();

    w = fresh(); await load(w); await consent(w); w.loseApply = true; await press(w, 'approve');
    check(get(w, 'approve').disabled && await w.ensureCharacterRosterEditsDiscardable() === false, 'lost successful write locks repeated approval until readback');
    await press(w, 'load'); check(get(w, 'undo').disabled === false, 'reload reads back persisted approval after lost response');
    check(w.calls.filter(c => c.url.endsWith('/apply')).length === 1, 'lost response does not duplicate write'); w.close();

    w = fresh(); await load(w); await consent(w); w.loseApply = true; await press(w, 'approve');
    const readback = w.API.get;
    w.API.get = async () => { throw Error('offline readback'); }; await press(w, 'load');
    check(await w.ensureCharacterRosterEditsDiscardable() === false && get(w, 'fields').disabled, 'failed readback retains uncertain-write book fence');
    w.API.get = async url => { if (url.endsWith('/undo')) { throw Error('undo unavailable'); } return readback(url); }; await press(w, 'load');
    check(await w.ensureCharacterRosterEditsDiscardable() === false, 'metadata alone does not resolve uncertain write');
    w.API.get = async url => url.endsWith('/undo') ? {} : readback(url); await press(w, 'load');
    check(await w.ensureCharacterRosterEditsDiscardable() === false, 'malformed undo status cannot release uncertain fence');
    w.API.get = readback; await press(w, 'load');
    check(await w.ensureCharacterRosterEditsDiscardable() === true && !get(w, 'undo').disabled, 'complete saved-state readback resolves uncertain write'); w.close();

    w = fresh(); const normalReadback = w.API.get;
    w.API.get = async url => { if (url.endsWith('/undo')) { throw Error('fixture undo offline'); } return normalReadback(url); };
    await press(w, 'load');
    check(get(w, 'status').textContent.includes('fixture undo offline') && get(w, 'approve').disabled, 'ordinary undo-fetch diagnostic survives final sync'); w.close();

    w = fresh(); w.meta.undo_revision = '9'.repeat(64); w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)};
    await press(w, 'load');
    check(get(w, 'undo').disabled && get(w, 'discard').disabled && get(w, 'approve').disabled, 'fresh undo from another context cannot be paired with displayed metadata');
    check(get(w, 'status').textContent.includes('changed during loading'), 'inconsistent metadata and undo require reload');
    await press(w, 'undo'); await press(w, 'discard');
    check(!w.calls.some(c => c.method === 'POST'), 'mismatched undo context cannot send a mutation'); w.close();

    w = fresh(); w.meta.undo_revision = null; w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)};
    await press(w, 'load'); await press(w, 'undo'); await press(w, 'discard');
    check(get(w, 'undo').disabled && get(w, 'discard').disabled && !w.calls.some(c => c.method === 'POST'), 'metadata without an undo cannot authorize a subsequently created record'); w.close();

    w = fresh(); w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)}; await load(w);
    const sourceLostReadback = w.API.get;
    w.API.get = async url => { if (!url.endsWith('/undo')) { throw Error('source disappeared'); } return sourceLostReadback(url); };
    await press(w, 'discard');
    check(JSON.stringify(w.calls.find(c => c.method === 'POST' && c.url.endsWith('/discard_undo')).body) === JSON.stringify({expected_revision: '6'.repeat(64)}), 'source disappearance after display preserves discard of the loaded exact record'); w.close();

    w = fresh(); const failedMetadataReadback = w.API.get;
    w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)};
    w.API.get = async url => { if (!url.endsWith('/undo')) { throw Error('book details temporarily busy'); } return failedMetadataReadback(url); };
    await press(w, 'load'); await press(w, 'undo');
    check(get(w, 'undo').disabled && !get(w, 'discard').disabled && !w.calls.some(c => c.method === 'POST'), 'undo requires loaded book metadata even when later status is applicable');
    await press(w, 'discard');
    check(w.calls.find(c => c.method === 'POST' && c.url.endsWith('/discard_undo')).body.expected_revision === '6'.repeat(64), 'failed metadata still permits explicit discard of the inspected exact record'); w.close();

    w = fresh(); const missingSourceReadback = w.API.get;
    w.undo = {available: true, can_undo: false, revision: '6'.repeat(64), reason: 'Source unavailable'};
    w.API.get = async url => { if (!url.endsWith('/undo')) { throw Error('source unavailable'); } return missingSourceReadback(url); };
    await press(w, 'load'); check(!get(w, 'discard').disabled && get(w, 'undo').disabled, 'source-unavailable stale undo remains explicitly discardable');
    await press(w, 'discard');
    check(JSON.stringify(w.calls.find(c => c.method === 'POST' && c.url.endsWith('/discard_undo')).body) === JSON.stringify({expected_revision: '6'.repeat(64)}), 'source-independent discard carries the exact saved record revision'); w.close();

    w = fresh(); w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)}; await load(w);
    text(w, 0, 0, 'UNSAVED ALICE'); text(w, 0, 1, 'NEW ALIAS'); text(w, 0, 2, 'NEW QUOTE');
    await press(w, 'discard'); await press(w, 'preview'); request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    check(request.context_revision === '7'.repeat(64) && !get(w, 'consent').checked, 'discard refreshes context and requires a new consent');
    check(request.proposal.entries[0].canonical_name === 'UNSAVED ALICE' && request.proposal.entries[0].aliases[0] === 'NEW ALIAS'
        && request.proposal.entries[0].evidence[0].source_quote === 'NEW QUOTE', 'discard saved undo preserves name alias and quote draft edits'); w.close();

    w = fresh(); w.undo = {available: true, can_undo: true, revision: '6'.repeat(64)}; await load(w);
    text(w, 0, 0, 'KEEP THIS DRAFT'); w.confirmed = false; await press(w, 'undo');
    check(!w.calls.some(c => c.method === 'POST' && c.url.endsWith('/undo')) && boxes(w)[0].querySelectorAll('textarea')[0].value === 'KEEP THIS DRAFT', 'undo respects refusal to discard an edited draft'); w.close();

    w = fresh(); await load(w); text(w, 0, 0, '<img src=x onerror=bad()>');
    get(w, 'import').files = [{size: 2 * 1024 ** 2 + 1, text: async () => { throw Error('must not read oversized file'); }}];
    get(w, 'import').dispatchEvent({type: 'change'}); await settle();
    check(get(w, 'status').textContent.includes('2 MiB'), 'file size bounded before reading');
    check(boxes(w)[0].querySelector('textarea').value.startsWith('<img'), 'bad import preserves draft');
    await press(w, 'preview'); check(!get(w, 'review').querySelector('img'), 'names and quotes render as text'); w.close();

    w = fresh(); await load(w);
    const imported = {schema: 1, source_sha256: '2'.repeat(64), source_characters: 100, entries: [entry(C, 'IMPORTED NAME')], retired_ids: []};
    get(w, 'import').files = [{size: 1000, text: async () => JSON.stringify(imported)}]; get(w, 'import').dispatchEvent({type: 'change'}); await settle();
    const mapping = get(w, 'import-review').querySelector('select'); mapping.value = A; mapping.dispatchEvent({type: 'change'});
    get(w, 'import-review').querySelector('button').click(); await settle(); await press(w, 'preview');
    request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    check(request.import_id_mapping[C] === A && request.imported_roster.entries[0].id === C, 'explicit import mapping reaches review with original IDs');
    check(request.proposal.entries.find(e => e.id === A).aliases.includes('IMPORTED NAME'), 'mapped import contributes visible reviewed alias');
    w.applyCurrentBookFilename('book-b.txt'); w.applyCurrentBookFilename('book-a.txt'); await load(w); await press(w, 'recover');
    check(!!get(w, 'import-review').querySelector('button'), 'restored import retains its cancellation control');
    get(w, 'import-review').querySelector('button').click(); await settle(); await press(w, 'preview');
    request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    check(request.imported_roster === null && !request.proposal.entries.find(e => e.id === A).aliases.length, 'cancel restored import exactly restores pre-import draft'); w.close();

    w = fresh(); await load(w);
    get(w, 'import').files = [{size: 1000, text: async () => JSON.stringify(imported)}]; get(w, 'import').dispatchEvent({type: 'change'}); await settle();
    const mergeMapping = get(w, 'import-review').querySelector('select'); mergeMapping.value = A; mergeMapping.dispatchEvent({type: 'change'});
    get(w, 'import-review').querySelector('button').click(); await settle();
    select(w, 0); select(w, 1); get(w, 'survivor').value = B;
    await press(w, 'merge'); await press(w, 'preview'); request = w.calls.findLast(c => c.url.endsWith('/preview')).body;
    check(request.import_id_mapping[C] === B && request.proposal.entries.length === 1 && request.proposal.entries[0].id === B,
        'explicit merge transfers imported mapping to its chosen surviving identity'); w.close();

    w = fresh(); w.meta.roster = null; w.meta.approval_current = false; await load(w);
    const longQuote = '😀'.repeat(4000), exported = {schema: 1, source_sha256: '2'.repeat(64), source_characters: 4000,
        entries: [{id: C, canonical_name: 'LONG QUOTE', aliases: [], evidence: [{source_quote: longQuote, start_char: 0, end_char: 4000}]}], retired_ids: []};
    get(w, 'import').files = [{size: 20000, text: async () => JSON.stringify(exported)}]; get(w, 'import').dispatchEvent({type: 'change'}); await settle();
    get(w, 'import-review').querySelector('button').click(); await settle();
    const importedQuote = boxes(w)[0].querySelectorAll('textarea')[2];
    check([...importedQuote.value].length === 4000 && importedQuote.value.length === 8000, '4000 Unicode-character quote imports without UTF16 truncation'); w.close();

    w = fresh(); await load(w); const malformed = clone(exported); malformed.entries[0].id = [C];
    get(w, 'import').files = [{size: 20000, text: async () => JSON.stringify(malformed)}]; get(w, 'import').dispatchEvent({type: 'change'}); await settle();
    check(!get(w, 'import-review').children.length && boxes(w).length === 2, 'non-string imported ID refused before changing draft'); w.close();

    console.log(JSON.stringify({harness: 'existing dependency-free Node test DOM, not browser QA', assertions, passed: true}));
})().catch(error => { console.error(error); process.exitCode = 1; });
