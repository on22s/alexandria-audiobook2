"""Static contracts for UI behavior that is easy to regress silently."""
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).parent.parent / "static"


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tabs = []
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        self.nodes.append((tag, values, tuple(self.stack)))
        if tag == "a" and values.get("data-tab"):
            self.tabs.append(values)
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.stack:
            self.stack = self.stack[:len(self.stack) - 1 - self.stack[::-1].index(tag)]


class UIContractTests(unittest.TestCase):
    def test_bread_navigation_label_uses_palette_color(self):
        source = (STATIC / 'index.html').read_text(encoding='utf-8')
        rule = re.search(r'html\[data-theme="bread"\] \.nav-section-label\s*\{([^}]+)', source)
        self.assertIsNotNone(rule)
        self.assertIn('color: var(--nav-accent)', rule[1])

    def test_secondary_text_uses_semantic_theme_color(self):
        source = (STATIC / 'index.html').read_text(encoding='utf-8')
        rule = re.search(r'(?m)^\s*\.text-secondary\s*\{([^}]+)', source)
        self.assertIsNotNone(rule)
        self.assertIn('color: var(--text-muted) !important', rule[1])

    def test_script_links_use_semantic_theme_text_in_every_state(self):
        source = (STATIC / 'index.html').read_text(encoding='utf-8')
        link_rule = re.search(r'\.script-ingest-steps-list \.btn\s*\{([^}]+)', source)[1]
        for state in ('color', 'hover-color', 'active-color'):
            self.assertIn('--bs-btn-' + state + ': var(--accent-strong)', link_rule)

    def test_keyboard_skip_link_targets_single_focusable_main(self):
        parser = _NavigationParser()
        parser.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        mains = [attrs for tag, attrs, _ in parser.nodes if tag == 'main']
        self.assertEqual(1, len(mains))
        self.assertEqual('-1', mains[0].get('tabindex'))
        self.assertEqual('app-main', mains[0].get('id'))
        skip = next(attrs for tag, attrs, _ in parser.nodes if tag == 'a' and attrs.get('href') == '#' + mains[0]['id'])
        self.assertIn('visually-hidden-focusable', skip.get('class', '').split())
        self.assertEqual('shell.skip-content', skip.get('data-i18n'))
        body_children = [tag for tag, _, parents in parser.nodes if parents and parents[-1] == 'body']
        self.assertEqual('a', body_children[0])

    def test_tab_panels_are_labelled_regions_matching_nav_links(self):
        parser = _NavigationParser()
        parser.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        expected_tabs = {
            'home', 'setup', 'script', 'voices', 'editor', 'audio', 'designer',
            'preparer', 'dataset-builder', 'training', 'voicelab', 'reports', 'casting',
        }
        tab_links = {attrs['data-tab']: attrs for tag, attrs, _ in parser.nodes if tag == 'a' and attrs.get('data-tab')}
        self.assertEqual(expected_tabs, set(tab_links.keys()))

        tab_panels = {
            attrs['id'][:-4]: attrs
            for tag, attrs, _ in parser.nodes
            if attrs.get('id', '').endswith('-tab') and 'tab-content' in attrs.get('class', '').split()
        }
        self.assertEqual(expected_tabs, set(tab_panels.keys()))

        for tab_name, panel in tab_panels.items():
            with self.subTest(tab=tab_name):
                self.assertEqual('region', panel.get('role'), f'{tab_name}-tab must have role="region"')
                self.assertNotEqual('tabpanel', panel.get('role'), f'{tab_name}-tab must not have role="tabpanel"')
                labelled_by = panel.get('aria-labelledby')
                self.assertTrue(labelled_by, f'{tab_name}-tab must have aria-labelledby')
                nav_link = tab_links.get(tab_name)
                self.assertIsNotNone(nav_link)
                self.assertEqual(labelled_by, nav_link.get('id'), f'{tab_name}-tab aria-labelledby must match nav link id')
                self.assertNotEqual('tab', nav_link.get('role'), f'nav link for {tab_name} must not have role="tab"')

    def test_logs_are_explicitly_keyboard_focusable_named_regions(self):
        parser = _NavigationParser()
        parser.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        logs = [attrs for _, attrs, _ in parser.nodes if 'log-window' in attrs.get('class', '').split() or attrs.get('id') == 'dsb-logs']
        self.assertEqual(7, len(logs))
        for attrs in logs:
            with self.subTest(log=attrs['id']):
                self.assertEqual('0', attrs.get('tabindex'))
                self.assertEqual('region', attrs.get('role'))
                self.assertTrue(attrs.get('aria-label'))
                self.assertIn('aria-label:', attrs.get('data-i18n-attr', ''))
        self.assertEqual(7, len({attrs['aria-label'] for attrs in logs}))
        for catalog_path in sorted((STATIC / 'i18n').glob('ui.*.json')):
            catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
            with self.subTest(catalog=catalog_path.name):
                names = [catalog[attrs['data-i18n-attr'].split('aria-label:')[1]] for attrs in logs]
                self.assertTrue(all(names))
                self.assertEqual(7, len(set(names)))

    def test_render_progress_has_named_numeric_initial_state(self):
        parser = _NavigationParser()
        parser.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        progress = next(attrs for _, attrs, _ in parser.nodes if attrs.get('id') == 'full-progress-bar')
        self.assertEqual('progressbar', progress['role'])
        self.assertTrue(progress.get('aria-label'))
        self.assertEqual(('0', '100'), tuple(progress.get(name) for name in ('aria-valuemin', 'aria-valuemax')))
        self.assertNotIn('aria-valuenow', progress)
        self.assertEqual('true', progress.get('aria-busy'))

    def test_system_status_preserves_navigation_list_semantics(self):
        parser = _NavigationParser()
        parser.feed((STATIC / 'index.html').read_text(encoding='utf-8'))
        statuses = [(tag, attrs, parents) for tag, attrs, parents in parser.nodes if 'status-cluster' in attrs.get('class', '').split()]
        self.assertEqual(1, len(statuses))
        tag, status, parents = statuses[0]
        self.assertEqual('div', tag)
        self.assertNotIn('ul', parents)
        self.assertEqual(('status', 'polite', 'true'), tuple(status.get(name) for name in ('role', 'aria-live', 'aria-atomic')))
        self.assertTrue(status.get('aria-label'))

    def test_theme_text_and_input_border_contrast(self):
        source = (STATIC / 'index.html').read_text(encoding='utf-8')

        def luminance(color):
            channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        def contrast(first, second):
            bright, dark = sorted((luminance(first), luminance(second)), reverse=True)
            return (bright + .05) / (dark + .05)

        base = dict(re.findall(r'(--[\w-]+):\s*(#[0-9a-fA-F]{6});', re.search(r':root\s*\{([^}]+)', source)[1]))
        for theme in ('light', 'night', 'super-night', 'cyberpunk', 'bread', 'leek'):
            tokens = dict(base)
            if theme != 'light':
                for block in re.finditer(r'html\[data-theme="' + theme + r'"\]\s*\{([^}]+)', source):
                    tokens.update(re.findall(r'(--[\w-]+):\s*(#[0-9a-fA-F]{6});', block[1]))
            for foreground in ('--text-primary', '--text-muted', '--text-heading', '--accent-strong', '--border-input'):
                for background in ('--bg-page', '--bg-card', '--bg-card-header', '--bg-input', '--bg-modal'):
                    with self.subTest(theme=theme, foreground=foreground, background=background):
                        self.assertGreaterEqual(contrast(tokens[foreground], tokens[background]), 3 if foreground == '--border-input' else 4.5)
            self.assertIn('--progress-bg', tokens)
            self.assertIn('--progress-text', tokens)
            for foreground, background in (('--step-color', '--step-bg'), ('--log-color', '--log-bg'), ('--progress-text', '--progress-bg')):
                with self.subTest(theme=theme, foreground=foreground):
                    self.assertGreaterEqual(contrast(tokens[foreground], tokens[background]), 4.5)
            progress_channels = [int(tokens['--progress-bg'][index:index + 2], 16) for index in (1, 3, 5)]
            stripe = '#' + ''.join(f'{round(channel * .85 + 255 * .15):02x}' for channel in progress_channels)
            self.assertGreaterEqual(contrast(tokens['--progress-text'], stripe), 4.5)

    def test_primary_navigation_is_keyboard_addressable(self):
        parser = _NavigationParser()
        parser.feed((STATIC / "index.html").read_text(encoding="utf-8"))
        expected = {'home', 'setup', 'script', 'voices', 'editor', 'audio', 'designer',
                    'preparer', 'dataset-builder', 'training', 'voicelab', 'reports', 'casting'}
        self.assertEqual(expected, {tab['data-tab'] for tab in parser.tabs})
        self.assertEqual(len(expected), len(parser.tabs))
        self.assertTrue(all(tab.get("href", "").startswith("#") for tab in parser.tabs))
        self.assertEqual(1, sum(tab.get("aria-current") == "page" for tab in parser.tabs))

    def test_pass_prompt_defaults_have_a_server_and_browser_path(self):
        import json
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import system
        app = FastAPI(); app.include_router(system.router)
        with TestClient(app) as client:
            response = client.get('/api/default_prompts')
            self.assertEqual(200, response.status_code, response.text)
            defaults = response.json()
        from tests import test_prompt_preset_transactions_js as fixtures
        fixtures.PromptPresetTransactionTests.run_case(self, "const defaults=" + json.dumps(defaults) + ";" + r"""
const a=source.indexOf('let promptDefaultsLoadPending =');
assert(a>=0);run(source.slice(a,source.indexOf('async function loadConfig()',a)));
context.API.get=async()=>defaults;await context.loadMissingPromptDefaults();
for(const pass of ['pass1','pass3','cast','pass1_compact','pass3_gaps','pass3_directed']){
 const isCast=pass==='cast';
 const systemId=isCast?'cast-whole-book-prompt':pass+'-system-prompt';
 const userId=isCast?'cast-incremental-prompt':pass+'-user-prompt';
 const systemKey=isCast?'whole_book_prompt':'system_prompt';
 const userKey=isCast?'incremental_prompt':'user_prompt';
 const presets=[{name:'custom',[systemKey]:'custom instruction',[userKey]:'custom context'}];
 context.renderPassPromptPresets(pass,presets,'default');
 assert.strictEqual(element(systemId).value,run('passPromptDefaults.'+pass)[systemKey]);
 assert.strictEqual(element(userId).value,run('passPromptDefaults.'+pass)[userKey]);
 context.renderPassPromptPresets(pass,presets,'custom');
 assert.strictEqual(element(systemId).value,'custom instruction');
 assert.strictEqual(element(userId).value,'custom context');
 context.renderPassPromptPresets(pass,[{name:'partial',[systemKey]:'partial instruction'}],'partial');
 assert.strictEqual(element(systemId).value,'partial instruction');
 assert.strictEqual(element(userId).value,run('passPromptDefaults.'+pass)[userKey]);
}
""")

    def test_reset_preserves_numbered_prompt_presets(self):
        import json
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import system
        from tests import test_prompt_preset_transactions_js as fixtures
        app = FastAPI(); app.include_router(system.router)
        with TestClient(app) as client:
            response = client.get('/api/default_prompts')
            self.assertEqual(200, response.status_code, response.text)
            defaults = response.json()
        fixtures.PromptPresetTransactionTests.run_case(self, "const defaults=" + json.dumps(defaults) + ";" + r"""
const helper=source.indexOf('const generationControlFields =');assert(helper>=0);
run(source.slice(helper,source.indexOf('async function loadConfig()',helper)));
const a=source.indexOf('window.resetPrompts ='),b=source.indexOf('function buildConfigPayload(',a);
assert(a>=0&&b>a);run(source.slice(a,b));
const families=['pass1','pass3','pass1_compact','pass3_gaps','pass3_directed'];
for(const pass of families){context.renderPassPromptPresets(pass,[{name:'saved',system_prompt:'custom S',user_prompt:'custom U'}],'saved');}
const before=run('JSON.stringify(passPromptPresets)');context.API.get=async()=>defaults;
await context.window.resetPrompts();assert.strictEqual(run('JSON.stringify(passPromptPresets)'),before);
for(const pass of families){
 assert.strictEqual(run('activePassPromptPreset.'+pass),'default');
 assert.strictEqual(element(pass+'-system-prompt').value,defaults[pass+'_system_prompt']);
 assert.strictEqual(element(pass+'-user-prompt').value,defaults[pass+'_user_prompt']);
}
""")

    def test_error_toasts_are_assertive_but_other_toasts_are_polite(self):
        script = (STATIC / "js" / "app-core.js").read_text(encoding="utf-8")
        self.assertIn("const liveRole = type === 'error' ? 'alert' : 'status';", script)
        self.assertIn("const livePriority = type === 'error' ? 'assertive' : 'polite';", script)

    def test_ui_docs_cover_required_review_dimensions(self):
        guide = (STATIC.parent.parent / "docs" / "UI_GUIDELINES.md").read_text(encoding="utf-8")
        checklist = (STATIC.parent.parent / "docs" / "UI_TEST_CHECKLIST.md").read_text(encoding="utf-8")
        for text in ("focus-visible", "reduced-motion", "Cyberpunk", "loading", "error"):
            self.assertIn(text, guide)
        for text in ("320px", "375px", "414px", "768px", "Pass 1", "Pass 3"):
            self.assertIn(text, checklist)


if __name__ == "__main__":
    unittest.main()
