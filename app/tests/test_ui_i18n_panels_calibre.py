"""The Export/Audio panel modules, the Designer list and the Calibre flow translate.

Phase 9b keyed the user-facing text of app-export-quality.js, app-mastering.js,
app-publication.js, app-editor-repair.js, app-word-timing.js,
app-synthesis-windows.js, app-speech-verification.js, app-narration-markup.js,
app-audio-channels.js, app-calibre.js and the Designer's saved-voice list in
app-scripts.js. These tests run the real renderers against the real catalogs in
the existing dependency-free Node DOM harness, so they verify what a reader
sees:

  * the real export-quality card, sentence-gap report, Calibre provider block,
    Designer table and audio-channel picker paint each locale and restore the
    authored English;
  * a key missing from the catalog keeps its English fallback;
  * every new `safety.*` string keeps the authored English beneath the
    translation, and the Calibre flow renders its safety text through
    formatUiSafetyText.

Every assertion references a key the phase-9a tree does not have, so the whole
module fails on origin/feat/ui-i18n-p9a.
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
    ('safety.calibre.capture-start-unknown',
     '{label} outcome is UNKNOWN: the start response was lost or invalid, so the capture may have been accepted by the backend. No retry was sent and this attempt is not claimed failed or cancelled. Capture is disabled while this outcome is unknown; use Check status if a job token is shown, or explicitly save the catalog settings to retire this local view after a successful save. Saving does not claim the capture never ran or was cancelled, and a backend slot may still block a new capture until its worker cleans up.',
     {'label': 'Capture start'}),
    ('safety.calibre.import-unknown',
     'Import outcome is UNKNOWN: the import response was lost or invalid, so the authority may already have published the book. This is not claimed failed or imported, and no retry was sent. Cancel and discard are disabled until the actual status is safe; use Check status to reconcile this job token. A server status of captured means the import never reserved, imported confirms publication, and a terminal status is shown truthfully.',
     {}),
    ('safety.designer.discard-edits',
     'Discard unsaved Designer edits and open another voice? Save Voice first to keep them, or Cancel to continue editing.',
     {}),
    ('safety.m4b.publication.capture-stale',
     'Capture is from an earlier input; review it before use.', {}),
    ('safety.editor.markup.apply-confirm-keep',
     'Apply this preview to the saved Editor line? Keep the existing pause. The original text is retained in the application receipt. Existing take bytes stay on disk, but this changed line needs regeneration. Script and source are not rewritten. No audio is generated now.',
     {}),
    ('safety.editor.word-timing.cancel-unconfirmed',
     'Timing cancellation is unconfirmed', {}),
)


def js(value):
    return json.dumps(value, ensure_ascii=False)


PREAMBLE = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
document.documentElement=new Element('html');
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this._html??'';},set(value){this._html=String(value);}});
'''


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


class LocaleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def run_node(self, body, modules=(), setup=PREAMBLE, arguments=()):
        source = DOM_FIXTURE + setup
        for module in modules:
            source += f"vm.runInContext(fs.readFileSync({js(str(module))},'utf8'),context);\n"
        source += "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});\n"
        handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        try:
            with handle:
                handle.write(source)
            result = subprocess.run(['node', handle.name, *arguments], capture_output=True, text=True,
                                    encoding='utf-8', timeout=20)
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

    def slice_source(self, name, start, end):
        text = (STATIC / 'js' / name).read_text(encoding='utf-8')
        first = text.index(start)
        return text[first:text.index(end, first)]

    def assertTranslated(self, key):
        for locale in LOCALES:
            with self.subTest(key=key, locale=locale):
                self.assertNotEqual(self.catalogs[locale][key], self.english[key])


class RealRendererTests(LocaleTestCase):
    def test_the_export_quality_card_and_status_translate(self):
        for key in ('audio.export-quality.card', 'audio.export-quality.audacity',
                    'audio.export-quality.track', 'audio.export-quality.idle'):
            self.assertTranslated(key)
        report = {'file': 'book.mp3', 'status': 'verified', 'verification_scope': 'audacity_structure_and_frames',
                  'tracks_checked': 2, 'members_checked': 2, 'seconds': 1.5,
                  'track_summaries': [{'file': 'track.wav', 'frames': 24000, 'sample_rate': 24000, 'channels': 1}],
                  'sha256': 'abc123'}
        for index, locale in enumerate(LOCALES):
            cat = self.catalogs[locale]
            expected = [
                cat['audio.export-quality.card'].format(kind=cat['audio.export-quality.kind.completed'],
                                                         name='book.mp3', status='verified'),
                cat['audio.export-quality.validation-time'].format(seconds='1.500'),
                cat['audio.export-quality.audacity'].format(tracks=2, members=2),
                cat['audio.export-quality.track'].format(file='track.wav', frames=24000, rate=24000, channels=1),
                cat['audio.export-quality.sha'].format(sha='abc123'),
            ]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "const card=context.renderExportQualityReport(" + js(report) + ",false);\n"
                + "document.getElementById('export-quality-status').textContent='';\n"
                + "context.resetExportQualityView();\n"
                + "console.log(JSON.stringify({texts:card.children.map(node=>node.textContent),"
                + "status:document.getElementById('export-quality-status').textContent}));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-export-quality.js'),
                setup=PREAMBLE + "for(const id of ['export-quality-reports','export-quality-status']){body.appendChild(new Element('div',{id}));}\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(expected, payload['texts'])
            self.assertEqual(cat['audio.export-quality.idle'], payload['status'])

    def test_the_sentence_gap_report_translates(self):
        keys = ('editor.word-timing.gaps-heading', 'editor.word-timing.gaps-summary',
                'editor.word-timing.reason.explicit-user-pause', 'editor.word-timing.gaps-footer')
        for key in keys:
            self.assertTranslated(key)
        report = {'kind': 'sentence_gap_report_only', 'mutates_audio': False, 'authorizes_edit': False,
                  'proposals': [
                      {'status': 'proposal', 'proposed_extension_ms': 120,
                       'original_coordinates': {'left_sentence': {'char_start': 0, 'char_end': 3},
                                                'right_sentence': {'char_start': 4, 'char_end': 7}}},
                      {'status': 'skipped', 'skip_reason': 'explicit_user_pause',
                       'original_coordinates': {'left_sentence': {'char_start': 8, 'char_end': 9},
                                                'right_sentence': {'char_start': 10, 'char_end': 12}}},
                  ]}
        result_value = {'display_text': 'One. Two. Three. Four.', 'sentence_gap_report': report}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.renderSentenceGapReport(" + js(result_value) + ");\n"
                + "const area=document.getElementById('editor-sentence-gaps');\n"
                + "const list=area.children.find(node=>node.tagName==='OL');\n"
                + "console.log(JSON.stringify({texts:area.children.map(node=>node.textContent),"
                + "items:list?list.children.map(item=>item.children.map(line=>line.textContent)):[],"
                + "status:document.getElementById('editor-word-status').textContent}));",
                modules=(STATIC / 'js' / 'app-i18n.js', STATIC / 'js' / 'app-word-timing.js'),
                setup=PREAMBLE + "for(const id of ['editor-sentence-gaps','editor-word-status']){body.appendChild(new Element('div',{id}));}\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(cat['editor.word-timing.gaps-heading'], payload['texts'][0])
            self.assertEqual(cat['editor.word-timing.gaps-summary'].format(count=1), payload['texts'][1])
            self.assertIn(cat['editor.word-timing.gaps-suggested'].format(ms=120), payload['items'][0])
            self.assertIn(cat['editor.word-timing.gaps-no-suggestion'].format(
                reason=cat['editor.word-timing.reason.explicit-user-pause']), payload['items'][1])
            self.assertEqual(cat['editor.word-timing.gaps-footer'].format(suggested=1, skipped=1), payload['status'])

    def test_the_calibre_provider_block_translates(self):
        for key in ('calibre.provider.title', 'calibre.provider.authors', 'calibre.job.status',
                    'calibre.job.sha', 'calibre.entry.not-available'):
            self.assertTranslated(key)
        provider = {'title': 'A Book', 'authors': ['Ann', 'Bo'], 'series': 'Saga', 'series_index': 2,
                    'subtitle': 'Sub', 'format': 'epub'}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            expected = '\n'.join([
                cat['calibre.provider.title'].format(value='A Book'),
                cat['calibre.provider.authors'].format(value='Ann, Bo'),
                cat['calibre.provider.series'].format(value='Saga #2'),
                cat['calibre.provider.subtitle'].format(value='Sub'),
                cat['calibre.provider.format'].format(value='epub'),
            ])
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "console.log(JSON.stringify([context.calibreProviderText(" + js(provider) + "),"
                + "context.t('calibre.job.status','Status: {status}',{status:'captured'})]));",
                modules=(STATIC / 'js' / 'app-i18n.js',),
                setup=PREAMBLE + "vm.runInContext(" + js(self.slice_source(
                    'app-calibre.js', 'function calibreProviderText(', 'function calibreCopyProvider(')) + ",context);\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(expected, payload[0])
            self.assertEqual(cat['calibre.job.status'].format(status='captured'), payload[1])

    def test_the_designer_saved_voice_table_translates(self):
        for key in ('designer.saved.column.name', 'designer.saved.play-aria',
                    'designer.saved.delete', 'designer.saved.empty'):
            self.assertTranslated(key)
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "context.loadDesignedVoices().then(()=>{\n"
                + "console.log(JSON.stringify({html:document.getElementById('designed-voices-list').innerHTML,"
                + "empty:document.getElementById('designed-voices-list').innerHTML}));\n"
                + "});\n"
                + "await new Promise(resolve=>setImmediate(resolve));\n",
                modules=(STATIC / 'js' / 'app-i18n.js',),
                setup=PREAMBLE
                + "context.escapeHtml=value=>String(value);context.showToast=()=>{};\n"
                + "context.getInlineStringArgument=value=>JSON.stringify(String(value));\n"
                + "for(const id of ['designed-voices-list','designed-voices-load-status']){body.appendChild(new Element('div',{id}));}\n"
                + "context.API={get:async()=>[{id:'v1',name:'Narrator One',description:'Warm',filename:'v1.wav'}]};\n"
                + "vm.runInContext(" + js(self.slice_source(
                    'app-scripts.js', 'async function loadDesignedVoices()', 'function getDesignerFormSnapshot()')) + ",context);\n")
            html = json.loads(result.stdout.strip().splitlines()[-1])['html']
            self.assertIn(cat['designer.saved.column.name'], html)
            self.assertIn(cat['designer.saved.column.description'], html)
            self.assertIn(cat['designer.saved.column.actions'], html)
            self.assertIn(cat['designer.saved.play-aria'].format(name='Narrator One'), html)
            self.assertIn(cat['designer.saved.edit-aria'].format(name='Narrator One'), html)
            self.assertIn(cat['designer.saved.delete-aria'].format(name='Narrator One'), html)

    def test_the_audio_channel_picker_translates(self):
        for key in ('audio.channel.label', 'audio.channel.mix', 'audio.channel.preview',
                    'audio.channel.intro'):
            self.assertTranslated(key)
        file = {'name': 'take.wav'}
        for locale in LOCALES:
            cat = self.catalogs[locale]
            result = self.run_node(
                self.catalogs_js() + '\n'
                + f"context.applyUiLocale({js(locale)});\n"
                + "const host=body.appendChild(new Element('div'));\n"
                + "context.window.createAudioChannelPicker(host," + js(file) + ",'preparer');\n"
                + "const select=host.children[0].children[0];\n"
                + "console.log(JSON.stringify({label:host.children[0].textContent,"
                + "options:[...select.children].map(o=>o.textContent),"
                + "button:host.children[1].textContent,status:host.children[3].textContent,"
                + "aria:select.getAttribute('aria-label')}));",
                modules=(STATIC / 'js' / 'app-i18n.js',),
                setup=PREAMBLE + "vm.runInContext(" + js(
                    (STATIC / 'js' / 'app-audio-channels.js').read_text(encoding='utf-8')
                    .split("window.addEventListener('DOMContentLoaded'")[0]) + ",context);\n")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertEqual(cat['audio.channel.label'], payload['label'])
            self.assertEqual([cat['audio.channel.mix'], cat['audio.channel.left'], cat['audio.channel.right']],
                             payload['options'])
            self.assertEqual(cat['audio.channel.preview'], payload['button'])
            self.assertEqual(cat['audio.channel.intro'], payload['status'])
            self.assertEqual(cat['audio.channel.aria-label'].format(file='take.wav'), payload['aria'])


class FallbackTests(LocaleTestCase):
    def test_a_missing_catalog_key_keeps_its_english(self):
        key = 'audio.mastering.verified'
        self.assertNotEqual(self.catalogs['ja'][key], self.english[key])
        result = self.run_node(
            self.catalogs_js(drop=(key,)) + '\n'
            + "context.applyUiLocale('ja');\n"
            + "console.log(context.t(" + js(key) + "," + js(self.english[key]) + ",{count:2}));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        self.assertEqual(self.english[key].format(count=2), result.stdout.strip().splitlines()[-1])


class SafetyTextTests(LocaleTestCase):
    def test_every_new_safety_string_keeps_english_beneath_the_translation(self):
        for key, english, values in SAFETY_KEYS:
            for locale in LOCALES:
                with self.subTest(key=key, locale=locale):
                    self.assertNotEqual(self.catalogs[locale][key], english)
            rendered = english.format(**values) if values else english
            expected = {locale: self.catalogs[locale][key].format(**values) if values
                        else self.catalogs[locale][key] for locale in LOCALES}
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

    def test_the_modules_render_safety_text_through_the_safety_helper(self):
        expectations = (
            ('app-calibre.js', "formatUiSafetyText('safety.calibre.capture-start-unknown'"),
            ('app-calibre.js', "formatUiSafetyText('safety.calibre.import-unknown'"),
            ('app-calibre.js', "formatUiSafetyText('safety.calibre.preview.readonly'"),
            ('app-scripts.js', "formatUiSafetyText('safety.designer.discard-edits'"),
            ('app-publication.js', "formatUiSafetyText('safety.m4b.publication.book-changed'"),
            ('app-narration-markup.js', "formatUiSafetyText('safety.editor.markup.apply-confirm-keep'"),
            ('app-speech-verification.js', "formatUiSafetyText('safety.editor.speech.snapshot-changed'"),
            ('app-publication.js', "formatUiSafetyText('safety.m4b.publication.incomplete'"),
        )
        for name, needle in expectations:
            with self.subTest(file=name, needle=needle):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))


class KeyingTests(unittest.TestCase):
    def test_each_module_keys_its_user_facing_text(self):
        expectations = (
            ('app-export-quality.js', "t('audio.export-quality.summary'"),
            ('app-mastering.js', "t('audio.mastering.card'"),
            ('app-publication.js', "t('audio.m4b.publication.selected-input'"),
            ('app-editor-repair.js', "t('editor.repair.preview-summary'"),
            ('app-word-timing.js', "t('editor.word-timing.gaps-no-suggestion'"),
            ('app-synthesis-windows.js', "t('setup.audio.synthesis-window.range'"),
            ('app-speech-verification.js', "t('editor.speech.rechecking'"),
            ('app-narration-markup.js', "t('editor.markup.preview-only'"),
            ('app-audio-channels.js', "t('audio.channel.source-count'"),
            ('app-calibre.js', "t('calibre.status.capture-started'"),
            ('app-scripts.js', "t('designer.saved.load-failed'"),
            ('app-core.js', "t('setup.prompts.save-pass1-title', 'Save Step 1 prompt preset')"),
            ('app-core.js', "t('setup.prompts.save-pass3-title', 'Save Step 3 prompt preset')"),
            ('app-core.js', "t('setup.prompts.save-attribution-title'"),
            ('app-core.js', "t('setup.prompts.builtin-name-taken'"),
        )
        for name, needle in expectations:
            with self.subTest(file=name, needle=needle):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))

    def test_the_pass_titles_name_the_translated_step_not_the_pass_id(self):
        core = (STATIC / 'js' / 'app-core.js').read_text(encoding='utf-8')
        self.assertNotIn('Save ${pass} prompt preset', core)
        for locale in LOCALES:
            for key in ('setup.prompts.save-pass1-title', 'setup.prompts.save-pass3-title'):
                with self.subTest(key=key, locale=locale):
                    value = load(locale)[key]
                    self.assertNotIn('pass1', value)
                    self.assertNotIn('pass3', value)


if __name__ == '__main__':
    unittest.main()
