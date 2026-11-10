"""Every translated UI catalog must carry exactly the source's keys and meaning.

The English text lives in index.html and at each t() call site; ui.en.json is
harvested from that source by tools/i18n/extract_ui_catalog.py. These tests are
the gate between the two: key set equality in both directions, placeholder and
protected-token parity, no untranslated copy, the safety review rule, and the
catalog file itself matching a fresh harvest.

Rule 21 is the reason half of this file exists: every rule the real catalogs are
checked against is also run against a deliberately broken fixture that the
checker MUST reject. A checker that silently stopped discriminating would
otherwise pass the real catalogs forever.
"""
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent.parent
I18N = Path(__file__).resolve().parent.parent / 'static' / 'i18n'
ENGLISH_PATH = I18N / 'ui.en.json'
LOCALES = ('zh-CN', 'ja', 'es-419')

KEY = re.compile(r'^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$')
PLACEHOLDER = re.compile(r'\{[a-zA-Z][a-zA-Z0-9_]*\}')
PROTECTED = {
    'backticked identifier': re.compile(r'`[^`\n]+`'),
    'API path': re.compile(r'/api/[^\s";)]+'),
    'environment name': re.compile(r'\benv:[A-Za-z_][A-Za-z0-9_]*'),
    'file extension': re.compile(r'\.[A-Za-z0-9]{1,5}\b'),
}
# Terms this UI keeps in English by design; a value equal to its English source
# is allowed only when the whole value is one of these.
KEEP_ENGLISH = frozenset({'GPU', 'LoRA', 'M4B', 'WAV', 'MP3', 'API', 'Alexandria',
                          'Whisper', 'CUDA', 'ROCm', 'Qwen3-TTS'})
HTML_KEYS = frozenset()


def load(locale):
    return json.loads((I18N / f'ui.{locale}.json').read_text(encoding='utf-8'))


def messages_of(payload):
    return {key: value for key, value in payload.items() if key != '_meta'}


def validate(payload, english, locale):
    """Return every reason this catalog must not ship. Empty list means valid."""
    errors = []
    meta = payload.get('_meta')
    if not isinstance(meta, dict):
        return [f'{locale}: missing _meta']
    if meta.get('locale') != locale:
        errors.append(f'{locale}: _meta.locale is {meta.get("locale")!r}')
    status = meta.get('review_status')
    if not isinstance(status, dict):
        errors.append(f'{locale}: _meta.review_status is not an object')
        status = {}
    if status.get('all') not in {'human-reviewed', 'machine-draft'}:
        errors.append(f'{locale}: _meta.review_status.all must say whether the locale is reviewed')
    messages = messages_of(payload)
    for key in sorted(set(messages) - set(english)):
        errors.append(f'{locale}: {key} does not exist in the English source')
    for key in sorted(set(english) - set(messages)):
        errors.append(f'{locale}: {key} is missing')
    for key in sorted(set(messages) & set(english)):
        value, source = messages[key], english[key]
        if not KEY.match(key):
            errors.append(f'{locale}: {key} is not a dotted lowercase key')
        if not isinstance(value, str):
            errors.append(f'{locale}: {key} is not a string')
            continue
        if not value.strip() or value != value.strip():
            errors.append(f'{locale}: {key} is empty or padded with whitespace')
        if '${' in value:
            errors.append(f'{locale}: {key} contains template interpolation')
        if '<' in value and key not in HTML_KEYS:
            errors.append(f'{locale}: {key} contains markup outside the HTML allowlist')
        if sorted(PLACEHOLDER.findall(value)) != sorted(PLACEHOLDER.findall(source)):
            errors.append(f'{locale}: {key} placeholder set differs from English')
        for label, pattern in PROTECTED.items():
            if sorted(pattern.findall(value)) != sorted(pattern.findall(source)):
                errors.append(f'{locale}: {key} changed a protected {label}')
        if value == source and value.strip(' .:') not in KEEP_ENGLISH:
            errors.append(f'{locale}: {key} is untranslated English')
        if key.endswith('.one') and locale in {'zh-CN', 'ja'}:
            counterpart = messages.get(key[:-4] + '.many')
            if counterpart is not None and value != counterpart:
                errors.append(f'{locale}: {key} must equal its .many text (no plural inflection)')
    safety = [key for key in messages if key.startswith('safety.')]
    if safety and status.get('safety') != 'human-reviewed' and status.get('all') == 'human-reviewed':
        errors.append(f'{locale}: safety text is unreviewed but the locale is offered as reviewed')
    return errors


