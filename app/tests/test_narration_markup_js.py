"""Opt-in markup boundaries using real Editor selection and live draft checks."""
from pathlib import Path
from html.parser import HTMLParser
import subprocess
import unittest

from tests.test_editor_review_js import DOM_FIXTURE, SETUP


STATIC = Path(__file__).resolve().parent.parent / 'static'
MARKUP_SETUP = r'''
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]),'app-narration-markup.js'),'utf8'),context);
context.cachedChunks[1].text='Saved words {pause:500}';fields.get('u1').text.value=context.cachedChunks[1].text;
context.selectEditorReviewLineById(1);
const posts=[],confirmations=[],loads=[];
function report(overrides={}){return Object.assign({book_token:context.chunkSnapshotBookToken,uid:'u1',binding:'f'.repeat(64),original_text:'Saved words {pause:500}',spoken_text:'Saved words',pause_after:500,existing_pause:null,spans:[],changed:true,conflict:false},overrides);}
context.API.post=async(url,payload)=>{posts.push({url,payload});return report();};
context.showConfirm=async(...args)=>{confirmations.push(args);return true;};
context.loadChunks=async(...args)=>loads.push(args);
const original=JSON.stringify(context.cachedChunks),originalFields=JSON.stringify([...fields].map(([uid,controls])=>[uid,Object.values(controls).map(field=>field.value)]));
function unchanged(){assert.equal(JSON.stringify(context.cachedChunks),original);assert.equal(JSON.stringify([...fields].map(([uid,controls])=>[uid,Object.values(controls).map(field=>field.value)])),originalFields);assert.equal(generates.length,0);assert.equal(plays.length,0);}
function node(id){return document.getElementById('editor-line-markup-'+id);}
function applies(){return posts.filter(post=>post.url.endsWith('/apply'));}
'''


