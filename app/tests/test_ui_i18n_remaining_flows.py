"""The last English-only UI flows translate, and their safety text keeps English.

Phase 9c keyed the guided single-narrator workflow (app-single-voice.js), the
speaker-label review panel (app-speaker-review.js), the source-scope editor
(app-project-scope.js), the translated-edition preview (app-translated-edition.js),
the TTS capability report (app-tts-capabilities.js), the TTS diagnostics list
(app-tts-diagnostics.js) and the saved-script library, audit panel and clone
import/cleanup flow in app-scripts.js.

These tests run the real renderers against the real catalogs in the existing
dependency-free Node DOM harness, so they verify what a reader sees:

  * the real source-preview status line, speaker-review reset notice, scope
    preview, translation proposal, capability report, diagnostics list, saved
    script row and audit panel paint each locale and restore the authored
    English;
  * a key missing from the catalog keeps its English fallback;
  * every new `safety.*` string keeps the authored English beneath the
    translation, and the confirm/safety sinks use the safety helper.

Every assertion references a key the phase-9b tree does not have, so the whole
module fails on origin/feat/ui-i18n-p9b.
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_ui_help_js import DOM_FIXTURE

STATIC = Path(__file__).resolve().parent.parent / 'static'
I18N = STATIC / 'i18n'
LOCALES = ('zh-CN', 'ja', 'es-419')

SAFETY_KEYS = (
    ('safety.single_voice.apply-confirm',
     'Use the {speaker} voice ({asset}) for all {speakers} speakers ({lines} lines)?\n{detail}\n\n'
     'Ready approvals are cleared for every affected speaker on this apply. Changed-voice lines '
     'become pending for re-render; already rendered take files are retained, not deleted. Voice '
     'approvals are reset for speakers whose effective voice changes.',
     {'speaker': 'ALICE', 'asset': 'custom voice Serena', 'speakers': 2, 'lines': 10, 'detail': 'ALICE: 4 lines · unchanged'}),
    ('safety.single_voice.save.unknown',
     'Voice save could not be confirmed: {detail} It may have been saved. Check Voices state before '
     'trying again; your edits are retained.', {'detail': 'timeout'}),
    ('safety.scope.undo.available',
     'An exact undo is available. Undo or discard it before applying another scope.', {}),
    ('safety.clone.cleanup.rights-required',
     'Enter the exact transcript and confirm rights before preparing a cleaned candidate.', {}),
    ('safety.clone.cleanup.accept-maybe-toast',
     'Cleanup acceptance may have completed even though its reply was lost. Do not accept or import '
     'again until you check the voice library. Your current selection is unchanged.', {}),
    ('safety.speaker_review.apply.confirm',
     'Save alias “{alias}” → “{canonical}” in the shared character registry? Later Review runs can '
     'merge these names using this registry. This does not rewrite the current script. The reference '
     'and model judgments are provisional. This single change makes the remaining report stale.',
     {'alias': 'Al', 'canonical': 'Alice'}),
    ('safety.translation.preview.stale',
     'Stale: the saved Script changed after this preview was built. Build a new preview.', {}),
    ('safety.script.saved.delete-confirm',
     'Delete saved script "{name}"? This cannot be undone.', {'name': 'Book'}),
)

PREAMBLE = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
Object.defineProperty(Element.prototype,'disabled',{get(){return this.hasAttribute('disabled');},set(value){if(value){this.setAttribute('disabled','');}else{this.removeAttribute('disabled');}}});
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this._html??'';},set(value){this._html=String(value);}});
const text=node=>node.textContent+node.children.map(text).join('');
'''


def js(value):
    return json.dumps(value, ensure_ascii=False)


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


class LocaleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def run_node(self, body, modules=(), setup=PREAMBLE):
        source = DOM_FIXTURE + setup
        for module in modules:
            source += f"vm.runInContext(fs.readFileSync({js(str(module))},'utf8'),context);\n"
        source += "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});\n"
        handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        try:
            with handle:
                handle.write(source)
            result = subprocess.run(['node', handle.name], capture_output=True, text=True,
                                    encoding='utf-8', timeout=30)
        finally:
            Path(handle.name).unlink(missing_ok=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def catalogs_js(self, drop=()):
        calls = []
        for locale in LOCALES:
            messages = {key: self.catalogs[locale][key] for key in self.catalogs[locale]
                        if not key.startswith('_') and key not in drop}
            payload = {'meta': {'review_status': {'all': 'machine-draft', 'safety': 'not-reviewed'}},
                       'messages': messages}
            calls.append("vm.runInContext('uiCatalogs.set(' + " + js(js(locale)) + " + ',' + "
                         + js(js(payload)) + " + ')', context);")
        return '\n'.join(calls)

    def assertTranslated(self, key):
        for locale in LOCALES:
            with self.subTest(key=key, locale=locale):
                self.assertNotEqual(self.catalogs[locale][key], self.english[key])


class RealRendererTests(LocaleTestCase):
    def test_the_single_voice_source_preview_translates(self):
        for key in ('single_voice.source.meta-truncated', 'single_voice.source.confirmed',
                    'single_voice.source.unconfirmed'):
            self.assertTranslated(key)
        preview = {'text_start': 'Alpha beta', 'source_name': 'book.txt',
                   'imported_source_name': 'book.docx', 'encoding': 'utf-8',
                   'total_chars': 200, 'truncated': True,
                   'source_sha256': 'a' * 64, 'book_token': 'b' * 64}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            expected = cat['single_voice.source.meta-truncated'].format(
                identity=cat['single_voice.source.identity.imported'].format(
                    imported='book.docx', retained='book.txt'),
                encoding='utf-8', total=200, shown=10, sha='a' * 16, token='b' * 12)
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "vm.runInContext('_singleVoiceSourcePreview = ' + " + js(js(preview)) + " + ';',context);\n"
                + "context.renderSingleVoiceSourcePreview();\n"
                + "console.log(JSON.stringify({meta:document.getElementById('source-preview-meta').textContent,"
                + "status:document.getElementById('source-preview-status').textContent}));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-single-voice.js'),
                setup=PREAMBLE + "context.currentBookFilename='book.txt';\n"
                + "context._isLiteralSourcePreservingChecked=()=>true;\n"
                + "for(const id of ['source-preview-text','source-preview-meta','source-preview-status','source-preview-confirm']){body.appendChild(new Element('div',{id}));}\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(expected, payload['meta'])
            self.assertEqual(cat['single_voice.source.unconfirmed'], payload['status'])

    def test_the_speaker_review_reset_notice_translates(self):
        for key in ('speaker_review.ready', 'speaker_review.source.entries'):
            self.assertTranslated(key)
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.resetSpeakerLabelReview();\n"
                + "console.log(document.getElementById('speaker-review-status').textContent);",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-speaker-review.js'),
                setup=PREAMBLE + "context.window=context;\n"
                + "context.clearTimeout=()=>{};\ncontext.setTimeout=()=>0;\n"
                + "context.currentBookFilename='book.json';\n"
                + "context.showConfirm=async()=>true;\n"
                + "context.getActionErrorMessage=(a,e,r)=>a+'. '+r+' Details: '+e.message;\n"
                + "context.clearCharacterAliases=()=>{};\n"
                + "for(const id of ['source','reference','max-pairs','partial','load','preview','consent','start','cancel','cancel-previous','refresh','results','options','consent-panel','book','source-help','status','profile','budget']){body.appendChild(new Element('div',{id:'speaker-review-'+id}));}\n")
            self.assertEqual(cat['speaker_review.ready'], result.stdout.strip().splitlines()[-1])

    def test_the_source_scope_preview_translates(self):
        for key in ('scope.preview.remove', 'scope.rows.file-kept', 'scope.rows.retained'):
            self.assertTranslated(key)
        plan = {'source_name': 'book.txt', 'revision': 'a' * 64, 'start_entry': 1,
                'source_range': {'removed_start_word': 0, 'retained_start_word': 3, 'end_word': 6},
                'removed_uids': ['old'], 'history_versions': 1,
                'removed_script': [{'speaker': 'A', 'text': 'gone'}],
                'retained': [{'uid': 'kept', 'speaker': 'B', 'text': 'kept text', 'audio_verified': True}],
                'voices': [{'uid': 'kept', 'effective_voices': {'B': {'voice': 'Ryan'}}}],
                'preserved_files': [{'path': 'old.wav', 'exists': True}],
                'notice': 'Older exports stay on disk.'}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.renderSourceScopePreview(" + js(plan) + ");\n"
                + "console.log(JSON.stringify(text(document.getElementById('source-scope-review')).length>0"
                + "?document.getElementById('source-scope-review').children.map(child=>text(child)):[]));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-project-scope.js'),
                setup=PREAMBLE + "for(const id of ['review','entry','search','find','next','preview','apply','undo','discard','undo-status','feedback','panel']){body.appendChild(new Element('div',{id:'source-scope-'+id}));}\n")
            texts = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(cat['scope.preview.remove'].format(
                source='book.txt', **{'from': 1, 'to': 3, 'keepFrom': 4, 'keepTo': 6}), texts[0])
            heading = cat['scope.rows-heading'].format(label=cat['scope.rows.removed'], count=1)
            self.assertTrue(any(item.startswith(heading) for item in texts), heading)

    def test_the_translated_edition_proposal_translates(self):
        for key in ('translation.proposal.heading', 'translation.row.source',
                    'translation.row.proposal', 'translation.row.meta'):
            self.assertTranslated(key)
        proposal = {'proposal_id': 'a' * 32, 'status': 'partial', 'stale': True,
                    'target_language': 'fr',
                    'counts': {'translated': 3, 'flagged': 1, 'unsupported': 0, 'failed': 1,
                               'skipped': 0, 'cancelled': 0},
                    'excerpt': {'row_count': 4, 'total_rows': 9},
                    'source_script_sha256': 'b' * 64,
                    'rows': [{'index': 0, 'speaker': 'A', 'status': 'translated', 'flags': [],
                              'source_text': 'Hello', 'translated_text': 'Bonjour', 'error': None}]}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.renderTranslatedEditionProposal(" + js(proposal) + ");\n"
                + "console.log(JSON.stringify(document.getElementById('translated-edition-review').children.map(child=>text(child))));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-translated-edition.js'),
                setup=PREAMBLE + "for(const id of ['review','pair','create','refresh','cancel','feedback','panel']){body.appendChild(new Element('div',{id:'translated-edition-'+id}));}\n")
            texts = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(cat['translation.proposal.heading'].format(
                status='partial', translated=3, flagged=1, unsupported=0, failed=1,
                rows=4, sha='b' * 12, target='fr'), texts[0])
            self.assertIn(cat['translation.row.source'].format(text='Hello'), texts[-1])
            self.assertIn(cat['translation.row.proposal'].format(text='Bonjour'), texts[-1])

    def test_the_tts_capability_report_translates(self):
        for key in ('setup.tts.cap.report.heading', 'setup.tts.cap.report.operation',
                    'setup.tts.cap.report.unsupported'):
            self.assertTranslated(key)
        report = {'fingerprint': 'schema-1',
                  'operations': {
                      'custom': {'status': 'supported', 'endpoint': 'custom-endpoint',
                                 'adapter': 'adapter-a', 'parameters': ['text'], 'missing': []},
                      'clone': {'status': 'unsupported', 'endpoint': None, 'adapter': None,
                                'parameters': ['ref_audio'], 'missing': ['ref_audio']}},
                  'unsupported_operations': ['design']}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            expected = cat['setup.tts.cap.report.operation'].format(
                operation='custom', status='supported', endpoint='custom-endpoint',
                adapter='adapter-a', required='text', missing=cat['setup.tts.cap.report.none-reported'])
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.renderTtsExternalCapabilitiesReport(" + js(report) + ");\n"
                + "console.log(JSON.stringify(document.getElementById('tts-external-capabilities-results').children.map(child=>child.textContent)));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-tts-capabilities.js'),
                setup=PREAMBLE + "for(const id of ['tts-external-capabilities-results','tts-external-capabilities-status','tts-external-capabilities-check','tts-mode','tts-url','tts-external-urls','tts-external-timeout','tts-external-server-number','tts-url-group','tts-external-timeout-group','tts-device-group','tts-local-options']){body.appendChild(new Element('div',{id}));}\n"
                + "context.getNumFieldValue=()=>300;\n")
            texts = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(cat['setup.tts.cap.report.heading'], texts[0])
            self.assertEqual(expected, texts[2])
            self.assertEqual(cat['setup.tts.cap.report.unsupported'].format(operations='design'), texts[4])

    def test_the_tts_diagnostics_list_translates(self):
        for key in ('tts_diag.none', 'tts_diag.residency.busy', 'tts_diag.device.available'):
            self.assertTranslated(key)
        report = {'sections': {'tts': {'records': [],
                                       'residency': {'state': 'busy', 'residents': None},
                                       'device': {'state': 'observed', 'gpu_backend': 'rocm'}}}}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.API={get:async()=>(" + js(report) + ")};\n"
                + "await context.refreshTtsDiagnostics();\n"
                + "console.log(JSON.stringify({status:document.getElementById('tts-diagnostics-status').textContent,"
                + "rows:document.getElementById('tts-diagnostics-records').children.map(child=>child.textContent)}));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-tts-diagnostics.js'),
                setup=PREAMBLE + "for(const id of ['tts-diagnostics-status','tts-diagnostics-records','tts-diagnostics-download','tts-diagnostics-preview']){body.appendChild(new Element('div',{id}));}\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(cat['tts_diag.none'], payload['status'])
            self.assertEqual(cat['tts_diag.residency.busy'], payload['rows'][0])
            self.assertEqual(cat['tts_diag.device.available'].format(backend='rocm'), payload['rows'][1])

    def test_the_saved_script_row_and_audit_panel_translate(self):
        for key in ('script.saved.audit', 'script.saved.load', 'script.saved.delete-aria',
                    'script.audit.counts', 'script.audit.no-findings'):
            self.assertTranslated(key)
        for locale in LOCALES:
            cat = self.catalogs[locale]
            core = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
            helpers = core[core.index('function escapeHtml('):core.index('// Parse a numeric input')]
            scripts = (STATIC / 'js' / 'app-scripts.js').read_text(encoding='utf-8')
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.API={post:async()=>({counts:{blocking:0,manual_review:0},findings:[]})};\n"
                + "await context.auditSavedScript('Book');\n"
                + "await context.loadSavedScripts();\n"
                + "console.log(JSON.stringify({panel:document.getElementById('saved-script-preflight').innerHTML,"
                + "rows:document.getElementById('saved-scripts-list').innerHTML}));",
                modules=(STATIC / 'js' / 'app-i18n.js',),
                setup=PREAMBLE + "context.currentBookFilename='book.json';\n"
                + "context.getActionErrorMessage=(a,e,r)=>a+'. '+r+' Details: '+e.message;\n"
                + "context.getIntlUiLocale=()=> 'en';\n"
                + "for(const id of ['saved-script-preflight','saved-scripts-list']){body.appendChild(new Element('div',{id}));}\n"
                + "vm.runInContext(" + js(helpers) + ",context);\n"
                + "vm.runInContext(" + js(scripts[scripts.index('async function loadSavedScripts()'):scripts.index('async function saveScript()')]) + ",context);\n"
                + "context._loadScriptList=async(id,callback)=>callback([{name:'Book',created:0,has_voice_config:true}]);\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertIn(cat['script.saved.audit'], payload['rows'])
            self.assertIn(cat['script.saved.load'], payload['rows'])
            self.assertIn(cat['script.saved.delete-aria'].format(name='Book'), payload['rows'])
            self.assertIn(cat['script.audit.counts'].format(blocking=0, manual=0), payload['panel'])
            self.assertIn(cat['script.audit.no-findings'], payload['panel'])


class FallbackTests(LocaleTestCase):
    def test_a_missing_catalog_key_keeps_its_english(self):
        key = 'script.audit.counts'
        self.assertNotEqual(self.catalogs['ja'][key], self.english[key])
        result = self.run_node(
            self.catalogs_js(drop=(key,)) + '\n'
            + "context.applyUiLocale('ja');\n"
            + "console.log(context.t(" + js(key) + "," + js(self.english[key]) + ",{blocking:1,manual:2}));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        self.assertEqual(self.english[key].format(blocking=1, manual=2),
                         result.stdout.strip().splitlines()[-1])


class SafetyTextTests(LocaleTestCase):
    def test_every_new_safety_string_keeps_english_beneath_the_translation(self):
        for key, english, values in SAFETY_KEYS:
            for locale in LOCALES:
                with self.subTest(key=key, locale=locale):
                    self.assertNotEqual(self.catalogs[locale][key], english)
            rendered = english.format(**values)
            expected = {locale: self.catalogs[locale][key].format(**values) for locale in LOCALES}
            result = self.run_node(
                self.catalogs_js() + '\nconst seen={};\n'
                + "for(const locale of ['zh-CN','ja','es-419','en']){context.applyUiLocale(locale);\n"
                + f"seen[locale]=context.formatUiSafetyText({js(key)},{js(english)},{js(values)});}}\n"
                + "console.log(JSON.stringify(seen));",
                modules=(STATIC / 'js' / 'app-i18n.js',))
            seen = json.loads(result.stdout.strip().splitlines()[-1])
            for locale in LOCALES:
                self.assertEqual(expected[locale] + '\n' + rendered, seen[locale])
            self.assertEqual(rendered, seen['en'])

    def test_the_flows_render_safety_text_through_the_safety_helper(self):
        expectations = (
            ('app-single-voice.js', "formatUiSafetyText('safety.single_voice.save.unknown'"),
            ('app-single-voice.js', "formatUiSafetyText('safety.single_voice.apply-confirm'"),
            ('app-speaker-review.js', "formatUiSafetyText('safety.speaker_review.apply.confirm'"),
            ('app-project-scope.js', "formatUiSafetyText('safety.scope.undo.available'"),
            ('app-project-scope.js', "formatUiSafetyText('safety.scope.applied'"),
            ('app-translated-edition.js', "formatUiSafetyText('safety.translation.preview.stale'"),
            ('app-scripts.js', "formatUiSafetyText('safety.clone.cleanup.prepared'"),
            ('app-scripts.js', "formatUiSafetyText('safety.script.saved.load-confirm'"),
            ('app-scripts.js', "formatUiSafetyText('safety.clone.delete'"),
        )
        for name, needle in expectations:
            with self.subTest(file=name, needle=needle):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))


class KeyingTests(LocaleTestCase):
    def test_each_flow_keys_its_user_facing_text(self):
        expectations = (
            ('app-single-voice.js', "t('single_voice.source.meta'"),
            ('app-single-voice.js', "t('safety.single_voice.recovery.nothing'"),
            ('app-speaker-review.js', "t('speaker_review.budget'"),
            ('app-speaker-review.js', "t('safety.speaker_review.report.stale'"),
            ('app-project-scope.js', "t('scope.choices'"),
            ('app-translated-edition.js', "t('translation.proposal.heading'"),
            ('app-tts-capabilities.js', "t('setup.tts.cap.report.heading'"),
            ('app-tts-diagnostics.js', "t('tts_diag.record'"),
            ('app-scripts.js', "t('script.audit.counts'"),
            ('app-scripts.js', "t('script.saved.list-empty'"),
            ('app-scripts.js', "t('clone.cleanup.plan'"),
        )
        for name, needle in expectations:
            with self.subTest(file=name, needle=needle):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))

    def test_the_binding_consistency_terms_hold_in_the_new_values(self):
        ja = self.catalogs['ja']
        zh = self.catalogs['zh-CN']
        for key in ('single_voice.catalog.designed-voices', 'single_voice.catalog.clone-voices',
                    'single_voice.placeholder', 'single_voice.book-unavailable',
                    'speaker_review.phase.suggestions'):
            with self.subTest(key=key):
                self.assertNotIn('ボイス', ja[key].replace('ボイスラボ', '').replace('ボイスデザイン', ''))
                self.assertNotIn('エクスポート', ja[key])
                self.assertNotIn('配音', zh[key])

    def test_no_spanish_count_takes_a_plural_noun(self):
        """A count that can be 1 must not be followed by a plural noun."""
        es = self.catalogs['es-419']
        for key in ('translation.reason.flagged', 'translation.reason.unsupported',
                    'translation.reason.failed', 'translation.reason.skipped',
                    'translation.reason.cancelled'):
            with self.subTest(key=key):
                value = es[key]
                self.assertNotRegex(value, r'\{count\}\s+\w+(a|o)s\b')


if __name__ == '__main__':
    unittest.main()