def catalog(locale, messages, all_status='machine-draft', safety_status='not-reviewed'):
    payload = {'_meta': {'locale': locale, 'generated_from': 'fixture', 'reviewers': [],
                         'review_status': {'all': all_status, 'safety': safety_status}}}
    payload.update(messages)
    return payload


ENGLISH_FIXTURE = {
    'setup.finder.label': 'Find a setting',
    'setup.finder.one': '{count} matching setting.',
    'setup.finder.many': '{count} matching settings.',
    'setup.finder.opened': 'Showing the original setting. Save Configuration to apply any edits.',
    'safety.delete_take': 'Delete {count} takes? This cannot be undone. See `TAKE_HISTORY.md`.',
}
ZH_FIXTURE = {
    'setup.finder.label': '查找设置',
    'setup.finder.one': '匹配到 {count} 项设置。',
    'setup.finder.many': '匹配到 {count} 项设置。',
    'setup.finder.opened': '已定位到原始设置。如需应用修改，请点击 Save Configuration。',
    'safety.delete_take': '删除 {count} 条录音？此操作无法撤销。见 `TAKE_HISTORY.md`。',
}


class CheckerFixtureTests(unittest.TestCase):
    """Rule 21: the checker is checked on cases whose answer is known."""

    def assertRejected(self, payload, english, locale, needle):
        errors = validate(payload, english, locale)
        self.assertTrue(errors, 'the checker accepted a deliberately broken catalog')
        self.assertTrue(any(needle in error for error in errors), errors)

    def test_a_valid_fixture_is_accepted_so_the_rejections_mean_something(self):
        self.assertEqual([], validate(catalog('zh-CN', ZH_FIXTURE, safety_status='human-reviewed'),
                                      ENGLISH_FIXTURE, 'zh-CN'))

    def test_a_missing_key_is_rejected(self):
        broken = dict(ZH_FIXTURE)
        broken.pop('setup.finder.label')
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'is missing')

    def test_an_extra_key_is_rejected(self):
        broken = dict(ZH_FIXTURE, **{'setup.finder.future': '未来'})
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'does not exist')

    def test_a_dropped_placeholder_is_rejected(self):
        broken = dict(ZH_FIXTURE, **{'setup.finder.one': '匹配到某项设置。'})
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'placeholder set differs')

    def test_an_added_placeholder_is_rejected(self):
        broken = dict(ZH_FIXTURE, **{'setup.finder.one': '匹配到 {count} 项设置（共 {total} 项）。'})
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'placeholder set differs')

    def test_an_english_copy_is_rejected(self):
        broken = dict(ZH_FIXTURE, **{'setup.finder.label': 'Find a setting'})
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'untranslated English')

    def test_markup_in_a_plain_text_value_is_rejected(self):
        broken = dict(ZH_FIXTURE, **{'setup.finder.label': '<b>查找设置</b>'})
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'contains markup')

    def test_chinese_plural_forms_that_differ_are_rejected(self):
        broken = dict(ZH_FIXTURE, **{'setup.finder.many': '匹配到 {count} 项设置们。'})
        self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', 'must equal its .many')

    def test_empty_padded_and_interpolated_values_are_rejected(self):
        for value, needle in (('  ', 'empty or padded'), (' 查找设置', 'empty or padded'),
                              ('匹配 ${count}', 'template interpolation')):
            with self.subTest(value=value):
                broken = dict(ZH_FIXTURE, **{'setup.finder.label': value})
                self.assertRejected(catalog('zh-CN', broken), ENGLISH_FIXTURE, 'zh-CN', needle)

    def test_a_translated_protected_token_is_rejected(self):
        broken = dict(ZH_FIXTURE, **{'safety.delete_take':
                                     '删除 {count} 条录音？此操作无法撤销。见 `录音历史.md`。'})
        self.assertRejected(catalog('zh-CN', broken, safety_status='human-reviewed'),
                            ENGLISH_FIXTURE, 'zh-CN', 'protected backticked identifier')

    def test_a_translated_api_path_is_rejected(self):
        source = dict(ENGLISH_FIXTURE, **{'error.preview': 'Call /api/config/preview again.'})
        broken = dict(ZH_FIXTURE, **{'error.preview': '请再次调用 /api/config/预览。'})
        self.assertRejected(catalog('zh-CN', broken), source, 'zh-CN', 'protected API path')

    def test_unreviewed_safety_text_in_a_reviewed_locale_is_rejected(self):
        self.assertRejected(catalog('zh-CN', ZH_FIXTURE, all_status='human-reviewed',
                                    safety_status='not-reviewed'),
                            ENGLISH_FIXTURE, 'zh-CN', 'safety text is unreviewed')

    def test_unreviewed_safety_text_in_a_draft_locale_is_allowed_but_visible(self):
        payload = catalog('zh-CN', ZH_FIXTURE)
        self.assertEqual([], validate(payload, ENGLISH_FIXTURE, 'zh-CN'))
        self.assertNotEqual(payload['_meta']['review_status']['all'], 'human-reviewed')

    def test_a_wrong_or_missing_review_status_is_rejected(self):
        payload = catalog('zh-CN', ZH_FIXTURE)
        payload['_meta']['review_status']['all'] = 'probably-fine'
        self.assertRejected(payload, ENGLISH_FIXTURE, 'zh-CN', 'review_status.all')
        payload = catalog('zh-CN', ZH_FIXTURE)
        payload['_meta']['locale'] = 'zh-TW'
        self.assertRejected(payload, ENGLISH_FIXTURE, 'zh-CN', '_meta.locale')


class RealCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.english = messages_of(json.loads(ENGLISH_PATH.read_text(encoding='utf-8')))
        cls.catalogs = {locale: load(locale) for locale in LOCALES}

    def test_the_english_catalog_matches_a_fresh_harvest_of_the_source(self):
        spec = importlib.util.spec_from_file_location(
            'extract_ui_catalog', ROOT / 'tools' / 'i18n' / 'extract_ui_catalog.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.render(module.harvest(ROOT)), ENGLISH_PATH.read_text(encoding='utf-8'),
                         'ui.en.json is stale; run: python tools/i18n/extract_ui_catalog.py')

    def test_every_locale_carries_exactly_the_english_key_set(self):
        for locale, payload in self.catalogs.items():
            with self.subTest(locale=locale):
                self.assertEqual(set(self.english), set(messages_of(payload)))

    def test_every_real_catalog_passes_every_rule(self):
        for locale, payload in self.catalogs.items():
            with self.subTest(locale=locale):
                self.assertEqual([], validate(payload, self.english, locale))

    def test_no_locale_claims_a_review_it_does_not_have(self):
        for locale, payload in self.catalogs.items():
            with self.subTest(locale=locale):
                self.assertEqual('machine-draft', payload['_meta']['review_status']['all'])
                self.assertEqual([], payload['_meta']['reviewers'])

    def test_the_catalogs_are_utf8_json_without_a_bom(self):
        for locale in LOCALES:
            with self.subTest(locale=locale):
                raw = (I18N / f'ui.{locale}.json').read_bytes()
                self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
                self.assertIn('"locale": "' + locale + '"', raw.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
