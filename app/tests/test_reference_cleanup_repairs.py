"""Reproduce reviewed cleanup boundary and ordinary import regressions."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

import reference_cleanup
from reference_cleanup import apply_affine_cleanup, validate_cleanup_plan, verify_affine_candidate
from routers import voice_design
from tests import test_clone_import_lifecycle_js


def _synthetic_wav(path, peak=0.2, seconds=5, rate=24000):
    samples = (peak * np.sin(2 * np.pi * 220 * np.arange(rate * seconds) / rate) * 32767).astype(np.int16)
    sf.write(path, samples, rate, subtype="PCM_16")
    return path.read_bytes()


class CleanupBoundaryRepairs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.cleanup = self.uploads / ".reference-cleanup"
        self.cleanup.mkdir()
        directory_patch = patch.object(voice_design, "UPLOADS_DIR", str(self.uploads))
        directory_patch.start()
        self.addCleanup(directory_patch.stop)
        application = FastAPI()
        application.include_router(voice_design.router)
        self.client = TestClient(application, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_encoded_parent_reject_is_refused_before_any_delete(self):
        sentinel = self.uploads / "unrelated-source.wav"
        sentinel.write_bytes(b"synthetic sentinel, not audio")
        with patch.object(voice_design.shutil, "rmtree", side_effect=AssertionError("delete must not be attempted")) as removal:
            response = self.client.delete("/api/clone_voices/cleanup/%2e%2e")
        self.assertEqual(400, response.status_code, response.text)
        removal.assert_not_called()
        self.assertEqual(b"synthetic sentinel, not audio", sentinel.read_bytes())

    def test_empty_accept_handle_is_a_client_refusal(self):
        response = self.client.post("/api/clone_voices/cleanup/accept", data={
            "cleanup_token": "", "ref_text": "Synthetic transcript.", "rights_confirmed": "true",
            "listened_original": "true", "listened_candidate": "true"})
        self.assertEqual(400, response.status_code, response.text)


class PlainImportEditRepair(unittest.TestCase):
    def test_typing_transcript_and_confirming_rights_keeps_plain_import_live(self):
        test_clone_import_lifecycle_js.CloneImportLifecycleJsTests.run_js(self, r'''
ctx._openCloneImportModal(file('typed.wav'));
el('clone-import-ref-text').value='Synthetic spoken transcript.';
el('clone-import-ref-text').oninput();
el('clone-import-rights-confirmed').checked=true;
el('clone-import-rights-confirmed').onchange();
const pending=el('clone-import-submit').onclick();
assert.strictEqual(uploads.length,1,'typing required fields must not disable ordinary Import');
complete(0);await pending;
assert.strictEqual(hides,1);
''')


class CleanupInstrumentRepairs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_cleanup_slot_admission_blocks_concurrent_work(self):
        import threading
        original = self.root / "original.wav"
        candidate = self.root / "candidate.wav"
        sf.write(original, np.zeros(2400, dtype=np.int16), 24000, subtype="PCM_16")
        results = []
        def attempt():
            try:
                apply_affine_cleanup(str(original), str(candidate), validate_cleanup_plan(1.0, False))
                results.append("ok")
            except ValueError as error:
                results.append(str(error))
        self.assertTrue(reference_cleanup._SLOTS.acquire(blocking=False))
        self.assertTrue(reference_cleanup._SLOTS.acquire(blocking=False))
        try:
            worker = threading.Thread(target=attempt)
            worker.start()
            worker.join()
        finally:
            reference_cleanup._SLOTS.release()
            reference_cleanup._SLOTS.release()
        self.assertEqual(["Cleanup is already running; try again shortly."], results)
        apply_affine_cleanup(str(original), str(candidate), validate_cleanup_plan(1.0, False))
        self.assertTrue(candidate.is_file())

    def test_empty_candidate_is_not_valid(self):
        original = self.root / "original.wav"
        candidate = self.root / "candidate.wav"
        sf.write(original, np.zeros(0, dtype=np.int16), 24000, subtype="PCM_16")
        sf.write(candidate, np.zeros(0, dtype=np.int16), 24000, subtype="PCM_16")
        valid, problems, _ = verify_affine_candidate(str(original), str(candidate), validate_cleanup_plan(1.0, False))
        self.assertFalse(valid)
        self.assertTrue(any("empty" in problem for problem in problems), problems)

    def test_representable_sample_below_rounding_clip_is_not_clipped(self):
        original = self.root / "original.wav"
        candidate = self.root / "candidate.wav"
        samples = np.full(24000, 30000, dtype=np.int16)
        sf.write(original, samples, 24000, subtype="PCM_16")
        plan = validate_cleanup_plan(32767.2 / 30000, False)
        apply_affine_cleanup(str(original), str(candidate), plan)
        valid, problems, records = verify_affine_candidate(str(original), str(candidate), plan)
        self.assertTrue(valid, problems)
        self.assertEqual(0, records["clipped_samples"])

    def test_clipping_rule_itself_is_discriminated(self):
        original = self.root / "original.wav"
        candidate = self.root / "candidate.wav"
        samples = np.full(24000, 20000, dtype=np.int16)
        sf.write(original, samples, 24000, subtype="PCM_16")
        plan = validate_cleanup_plan(2.0, False)
        expected = np.full(24000, 32767, dtype=np.int16)
        sf.write(candidate, expected, 24000, subtype="PCM_16")
        valid, problems, records = verify_affine_candidate(str(original), str(candidate), plan)
        self.assertFalse(valid)
        self.assertTrue(any("clipping:" in problem for problem in problems), problems)
        self.assertGreater(records["clipped_samples"], 0)


class CleanupRouteRepairs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.voices = self.root / "voices"
        self.voices.mkdir()
        self.cleanup_root = self.uploads / ".reference-cleanup"
        self.cleanup_root.mkdir()
        for name, value in [("UPLOADS_DIR", str(self.uploads)), ("CLONE_VOICES_DIR", str(self.voices)), ("CLONE_VOICES_MANIFEST", str(self.voices / "manifest.json"))]:
            patcher = patch.object(voice_design, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        disk = patch.object(voice_design, "check_disk_space", return_value=(True, 20))
        disk.start()
        self.addCleanup(disk.stop)
        application = FastAPI()
        application.include_router(voice_design.router)
        self.client = TestClient(application, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def _prepare(self, peak=0.2, gain="1.5", extra=None):
        source = self.root / "source.wav"
        raw = _synthetic_wav(source, peak=peak)
        staged = self.client.post("/api/audio_channels/stage", data={"purpose": "clone"}, files={"file": ("source.wav", raw, "audio/wav")})
        self.assertEqual(200, staged.status_code, staged.text)
        data = {"preview_token": staged.json()["token"], "audio_channel": "mix", "gain": gain, "remove_dc": "false",
                "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true"}
        if extra:
            data.update(extra)
        return self.client.post("/api/clone_voices/cleanup/prepare", data=data)

    def _accept(self, body, **overrides):
        data = {"cleanup_token": body["cleanup_token"], "ref_text": "Synthetic reference transcript.", "rights_confirmed": "true",
                "listened_original": "true", "listened_candidate": "true", "raw_source_sha256": body["raw_source_sha256"],
                "normalized_original_sha256": body["normalized_original_sha256"], "candidate_sha256": body["candidate_sha256"],
                "plan_digest_value": body["plan_digest"], "provenance_digest_value": body.get("provenance_digest", ""), "audio_channel": "mix"}
        data.update(overrides)
        return self.client.post("/api/clone_voices/cleanup/accept", data=data)

    def test_prepare_requires_transcript_and_rights_provenance(self):
        response = self._prepare(extra={"ref_text": "", "rights_confirmed": "false"})
        self.assertEqual(400, response.status_code, response.text)

    def test_candidate_checks_run_and_are_returned_at_prepare(self):
        response = self._prepare(peak=0.08, gain="0.25")
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn("peak", response.text.lower() + response.text.lower())

    def test_accept_rejects_transcript_different_from_prepare(self):
        prepared = self._prepare()
        self.assertEqual(200, prepared.status_code, prepared.text)
        body = prepared.json()
        self.assertIn("candidate_measures", body)
        self.assertIn("verification", body)
        refused = self._accept(body, ref_text="A different transcript.")
        self.assertEqual(400, refused.status_code, refused.text)
        self.assertEqual([], list(self.voices.iterdir()))

    def test_expired_sessions_are_swept_before_capacity_check(self):
        for index in range(8):
            directory = self.cleanup_root / (f"{index:032x}")
            directory.mkdir()
            (directory / "session.json").write_text(json.dumps({"created": time.time() - 7200, "consumed": False}), encoding="utf-8")
        response = self._prepare()
        self.assertEqual(200, response.status_code, response.text)

    def test_malformed_session_is_a_client_refusal_not_a_server_error(self):
        directory = self.cleanup_root / ("a" * 32)
        directory.mkdir()
        (directory / "session.json").write_text(json.dumps({"consumed": False}), encoding="utf-8")
        response = self.client.get(f"/api/clone_voices/cleanup/{'a' * 32}/audio", params={"which": "original"})
        self.assertEqual(400, response.status_code, response.text)

    def test_malformed_session_fields_are_refused_before_audio_or_accept(self):
        mutations = [
            lambda session: session.pop("candidate_sha256"),
            lambda session: session.pop("publication_id"),
            lambda session: session.update(plan={"operations": []}),
            lambda session: session.update(provenance=[]),
            lambda session: session.update(created=float("nan")),
            lambda session: session.update(consumed="false"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                body = self._prepare().json()
                directory = self.cleanup_root / body["cleanup_token"]
                session_path = directory / "session.json"
                session = json.loads(session_path.read_text(encoding="utf-8"))
                mutate(session)
                session_path.write_text(json.dumps(session), encoding="utf-8")
                response = self.client.get(f"/api/clone_voices/cleanup/{body['cleanup_token']}/audio")
                self.assertEqual(400, response.status_code, response.text[:200])
                response = self._accept(body)
                self.assertEqual(400, response.status_code, response.text)
                self.assertFalse((self.voices / "manifest.json").exists())

    def test_concurrent_accept_mints_only_one_voice(self):
        prepared = self._prepare()
        self.assertEqual(200, prepared.status_code, prepared.text)
        body = prepared.json()
        import threading
        outcomes = []
        def attempt():
            outcomes.append(self._accept(body).status_code)
        workers = [threading.Thread(target=attempt) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual([200, 400], sorted(outcomes), outcomes)
        manifest = json.loads((self.voices / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(manifest))

    def test_reject_endpoint_actually_removes_session(self):
        prepared = self._prepare()
        self.assertEqual(200, prepared.status_code, prepared.text)
        token = prepared.json()["cleanup_token"]
        rejected = self.client.delete(f"/api/clone_voices/cleanup/{token}")
        self.assertEqual(200, rejected.status_code, rejected.text)
        self.assertFalse((self.cleanup_root / token).exists())
        audio = self.client.get(f"/api/clone_voices/cleanup/{token}/audio", params={"which": "candidate"})
        self.assertEqual(400, audio.status_code, audio.text)

    def test_accept_is_idempotent_after_consumed_save_failure(self):
        prepared = self._prepare()
        self.assertEqual(200, prepared.status_code, prepared.text)
        body = prepared.json()
        original_save = voice_design._save_cleanup_session
        calls = {"count": 0}
        def failing_save(directory, session):
            calls["count"] += 1
            if session.get("consumed"):
                raise RuntimeError("injected consumed-save failure")
            return original_save(directory, session)
        with patch.object(voice_design, "_save_cleanup_session", side_effect=failing_save):
            first = self._accept(body)
            self.assertIn(first.status_code, (200, 500), first.text)
        with patch.object(voice_design, "_save_cleanup_session", side_effect=failing_save):
            second = self._accept(body)
        self.assertEqual(200, second.status_code, second.text)
        manifest = json.loads((self.voices / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(1, len(manifest), manifest)
        self.assertEqual(1, len([p for p in self.voices.iterdir() if p.suffix == ".wav"]))
        self.assertFalse((self.cleanup_root / body["cleanup_token"]).exists(), "reconciliation must release its session slot")


class CleanupJsLifecycleRepairs(unittest.TestCase):
    def test_accept_freezes_reject_edits_and_plain_import_until_failure(self):
        test_clone_import_lifecycle_js.CloneImportLifecycleJsTests.run_js(self, r'''
let finishAccept;let deletes=0;
ctx.AbortController=class{constructor(){this.signal={};}abort(){}};
ctx.fetch=async(url,options)=>{
if(url.endsWith("/cleanup/prepare")){return {ok:true,json:async()=>({cleanup_token:"a".repeat(32),candidate_measures:{clipping_ratio:0.002,silence_ratio:0.1},measures:{duration_s:5},candidate_sha256:"c".repeat(64),normalized_original_sha256:"n".repeat(64)})};}
if(url.endsWith("/cleanup/accept")){return await new Promise(resolve=>{finishAccept=resolve;});}
if(options.method==="DELETE"){deletes++;return {ok:true};}
throw Error("unexpected request "+url);};
ctx.createAudioChannelPicker=()=>({getSelection:()=>({channel:"mix",token:"b".repeat(32)}),dispose(){}});
ctx._openCloneImportModal(file("x.wav"));
el("clone-import-ref-text").value="Synthetic transcript.";el("clone-import-rights-confirmed").checked=true;
await el("clone-cleanup-prepare").onclick();
assert(el("clone-cleanup-summary").textContent.includes("clipping 0.002"));
assert(el("clone-cleanup-summary").textContent.includes("silence 0.1"));
assert(el("clone-cleanup-summary").textContent.includes("c".repeat(64)));
el("clone-cleanup-listened-original").checked=true;el("clone-cleanup-listened-candidate").checked=true;
const accepting=el("clone-cleanup-accept").onclick();
assert.strictEqual(el("clone-import-fields").disabled,true);
assert.strictEqual(el("clone-cleanup-fields").disabled,true);
assert.strictEqual(el("clone-import-submit").disabled,true);
await el("clone-cleanup-reject").onclick();
await el("clone-import-submit").onclick();
assert.strictEqual(deletes,0);assert.strictEqual(uploads.length,0);
finishAccept({ok:false,status:503,json:async()=>({detail:"injected busy refusal"})});await accepting;
assert.strictEqual(el("clone-import-fields").disabled,false);
assert.strictEqual(el("clone-cleanup-fields").disabled,false);
assert.strictEqual(el("clone-import-submit").disabled,false);
assert.strictEqual(el("clone-cleanup-players").hidden,false);
''')

    def test_reject_failure_preserves_retry_state(self):
        test_clone_import_lifecycle_js.CloneImportLifecycleJsTests.run_js(self, r'''
ctx.AbortController=class{constructor(){this.signal={aborted:false};}abort(){this.signal.aborted=true;}};
ctx.fetch=async(url,options)=>{if(url.includes('/cleanup/')&&options.method==='DELETE'){return {ok:false,status:500,json:async()=>({detail:'injected delete failure'})};}if(url.includes('/cleanup/prepare')){return {ok:true,json:async()=>({cleanup_token:'a'.repeat(32),raw_source_sha256:'r',normalized_original_sha256:'n',candidate_sha256:'c',plan_digest:'p',audio_channel:'mix'})};}throw Error('unexpected url '+url);};
ctx.createAudioChannelPicker=()=>({getSelection:()=>({channel:'mix',token:'b'.repeat(32)}),dispose(){}});
ctx._openCloneImportModal(file('x.wav'));
el('clone-import-ref-text').value='Synthetic transcript.';
el('clone-import-rights-confirmed').checked=true;
await el('clone-cleanup-prepare').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,false,'prepare arms candidate');
await el('clone-cleanup-reject').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,false,'failed DELETE must preserve candidate for retry');
assert(!el('clone-import-status').textContent.includes('Candidate rejected'),'must not claim rejected on failure');
''')

    def test_gain_edit_invalidates_prepared_candidate(self):
        test_clone_import_lifecycle_js.CloneImportLifecycleJsTests.run_js(self, r'''
let deletes=0;
ctx.AbortController=class{constructor(){this.signal={aborted:false};}abort(){this.signal.aborted=true;}};
ctx.fetch=async(url,options)=>{if(url.includes('/cleanup/')&&options.method==='DELETE'){deletes++;return {ok:true,json:async()=>({status:'discarded'})};}if(url.includes('/cleanup/prepare')){return {ok:true,json:async()=>({cleanup_token:'a'.repeat(32),raw_source_sha256:'r',normalized_original_sha256:'n',candidate_sha256:'c',plan_digest:'p',audio_channel:'mix'})};}throw Error('unexpected url '+url);};
ctx.createAudioChannelPicker=()=>({getSelection:()=>({channel:'mix',token:'b'.repeat(32)}),dispose(){}});
ctx._openCloneImportModal(file('x.wav'));
el('clone-import-ref-text').value='Synthetic transcript.';
el('clone-import-rights-confirmed').checked=true;
await el('clone-cleanup-prepare').onclick();
assert.strictEqual(el('clone-cleanup-players').hidden,false,'prepare arms candidate');
el('clone-cleanup-gain').onchange();
assert.strictEqual(el('clone-cleanup-players').hidden,true,'gain edit must invalidate prepared candidate');
assert.strictEqual(deletes,1,'gain edit must DELETE the server session');
''')
