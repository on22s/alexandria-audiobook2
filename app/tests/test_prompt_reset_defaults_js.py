"""Native factory defaults drive real reset controls, payload and persisted config."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from config_settings import GenerationConfig
from routers import system
from tests import test_prompt_preset_transactions_js as actions
from attribution_prompt_variants import builtin_presets

FIELDS={'max-tokens':'max_tokens','temperature':'temperature','top-p':'top_p','top-k':'top_k','min-p':'min_p','presence-penalty':'presence_penalty','tp-chunk-size':'three_pass_chunk_size','tp-attribute-batch-size':'three_pass_attribute_batch_size','tp-attribute-context-chars':'three_pass_attribute_context_chars','tp-segment-output-ratio':'three_pass_segment_output_ratio','tp-segment-temperature':'three_pass_segment_temperature','tp-attribute-temperature':'three_pass_attribute_temperature','tp-instruct-temperature':'three_pass_instruct_temperature','tp-segmentation':'three_pass_segmentation','context-rescue-retries':'context_rescue_retries'}
SETUP=r'''
seed();const helper=source.indexOf('const generationControlFields =');if(helper>=0){run(source.slice(helper,source.indexOf('async function loadConfig()',helper)));}
run(source.slice(source.indexOf('window.resetPrompts ='),source.indexOf('// Toggle chevron')));
for(const id of Object.keys(fields)){const el=element(id);let stored=el.value;Object.defineProperty(el,'value',{enumerable:true,get:()=>stored,set:value=>stored=String(value)});el.value=id==='tp-segmentation'?'llm':'99';}element('banned-tokens').value='old token';element('context-rescue-windows').value='100';element('merge-narrators').checked=true;element('tp-quoted-must-be-spoken').checked=false;element('tp-unquoted-must-be-narrator').checked=false;element('tp-keep-whole-batch').checked=true;element('tp-group-rule').checked=true;run('legacyChunkSize=9999;');
const state=()=>JSON.stringify({elements,legacy:run('legacyChunkSize'),cache:snapshot()});
'''


class PromptResetDefaultTests(unittest.TestCase):
    def test_native_factory_defaults_reset_all_visible_generation_fields_and_persist(self):
        app=FastAPI();app.include_router(system.router)
        with TestClient(app) as client:
            response=client.get('/api/default_prompts');self.assertEqual(200,response.status_code,response.text);defaults=response.json();self.assertEqual(GenerationConfig().model_dump(),defaults.get('generation'))
        code='const fields='+json.dumps(FIELDS)+';const defaults='+json.dumps(defaults)+';const builtins='+json.dumps(builtin_presets())+';'+SETUP+r'''
run('renderPromptPresets('+JSON.stringify(builtins)+',"michel2_full");');const cacheBefore=run('JSON.stringify(passPromptPresets)');context.API.get=async()=>defaults;
for(const [id,value] of Object.entries({'tts-mode':'local','tts-device':'auto','tts-language':'English'})){element(id).value=value;}
await context.window.resetPrompts();assert.strictEqual(run('JSON.stringify(passPromptPresets)'),cacheBefore);assert.strictEqual(run('activePassPromptPreset.pass1'),'default');assert.strictEqual(run('activePassPromptPreset.pass3'),'default');
assert.strictEqual(element('pass1-system-prompt').value,defaults.pass1_system_prompt);assert.strictEqual(element('pass3-user-prompt').value,defaults.pass3_user_prompt);
console.log(JSON.stringify(context.buildConfigPayload(2)));
'''
        payload=actions.PromptPresetTransactionTests().run_case(code);expected=GenerationConfig().model_dump()
        keys=list(FIELDS.values())+['chunk_size','banned_tokens','context_rescue_windows','merge_narrators','three_pass_quoted_must_be_spoken','three_pass_unquoted_must_be_narrator','three_pass_keep_whole_batch','three_pass_group_rule']
        self.assertEqual({key:expected[key] for key in keys},{key:payload['generation'][key] for key in keys})
        with tempfile.TemporaryDirectory() as root,patch.object(system,'CONFIG_PATH',str(Path(root,'config.json'))),patch.object(system,'project_manager',SimpleNamespace(invalidate_config_cache=lambda:None,engine=None)),TestClient(app) as client:
            response=client.post('/api/config',json=payload);self.assertEqual(200,response.status_code,response.text);saved=json.loads(Path(root,'config.json').read_text());self.assertEqual({key:expected[key] for key in keys},{key:saved['generation'][key] for key in keys})

    def test_failed_or_incomplete_defaults_response_leaves_every_editor_value_unchanged(self):
        code='const fields='+json.dumps(FIELDS)+';'+SETUP+r'''
const before=state();context.API.get=async()=>{throw Error('fixture defaults unavailable');};await context.window.resetPrompts();assert.strictEqual(state(),before);assert.strictEqual(toasts.at(-1)[1],'error');
context.API.get=async()=>({system_prompt:'should not apply',generation:{}});await context.window.resetPrompts();assert.strictEqual(state(),before);assert.strictEqual(toasts.at(-1)[1],'error');
'''
        actions.PromptPresetTransactionTests().run_case(code)

    def test_config_load_helper_preserves_zero_and_false_values_and_clears_arrays(self):
        code='const fields='+json.dumps(FIELDS)+';'+SETUP+r'''
context.applyGenerationSettings({temperature:0,top_k:0,three_pass_attribute_context_chars:0,context_rescue_retries:0,banned_tokens:[],context_rescue_windows:[1],merge_narrators:false,three_pass_quoted_must_be_spoken:false,three_pass_unquoted_must_be_narrator:false});
for(const id of ['temperature','top-k','tp-attribute-context-chars','context-rescue-retries']){assert.strictEqual(element(id).value,'0');}assert.strictEqual(element('banned-tokens').value,'');assert.strictEqual(element('context-rescue-windows').value,'1');assert.strictEqual(element('merge-narrators').checked,false);assert.strictEqual(element('tp-quoted-must-be-spoken').checked,false);assert.strictEqual(element('tp-unquoted-must-be-narrator').checked,false);
'''
        actions.PromptPresetTransactionTests().run_case(code)