class NarrationMarkupJsTests(unittest.TestCase):
    def test_real_html_exposes_both_conflict_choices_and_control_help(self):
        class MarkupOptions(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inside = False
                self.values = []
                self.controls = {}

            def handle_starttag(self, tag, attributes):
                attrs = dict(attributes)
                identifier = attrs.get('id', '')
                if identifier.startswith('editor-line-markup-'):
                    self.controls[identifier] = attrs
                if tag == 'select':
                    self.inside = identifier == 'editor-line-markup-choice'
                if self.inside and tag == 'option':
                    self.values.append(attrs.get('value'))

            def handle_endtag(self, tag):
                if tag == 'select':
                    self.inside = False

        parsed = MarkupOptions()
        parsed.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        self.assertEqual(['', 'keep', 'replace'], parsed.values)
        for name in ('check', 'choice', 'apply', 'cancel'):
            self.assertTrue(parsed.controls['editor-line-markup-' + name].get('title'))

    def test_success_survives_a_poll_revision_change_without_touching_drafts(self):
        self.run_js(r'''
await context.previewNarrationMarkup();let release;
context.API.post=(url,payload)=>{posts.push({url,payload});return new Promise(resolve=>release=resolve);};
const pending=context.applyNarrationMarkup();await tick();
context.chunkSnapshotRevision='d'.repeat(64);context.applyNarrationMarkupInspector();
release({status:'applied',uid:'u1'});await pending;
assert(node('status').textContent.includes('Applied once'));assert.deepEqual(loads,[[true]]);unchanged();
''')

    def run_js(self, script):
        result = subprocess.run(
            ['node', '-e', DOM_FIXTURE + SETUP + MARKUP_SETUP + script
             + "\ncompleted=true;})().catch(error=>{console.error(error);process.exitCode=1;});",
             str(STATIC / 'js/app-editor-review.js'), str(STATIC / 'index.html')],
            capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_read_only_preview_discard_and_decline(self):
        self.run_js(r'''
await context.previewNarrationMarkup();unchanged();assert.equal(posts.length,1);
assert.equal(posts[0].url,'/api/chunks/1/narration-markup/preview');assert.equal(posts[0].payload.expected_uid,'u1');assert.equal(posts[0].payload.expected_book_token,context.chunkSnapshotBookToken);
assert(node('preview').textContent.includes('Saved words {pause:500}'));assert.equal(confirmations.length,0);
context.showConfirm=async(...args)=>{confirmations.push(args);return false;};await context.applyNarrationMarkup();assert.equal(confirmations.length,1);assert.equal(applies().length,0);assert.equal(loads.length,0);unchanged();
context.cancelNarrationMarkup();assert.equal(node('preview').textContent,'');assert(node('apply').disabled);await context.applyNarrationMarkup();assert.equal(applies().length,0);unchanged();
''')

    def test_conflict_requires_explicit_keep_or_replace(self):
        for decision in ('keep', 'replace'):
            with self.subTest(decision=decision):
                self.run_js(r'''
context.API.post=async(url,payload)=>{posts.push({url,payload});return report({conflict:true,existing_pause:200});};
await context.previewNarrationMarkup();assert(!node('choice').hidden);assert(node('apply').disabled);
for(const invalid of ['', 'apply', 'unexpected']){node('choice').value=invalid;await context.applyNarrationMarkup();}
assert.equal(applies().length,0);assert.equal(confirmations.length,0);
node('choice').value=DECISION;context.applyNarrationMarkupInspector();assert(!node('apply').disabled);
await context.applyNarrationMarkup();assert.equal(applies().length,1);assert.equal(applies()[0].payload.decision,DECISION);assert.equal(confirmations.length,1);assert.deepEqual(loads,[[true]]);unchanged();
'''.replace('DECISION', repr(decision)))

    def test_apply_waits_for_confirmation_and_exact_binding(self):
        self.run_js(r'''
await context.previewNarrationMarkup();let release;context.showConfirm=(...args)=>{confirmations.push(args);return new Promise(resolve=>release=resolve);};
const applying=context.applyNarrationMarkup();await tick();assert.equal(applies().length,0);assert(node('apply').disabled);
await context.applyNarrationMarkup();assert.equal(confirmations.length,1);release(true);await applying;
assert.equal(applies().length,1);assert.equal(JSON.stringify(applies()[0].payload),JSON.stringify({expected_book_token:context.chunkSnapshotBookToken,expected_uid:'u1',binding:'f'.repeat(64),decision:'apply'}));
await context.applyNarrationMarkup();assert.equal(applies().length,1);assert.deepEqual(loads,[[true]]);unchanged();
''')

    def test_drafts_and_pending_edits_refuse_preview_and_apply(self):
        for change in ("fields.get('u1').text.value='Draft';",
                       "fields.get('u1').pause_after.value='900';",
                       "context.isEditorEditPending=()=>true;"):
            with self.subTest(change=change):
                self.run_js(r'''
await context.previewNarrationMarkup();CHANGE
context.applyNarrationMarkupInspector();await context.applyNarrationMarkup();await context.previewNarrationMarkup();
assert.equal(posts.length,1);assert.equal(applies().length,0);assert.equal(confirmations.length,0);assert(node('check').disabled);assert(node('apply').disabled);assert.equal(JSON.stringify(context.cachedChunks),original);assert.equal(generates.length,0);
'''.replace('CHANGE', change))

    def test_stale_selection_book_revision_or_text_invalidates_preview(self):
        for change in ("context.selectEditorReviewLineById(0);",
                       "context.chunkSnapshotBookToken='c'.repeat(64);",
                       "context.chunkSnapshotRevision='d'.repeat(64);",
                       "context.cachedChunks[1].text='Changed';"):
            with self.subTest(change=change):
                self.run_js(r'''
await context.previewNarrationMarkup();CHANGE
context.applyNarrationMarkupInspector();await context.applyNarrationMarkup();assert.equal(applies().length,0);assert.equal(confirmations.length,0);assert(node('apply').disabled);assert.equal(node('preview').textContent,'');
'''.replace('CHANGE', change))

    def test_late_preview_cannot_paint_after_invalidation(self):
        for change in ("context.cancelNarrationMarkup();",
                       "context.selectEditorReviewLineById(0);",
                       "context.chunkSnapshotBookToken='c'.repeat(64);"):
            with self.subTest(change=change):
                self.run_js(r'''
let release;const oldReport=report();context.API.post=async(url,payload)=>{posts.push({url,payload});return new Promise(resolve=>release=resolve);};
const preview=context.previewNarrationMarkup();await tick();CHANGE
release(oldReport);await preview;await context.applyNarrationMarkup();assert.equal(node('preview').textContent,'');assert(node('apply').disabled);assert.equal(applies().length,0);assert.equal(confirmations.length,0);assert(!run('narrationMarkupBusy'));unchanged();
'''.replace('CHANGE', change))

    def test_confirmation_invalidation_prevents_apply_request(self):
        for change in ("context.cancelNarrationMarkup();",
                       "context.selectEditorReviewLineById(0);",
                       "context.chunkSnapshotBookToken='c'.repeat(64);",
                       "context.chunkSnapshotRevision='d'.repeat(64);",
                       "fields.get('u1').text.value='Draft';"):
            with self.subTest(change=change):
                self.run_js(r'''
await context.previewNarrationMarkup();let release;context.showConfirm=()=>new Promise(resolve=>release=resolve);
const applying=context.applyNarrationMarkup();await tick();CHANGE
release(true);await applying;assert.equal(applies().length,0);assert.equal(loads.length,0);assert(node('apply').disabled);assert(!run('narrationMarkupBusy'));
'''.replace('CHANGE', change))

    def test_malformed_preview_never_enables_apply(self):
        for malformed in ('null', '{}', "report({binding:'bad'})",
                          "report({uid:'other'})", "report({book_token:'other'})",
                          'report({spoken_text:42})', 'report({spans:null})'):
            with self.subTest(malformed=malformed):
                self.run_js(r'''
context.API.post=async(url,payload)=>{posts.push({url,payload});return MALFORMED;};
await context.previewNarrationMarkup();await context.applyNarrationMarkup();assert(node('apply').disabled);assert.equal(applies().length,0);assert.equal(confirmations.length,0);assert.equal(loads.length,0);assert(node('status').textContent);assert(!run('narrationMarkupBusy'));unchanged();
'''.replace('MALFORMED', malformed))

    def test_unchanged_report_has_no_apply_path(self):
        self.run_js(r'''
context.API.post=async(url,payload)=>{posts.push({url,payload});return report({changed:false});};
await context.previewNarrationMarkup();await context.applyNarrationMarkup();assert(node('apply').disabled);assert.equal(applies().length,0);assert.equal(confirmations.length,0);unchanged();
''')

    def test_apply_error_restores_controls_and_requests_receipt_inspection(self):
        self.run_js(r'''
await context.previewNarrationMarkup();context.API.post=async(url,payload)=>{posts.push({url,payload});throw new Error('Synthetic apply refusal');};
await context.applyNarrationMarkup();assert.equal(applies().length,1);assert.equal(loads.length,0);assert(node('status').textContent.includes('Synthetic apply refusal'));assert(node('status').textContent.includes('inspect the saved receipt'));
assert(!run('narrationMarkupBusy'));assert(!run('narrationMarkupApplying'));assert(!node('cancel').disabled);unchanged();
''')
