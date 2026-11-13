"""Regression coverage for PR 1936's editor review findings."""
from html.parser import HTMLParser
import unittest

from tests import test_editor_review_js as review_ui


WAVEFORM = r'''
const canvas=document.getElementById('editor-line-waveform');
canvas.tagName='CANVAS';canvas.clientWidth=200;canvas.clientHeight=48;canvas.hidden=true;
let draws=0,decodeCalls=0,resize;
canvas.getContext=()=>({setTransform(){},clearRect(){draws++;},fillRect(){}});
context.window.devicePixelRatio=2;
context.window.ResizeObserver=class {constructor(callback){resize=callback;}observe(node){assert.equal(node,canvas);}};
context.getComputedStyle=()=>({position:'sticky',getPropertyValue:()=> '#123456'});
const retry=document.getElementById('editor-line-waveform-retry')||el('editor-line-waveform-retry','button');retry.hidden=true;
context.decodeEditorWaveformPeaks=async()=>{decodeCalls++;return [[-0.5,0.5],[0,1]];};
'''

DRAWER = r'''
Object.defineProperty(Element.prototype,'inert',{get(){return this.hasAttribute('inert');},set(value){if(value){this.setAttribute('inert','');}else{this.removeAttribute('inert');}}});
Object.defineProperty(Element.prototype,'offsetParent',{get(){for(let n=this;n;n=n.parentElement){if(n.hidden||n.style.display==='none'){return null;}}return body;}});
const editor=el('editor-tab'),card=el('editor-card','div',editor);
const toggle=el('editor-tools-toggle','button',card),scrim=el('editor-drawer-scrim','div',card);
const drawer=el('editor-tool-drawer','section',card);drawer.hidden=true;
const all=document.querySelectorAll;document.querySelectorAll=selector=>selector.startsWith('#editor-tool-drawer ')?drawer.querySelectorAll(selector.slice('#editor-tool-drawer '.length)):all(selector);
const first=el('drawer-tab','button',drawer);first.setAttribute('data-editor-tool-target','sound-assets');
const panel=el('drawer-panel','details',drawer);panel.setAttribute('data-editor-tool','sound-assets');
const last=el('drawer-last','button',panel),disabled=el('drawer-disabled','button',panel);disabled.disabled=true;
const disabledGroup=el('disabled-group','fieldset',drawer);disabledGroup.disabled=true;el('disabled-descendant','button',disabledGroup);
const background=el('background','section',card);background.setAttribute('aria-hidden','false');
const outside=el('outside','button',background),preserved=el('already-inert');preserved.inert=true;preserved.setAttribute('aria-hidden','true');
function key(key,shiftKey=false){const event={key,shiftKey,prevented:false,preventDefault(){this.prevented=true;}};for(const fn of document.listeners.keydown||[]){fn(event);}return event;}
'''


