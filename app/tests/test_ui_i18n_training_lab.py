"""The Training, Preparer, Voice Lab, Dataset and Designer surfaces render in every locale.

P8 keyed the training, preparer, voicelab, dataset-builder and designer panes
and the user-facing text of app-training.js, app-voicelab.js and the
non-Dataset-Builder sections of app-workbench.js (Preparer outputs, the system
status/ETA strip, the LM Studio toggle and task-control restoration), plus the
one task-status label map those panes share. These tests run the real catalogs
and real module code against the existing dependency-free Node DOM harness, so
they verify what a reader actually sees:

  * sampled pane nodes paint each locale and restore the authored English;
  * an authored placeholder follows the locale;
  * the real Voice Lab stage label, task-status label, duration unit and dataset
    sample count translate;
  * the destructive training confirms keep the English underneath the
    translation.

Every test references at least one key the P7 tree does not have, so all of
them fail on origin/feat/ui-i18n-p7.
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

MARKUP_KEYS = (
    'training.header.title',
    'training.config.title',
    'training.help.title',
    'preparer.header.title',
    'preparer.advanced.title',
    'preparer.outputs.title',
    'voicelab.header.title',
    'voicelab.stages.title',
    'dataset.header.title',
    'dataset.table.emotion',
    'designer.header.title',
    'designer.saved.title',
)
PLACEHOLDER_KEY = 'preparer.model.placeholder'
SAFETY_CONFIRMS = (
    ('safety.training.dataset.delete',
     'Delete dataset "{name}"? The uploaded ZIP and its samples are removed. This cannot be undone.', {'name': 'set-a'}),
    ('safety.training.delete-adapter', 'Delete this trained adapter? This cannot be undone.', {}),
    ('safety.training.delete-backup',
     'Permanently delete this rollback backup? You will no longer be able to restore the pre-promotion checkpoint.', {}),
    ('safety.training.promote',
     'Promote {candidate}? The current production checkpoint will be preserved for rollback.', {'candidate': 'ckpt-900'}),
)


def js(value):
    return json.dumps(value, ensure_ascii=False)


PREAMBLE = r'''
Element.prototype.append=function(...children){for(const child of children){this.appendChild(child);}};
Element.prototype.replaceChildren=function(...children){for(const child of [...this.children]){child.remove();}for(const child of children){this.appendChild(child);}};
Element.prototype.focus=function(){document.activeElement=this;};
document.documentElement=new Element('html');
Object.defineProperty(Element.prototype,'innerHTML',{get(){return this._html??'';},set(value){this._html=String(value);}});
function addNode(attrs,text){const node=body.appendChild(new Element('div',attrs));node.textContent=text;return node;}
'''


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


class LocaleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = load('en')
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def run_node(self, body, modules=(), setup=''):
        source = DOM_FIXTURE + PREAMBLE + setup
        for module in modules:
            source += f"vm.runInContext(fs.readFileSync({js(str(module))},'utf8'),context);\n"
        source += "\n(async()=>{\n" + body + "\n})().catch(error=>{console.error(error);process.exitCode=1;});\n"
        handle = tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8')
        try:
            with handle:
                handle.write(source)
            result = subprocess.run(['node', handle.name], capture_output=True, text=True,
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
        first, second = text.index(start), text.index(end, text.index(start))
        return text[first:second]


class MarkupRenderTests(LocaleTestCase):
    def test_training_and_lab_panes_paint_each_locale_and_restore_english(self):
        for locale in LOCALES:
            for key in MARKUP_KEYS:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        expected = {locale: {key: self.catalogs[locale][key] for key in MARKUP_KEYS} for locale in LOCALES}
        expected['en'] = {key: self.english[key] for key in MARKUP_KEYS}
        self.run_node(
            self.catalogs_js() + '\n'
            + ''.join(f"addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n" for key in MARKUP_KEYS)
            + "const nodes=body.querySelectorAll('[data-i18n]');\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=Object.fromEntries(nodes.map(node=>[node.getAttribute('data-i18n'),node.textContent]));\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});\n"
            + f"assert.strictEqual(nodes.length,{len(MARKUP_KEYS)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_a_preparer_placeholder_follows_the_locale(self):
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][PLACEHOLDER_KEY], self.english[PLACEHOLDER_KEY])
        expected = {locale: self.catalogs[locale][PLACEHOLDER_KEY] for locale in LOCALES}
        expected['en'] = self.english[PLACEHOLDER_KEY]
        self.run_node(
            self.catalogs_js() + '\n'
            + f"const input=body.appendChild(new Element('input',{{'data-i18n-attr':{js('placeholder:' + PLACEHOLDER_KEY)}}}));\n"
            + f"input.setAttribute('placeholder',{js(self.english[PLACEHOLDER_KEY])});\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419','en']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=input.getAttribute('placeholder');\n"
            + "}\n"
            + f"assert.deepStrictEqual(seen,{js(expected)});",
            modules=(STATIC / 'js' / 'app-i18n.js',))

    def test_a_missing_catalog_key_keeps_its_english(self):
        key = 'preparer.header.title'
        self.assertNotEqual(self.catalogs['ja'][key], self.english[key])
        self.run_node(
            self.catalogs_js(drop=(key,)) + '\n'
            + f"const node=addNode({{'data-i18n':{js(key)}}},{js(self.english[key])});\n"
            + "context.applyUiLocale('ja');\n"
            + f"assert.strictEqual(node.textContent,{js(self.english[key])});",
            modules=(STATIC / 'js' / 'app-i18n.js',))


class RealRendererTests(LocaleTestCase):
    def test_voicelab_stage_labels_translate(self):
        keys = ('voicelab.stage.quality', 'voicelab.stage.dedup')
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-voicelab.js', '        function voicelabStageLabel(', '        function renderVoicelabPreflight(')) + ",context);\n"
            + "context.applyUiLocale('zh-CN');\n"
            + "console.log(JSON.stringify([context.voicelabStageLabel('quality'),context.voicelabStageLabel('dedup'),context.voicelabStageLabel('unknown')]));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        labels = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual([self.catalogs['zh-CN']['voicelab.stage.quality'],
                          self.catalogs['zh-CN']['voicelab.stage.dedup'],
                          'unknown'], labels)

    def test_task_status_labels_translate(self):
        key = 'core.task-status.finished'
        for locale in LOCALES:
            self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-core.js', '        function taskStatusLabel(', '        // Notify the user that a long-running job finished')) + ",context);\n"
            + "context.applyUiLocale('ja');\n"
            + "console.log(context.taskStatusLabel('finished'));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        self.assertEqual(self.catalogs['ja'][key], result.stdout.strip().splitlines()[-1])

    def test_duration_units_translate(self):
        keys = ('core.duration.hours', 'core.duration.minutes', 'core.duration.seconds')
        for locale in LOCALES:
            for key in keys:
                with self.subTest(locale=locale, key=key):
                    self.assertNotEqual(self.catalogs[locale][key], self.english[key])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-workbench.js', '        function formatDuration(', '        // Always-visible')) + ",context);\n"
            + "context.applyUiLocale('es-419');\n"
            + "console.log(JSON.stringify([context.formatDuration(30),context.formatDuration(90),context.formatDuration(3700)]));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        values = json.loads(result.stdout.strip().splitlines()[-1])
        catalog = self.catalogs['es-419']
        self.assertEqual([catalog['core.duration.seconds'].format(seconds=30),
                          catalog['core.duration.minutes'].format(minutes=1, seconds=30),
                          catalog['core.duration.hours'].format(hours=1, minutes=1)], values)

    def test_dataset_sample_counts_use_the_locale_forms(self):
        key_one, key_many = 'training.dataset.samples.one', 'training.dataset.samples.many'
        for locale in LOCALES:
            with self.subTest(locale=locale):
                self.assertNotEqual(self.catalogs[locale][key_one], self.english[key_one])
        result = self.run_node(
            self.catalogs_js() + '\n'
            + "vm.runInContext(" + js(self.slice_source('app-training.js', '        function trainingSamplesText(', '        async function loadLoraDatasets(')) + ",context);\n"
            + "const seen={};\n"
            + "for(const locale of ['zh-CN','ja','es-419']){\n"
            + " context.applyUiLocale(locale);\n"
            + " seen[locale]=[context.trainingSamplesText(1),context.trainingSamplesText(4)];\n"
            + "}\n"
            + "console.log(JSON.stringify(seen));",
            modules=(STATIC / 'js' / 'app-i18n.js',))
        seen = json.loads(result.stdout.strip().splitlines()[-1])
        for locale in LOCALES:
            catalog = self.catalogs[locale]
            self.assertEqual([catalog[key_one].format(count=1), catalog[key_many].format(count=4)], seen[locale])
            if locale in ('zh-CN', 'ja'):
                self.assertEqual(catalog[key_one], catalog[key_many], 'no plural inflection in ' + locale)


class SafetyConfirmTests(LocaleTestCase):
    def test_each_destructive_training_confirm_keeps_english_beneath_the_translation(self):
        for key, english, values in SAFETY_CONFIRMS:
            for locale in LOCALES:
                with self.subTest(key=key, locale=locale):
                    self.assertNotEqual(self.catalogs[locale][key], english)
            rendered = english.format(**values) if values else english
            expected = {locale: (self.catalogs[locale][key].format(**values) if values
                                 else self.catalogs[locale][key]) for locale in LOCALES}
            self.run_node(
                self.catalogs_js() + '\nconst seen={};\n'
                + "for(const locale of ['zh-CN','ja','es-419','en']){context.applyUiLocale(locale);\n"
                + f"seen[locale]=context.formatUiSafetyText({js(key)},{js(english)},{js(values)});}}\n"
                + ''.join(f"assert.strictEqual(seen[{js(locale)}],{js(expected[locale] + chr(10) + rendered)});\n"
                          for locale in LOCALES)
                + f"assert.strictEqual(seen['en'],{js(rendered)});",
                modules=(STATIC / 'js' / 'app-i18n.js',))


class TrainingLabKeyingTests(unittest.TestCase):
    """Source-level: the P8 panes and modules are keyed."""

    @classmethod
    def setUpClass(cls):
        cls.markup = (STATIC / 'index.html').read_text(encoding='utf-8')

    def test_panes_are_keyed(self):
        for key in MARKUP_KEYS:
            with self.subTest(key=key):
                self.assertIn(f'data-i18n="{key}"', self.markup)
        self.assertIn(f'data-i18n-attr="placeholder:{PLACEHOLDER_KEY}"', self.markup)

    def test_each_module_keys_its_safety_confirms(self):
        expectations = (
            ('app-training.js', "formatUiSafetyText('safety.training.dataset.delete'"),
            ('app-training.js', "formatUiSafetyText('safety.training.delete-adapter'"),
            ('app-training.js', "formatUiSafetyText('safety.training.delete-backup'"),
            ('app-training.js', "formatUiSafetyText('safety.training.promote'"),
            ('app-training.js', "formatUiSafetyText('safety.training.rollback'"),
            ('app-training.js', "formatUiSafetyText('safety.training.recover'"),
            ('app-training.js', "formatUiSafetyText('safety.training.review.clear'"),
        )
        for name, needle in expectations:
            with self.subTest(file=name, needle=needle):
                self.assertIn(needle, (STATIC / 'js' / name).read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
