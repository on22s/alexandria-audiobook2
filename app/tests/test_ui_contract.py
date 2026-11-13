"""Static contracts for UI behavior that is easy to regress silently."""
import unittest
from html.parser import HTMLParser
from pathlib import Path


STATIC = Path(__file__).parent.parent / "static"


class _NavigationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tabs = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("data-tab"):
            self.tabs.append(values)


class UIContractTests(unittest.TestCase):
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
