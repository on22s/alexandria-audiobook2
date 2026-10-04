"""The Voices-tab "Voice changes" block (#653 PR 2), run in node against the
real app-core.js: what each state offers and preselects, escaping of model and
library text, and what Apply sends."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-core.js"

PRELUDE = r'''
const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8'),payload=JSON.parse(process.argv[2]);
const calls=[];const context={window:{},currentBookFilename:'A',calls,
  API:{get:async u=>payload.suggestion,post:async(u,b)=>{calls.push(['POST',u,b]);return {};},del:async u=>{calls.push(['DEL',u]);return {};}},
  loadVoices:async()=>{},showToast:(m,k)=>calls.push(['TOAST',k,m])};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function escapeHtml('),source.indexOf('// Parse a numeric input')),context);
const a=source.indexOf('function getVoiceStateDefault('),b=source.indexOf('// Editor: from this line on',a);
vm.runInContext(source.slice(a,b),context);
(async()=>{
'''


def lib(adapter_id, used_by=(), name=None):
    return {"adapter_id": adapter_id, "name": name or adapter_id, "gender": "male", "age_group": "child",
            "used_by": list(used_by), "config": {"type": "lora", "adapter_id": adapter_id,
                                                 "adapter_path": f"lora_models/{adapter_id}"}}


def suggestion(applied=()):
    return {"speaker": "RUDY", "applied": list(applied), "chunks_built": True, "states": [
        {"from_index": 1, "gender": "male", "age_group": "toddler", "chapter": "Chapter 1",
         "sources": {"versions": [], "library_unused": [lib("baby_free")], "library_used": [], "offer_generate": False}},
        {"from_index": 29, "gender": "male", "age_group": "child", "chapter": "Chapter 5",
         "sources": {"versions": [], "library_unused": [lib("boy_free"), lib("boy_two")],
                     "library_used": [lib("boy_used", ["PAUL"])], "offer_generate": False}},
        {"from_index": None, "gender": "male", "age_group": "teen", "chapter": None,
         "sources": {"versions": [], "library_unused": [], "library_used": [], "offer_generate": True}}]}


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class VoiceStatesJsTests(unittest.TestCase):
    def run_js(self, code, payload):
        res = subprocess.run(["node", "-e", PRELUDE + code + "\n})().catch(e=>{console.error(e);process.exitCode=1;});",
                              str(SOURCE), json.dumps(payload)], capture_output=True, text=True, timeout=10)
        self.assertEqual(0, res.returncode, res.stderr)
        return json.loads(res.stdout)

    def render(self, data):
        return self.run_js("console.log(JSON.stringify(context.renderVoiceStateRows(payload.suggestion)));",
                           {"suggestion": data})

    def test_voice_change_names_identify_speaker_state_and_location(self):
        from html.parser import HTMLParser
        class Controls(HTMLParser):
            def __init__(self):
                super().__init__()
                self.names = []
            def handle_starttag(self, tag, attrs):
                if tag in ('select', 'button'):
                    self.names.append((tag, dict(attrs).get('aria-label')))
        html = self.run_js("console.log(JSON.stringify(context.renderVoiceStateRows(payload.suggestion, payload.speaker)));",
                           {"suggestion": suggestion(), "speaker": 'RUDY "< & 日本語'})
        controls = Controls()
        controls.feed(html)
        selects = [name for tag, name in controls.names if tag == 'select']
        self.assertEqual(len(selects), 3)
        self.assertIn('Voice for RUDY "< & 日本語, male, child, from line 30', selects)
        self.assertIn(('button', 'Apply voice changes for RUDY "< & 日本語'), controls.names)
        self.assertIn(('button', 'Clear voice changes for RUDY "< & 日本語'), controls.names)

    def test_unused_library_voices_are_listed_and_preselected_before_used_ones(self):
        html = self.render(suggestion())
        self.assertLess(html.index("boy_free · male · child · unused"), html.index("used by PAUL"))
        self.assertIn('value="library:boy_free" selected', html)
        self.assertNotIn('value="library:baby_free" selected', html)  # the first state keeps the main voice
        self.assertNotIn('value="library:boy_two" selected', html)

    def test_an_applied_point_is_what_shows_selected(self):
        html = self.render(suggestion([{"from_index": 29, "version_id": "child-boy_used"}]))
        self.assertIn('value="version:child-boy_used" selected', html)
        self.assertNotIn('value="library:boy_free" selected', html)

    def test_generate_is_offered_with_the_age_and_an_unmapped_state_is_disabled(self):
        html = self.render(suggestion())
        self.assertIn('data-age="teen" onclick="generateAgeVersion(this, this.dataset.age)"', html)
        self.assertIn("line not found in the Editor yet", html)
        self.assertEqual(1, html.count(" disabled>"))

    def test_library_and_model_text_is_escaped(self):
        data = suggestion()
        data["states"][1]["sources"]["library_unused"] = [lib("x", name='<img src=x onerror=alert(1)>')]
        data["states"][1]["chapter"] = '"><script>'
        html = self.render(data)
        self.assertNotIn("<img", html)
        self.assertNotIn("<script>", html)

    def test_apply_saves_a_library_voice_as_a_version_then_the_timeline(self):
        calls = self.run_js(r'''
const row=(from,age,value)=>({dataset:{fromIndex:from,age},querySelector:()=>({value})});
const rows=[row('1','toddler','main'),row('29','child','library:boy_free'),row('','teen','main')];
const card={dataset:{voice:'RUDY'},querySelectorAll:()=>rows};
context.window._voiceStateSuggestions={RUDY:payload.suggestion};
await context.applyVoiceStates({closest:()=>card});
console.log(JSON.stringify(calls));''', {"suggestion": suggestion()})
        self.assertEqual(["POST", "/api/voices/RUDY/versions",
                          {"version_id": "child-boy_free", "age_group": "child",
                           "config": {"type": "lora", "adapter_id": "boy_free", "adapter_path": "lora_models/boy_free"}}],
                         calls[0])
        self.assertEqual(["POST", "/api/voices/RUDY/version_timeline",
                          {"points": [{"from_index": 29, "version_id": "child-boy_free"}]}], calls[1])
        self.assertIn("from line 30 on", calls[2][2])

    def test_main_after_a_change_returns_to_the_main_voice_and_all_main_clears(self):
        calls = self.run_js(r'''
const row=(from,age,value)=>({dataset:{fromIndex:from,age},querySelector:()=>({value})});
context.window._voiceStateSuggestions={RUDY:payload.suggestion};
const run=async values=>{const rows=values.map(([f,v])=>row(f,'child',v));
  await context.applyVoiceStates({closest:()=>({dataset:{voice:'RUDY'},querySelectorAll:()=>rows})});};
await run([['1','version:kid'],['29','main']]);
await run([['1','main'],['29','main']]);
console.log(JSON.stringify(calls.filter(c=>c[0]!=='TOAST')));''', {"suggestion": suggestion()})
        self.assertEqual([["POST", "/api/voices/RUDY/version_timeline",
                           {"points": [{"from_index": 1, "version_id": "kid"},
                                       {"from_index": 29, "version_id": None}]}],
                          ["DEL", "/api/voices/RUDY/version_timeline"]], calls)


if __name__ == "__main__":
    unittest.main()
