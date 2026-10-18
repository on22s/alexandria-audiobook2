"""Saved voice candidates never lose the voice a card holds (#1297), and list
newest first (#1300)."""
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import generate_personas
import routers.voices as voices_module
from tests.test_voice_design_text_range import FakeEngine
from voice_candidates import get_entry_using_candidate, get_entry_with_current_candidate, get_voice_candidate

DESIGNED = {"type": "clone", "ref_audio": "designed_voices/persona/new/preview.wav",
            "ref_text": "Bring the second lantern.", "description": "Bright, quick voice.",
            "character_style": "Bright, quick voice.", "seed": 7}
OLD = {"type": "clone", "ref_audio": "designed_voices/persona/old/preview.wav",
       "ref_text": "Okay then.", "description": "Bright, quick voice.",
       "character_style": "Bright, quick voice.", "seed": 7}


class CurrentCandidateTests(unittest.TestCase):
    def test_the_current_voice_is_added_once_and_made_active(self):
        entry = {**DESIGNED, "persona_status": "approved", "ready": True}
        once = get_entry_with_current_candidate(entry)
        twice = get_entry_with_current_candidate(once)
        [candidate] = twice["candidates"]
        self.assertEqual(candidate["ref_audio"], DESIGNED["ref_audio"])
        self.assertEqual(twice["active_candidate"], candidate["candidate_id"])
        self.assertNotIn("ready", candidate)  # character fields stay off candidates
        self.assertNotIn("candidates", entry)  # the input is not changed

    def test_resaving_keeps_the_original_place_in_the_list(self):
        old = get_voice_candidate(OLD)
        entry = {**DESIGNED, "candidates": [old, get_voice_candidate(DESIGNED)]}
        updated = get_entry_with_current_candidate(entry)
        self.assertEqual([c["candidate_id"] for c in updated["candidates"]],
                         [c["candidate_id"] for c in entry["candidates"]])

    def test_the_save_time_is_recorded_once_and_is_not_part_of_the_voice(self):
        first = datetime(2026, 10, 17, 23, 52, 43, tzinfo=timezone.utc)
        later = datetime(2026, 10, 18, 1, 0, 0, tzinfo=timezone.utc)
        once = get_entry_with_current_candidate(DESIGNED, now=first)
        again = get_entry_with_current_candidate(once, now=later)
        [candidate] = again["candidates"]
        self.assertEqual(candidate["saved_at"], "2026-10-17T23:52:43Z")
        self.assertEqual(candidate["candidate_id"], get_voice_candidate(DESIGNED)["candidate_id"])

    def test_a_voice_saved_under_another_id_is_not_saved_twice(self):
        lora = {"type": "lora", "adapter_path": "lora_models/warm_tenor", "seed": 3}
        entry = {**lora, "candidates": [{**lora, "candidate_id": "warm_tenor", "rank": 1}]}
        updated = get_entry_with_current_candidate(entry)
        self.assertEqual([c["candidate_id"] for c in updated["candidates"]], ["warm_tenor"])
        self.assertEqual(updated["active_candidate"], "warm_tenor")

    def test_a_seedless_candidate_is_the_same_voice_whatever_the_current_seed(self):
        for current_seed in (3, -1):
            with self.subTest(seed=current_seed):
                entry = {"type": "lora", "adapter_path": "lora_models/warm_tenor",
                         "seed": current_seed,
                         "candidates": [{"type": "lora",
                                         "adapter_path": "lora_models/warm_tenor",
                                         "candidate_id": "warm_tenor", "rank": 1}]}
                updated = get_entry_with_current_candidate(entry)
                self.assertEqual([c["candidate_id"] for c in updated["candidates"]],
                                 ["warm_tenor"])
                self.assertEqual(updated["active_candidate"], "warm_tenor")

    def test_a_saved_seed_is_part_of_the_voice(self):
        entry = {"type": "lora", "adapter_path": "lora_models/warm_tenor", "seed": 5,
                 "candidates": [{"type": "lora", "adapter_path": "lora_models/warm_tenor",
                                 "seed": 3, "candidate_id": "warm_tenor", "rank": 1}]}
        updated = get_entry_with_current_candidate(entry)
        self.assertEqual(len(updated["candidates"]), 2)
        self.assertEqual(updated["candidates"][0]["candidate_id"], "warm_tenor")
        self.assertEqual(updated["active_candidate"], updated["candidates"][1]["candidate_id"])
        self.assertNotEqual(updated["active_candidate"], "warm_tenor")

    def test_a_seedless_candidate_with_another_adapter_is_another_voice(self):
        entry = {"type": "lora", "adapter_path": "lora_models/cool_baritone", "seed": 3,
                 "candidates": [{"type": "lora", "adapter_path": "lora_models/warm_tenor",
                                 "candidate_id": "warm_tenor", "rank": 1}]}
        updated = get_entry_with_current_candidate(entry)
        self.assertEqual(len(updated["candidates"]), 2)
        self.assertEqual(updated["candidates"][0]["candidate_id"], "warm_tenor")
        self.assertEqual(updated["active_candidate"], updated["candidates"][1]["candidate_id"])
        self.assertNotEqual(updated["active_candidate"], "warm_tenor")

    def test_using_a_candidate_installs_exactly_its_voice(self):
        lora = {"candidate_id": "warm_tenor", "type": "lora", "adapter_path": "lora_models/warm_tenor",
                "rank": 1, "favorite": True}
        card = {**OLD, "ready": True, "persona_state": "teen", "description": "Teen voice."}
        used = get_entry_using_candidate(card, lora, kept={"description", "persona_state"})
        self.assertEqual(used["type"], "lora")
        for leftover in ("ref_audio", "ref_text", "character_style"):
            self.assertNotIn(leftover, used)  # nothing left from the clone voice
        self.assertEqual(used["seed"], OLD["seed"])  # the character's seed survives
        self.assertEqual((used["description"], used["persona_state"], used["ready"]),
                         ("Teen voice.", "teen", True))  # protected and character fields stay
        self.assertNotIn("rank", used)
        self.assertEqual(used["active_candidate"], "warm_tenor")

    def test_an_entry_without_a_voice_adds_nothing(self):
        entry = {"type": "custom"}
        self.assertEqual(get_entry_with_current_candidate(entry), entry)