class EditorReviewRepairTests(unittest.TestCase):
    run_js = review_ui.EditorReviewJsTests.run_js

    def test_space_does_not_play_audio_rejected_by_the_inspector(self):
        self.run_js(r'''
el('editor-tab').style.display='block';
context.API.get=async()=>{const data=facts();data.rows[1].audio='missing';return data;};
await context.refreshEditorReview();context.selectEditorReviewLineById(1);document.activeElement=body;
assert(document.getElementById('editor-line-play').disabled);
const audio=rows.get('u1').querySelector('.chunk-audio');audio.paused=true;
const event={code:'Space',preventDefault(){}};for(const fn of document.listeners.keydown||[]){fn(event);}
await tick();assert.equal(plays.length,0,'Space must obey the inspector playback guard');assert.equal(stops,0);
''')

    def test_waveform_resize_redraws_cached_peaks_without_fetching(self):
        self.run_js(WAVEFORM + r'''
context.selectEditorReviewLineById(1);await tick();assert.equal(canvas.width,400);assert.equal(decodeCalls,1);
assert.equal(typeof resize,'function','a layout observer must redraw the sketch');
canvas.clientWidth=320;resize();assert.equal(canvas.width,640);assert.equal(decodeCalls,1);assert.equal(draws,2);
''')

    def test_waveform_failure_has_an_explicit_retry_without_a_keystroke_storm(self):
        self.run_js(WAVEFORM + r'''
context.decodeEditorWaveformPeaks=async()=>{decodeCalls++;throw Error('offline');};
context.selectEditorReviewLineById(1);await tick();assert.equal(decodeCalls,1);assert(!retry.hidden,'the reader can recover from a transient failure');
context.renderEditorLineInspector();context.renderEditorLineInspector();await tick();assert.equal(decodeCalls,1,'typing must not retry the whole download');
context.decodeEditorWaveformPeaks=async()=>{decodeCalls++;return [[0,1]];};
await context.retryEditorLineWaveform();assert.equal(decodeCalls,2);assert(retry.hidden);assert(!canvas.hidden);
assert(document.getElementById('editor-line-waveform-status').textContent.includes('Peaks'));
''')

    def test_late_decodes_cannot_evict_the_visible_waveform_before_resize(self):
        self.run_js(WAVEFORM + r'''
context.selectEditorReviewLineById(1);await tick();
const waiting=[];context.decodeEditorWaveformPeaks=()=>new Promise(resolve=>waiting.push(resolve));
const chunk=context.cachedChunks[1];
for(let i=0;i<25;i++){chunk.audio_revision='pending-'+i;context.renderEditorLineInspector();}
delete chunk.audio_revision;context.renderEditorLineInspector();
for(const resolve of waiting){resolve([[0,1]]);}await tick();
canvas.clientWidth=320;resize();assert.equal(canvas.width,640,'late cache fills must retain the displayed take for resizing');
''')

    def test_pending_waveform_is_shared_and_cannot_resurrect_a_cleared_selection(self):
        self.run_js(WAVEFORM + r'''
let resolve;context.decodeEditorWaveformPeaks=()=>{decodeCalls++;return new Promise(done=>resolve=done);};
context.selectEditorReviewLineById(1);context.renderEditorLineInspector();assert.equal(decodeCalls,1,'one decode per take may be in flight');
context.selectEditorReviewLineById(0);resolve([[0,1]]);await tick();
assert(document.getElementById('editor-line-waveform-wrap').hidden);assert(canvas.hidden);
assert.equal(document.getElementById('editor-line-waveform-status').textContent,'');assert(retry.hidden);
''')

    def test_drawer_isolates_background_and_restores_existing_attributes(self):
        self.run_js(DRAWER + r'''
context.toggleEditorTools(true);assert.equal(document.activeElement,first);
assert(background.inert,'the page behind the drawer must not receive interaction');assert.equal(background.getAttribute('aria-hidden'),'true');
assert(!drawer.inert);assert(!editor.inert);assert(!scrim.inert,'the scrim remains clickable');
context.toggleEditorTools(false);assert(!background.inert);assert.equal(background.getAttribute('aria-hidden'),'false');
assert(preserved.inert);assert.equal(preserved.getAttribute('aria-hidden'),'true');assert.equal(document.activeElement,toggle);
''')

    def test_drawer_skips_disabled_controls_and_recaptures_outside_focus(self):
        self.run_js(DRAWER + r'''
context.toggleEditorTools(true);last.focus();assert(key('Tab').prevented);assert.equal(document.activeElement,first);
first.focus();assert(key('Tab',true).prevented);assert.equal(document.activeElement,last);
document.activeElement=outside;for(const fn of document.listeners.focusin||[]){fn({target:outside});}assert.equal(document.activeElement,first);
assert(key('Escape').prevented);assert(drawer.hidden);assert.equal(document.activeElement,toggle);
''')

    def test_opening_a_shared_modal_releases_the_drawer_background(self):
        self.run_js(DRAWER + r'''
context.toggleEditorTools(true);
for(const fn of document.listeners['show.bs.modal']||[]){fn({target:el('shared-modal')});}
assert(drawer.hidden,'a Bootstrap modal must not open behind an inert ancestor');assert(!background.inert);
''')

    def test_editor_body_and_tools_remain_inside_the_editor_card(self):
        class DivParents(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []
                self.parents = {}
                self.stray = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if attrs.get('id'):
                    self.parents[attrs['id']] = list(self.stack)
                if tag == 'div':
                    self.stack.append(attrs)

            def handle_endtag(self, tag):
                if tag == 'div':
                    if self.stack:
                        self.stack.pop()
                    else:
                        self.stray.append(self.getpos()[0])

        parser = DivParents()
        parser.feed((review_ui.STATIC / 'index.html').read_text(encoding='utf-8'))
        for identifier in ('chunks-table-body', 'editor-tool-drawer', 'editor-line-content'):
            with self.subTest(identifier=identifier):
                classes = [attrs.get('class', '').split() for attrs in parser.parents[identifier]]
                self.assertTrue(any('card' in tokens for tokens in classes), 'editor controls must retain their card ancestor')
        self.assertEqual([], parser.stray, 'unexpected closing divs must not silently reparent the page')
