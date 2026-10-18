"""Designing several voices from one chosen line in one task (#1313).

Every fixture sentence is synthetic.
"""
import asyncio
import contextlib
import io
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core
import generate_personas as personas
from persona_validation import DESIGN_COUNT_MAX
from routers import script, voices
# The module, not its TestCase classes: importing those by name makes discovery run them twice.
from tests import test_voice_design_text_range as design_tests
from tests.test_voice_design_text_range import FakeEngine, run_main

OLD_VOICE = {"type": "clone", "ref_audio": "designed_voices/persona/old/preview.wav", "ref_text": "Okay",
             "description": "Bright, quick voice.", "seed": 7}


class FailingAfter(FakeEngine):
    """Designs `limit` voices, then fails like a crashed or cancelled design."""

    def __init__(self, folder, limit):
        super().__init__(folder)
        self.limit = limit

    def generate_voice_design(self, description, sample_text):
        if len(self.texts) >= self.limit:
            raise RuntimeError("design failed")
        return super().generate_voice_design(description, sample_text)


def book(tmp):
    Path(tmp, "annotated_script.json").write_text(json.dumps(design_tests.ChosenLineDesignTests.SCRIPT))
    Path(tmp, "voice_config.json").write_text(json.dumps({"ALICE": OLD_VOICE}))


def saved_alice(tmp):
    return json.loads(Path(tmp, "voice_config.json").read_text())["ALICE"]


class DesignCountCliTests(unittest.TestCase):
    def test_three_designs_load_the_engine_once_and_save_every_voice(self):
        with tempfile.TemporaryDirectory() as tmp:
            book(tmp)
            engine = FakeEngine(tmp)
            with patch.object(personas, "design_from_chosen_line", wraps=personas.design_from_chosen_line) as design:
                run_main(tmp, ["--speaker", "ALICE", "--ref-text=Okay", "--design-count", "3"], engine=engine)
            saved = saved_alice(tmp)
        self.assertEqual(engine.texts, ["Okay"] * 3)
        # One engine, so the model stays loaded across the designs.
        self.assertEqual(design.call_count, 3)
        self.assertEqual({id(call.args[4]) for call in design.call_args_list}, {id(engine)})
        old, *designed = saved["candidates"]
        self.assertEqual(old["ref_audio"], OLD_VOICE["ref_audio"])
        self.assertEqual(len(designed), 3)
        self.assertEqual(len({c["candidate_id"] for c in saved["candidates"]}), 4)
        self.assertEqual(len({c["ref_audio"] for c in designed}), 3)
        # The newest design is the card's voice, as after a single Design.
        self.assertEqual(saved["active_candidate"], designed[-1]["candidate_id"])
        self.assertEqual(saved["ref_audio"], designed[-1]["ref_audio"])

    def test_a_failure_part_way_keeps_every_voice_already_designed(self):
        with tempfile.TemporaryDirectory() as tmp:
            book(tmp)
            engine = FailingAfter(tmp, limit=2)
            with self.assertRaises(RuntimeError):
                run_main(tmp, ["--speaker", "ALICE", "--ref-text=Okay", "--design-count", "5"], engine=engine)
            saved = saved_alice(tmp)
        old, *designed = saved["candidates"]
        self.assertEqual(old["ref_audio"], OLD_VOICE["ref_audio"])
        self.assertEqual(len(designed), 2)
        self.assertEqual(saved["active_candidate"], designed[-1]["candidate_id"])

    def test_count_outside_the_bounds_or_without_a_line_is_refused(self):
        cases = (["--speaker", "ALICE", "--ref-text=Okay", "--design-count", "0"],
                 ["--speaker", "ALICE", "--ref-text=Okay", "--design-count", str(DESIGN_COUNT_MAX + 1)],
                 ["--speaker", "ALICE", "--design-count", "2"])
        for arguments in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as tmp:
                book(tmp)
                engine = FakeEngine(tmp)
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    run_main(tmp, arguments, engine=engine)
                self.assertEqual(engine.texts, [])
                self.assertEqual(saved_alice(tmp), OLD_VOICE)