class UseKeepsTheReplacedVoiceTests(unittest.TestCase):
    """#1297: Design, then press "Use" on an older candidate."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = Path(folder.name, "voices.json")
        script = Path(folder.name, "script.json")
        script.write_text(json.dumps([{"speaker": "ALICE", "text": "Okay then."}]))
        for field, path in (("SCRIPT_PATH", script), ("VOICE_CONFIG_PATH", self.config)):
            patcher = patch.object(voices_module, field, str(path))
            patcher.start()
            self.addCleanup(patcher.stop)
        api = FastAPI()
        api.include_router(voices_module.router)
        self.client = TestClient(api)
        self.addCleanup(self.client.close)

    def use(self, candidate_id):
        response = self.client.post(f"/api/voices/ALICE/candidates/{candidate_id}/select")
        self.assertEqual(200, response.status_code, response.text)
        return json.loads(self.config.read_text())["ALICE"]

    def test_the_designed_voice_survives_using_an_older_candidate(self):
        # The state #1284 left behind: the designed voice is current, only the
        # replaced voice is a candidate.
        old = get_voice_candidate(OLD)
        self.config.write_text(json.dumps({"ALICE": {**DESIGNED, "candidates": [old]}}))
        after_use = self.use(old["candidate_id"])
        self.assertEqual(after_use["ref_audio"], OLD["ref_audio"])
        designed_id = get_voice_candidate(DESIGNED)["candidate_id"]
        self.assertIn(designed_id, [c["candidate_id"] for c in after_use["candidates"]])
        restored = self.use(designed_id)
        self.assertEqual((restored["ref_audio"], restored["ref_text"]),
                         (DESIGNED["ref_audio"], DESIGNED["ref_text"]))
        self.assertEqual(restored["active_candidate"], designed_id)

    def test_use_keeps_candidate_bookkeeping_off_the_card_and_adds_no_duplicates(self):
        lora = {"type": "lora", "adapter_path": "lora_models/warm_tenor", "seed": 3}
        ranked = {**lora, "candidate_id": "warm_tenor", "rank": 1, "favorite": True,
                  "saved_at": "2026-10-17T23:52:43Z"}
        old = get_voice_candidate(OLD)
        self.config.write_text(json.dumps({"ALICE": {**OLD, "candidates": [old, ranked]}}))
        on_lora = self.use("warm_tenor")
        for field in ("rank", "favorite", "saved_at", "candidate_id"):
            self.assertNotIn(field, on_lora)
        back = self.use(old["candidate_id"])
        self.assertEqual(sorted(c["candidate_id"] for c in back["candidates"]),
                         sorted([old["candidate_id"], "warm_tenor"]))


class GeneratedVoicesAreSavedTests(unittest.TestCase):
    def test_a_generated_persona_voice_is_a_saved_active_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            voice_config = {"ALICE": {"type": "custom"}}
            saved = generate_personas._save_generated_preview(
                tmp, FakeEngine(tmp), voice_config, "ALICE", "Bright, quick voice.", "Okay then.",
                book_id="book")
            self.assertTrue(saved)
            entry = voice_config["ALICE"]
        [candidate] = entry["candidates"]
        self.assertEqual(candidate["ref_audio"], entry["ref_audio"])
        self.assertEqual(entry["active_candidate"], candidate["candidate_id"])

    def test_designing_keeps_both_voices_and_makes_the_new_one_active(self):
        lines = ["Okay then.", "Bring the second lantern."]
        with tempfile.TemporaryDirectory() as tmp:
            result = generate_personas.design_from_chosen_line(
                {"ALICE": copy.deepcopy(OLD)}, "ALICE", "Bring the second lantern.", lines,
                FakeEngine(tmp), tmp, book_id="book")
        entry = result["ALICE"]
        ids = [c["candidate_id"] for c in entry["candidates"]]
        self.assertEqual(ids[0], get_voice_candidate(OLD)["candidate_id"])  # replaced voice first
        self.assertEqual(len(ids), 2)
        self.assertEqual(entry["active_candidate"], ids[1])  # the new voice, active
        self.assertEqual(entry["candidates"][1]["ref_audio"], entry["ref_audio"])


class NewestFirstListTests(unittest.TestCase):
    """#1300, through the real app-core.js functions in node."""

    def test_newest_first_with_ranked_runs_in_rank_order_and_in_use_marked(self):
        core = Path(__file__).resolve().parent.parent / "static/js/app-core.js"
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');
const c={window:{},escapeHtml:x=>String(x),getInlineStringArgument:x=>JSON.stringify(x),getLocalAudioUrl:p=>'/'+p,getLoraModelsById:()=>new Map()};vm.createContext(c);
const start=s.indexOf('function getVoiceCandidateMarkup(');
vm.runInContext(s.slice(start,s.indexOf('function getLibraryVoiceReference(')),c);
const saved=[{candidate_id:'design-old'},{candidate_id:'lora-a',rank:1},{candidate_id:'lora-b',rank:2},
             {candidate_id:'lora-c',rank:1},{candidate_id:'design-new'}];
