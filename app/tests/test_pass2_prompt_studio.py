"""Step 2 presets stay beside the editor and preserve the existing request path."""
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import attribution_prompt_variants as apv
from generate_script import LLMGenParams
from routers import system
from tests import test_prompt_preset_transactions_js as fixtures


class EditorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        node = dict(attrs, tag=tag, parents=tuple(self.stack))
        self.nodes.append(node)
        if tag not in ('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'):
            self.stack.append(node.get('id') or tag)

    def handle_endtag(self, tag):
        if self.stack:
            self.stack.pop()


class Pass2PromptStudioTests(unittest.TestCase):
    def test_preset_controls_and_all_attribution_fields_share_step2_editor(self):
        parser = EditorParser()
        parser.feed((Path(__file__).resolve().parents[1] / 'static/index.html').read_text(encoding='utf8'))
        required = ('prompt-preset-select', 'prompt-preset-description', 'prompt-preset-variant-badge',
                    'system-prompt', 'user-prompt', 'prompt-example', 'prompt-preview-panel')
        for field in required:
            with self.subTest(field=field):
                nodes = [node for node in parser.nodes if node.get('id') == field]
                self.assertEqual(1, len(nodes))
                self.assertIn('pass2-prompt-editor', nodes[0]['parents'])
        for action in ('savePromptPreset()', 'deletePromptPreset()', 'previewAttributionPrompt()'):
            nodes = [node for node in parser.nodes if node.get('onclick') == action]
            self.assertEqual(1, len(nodes), action)
            self.assertIn('pass2-prompt-editor', nodes[0]['parents'])
            if action != 'previewAttributionPrompt()':
                self.assertIn('data-config-save-action', nodes[0])
        editor = next(node for node in parser.nodes if node.get('id') == 'pass2-prompt-editor')
        self.assertEqual('fieldset', editor['tag'])
        self.assertTrue(any(node['tag'] == 'legend' and 'pass2-prompt-editor' in node['parents'] for node in parser.nodes))
        positions = [next(i for i, node in enumerate(parser.nodes) if node.get('id') == field)
                     for field in ('pass1-system-prompt', 'prompt-preset-select', 'system-prompt', 'user-prompt', 'pass3-system-prompt')]
        self.assertEqual(sorted(positions), positions)

    def test_editor_save_roundtrips_through_http_disk_and_real_request_builder(self):
        payload = fixtures.PromptPresetTransactionTests.run_case(self, r"""
mode='success';
element('tts-mode').value='local';element('tts-device').value='auto';element('tts-language').value='English';
run("promptPresets=[{name:'default',variant:'default',builtin:true,system_prompt:'original system',user_prompt:'{roster} {batch}'}];renderPromptPresets(promptPresets,'default');");
element('system-prompt').value='Synthetic Step 2 instructions';
element('user-prompt').value='Synthetic roster: {roster}\nSynthetic entries: {batch}';
await context.window.savePromptPreset();
console.log(JSON.stringify(persisted));
""")
        app = FastAPI()
        app.include_router(system.router)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'config.json'
            with patch.object(system, 'CONFIG_PATH', str(target)), patch.object(system, 'project_manager', SimpleNamespace(reset_engine=lambda: None, engine=None)), TestClient(app) as client:
                response = client.post('/api/config', json=payload)
                self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(target.read_text(encoding='utf8'))
        variant, texts, name = apv.resolve_attribution_preset(saved)
        self.assertEqual(('default', 'new'), (variant, name))
        frozen = [{'type': 'SPOKEN', 'text': 'Synthetic hello.'}]
        system_prompt, body = apv.build_variant_request(variant, frozen, LLMGenParams(structured_output='off'), ['ALICE'], texts=texts)
        self.assertEqual('Synthetic Step 2 instructions', system_prompt)
        self.assertIn('Synthetic roster: ALICE', body)
        self.assertIn('Synthetic entries:', body)
        self.assertIn('Synthetic hello.', body)
        self.assertEqual('default', saved['generation']['three_pass_attribute_prompt_variant'])


if __name__ == '__main__':
    unittest.main()