class DesignCountRouteTests(unittest.TestCase):
    def test_chosen_line_status_ignores_live_prompt_then_regular_personas_restore_it(self):
        states = {"persona": {"running": False, "cancel": False, "logs": []}}
        observed = []
        with tempfile.TemporaryDirectory() as tmp:
            stack, client = design_tests.DesignLineRouteTests().client(tmp, {"ALICE": {"description": "Bright."}})
            pending = Path(tmp, "manual_llm", "pending.json")
            pending.parent.mkdir()
            pending.write_text(json.dumps({"id": "other-live-request", "sequence": 1}))
            before = pending.read_bytes()
            def observe(*_args):
                observed.append(asyncio.run(script.get_status("persona")))
            with stack, patch.object(voices, "project_manager", SimpleNamespace(engine=None)), \
                 patch.object(voices, "process_state", states), patch.object(script, "process_state", states), \
                 patch.object(script, "DATA_DIR", tmp), patch.object(core, "DATA_DIR", tmp), \
                 patch.object(script, "is_manual_request_owner_alive", return_value=True), \
                 patch.object(voices, "check_global_gpu_lock"), patch.object(core, "process_state", states), \
                 patch.object(core, "_task_claims", {}), patch.object(core, "_gpu_leases", {}), \
                 patch.object(core, "acquire_gpu_lock", return_value=None), \
                 patch.object(core, "llm_is_on_this_gpu", return_value=True), \
                 patch.object(voices, "run_process", side_effect=observe):
                for payload in ({"ref_text": "Okay", "design_count": 1},
                                {"ref_text": "Okay", "design_count": 3}, {}, {"advanced": True}):
                    with self.subTest(payload=payload):
                        response = client.post("/api/generate_personas", json={"speaker": "ALICE", **payload})
                        self.assertEqual(200, response.status_code, response.text)
                        status = observed[-1]
                        self.assertTrue(status["running"])
                        if "ref_text" in payload:
                            self.assertIsNone(status["manual_request"])
                        else:
                            self.assertEqual("other-live-request", status["manual_request"]["id"])
                        self.assertEqual("other-live-request",
                                         asyncio.run(script.manual_llm_pending())["pending"]["id"])
                        self.assertEqual(before, pending.read_bytes())

    def test_failed_registration_does_not_change_manual_capability(self):
        from fastapi import BackgroundTasks
        for capability, payload in ((False, {}), (True, {"ref_text": "Okay"})):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as tmp:
                states = {"persona": {"running": False, "cancel": False, "logs": [],
                                      "accepts_manual_requests": capability}}
                stack, _client = design_tests.DesignLineRouteTests().client(tmp, {"ALICE": {"description": "Bright."}})
                with stack, patch.object(voices, "project_manager", SimpleNamespace(engine=None)), \
                     patch.object(voices, "process_state", states), patch.object(voices, "check_global_gpu_lock"), \
                     patch.object(voices, "schedule_claimed_background_task", side_effect=RuntimeError("registration failed")):
                    with self.assertRaisesRegex(RuntimeError, "registration failed"):
                        asyncio.run(voices.generate_personas(
                            BackgroundTasks(), voices.GeneratePersonasRequest(speaker="ALICE", **payload)))
                self.assertEqual(capability, states["persona"]["accepts_manual_requests"])

    def post(self, payload):
        state = {"persona": {"running": False, "cancel": True, "logs": []}}
        with tempfile.TemporaryDirectory() as tmp:
            stack, client = design_tests.DesignLineRouteTests().client(tmp, {"ALICE": {"description": "Bright."}})
            with stack, patch.object(voices, "project_manager", SimpleNamespace(engine=None)), \
                 patch.object(voices, "process_state", state), \
                 patch.object(voices, "check_global_gpu_lock"), \
                 patch.object(core, "process_state", state), \
                 patch.object(core, "_task_claims", {}), \
                 patch.object(core, "_gpu_leases", {}), \
                 patch.object(core, "acquire_gpu_lock", return_value=None), \
                 patch.object(core, "llm_is_on_this_gpu", return_value=True), \
                 patch.object(voices, "run_process") as run:
                response = client.post("/api/generate_personas", json=payload)
        return response, run

    def test_count_reaches_the_command_only_when_above_one(self):
        for count, expected in ((5, ["--design-count", "5"]), (1, None), (None, None)):
            payload = {"speaker": "ALICE", "ref_text": "Okay"}
            if count is not None:
                payload["design_count"] = count
            with self.subTest(count=count):
                response, run = self.post(payload)
                self.assertEqual(response.status_code, 200, response.text)
                command = run.call_args.args[0]
                if expected:
                    index = command.index("--design-count")
                    self.assertEqual(command[index:index + 2], expected)
                else:
                    self.assertNotIn("--design-count", command)

    def test_bad_counts_are_refused_before_the_task_starts(self):
        for payload in ({"speaker": "ALICE", "ref_text": "Okay", "design_count": 0},
                        {"speaker": "ALICE", "ref_text": "Okay", "design_count": DESIGN_COUNT_MAX + 1},
                        {"speaker": "ALICE", "design_count": 3}):
            with self.subTest(payload=payload):
                response, run = self.post(payload)
                self.assertEqual(response.status_code, 422, response.text)
                run.assert_not_called()


class DesignCountUiTests(unittest.TestCase):
    CORE = Path(__file__).resolve().parent.parent / "static/js/app-core.js"

    def test_card_input_matches_the_server_bound(self):
        markup = re.search(r'<input type="number" class="form-control design-count"[^>]*>', self.CORE.read_text())
        self.assertIsNotNone(markup)
        self.assertIn('min="1"', markup.group(0))
        self.assertIn(f'max="{DESIGN_COUNT_MAX}"', markup.group(0))
        self.assertIn('value="1"', markup.group(0))

    def test_design_sends_the_clamped_count(self):
        code = r'''const fs=require('fs'),vm=require('vm'),assert=require('assert');
const s=fs.readFileSync(process.argv[1],'utf8');
const posts=[],toasts=[];
const c={window:{},showToast:(m)=>toasts.push(m),flushVoiceSaves:async()=>{},pollPersonaStatus:()=>{},
  showActionError:(t,e)=>{throw e;},API:{post:async(url,body)=>{posts.push(body);}}};
vm.createContext(c);
vm.runInContext(s.slice(s.indexOf('function getDesignCount('),s.indexOf('window.generateAgeVersion')),c);
function card(value){const input={value,max:'100'};
  return{dataset:{voice:'ALICE'},querySelector:(q)=>q==='.design-line-select'?{value:'Okay'}:q==='.design-count'?input:null};}
(async()=>{
  for(const [value,expected] of [['3',3],['1',1],['250',100],['0',1],['-4',1],['2.5',1],['',1],['abc',1]]){
    const k=card(value);
    await c.window.designFromChosenLine({closest:()=>k});
    assert.strictEqual(posts.at(-1).design_count,expected,'value '+value);
    assert.strictEqual(posts.at(-1).ref_text,'Okay');
  }
  assert(toasts[0].includes('3 voices for ALICE'));
  assert(toasts[1].includes("ALICE's voice"));
  process.stdout.write('ok');
})().catch(e=>{console.error(e);process.exitCode=1;});'''
        result = subprocess.run(["node", "-e", code, str(self.CORE)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "ok")


if __name__ == "__main__":
    unittest.main()