const order=c.getCandidatesNewestFirst(saved).map(x=>x.candidate_id);
assert.strictEqual(JSON.stringify(order),JSON.stringify(['design-new','lora-c','lora-a','lora-b','design-old']));
const html=c.getVoiceCandidateMarkup(saved,'design-new');
assert(html.indexOf('design-new')<html.indexOf('design-old'),'newest is listed first');
assert.strictEqual((html.match(/>In use</g)||[]).length,1,'exactly one In use');
assert(/is in use">In use</.test(html));
assert(!/selectVoiceCandidate\(this, "design-new"\)/.test(html),'the active voice has no Use button');
assert(/selectVoiceCandidate\(this, "design-old"\)/.test(html));
const stamped={candidate_id:'design-9f312197a74e',saved_at:'2026-10-17T23:55:37Z'};
assert.strictEqual(c.getCandidateLabel(stamped),'design-20261017-235537');
assert.strictEqual(c.getCandidateLabel({candidate_id:'design-01d13709db0f'}),'design-01d13709db0f');
const labelled=c.getVoiceCandidateMarkup([stamped],'');
assert(labelled.includes('>design-20261017-235537'),'the list shows the save time');
assert(labelled.includes('title="design-9f312197a74e"'),'the id stays on hover');'''
        result = subprocess.run(["node", "-e", code, str(core)], capture_output=True, text=True,
                                timeout=15, env={**os.environ, "TZ": "UTC"})
        self.assertEqual(result.returncode, 0, result.stderr)



class CandidatePlaybackTests(unittest.TestCase):
    """#1299: one Play/Stop button per candidate, one candidate playing at a time."""

    def test_play_stop_switches_and_only_one_candidate_plays(self):
        core = Path(__file__).resolve().parent.parent / "static/js/app-core.js"
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');
const played=[];
class Audio{constructor(url){this.url=url;this.paused=true;this.listeners={};played.push(this);}
  addEventListener(name,fn){this.listeners[name]=fn;} async play(){this.paused=false;} pause(){this.paused=true;}}
function button(){return{dataset:{label:'candidate x'},innerHTML:'',attrs:{},classes:new Set(['btn-outline-primary']),
  setAttribute(k,v){this.attrs[k]=v;},classList:{toggle:(c,on)=>{}},closest:()=>({})};}
const candidates=[{candidate_id:'design-a',type:'clone',ref_audio:'designed_voices/a.wav'},
  {candidate_id:'design-b',type:'clone',ref_audio:'designed_voices/b.wav'},
  {candidate_id:'warm_tenor',type:'lora',adapter_id:'warm_tenor'},
  {candidate_id:'builtin_tenor',type:'builtin_lora',adapter_id:'builtin_tenor'},
  {candidate_id:'builtin_silent',type:'builtin_lora',adapter_id:'builtin_silent'},
  {candidate_id:'bare',type:'lora',adapter_id:'missing'}];
let toasts=[];
const c={window:{},Audio,escapeHtml:x=>String(x),getInlineStringArgument:x=>JSON.stringify(x),
  getLocalAudioUrl:(p)=>'/'+p,getLoraModelsById:()=>new Map([
    ['warm_tenor',{id:'warm_tenor',preview_audio_url:'/lora/warm.wav'}],
    ['builtin_tenor',{id:'builtin_tenor',builtin:true,preview_audio_url:'/builtin_lora/builtin_tenor/preview_sample.wav'}],
    ['builtin_silent',{id:'builtin_silent',builtin:true,preview_audio_url:null}]]),
  getVoiceCardMetadata:()=>({candidates}),showToast:(m)=>toasts.push(m)};
vm.createContext(c);
vm.runInContext(s.slice(s.indexOf('function getVoiceCandidateMarkup('),s.indexOf('// Gender/age per line (#653)')),c);
(async()=>{
  const a=button(),b=button();
  await c.window.toggleCandidatePlayback(a,'design-a');
  assert.strictEqual(played[0].url,'/designed_voices/a.wav'); assert(a.innerHTML.includes('fa-stop'));
  await c.window.toggleCandidatePlayback(b,'design-b');
  assert(played[0].paused,'starting another candidate stops the first'); assert(a.innerHTML.includes('fa-play'));
  assert(b.innerHTML.includes('fa-stop')); assert.strictEqual(played.length,2);
  await c.window.toggleCandidatePlayback(b,'design-b');
  assert(played[1].paused,'pressing the playing one stops it'); assert(b.innerHTML.includes('fa-play'));
  assert.strictEqual(played.length,2,'stopping does not start anything');
  await c.window.toggleCandidatePlayback(a,'design-a'); played[2].listeners.ended();
  assert(a.innerHTML.includes('fa-play'),'the end of the audio resets the button');
  assert.strictEqual(c.window._candidatePlayback,null);
  await c.window.toggleCandidatePlayback(button(),'warm_tenor');
  assert.strictEqual(played[3].url,'/lora/warm.wav','a LoRA candidate plays its adapter preview');
  await c.window.toggleCandidatePlayback(button(),'builtin_tenor');
  assert.strictEqual(played.length,5,'a built-in LoRA candidate starts its preview');
  assert.strictEqual(played[4].url,'/builtin_lora/builtin_tenor/preview_sample.wav');
  assert(played[3].paused,'a built-in preview stops the previously playing user LoRA');
  const html=c.getVoiceCandidateMarkup(candidates,'');
  assert.strictEqual((html.match(/candidate-play/g)||[]).length,4,'only candidates with audio get a Play button');
  assert(/toggleCandidatePlayback\(this, "builtin_tenor"\)/.test(html));
  assert(!/toggleCandidatePlayback\(this, "builtin_silent"\)/.test(html));
  assert(!/toggleCandidatePlayback\(this, "bare"\)/.test(html));
  process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result = subprocess.run(["node", "-e", code, str(core)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")

if __name__ == "__main__":
    unittest.main()
