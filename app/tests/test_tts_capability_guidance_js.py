import json
import ast
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tests.test_ui_help_js import DOM_FIXTURE
from tts_capabilities import get_tts_capability_modes

STATIC = Path(__file__).resolve().parent.parent / 'static'

class TtsCapabilityGuidanceJsTests(unittest.TestCase):
    def test_mode_changes_preserve_assignment_fields_and_warn_without_posting(self):
        setup = r'''
context.window=context;document.addEventListener=()=>{};
for(const id of ['tts-mode','tts-capability-summary','tts-url-group','tts-device-group','tts-local-options','tts-external-timeout-group']){body.appendChild(new Element('div',{id}));}
const card=new Element('div',{class:'voice-card card-body'}), choice=new Element('input',{class:'voice-type'});choice.value='lora';choice.checked=true;card.appendChild(choice);body.appendChild(card);
const originalQuery=card.querySelector;card.querySelector=s=>s==='.voice-type:checked'?choice:card.querySelectorAll(s)[0]||null;
for(const kind of ['custom','builtin-lora','clone','lora','design','ensemble']){card.appendChild(new Element('div',{class:kind+'-opts'}));}
context.API={post(){throw Error('must not save');}};
load();
'''
        code = DOM_FIXTURE + setup + '\nconst modes=' + json.dumps(get_tts_capability_modes()) + r''';
document.getElementById('tts-mode').value='external';context.setTtsCapabilityModes(modes);
assert.equal(choice.checked,true);assert.equal(choice.value,'lora');assert.equal(document.getElementById('tts-url-group').style.display,'');assert.equal(document.getElementById('tts-device-group').style.display,'none');assert.equal(card.querySelector('.tts-capability-notice').hidden,false);
assert.match(card.querySelector('.tts-capability-notice').textContent,/assignment is kept/);
document.getElementById('tts-mode').value='local';context.applyTtsCapabilityGuidance();assert.equal(choice.checked,true);assert.equal(choice.value,'lora');assert.equal(card.querySelector('.tts-capability-notice').hidden,true);assert.equal(document.getElementById('tts-device-group').style.display,'');
document.getElementById('tts-mode').value='external';let saves=0;context.saveVoicesDebounced=()=>{saves++;};
const core=fs.readFileSync(process.argv[1].replace('app-tts-capabilities.js','app-core.js'),'utf8'),start=core.indexOf('function showVoiceTypeOptions(');vm.runInContext(core.slice(start,core.indexOf('        async function refreshVoiceMetadata()',start)),context);
choice.value='design';context.toggleVoiceType(choice);assert.equal(saves,1);assert.equal(card.querySelector('.tts-capability-notice').hidden,false);assert.equal(card.querySelector('.design-opts').style.display,'block');assert.equal(choice.value,'design');
context.setTtsCapabilityModes(null);assert.equal(choice.checked,true);assert.match(card.querySelector('.tts-capability-notice').textContent,/capabilities unavailable/);assert(!choice.hasAttribute('aria-description'));assert.match(document.getElementById('tts-capability-summary').textContent,/unavailable/);
'''
        result = subprocess.run(['node', '-e', code, str(STATIC/'js/app-tts-capabilities.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0,result.returncode,result.stderr)

    def run_external_capabilities_case(self, script_body):
        setup = r'''
document.addEventListener=()=>{};
for(const pair of [['tts-mode','select'],['tts-capability-summary','p'],['tts-url-group','div'],['tts-device-group','div'],['tts-local-options','div'],['tts-external-timeout-group','div'],['tts-url','input'],['tts-external-urls','textarea'],['tts-external-timeout','input'],['tts-external-server-number','input'],['tts-external-capabilities-check','button'],['tts-external-capabilities-status','p'],['tts-external-capabilities-results','div']]){body.appendChild(new Element(pair[1],{id:pair[0]}));}
document.getElementById('tts-mode').value='external';
document.getElementById('tts-url').value='http://saved.example:7860';
document.getElementById('tts-external-urls').value='';
document.getElementById('tts-external-timeout').value='300';
document.getElementById('tts-external-server-number').value='1';
let post_calls=[];
function rendered_text(){const results=document.getElementById('tts-external-capabilities-results');return results.textContent+results.children.map(child=>child.textContent).join(' ');}
context.API={post(url,body){post_calls.push([url,body]);return Promise.resolve(make_response());}};
function make_report(){return {fingerprint:'schema-1',operations:{custom:{status:'supported',endpoint:'custom-endpoint',adapter:'adapter-a',parameters:['text'],missing:[]},clone:{status:'unsupported',endpoint:null,adapter:null,parameters:['ref_audio'],missing:['ref_audio']}},unsupported_operations:['design','ensemble']};}
function make_response(){return {server_index:0,settings:{mode:'external',url:'http://saved.example:7860',external_urls:[],external_timeout_seconds:300},capabilities:make_report()};}
const numericCore=fs.readFileSync(process.argv[1].replace('app-tts-capabilities.js','app-core.js'),'utf8'),numericStart=numericCore.indexOf('function getNumFieldValue(');
vm.runInContext(numericCore.slice(numericStart,numericCore.indexOf('\n        }',numericStart)+10),context);
load();
'''
        code = DOM_FIXTURE + setup + '\n(async () => {\n' + script_body + '\n})().catch(error => { console.error(error); process.exitCode = 1; });'
        result = subprocess.run(['node', '-e', code, str(STATIC/'js/app-tts-capabilities.js')], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_external_capabilities_check_is_explicit_and_renders_report(self):
        script_body = r'''
assert.equal(post_calls.length,0);
context.setTtsCapabilityModes({external:{description:'External mode.',language_note:'Note.',controls:{server:true,external_timeout:true,device:false,local_options:false},voice_types:{custom:true,clone:true}}});
assert.equal(post_calls.length,0);
await context.checkTtsExternalCapabilities();
assert.equal(post_calls.length,1);
assert.equal(post_calls[0][0],'/api/tts/external-capabilities');
assert.equal(post_calls[0][1].server_index,0);
const results_text=rendered_text();
assert.match(results_text,/Schema compatibility report/);
assert.match(results_text,/custom: status supported/);
assert.match(results_text,/custom-endpoint/);
assert.match(results_text,/required fields text/);
assert.match(results_text,/clone: status unsupported/);
assert.match(results_text,/missing fields ref_audio/);
assert.match(results_text,/Unsupported server operations: design, ensemble/);
assert.match(document.getElementById('tts-external-capabilities-status').textContent,/generation revalidates every call/);
'''
        self.run_external_capabilities_case(script_body)

    def test_external_capabilities_unsaved_mismatch_and_edit_clear_report(self):
        script_body = r'''
await context.checkTtsExternalCapabilities();
assert.match(rendered_text(),/custom-endpoint/);
context.clearTtsExternalCapabilitiesReport('Saved server check cleared because settings changed.');
assert.equal(document.getElementById('tts-external-capabilities-results').textContent,'');
document.getElementById('tts-url').value='http://unsaved.example:7860';
await context.checkTtsExternalCapabilities();
assert.match(document.getElementById('tts-external-capabilities-status').textContent,/Save Configuration first/);
assert.equal(document.getElementById('tts-external-capabilities-results').textContent,'');
'''
        self.run_external_capabilities_case(script_body)

    def test_external_capabilities_overlap_late_response_is_discarded(self):
        script_body = r'''
const pending=[];
context.API={post(url,body){return new Promise(resolve=>{pending.push({body,resolve});});}};
const first_request=context.checkTtsExternalCapabilities();
const second_request=context.checkTtsExternalCapabilities();
assert.equal(pending.length,1);
await second_request;
document.getElementById('tts-url').value='http://edited.example:7860';
context.clearTtsExternalCapabilitiesReport('Settings changed.');
pending[0].resolve(make_response());
await first_request;
assert.equal(rendered_text(),'');
document.getElementById('tts-url').value='http://saved.example:7860';
const latest_request=context.checkTtsExternalCapabilities();
assert.equal(pending.length,2);
pending[1].resolve(make_response());
await latest_request;
assert.match(rendered_text(),/custom-endpoint/);
assert.match(document.getElementById('tts-external-capabilities-status').textContent,/schema check completed/);
'''
        self.run_external_capabilities_case(script_body)

    def test_external_capabilities_malicious_metadata_is_inert_text(self):
        script_body = r'''
const payload='<img src=x onerror=alert(1)>';
context.API={post(){const report=make_report();report.operations.custom.endpoint=payload;return Promise.resolve({server_index:0,settings:{mode:'external',url:'http://saved.example:7860',external_urls:[],external_timeout_seconds:300},capabilities:report});}};
await context.checkTtsExternalCapabilities();
const results_element=document.getElementById('tts-external-capabilities-results');
assert.match(rendered_text(),/<img src=x onerror=alert\(1\)>/);
const seen_tags=[];
function walk_element(node){seen_tags.push((node.tagName||node.tag||'').toUpperCase());for(const child of node.children||[]){walk_element(child);}}
walk_element(results_element);
assert(!seen_tags.includes('IMG'));
assert(!seen_tags.includes('SCRIPT'));
'''
        self.run_external_capabilities_case(script_body)

    def test_blank_saved_timeout_uses_same_default_as_configuration_save(self):
        self.run_external_capabilities_case(r'''
for(const raw of ['', 'not-a-number', 'Infinity']){
document.getElementById('tts-external-timeout').value=raw;
await context.checkTtsExternalCapabilities();
assert.match(rendered_text(),/custom-endpoint/);
assert.match(document.getElementById('tts-external-capabilities-status').textContent,/schema check completed/);
}
''')

    def test_check_errors_disclose_safe_actionable_category(self):
        self.run_external_capabilities_case(r'''
for(const [status,pattern] of [[400,/Save Configuration/],[409,/running|changed/],[503,/cleanup/]]){
 context.API={post(){return Promise.reject(Object.assign(new Error('PRIVATE_TOKEN'),{status,detail:'PRIVATE_TOKEN'}));}};
 await context.checkTtsExternalCapabilities();
 const text=document.getElementById('tts-external-capabilities-status').textContent;
 assert.match(text,pattern);assert(!text.includes('PRIVATE_TOKEN'));
 assert.equal(document.getElementById('tts-external-capabilities-check').disabled,false);
}
''')

    def test_external_capabilities_transport_failure_hides_raw_error(self):
        script_body = r'''
context.API={post(){return Promise.reject(new Error('PRIVATE_TOKEN should not appear'));}};
await context.checkTtsExternalCapabilities();
const status_text=document.getElementById('tts-external-capabilities-status').textContent;
assert.match(status_text,/Saved server check failed/);
assert(!status_text.includes('PRIVATE_TOKEN'));
assert(!document.getElementById('tts-external-capabilities-results').textContent.includes('PRIVATE_TOKEN'));
'''
        self.run_external_capabilities_case(script_body)

    def load_external_capabilities_endpoint(self, config_results, client_class):
        system_path = Path(STATIC.parent / 'routers' / 'system.py')
        source_text = system_path.read_text(encoding='utf-8')
        tree = ast.parse(source_text)
        selected_nodes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == 'ExternalCapabilitiesRequest':
                selected_nodes.append(node)
            if isinstance(node, ast.FunctionDef) and node.name in ('get_tts_external_capabilities', 'get_external_capability_settings'):
                node.decorator_list = []
                selected_nodes.append(node)
        self.assertEqual(3, len(selected_nodes))
        from fastapi import HTTPException
        from pydantic import BaseModel, StrictInt
        from external_tts_client import get_external_tts_urls, get_external_tts_settings
        from external_tts_capabilities import make_unknown_capability_report, UNAVAILABLE_ERROR
        load_calls = []
        def fake_load(config_path):
            load_calls.append(config_path)
            result_index = min(len(load_calls) - 1, len(config_results) - 1)
            return config_results[result_index]
        namespace = {
            'BaseModel': BaseModel,
            'StrictInt': StrictInt,
            'HTTPException': HTTPException,
            'load_app_config_result': fake_load,
            'project_manager': SimpleNamespace(config_path='saved-config.json'),
            'ExternalTTSClient': client_class,
            'get_external_tts_urls': get_external_tts_urls,
            'get_external_tts_settings': get_external_tts_settings,
            'make_unknown_capability_report': make_unknown_capability_report,
            'UNAVAILABLE_ERROR': UNAVAILABLE_ERROR,
        }
        module_node = ast.Module(body=selected_nodes, type_ignores=[])
        ast.fix_missing_locations(module_node)
        exec(compile(module_node, str(system_path), 'exec'), namespace)
        return namespace, load_calls

    def test_real_route_admission_is_bounded_and_releases_on_bad_body(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import system
        from external_tts_capabilities import make_unknown_capability_report, UNAVAILABLE_ERROR
        instances = []
        class Client:
            def __init__(self, url, timeout):
                self.url = url
                instances.append(self)
            def get_capabilities(self):
                return make_unknown_capability_report(self.url, UNAVAILABLE_ERROR)
            def close(self):
                self.closed = True
        app = FastAPI()
        app.include_router(system.router)
        with TestClient(app) as client, patch.object(system, 'ExternalTTSClient', Client), patch.object(system, 'load_app_config_result', return_value=self.make_config_result()):
            self.assertTrue(system._external_capability_check_slot.acquire(blocking=False))
            try:
                self.assertEqual(409, client.post('/api/tts/external-capabilities', json={'server_index': 0}).status_code)
                self.assertEqual([], instances)
            finally:
                system._external_capability_check_slot.release()
            self.assertEqual(422, client.post('/api/tts/external-capabilities', json={'server_index': True}).status_code)
            response = client.post('/api/tts/external-capabilities', json={'server_index': 0})
            self.assertEqual(200, response.status_code)
            self.assertEqual('unknown', response.json()['capabilities']['operations']['custom']['status'])
            self.assertTrue(instances[0].closed)
            self.assertTrue(system._external_capability_check_slot.acquire(blocking=False))
            system._external_capability_check_slot.release()

    def test_supported_report_requires_endpoint_adapter_and_parameters(self):
        self.run_external_capabilities_case(r'''
for(const key of ['endpoint','adapter','parameters']){
 const report=make_report();report.operations.custom[key]=key==='parameters'?[]:null;
 assert.equal(context.validateTtsExternalCapabilitiesReport(report),false);
}
''')

    def make_config_result(self, mode_value='external', url_value='http://saved.example:7860', pool_value=None, timeout_value=300, warnings_value=None):
        settings_value = dict(mode=mode_value, url=url_value, external_urls=[] if pool_value is None else pool_value, external_timeout_seconds=timeout_value)
        return SimpleNamespace(warnings=[] if warnings_value is None else warnings_value, data={'tts': settings_value})

    def test_external_capabilities_route_is_registered(self):
        from routers.system import router
        matching_routes = [route for route in router.routes if getattr(route, 'path', None) == '/api/tts/external-capabilities' and 'POST' in getattr(route, 'methods', set())]
        self.assertEqual(1, len(matching_routes))

    def test_external_capabilities_endpoint_success_closes_client(self):
        instances = []
        report_value = {'fingerprint': 'schema-1', 'operations': {'custom': {'status': 'supported', 'endpoint': 'custom-endpoint', 'adapter': 'adapter-a', 'parameters': ['text'], 'missing': []}, 'clone': {'status': 'supported', 'endpoint': 'clone-endpoint', 'adapter': 'adapter-b', 'parameters': ['ref_audio'], 'missing': []}}, 'unsupported_operations': ['design', 'ensemble']}
        class FakeClient:
            def __init__(self, url_value, timeout=None):
                self.url_value = url_value
                self.timeout_value = timeout
                self.close_calls = 0
                instances.append(self)
            def get_capabilities(self):
                return report_value
            def close(self):
                self.close_calls += 1
        namespace, load_calls = self.load_external_capabilities_endpoint([self.make_config_result(pool_value=['http://saved.example:7860', 'http://second.example:7860'])], FakeClient)
        result_value = namespace['get_tts_external_capabilities'](namespace['ExternalCapabilitiesRequest'](server_index=1))
        self.assertEqual(1, result_value['server_index'])
        self.assertEqual('http://second.example:7860', instances[0].url_value)
        self.assertEqual(10, instances[0].timeout_value)
        self.assertEqual(1, instances[0].close_calls)
        self.assertEqual(report_value['fingerprint'], result_value['capabilities']['fingerprint'])
        self.assertEqual(2, len(load_calls))
        function_source = ast.get_source_segment(Path(STATIC.parent / 'routers' / 'system.py').read_text(encoding='utf-8'), [node for node in ast.parse(Path(STATIC.parent / 'routers' / 'system.py').read_text(encoding='utf-8')).body if isinstance(node, ast.FunctionDef) and node.name == 'get_tts_external_capabilities'][0])
        self.assertNotIn('TTSEngine', function_source)

    def test_external_capabilities_transport_and_cleanup_failures_are_unknown(self):
        class TransportClient:
            def __init__(self, url_value, timeout=None):
                self.close_calls = 0
            def get_capabilities(self):
                raise RuntimeError('PRIVATE_TOKEN endpoint failure')
            def close(self):
                self.close_calls += 1
        namespace, _load_calls = self.load_external_capabilities_endpoint([self.make_config_result()], TransportClient)
        result_value = namespace['get_tts_external_capabilities'](namespace['ExternalCapabilitiesRequest']())
        self.assertEqual('unknown', result_value['capabilities']['operations']['custom']['status'])
        self.assertNotIn('PRIVATE_TOKEN', json.dumps(result_value))
        class CleanupClient:
            def __init__(self, url_value, timeout=None):
                pass
            def get_capabilities(self):
                return {'fingerprint': 'schema-1', 'operations': {'custom': {'status': 'supported', 'endpoint': 'endpoint', 'adapter': 'adapter', 'parameters': [], 'missing': []}, 'clone': {'status': 'supported', 'endpoint': 'endpoint', 'adapter': 'adapter', 'parameters': [], 'missing': []}}, 'unsupported_operations': ['design', 'ensemble']}
            def close(self):
                raise RuntimeError('cleanup failed')
        namespace, _load_calls = self.load_external_capabilities_endpoint([self.make_config_result()], CleanupClient)
        with self.assertRaises(Exception) as cleanup_context:
            namespace['get_tts_external_capabilities'](namespace['ExternalCapabilitiesRequest']())
        self.assertEqual(503, cleanup_context.exception.status_code)

    def test_external_capabilities_changed_settings_and_refusals(self):
        class RecordingClient:
            instances = []
            def __init__(self, url_value, timeout=None):
                RecordingClient.instances.append(url_value)
            def get_capabilities(self):
                return {'fingerprint': 'schema-1', 'operations': {'custom': {'status': 'supported', 'endpoint': 'endpoint', 'adapter': 'adapter', 'parameters': [], 'missing': []}, 'clone': {'status': 'supported', 'endpoint': 'endpoint', 'adapter': 'adapter', 'parameters': [], 'missing': []}}, 'unsupported_operations': ['design', 'ensemble']}
            def close(self):
                pass
        RecordingClient.instances = []
        namespace, _load_calls = self.load_external_capabilities_endpoint([self.make_config_result(), self.make_config_result(url_value='http://changed.example:7860')], RecordingClient)
        with self.assertRaises(Exception) as caught_context:
            namespace['get_tts_external_capabilities'](namespace['ExternalCapabilitiesRequest']())
        self.assertEqual(409, caught_context.exception.status_code)
        for config_result, request_value in [
            (self.make_config_result(warnings_value=['damaged']), namespace['ExternalCapabilitiesRequest']()),
            (self.make_config_result(mode_value='local'), namespace['ExternalCapabilitiesRequest']()),
            (self.make_config_result(pool_value=['http://saved.example:7860', 42]), namespace['ExternalCapabilitiesRequest']()),
            (self.make_config_result(), namespace['ExternalCapabilitiesRequest'](server_index=4)),
        ]:
            RecordingClient.instances = []
            refused_namespace, _refused_loads = self.load_external_capabilities_endpoint([config_result], RecordingClient)
            with self.assertRaises(Exception) as refused_context:
                refused_namespace['get_tts_external_capabilities'](request_value)
            self.assertEqual(400, refused_context.exception.status_code)
            self.assertEqual([], RecordingClient.instances)
