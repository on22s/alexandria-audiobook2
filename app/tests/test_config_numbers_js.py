"""Real config payload construction preserves numeric magnitude for server validation."""
import json
from pathlib import Path
import subprocess
import unittest
from pydantic import ValidationError
from config_settings import GenerationConfig, TTSConfig

SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-core.js"
FIELDS = (("top-k", "generation", "top_k", "1e1", 10),
          ("tp-chunk-size", "generation", "three_pass_chunk_size", "1e3", 1000),
          ("tp-attribute-batch-size", "generation", "three_pass_attribute_batch_size", "1e1", 10),
          ("context-rescue-retries", "generation", "context_rescue_retries", "1e0", 1),
          ("tts-external-timeout", "tts", "external_timeout_seconds", "3e2", 300),
          ("tts-max-new-tokens", "tts", "max_new_tokens", "2.048e3", 2048),
          ("sub-batch-min-size", "tts", "sub_batch_min_size", "4e0", 4),
          ("pause-between-speakers", "tts", "pause_between_speakers_ms", "5e2", 500))

class ConfigNumericJsTests(unittest.TestCase):
    def run_inputs(self, cases):
        program = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8');
const helper=source.slice(source.indexOf('function getNumFieldValue('),source.indexOf('// --- Desktop notifications ---'));
const passHelper=source.slice(source.indexOf('function passPromptFields('),source.indexOf('function renderPassPromptPresets('));
const builder=source.slice(source.indexOf('function buildConfigPayload('),source.indexOf("document.getElementById('config-form').addEventListener"));
const cases=JSON.parse(process.argv[2]),out=[];
for (const inputs of cases) {
 const fields={'tts-mode':'local','tts-device':'auto','tts-language':'English',...inputs};
 const context={document:{getElementById(id){return {value:fields[id]===undefined?'':fields[id],checked:false};}},
  llmProfiles:{local:{base_url:'http://localhost:1/v1',model_name:'fixture'}},currentLlmMode:'local',
  legacyChunkSize:3000,promptPresetPayload:()=>({active:'default',own:[]}),
  activePassPromptPreset:{pass1:'default',pass3:'default'},passPromptPresets:{pass1:[],pass3:[]},
  passPromptDefaults:{pass1:{system_prompt:'',user_prompt:''},pass3:{system_prompt:'',user_prompt:''}},selectedPromptPreset:()=>null};
 vm.createContext(context);vm.runInContext(helper+passHelper+builder,context);
 const payload=context.buildConfigPayload(2);
 assert.deepStrictEqual(fields,{'tts-mode':'local','tts-device':'auto','tts-language':'English',...inputs});
 out.push(payload);
}
console.log(JSON.stringify(out));
"""
        result = subprocess.run(["node", "-e", program, str(SOURCE), json.dumps(cases)],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_integer_fields_preserve_exponent_and_decimal_notation(self):
        cases = [{id: value} for id, _, _, value, _ in FIELDS]
        cases += [{id: str(expected) + ".0"} for id, _, _, _, expected in FIELDS]
        for spec, payload in zip(FIELDS + FIELDS, self.run_inputs(cases)):
            id, section, field, _, expected = spec
            with self.subTest(id=id, value=payload[section][field]):
                self.assertEqual(expected, payload[section][field])
                model = GenerationConfig if section == "generation" else TTSConfig
                self.assertEqual(expected, getattr(model(**payload[section]), field))

    def test_fractional_integer_fields_reach_server_validation_without_truncation(self):
        cases = [{id: str(expected) + ".5"} for id, _, _, _, expected in FIELDS]
        for spec, payload in zip(FIELDS, self.run_inputs(cases)):
            id, section, field, _, expected = spec
            with self.subTest(id=id):
                self.assertEqual(expected + .5, payload[section][field])
                model = GenerationConfig if section == "generation" else TTSConfig
                with self.assertRaises(ValidationError) as raised:
                    model(**payload[section])
                self.assertIn((field,), [error["loc"] for error in raised.exception.errors()])

    def test_empty_nonfinite_and_malformed_fields_keep_existing_defaults_and_zero_is_preserved(self):
        cases = [{"top-k": value, "temperature": "0.25"} for value in ("", "  ", "NaN", "Infinity", "10garbage", "0")]
        for index, payload in enumerate(self.run_inputs(cases)):
            with self.subTest(index=index):
                self.assertEqual(0, payload["generation"]["top_k"])
                self.assertEqual(.25, payload["generation"]["temperature"])
                self.assertEqual(0, GenerationConfig(**payload["generation"]).top_k)
