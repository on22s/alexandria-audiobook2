"""Single-voice preview + guarded save (#1724 simplification).

Real FastAPI routes (TestClient) and real TTS voice resolution.
Synthetic fixtures only: preview is read-only and the subsequent
POST /api/voice_config/save applies its proposed map through the ordinary
guarded queue contract. Every supported asset type is covered.
"""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import voices
from book_state_transaction import ensure_book_state, get_book_snapshot, get_book_snapshot_token
from voice_config_store import get_voice_config_revision
import tts


def _canonical_guards(root):
    """Exact revision/book_token via the canonical snapshot helpers under
    normal admission, without calling the roster/snapshot HTTP endpoints
    (which now refuse malformed active data)."""
    data_dir = str(root)
    with ensure_book_state(data_dir):
        snapshot = get_book_snapshot(data_dir, allow_missing_script=True)
        return {"revision": get_voice_config_revision(snapshot["voices"]),
                "book_token": get_book_snapshot_token(snapshot)}

ASSET_CASES = [
    ("custom", {"type": "custom", "voice": "Serena"}),
    ("builtin_lora", {"type": "builtin_lora", "adapter_id": "builtin-1", "adapter_path": "lora_models/builtin-1"}),
    ("lora", {"type": "lora", "adapter_id": "lora-1", "adapter_path": "lora_models/lora-1"}),
    ("clone", {"type": "clone", "ref_audio": "clone_voices/ref.wav", "ref_text": "Synthetic reference text."}),
    ("saved_designed", {"type": "clone", "ref_audio": "designed_voices/narrator.wav",
                        "ref_text": "Saved designed sample text."}),
]


def preview_and_save(client, source_speaker):
    snapshot = client.get("/api/voice_config/snapshot").json()
    preview = client.post("/api/voice_config/single-voice-preview", json={
        "source_speaker": source_speaker, "revision": snapshot["revision"],
        "book_token": snapshot["book_token"]})
    assert preview.status_code == 200, preview.text
    proposal = preview.json()
    assert proposal["revision"] == snapshot["revision"]
    result = client.post("/api/voice_config/save", json={
        "revision": proposal["revision"], "book_token": proposal["book_token"],
        "voices": proposal["voices"]})
    assert result.status_code == 200, result.text
    return snapshot, proposal, result


class SingleVoicePreviewSaveTests(unittest.TestCase):
    def fixture(self, root, stack, source_entry):
        script = root / 'annotated_script.json'
        original = (b'[{"speaker":"ALICE","text":"Synthetic first line."},'
                    b'{"speaker":"BOB","text":"Synthetic second line."},'
                    b'{"speaker":"CAROL","text":"Synthetic third line."}]')
        script.write_bytes(original)
        path = root / 'voice_config.json'
        source = {"seed": "7", "character_style": "calm", "alias_of": None, **source_entry}
        path.write_text(json.dumps({
            "ALICE": {**source, "ready": True,
                      "versions": {"v1": {"type": "custom", "voice": "Jenny", "seed": "1"}},
                      "style_timeline": [{"from_index": 0, "character_style": "steady"}],
                      "persona_voice_audit": {"notes": "preserve"}},
            "BOB": {"type": "lora", "adapter_id": "x", "adapter_path": "lora_models/x",
                    "seed": "-1", "ready": True, "ref_audio": "stale.wav", "alias_of": "ALICE",
                    "versions": {"v1": {"type": "custom", "voice": "Jenny", "seed": "1"}},
                    "version_timeline": [{"from_index": 0, "version_id": "v1"}],
                    "narrator_strategy": "focus"},
            "CAROL": {"type": "custom", "voice": "Ryan", "seed": "-1", "ready": False},
            # Unused legacy entry: not in the script, must stay untouched
            # and out of the submitted patch (not revalidated/normalised).
            "ZED_LEGACY": {"type": "custom", "voice": "Outdated", "seed": "not-a-number",
                           "legacy_notes": "preserve-me"},
        }))
        (root / 'state.json').write_text('{"active_book_id":"book","book_generation":"one"}')
        stack.enter_context(patch.object(voices, 'VOICE_CONFIG_PATH', str(path)))
        stack.enter_context(patch.object(voices, 'SCRIPT_PATH', str(script)))
        app = FastAPI()
        app.include_router(voices.router)
        return stack.enter_context(TestClient(app)), path, script, original

    def test_preview_is_read_only_and_lists_overrides(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            before = path.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(200, preview.status_code, preview.text)
            body = preview.json()
            self.assertEqual(3, body["affected_speakers"])
            self.assertEqual(3, body["affected_lines"])
            self.assertEqual(["ALICE", "BOB", "CAROL"], [row["name"] for row in body["speakers"]])
            self.assertEqual({"ALICE", "BOB", "CAROL"}, set(body["voices"]),
                             "patch scope is the active script speakers only")
            self.assertNotIn("ZED_LEGACY", body["voices"])
            for patch in body["voices"].values():
                self.assertNotIn("seed", patch, "seeds are preserved by merge, never sent")
                self.assertNotIn("character_style", patch)
                self.assertNotIn("versions", patch)
            alice = next(row for row in body["speakers"] if row["name"] == "BOB")
            self.assertIn("alias cleared", alice["changes"])
            self.assertIn("version timeline/active version cleared", alice["changes"])
            self.assertIn("narrator strategy reset to global", alice["changes"])
            self.assertEqual(before, path.read_bytes(), "preview must not write")
            self.assertEqual(original, script.read_bytes())

    def test_every_asset_type_applies_one_effective_voice(self):
        for label, asset in ASSET_CASES:
            with self.subTest(asset=label), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                client, path, script, original = self.fixture(Path(tmp), stack, asset)
                # BOB (a target) carries a stale alias/version timeline/strategy that must be cleared.
                _, proposal, _ = preview_and_save(client, "ALICE")
                saved = json.loads(path.read_bytes())
                chunks = [{"speaker": name, "text": "Synthetic line.", "focus_speaker": "BOB"}
                          for name in ("ALICE", "BOB", "CAROL")]
                for index, chunk in enumerate(chunks):
                    inputs = tts.get_chunk_voice_inputs(saved, chunk, index)
                    for name, fields in inputs.items():
                        self.assertEqual(asset["type"], fields["type"], (chunk, name))
                        for key, value in asset.items():
                            if key != "type":
                                self.assertEqual(value, fields[key], (label, chunk, name, key))
                        self.assertIn(fields["seed"], ("7", "-1"), "seed binding preserved")
                self.assertIsNone(saved["BOB"].get("alias_of"))
                self.assertEqual([], saved["BOB"].get("version_timeline"))
                self.assertIsNone(saved["BOB"].get("active_version"))
                self.assertEqual("global", saved["BOB"].get("narrator_strategy"))
                self.assertFalse(saved["ALICE"]["ready"], "changed voice resets ready")
                self.assertFalse(saved["BOB"]["ready"])
                self.assertEqual({"v1": {"type": "custom", "voice": "Jenny", "seed": "1"}},
                                 saved["ALICE"]["versions"], "saved versions/history preserved")
                self.assertEqual([{"from_index": 0, "character_style": "steady"}],
                                 saved["ALICE"]["style_timeline"])
                self.assertEqual({"notes": "preserve"}, saved["ALICE"]["persona_voice_audit"])
                self.assertEqual("calm", saved["ALICE"]["character_style"], "style preserved")
                self.assertEqual({"type": "custom", "voice": "Outdated", "seed": "not-a-number",
                                  "legacy_notes": "preserve-me"}, saved["ZED_LEGACY"],
                                 "unused legacy entry unchanged (not revalidated/normalised)")
                self.assertEqual(original, script.read_bytes(), "source/script bytes unchanged")

    def test_adapter_id_only_assets_derive_canonical_path(self):
        # Canonical representation: the picker and the library assign paths
        # derive adapter_path from adapter_id, and voice_is_set accepts an
        # adapter_id-carrying entry. An adapter_id-only source must work
        # through preview+save and resolve to the same effective inputs.
        cases = [
            {"type": "builtin_lora", "adapter_id": "builtin-1"},
            {"type": "lora", "adapter_id": "lora-1"},
        ]
        for source_entry in cases:
            with self.subTest(source=str(source_entry)), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                client, path, script, original = self.fixture(Path(tmp), stack, source_entry)
                _, proposal, _ = preview_and_save(client, "ALICE")
                expected_path = (f"builtin_lora/{source_entry['adapter_id']}"
                                 if source_entry["type"] == "builtin_lora"
                                 else f"lora_models/{source_entry['adapter_id']}")
                self.assertEqual(expected_path, proposal["voices"]["ALICE"]["adapter_path"])
                saved = json.loads(path.read_bytes())
                self.assertEqual(expected_path, saved["ALICE"]["adapter_path"])
                self.assertEqual(expected_path, saved["BOB"]["adapter_path"])
                chunks = [{"speaker": name, "text": "Synthetic line.", "focus_speaker": "BOB"}
                          for name in ("ALICE", "BOB", "CAROL")]
                for index, chunk in enumerate(chunks):
                    inputs = tts.get_chunk_voice_inputs(saved, chunk, index)
                    for fields in inputs.values():
                        self.assertEqual(source_entry["type"], fields["type"])
                        self.assertEqual(source_entry["adapter_id"], fields["adapter_id"])
                        self.assertEqual(expected_path, fields["adapter_path"])
                self.assertEqual(original, script.read_bytes())

    def test_lora_without_adapter_id_still_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "lora", "adapter_path": "lora_models/orphan"})
            before = path.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(422, preview.status_code, preview.text)
            self.assertEqual(before, path.read_bytes())

    def test_raw_design_description_source_is_refused(self):
        # A design description would create a new voice; choosing an existing
        # asset must refuse it clearly instead of generating implicitly.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "design", "description": "Synthetic warm design voice."})
            before = path.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(422, preview.status_code, preview.text)
            self.assertIn("new voice", preview.json()["detail"])
            self.assertEqual(before, path.read_bytes())

    def test_alias_ensemble_versioned_and_strategised_sources_refuse(self):
        cases = [
            {"type": "custom", "voice": "Serena", "alias_of": "BOB"},
            {"type": "ensemble", "members": ["ALICE", "BOB"]},
            {"type": "custom", "voice": "Serena", "active_version": "v1"},
            {"type": "custom", "voice": "Serena", "version_timeline": [{"from_index": 0, "version_id": "v1"}]},
            {"type": "custom", "voice": "Serena", "narrator_strategy": "focus"},
        ]
        for source_entry in cases:
            with self.subTest(source=str(source_entry)), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                client, path, script, original = self.fixture(Path(tmp), stack, source_entry)
                before = path.read_bytes()
                snapshot = client.get("/api/voice_config/snapshot").json()
                preview = client.post("/api/voice_config/single-voice-preview", json={
                    "source_speaker": "ALICE", "revision": snapshot["revision"],
                    "book_token": snapshot["book_token"]})
                self.assertEqual(422, preview.status_code, preview.text)
                self.assertIn("direct", preview.json()["detail"])
                self.assertEqual(before, path.read_bytes(), "refusal must not write")

    def test_stale_revision_and_book_token_reject_preview_and_save(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": "0" * 64,
                "book_token": snapshot["book_token"]})
            self.assertEqual(409, preview.status_code, preview.text)
            wrong_book = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": "0" * 64})
            self.assertEqual(409, wrong_book.status_code, wrong_book.text)
            _, proposal, _ = preview_and_save(client, "ALICE")
            before = path.read_bytes()
            stale = client.post("/api/voice_config/save", json={
                "revision": proposal["revision"], "book_token": proposal["book_token"],
                "voices": proposal["voices"]})
            self.assertEqual(409, stale.status_code, stale.text)
            self.assertEqual(before, path.read_bytes())

    def test_target_descriptions_are_preserved(self):
        # #1724 C014: the patch must never send description:None (or the
        # source's description) over a target's own persona description.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena", "description": "Source persona."})
            config = json.loads(path.read_bytes())
            config["BOB"]["description"] = "BOB persona."
            config["CAROL"]["description"] = "CAROL persona."
            path.write_text(json.dumps(config))
            _, proposal, _ = preview_and_save(client, "ALICE")
            self.assertNotIn("description", proposal["voices"]["BOB"],
                             "patch must omit description so the merge preserves it")
            saved = json.loads(path.read_bytes())
            self.assertEqual("Source persona.", saved["ALICE"]["description"])
            self.assertEqual("BOB persona.", saved["BOB"]["description"], "target persona preserved")
            self.assertEqual("CAROL persona.", saved["CAROL"]["description"], "target persona preserved")

    def test_applied_assets_bulk_ready_despite_preserved_descriptions(self):
        # Whole apply->snapshot->readiness contract with actual helpers:
        # after the real preview+save, descriptions stay per-speaker, TTS
        # inputs are uniform, the snapshot flags every speaker set, and the
        # JS per-type identity (voice for custom) is unanimous.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena", "description": "Source persona."})
            config = json.loads(path.read_bytes())
            config["BOB"]["description"] = "BOB persona."
            config["CAROL"]["description"] = "CAROL persona."
            path.write_text(json.dumps(config))
            _, proposal, _ = preview_and_save(client, "ALICE")
            saved = json.loads(path.read_bytes())
            self.assertEqual("BOB persona.", saved["BOB"]["description"])
            self.assertEqual("CAROL persona.", saved["CAROL"]["description"])
            for name in ("ALICE", "BOB", "CAROL"):
                inputs = tts.get_chunk_voice_inputs(saved, {"speaker": name, "text": "Synthetic line."}, 0)
                self.assertEqual("Serena", inputs[name]["voice"])
            rows = voices.get_voice_rows(json.loads(script.read_bytes()), saved)
            self.assertTrue(all(row["persona_pending"] is False for row in rows))
            identities = [{"type": (row["config"].get("type") or "custom"),
                           "voice": row["config"].get("voice")} for row in rows]
            self.assertTrue(all(item == identities[0] for item in identities))

    def test_legacy_alias_cleared_by_bulk_and_ordinary_save(self):
        # #1724 C020: the legacy "alias" key must not survive an explicit
        # alias_of clear, via the one guarded-save seam — and must survive
        # saves that never mention alias_of.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["BOB"] = {"type": "custom", "voice": "Ryan", "alias": "ALICE"}
            path.write_text(json.dumps(config))
            _, proposal, _ = preview_and_save(client, "ALICE")
            saved = json.loads(path.read_bytes())
            self.assertNotIn("alias", saved["BOB"], "bulk apply must clear the legacy alias key")
            self.assertIsNone(saved["BOB"].get("alias_of"))
            inputs = tts.get_chunk_voice_inputs(saved, {"speaker": "BOB", "text": "Synthetic line."}, 0)
            self.assertEqual("Serena", inputs["BOB"]["voice"], "effective voice is the applied asset, not the alias target")
            # Ordinary save: explicit alias_of clears the legacy key too.
            config = json.loads(path.read_bytes())
            config["BOB"]["alias"] = "ALICE"
            path.write_text(json.dumps(config))
            snapshot = client.get("/api/voice_config/snapshot").json()
            result = client.post("/api/voice_config/save", json={
                "revision": snapshot["revision"], "book_token": snapshot["book_token"],
                "voices": {"BOB": {"alias_of": None}}})
            self.assertEqual(200, result.status_code, result.text)
            saved = json.loads(path.read_bytes())
            self.assertNotIn("alias", saved["BOB"], "explicit alias_of clear must clear the legacy key in ordinary saves")
            self.assertEqual("Serena", saved["BOB"]["voice"], "unmentioned fields preserved")
            # Ordinary save that never mentions alias_of leaves legacy alias alone.
            config = json.loads(path.read_bytes())
            config["BOB"]["alias"] = "ALICE"
            path.write_text(json.dumps(config))
            snapshot = client.get("/api/voice_config/snapshot").json()
            result = client.post("/api/voice_config/save", json={
                "revision": snapshot["revision"], "book_token": snapshot["book_token"],
                "voices": {"BOB": {"seed": "3"}}})
            self.assertEqual(200, result.status_code, result.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual("ALICE", saved["BOB"].get("alias"), "saves not mentioning alias_of must not touch legacy alias")
            self.assertEqual("3", saved["BOB"]["seed"])

    def test_non_dict_source_and_unsafe_adapter_refuse(self):
        # A non-dict source entry is a clear 422 from the proposal builder
        # itself (the roster/snapshot paths assume dict entries), never a 500.
        with self.assertRaises(Exception) as cm:
            voices.build_single_voice_proposal(
                {"ALICE": "not-a-dict"}, [{"speaker": "ALICE", "text": "Synthetic line."}], "ALICE")
        self.assertEqual(422, cm.exception.status_code)
        self.assertIn("malformed", cm.exception.detail)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "lora", "adapter_id": "../evil"})
            before = path.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(422, preview.status_code, preview.text)
            self.assertIn("unsafe", preview.json()["detail"])
            self.assertEqual(before, path.read_bytes())

    def test_corrupt_script_json_refuses_without_write(self):
        # Corrupt script bytes change the snapshot token, so the preview
        # route refuses with a clear 409 (never a 500, never a write).
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            before = path.read_bytes()
            snapshot = client.get("/api/voice_config/snapshot").json()
            script.write_bytes(b'{not json')
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(409, preview.status_code, preview.text)
            self.assertIn("Active book changed", preview.json()["detail"])
            self.assertEqual(before, path.read_bytes())

    def test_corrupt_voice_json_refuses_without_write(self):
        # Corrupt voice JSON fails inside get_book_snapshot before any token
        # check: the preview route must map it to a clear 400, never a 500.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            snapshot = client.get("/api/voice_config/snapshot").json()
            path.write_bytes(b'{not json')
            before = path.read_bytes()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(400, preview.status_code, preview.text)
            self.assertIn("corrupt", preview.json()["detail"])
            self.assertEqual(before, path.read_bytes())

    def test_response_shape_has_no_dead_payload(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(200, preview.status_code, preview.text)
            body = preview.json()
            self.assertNotIn("asset", body, "top-level asset copy is dead payload")
            for row in body["speakers"]:
                self.assertEqual({"name", "line_count", "changes"}, set(row),
                                 "client renders only name/line_count/changes")

    def test_narrator_strategy_none_is_not_a_voice_change(self):
        # #1724 C007: strategy None ≡ "global"; a speaker already on the
        # asset with strategy None must show no change at all.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["CAROL"] = {"type": "custom", "voice": "Serena", "seed": "-1",
                               "narrator_strategy": None, "ready": False}
            path.write_text(json.dumps(config))
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(200, preview.status_code, preview.text)
            carol = next(row for row in preview.json()["speakers"] if row["name"] == "CAROL")
            self.assertEqual([], carol["changes"], "None->global must not report 'voice changed'")

    def test_empty_strategy_same_asset_preserves_approval_and_dynamic_still_resets(self):
        # CRM1400: stored narrator_strategy="" is the legacy global
        # spelling (canonical summary normalizes it to "global"), so a
        # same-asset target must show NO reset and keep its approval
        # through preview + guarded save; a real dynamic override on the
        # same asset still discloses the reset and loses approval.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, path, script, original = self.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["CAROL"] = {"type": "custom", "voice": "Serena", "seed": "-1",
                               "narrator_strategy": "", "ready": False,
                               "voice_status": "approved"}
            config["BOB"] = {"type": "custom", "voice": "Serena", "seed": "-1",
                             "narrator_strategy": "focus", "ready": False,
                             "voice_status": "approved"}
            path.write_text(json.dumps(config))
            snapshot = client.get("/api/voice_config/snapshot").json()
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "ALICE", "revision": snapshot["revision"],
                "book_token": snapshot["book_token"]})
            self.assertEqual(200, preview.status_code, preview.text)
            body = preview.json()
            carol = next(row for row in body["speakers"] if row["name"] == "CAROL")
            self.assertEqual([], carol["changes"],
                             "empty-string strategy is effective global: no reset disclosed")
            self.assertNotIn("voice_status", body["voices"]["CAROL"],
                             "same-asset empty-strategy target keeps its approval")
            bob = next(row for row in body["speakers"] if row["name"] == "BOB")
            self.assertIn("narrator strategy reset to global", bob["changes"],
                          "real dynamic override still discloses the reset")
            self.assertEqual("unassigned", body["voices"]["BOB"].get("voice_status"))
            result = client.post("/api/voice_config/save", json={
                "revision": body["revision"], "book_token": body["book_token"],
                "voices": body["voices"]})
            self.assertEqual(200, result.status_code, result.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual("approved", saved["CAROL"]["voice_status"],
                             "guarded save must preserve the empty-strategy target's approval")
            self.assertEqual("global", saved["CAROL"]["narrator_strategy"])
            self.assertEqual("unassigned", saved["BOB"]["voice_status"],
                             "dynamic-override target loses approval on apply")
            self.assertEqual("global", saved["BOB"]["narrator_strategy"])


class SingleVoiceAssetDescriptorTests(unittest.TestCase):
    """Bulk-readiness removal: no single_voice_asset metadata, no bulk action.

    The optional bulk readiness the branch previously added is removed by
    parent decision. _direct_source_asset stays for proposal validation,
    and the existing per-card Ready controls stay authoritative.
    """
    def test_rows_have_no_single_voice_asset_metadata(self):
        script = [{"speaker": "ALICE", "text": "Synthetic line."}]
        for config in [{}, {"type": "custom", "voice": "Serena"},
                       {"type": "clone", "ref_audio": "r.wav", "ref_text": "text"},
                       {"type": "lora", "adapter_id": "lora-1"}]:
            with self.subTest(config=config):
                row = voices.get_voice_rows(script, {"ALICE": config})[0]
                self.assertNotIn("single_voice_asset", row)
                self.assertNotIn("single_voice_asset", row.get("config", {}))

    def test_no_bulk_ready_action_or_helper(self):
        js = (Path(__file__).resolve().parent.parent / 'static/js/app-single-voice.js').read_text(encoding='utf-8')
        html = (Path(__file__).resolve().parent.parent / 'static/index.html').read_text(encoding='utf-8')
        self.assertNotIn('markSingleVoiceReady', js)
        self.assertNotIn('singleVoiceAllMatchSingleAsset', js)
        self.assertNotIn('markSingleVoiceReady', html)
        self.assertNotIn('I listened to this voice', html)
        # Existence/control preservation: the single-voice preview action
        # and the per-card Ready controls remain.
        self.assertIn('previewSingleVoiceBook', js)
        self.assertIn('previewSingleVoiceBook', html)
        self.assertIn('voice-ready', (Path(__file__).resolve().parent.parent / 'static/js/app-core.js').read_text(encoding='utf-8'))

    def test_direct_source_asset_still_validates_proposals(self):
        with self.assertRaises(Exception) as cm:
            voices._direct_source_asset("ALICE", {"type": "custom"})
        self.assertEqual(422, cm.exception.status_code)


class AdapterPinTransferTests(unittest.TestCase):
    def _proposal(self, voices_map):
        script = [{"speaker": "ALICE", "text": "Synthetic line."},
                  {"speaker": "BOB", "text": "Synthetic line."}]
        return voices.build_single_voice_proposal(voices_map, script, "ALICE")

    def test_source_pin_transfers_to_targets(self):
        pin = "a" * 64
        proposal = self._proposal({
            "ALICE": {"type": "lora", "adapter_id": "lora-1",
                      "adapter_path": "lora_models/lora-1",
                      "adapter_generation_sha256": pin},
            "BOB": {"type": "custom", "voice": "Ryan"}})
        self.assertEqual(pin, proposal["voices"]["BOB"]["adapter_generation_sha256"])
        self.assertEqual(pin, proposal["voices"]["ALICE"]["adapter_generation_sha256"])

    def test_unpinned_source_explicitly_clears_old_target_pin(self):
        old_pin = "b" * 64
        proposal = self._proposal({
            "ALICE": {"type": "custom", "voice": "Serena"},
            "BOB": {"type": "lora", "adapter_id": "lora-x",
                    "adapter_path": "lora_models/lora-x",
                    "adapter_generation_sha256": old_pin}})
        self.assertIsNone(proposal["voices"]["BOB"]["adapter_generation_sha256"])

    def test_malformed_source_pin_refuses(self):
        with self.assertRaises(Exception) as cm:
            self._proposal({
                "ALICE": {"type": "lora", "adapter_id": "lora-1",
                          "adapter_path": "lora_models/lora-1",
                          "adapter_generation_sha256": "not-hex"},
                "BOB": {"type": "custom", "voice": "Ryan"}})
        self.assertEqual(422, cm.exception.status_code)

    def test_custom_clone_replacement_clears_old_lora_pin(self):
        old_pin = "c" * 64
        proposal = self._proposal({
            "ALICE": {"type": "clone", "ref_audio": "r.wav", "ref_text": "text"},
            "BOB": {"type": "lora", "adapter_id": "lora-x",
                    "adapter_path": "lora_models/lora-x",
                    "adapter_generation_sha256": old_pin}})
        self.assertIsNone(proposal["voices"]["BOB"]["adapter_generation_sha256"])

    def test_source_lora_with_inactive_voice_stays_pinned(self):
        # LoRA configs may carry a stale voice=Ryan (inactive for LoRA);
        # the bulk patch clears voice to None but must keep explicit P.
        pin = "a" * 64
        proposal = self._proposal({
            "ALICE": {"type": "lora", "adapter_id": "lora-1",
                      "adapter_path": "lora_models/lora-1",
                      "adapter_generation_sha256": pin, "voice": "Ryan"},
            "BOB": {"type": "lora", "adapter_id": "lora-x",
                    "adapter_path": "lora_models/lora-x",
                    "adapter_generation_sha256": "f" * 64, "voice": "Ryan"}})
        self.assertEqual(pin, proposal["voices"]["BOB"]["adapter_generation_sha256"])
        self.assertIsNone(proposal["voices"]["BOB"]["voice"])

    def test_target_already_holding_source_pin_preserves_it(self):
        # Explicit P wins even when it equals the target's old pin.
        pin = "e" * 64
        proposal = self._proposal({
            "ALICE": {"type": "lora", "adapter_id": "lora-1",
                      "adapter_path": "lora_models/lora-1",
                      "adapter_generation_sha256": pin},
            "BOB": {"type": "custom", "voice": "Ryan",
                    "adapter_generation_sha256": pin}})
        self.assertEqual(pin, proposal["voices"]["BOB"]["adapter_generation_sha256"])

    def test_source_pin_without_stored_path_uses_canonical_fallback(self):
        # Id-only source: the proposal fills the canonical path and keeps P
        # (no silent unpin via fallback equality).
        pin = "9" * 64
        proposal = self._proposal({
            "ALICE": {"type": "lora", "adapter_id": "lora-q",
                      "adapter_generation_sha256": pin},
            "BOB": {"type": "custom", "voice": "Ryan"}})
        self.assertEqual("lora_models/lora-q", proposal["voices"]["BOB"]["adapter_path"])
        self.assertEqual(pin, proposal["voices"]["BOB"]["adapter_generation_sha256"])
        # And the guarded save of that explicit proposal keeps P.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "lora", "adapter_id": "lora-q",
                                   "adapter_generation_sha256": pin})
            _, proposal2, _ = preview_and_save(client, "ALICE")
            self.assertEqual(pin, proposal2["voices"]["BOB"]["adapter_generation_sha256"])
            saved = json.loads(path.read_bytes())
            self.assertEqual(pin, saved["BOB"]["adapter_generation_sha256"])

    def test_runtime_still_refuses_disk_q_against_pin_p(self):
        # Execute the mismatched-generation gate: admit a synthetic
        # snapshot whose disk generation is Q while the saved-test pin is
        # P. The helper must raise before any model/prompt load. No real
        # GPU or weights are involved.
        import contextlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            (root / "adapter_model.safetensors").write_bytes(b"synthetic")
            from tts import TTSEngine  # noqa: PLC0415
            pin_p = "a" * 64
            disk_q = "b" * 64

            @contextlib.contextmanager
            def fake_snapshot(adapter_path):
                yield ("synthetic-snapshot", disk_q)

            engine = object.__new__(TTSEngine)
            engine._lora_prompt_cache = {}
            engine._lora_adapter_path = None
            engine._lora_generation_sha256 = None
            import threading
            engine._model_lock = threading.Lock()
            with patch("tts.ensure_adapter_generation_snapshot", fake_snapshot), \
                 patch.object(TTSEngine, "_init_local_lora",
                            side_effect=AssertionError("load must not start on mismatch")):
                with self.assertRaisesRegex(
                        ValueError, "Adapter generation changed since benchmark fixture admission"):
                    engine._ensure_local_lora_generation(str(root), pin_p)


class MalformedAndApprovalTests(unittest.TestCase):
    def test_malformed_active_entry_refuses_without_write(self):
        # Present non-object ACTIVE entries (excluding null, the canonical
        # unconfigured representation) are corrupt saved data, not
        # unconfigured voices: every actual HTTP path refuses clearly
        # (GET listing, GET snapshot, preview, direct POST) with no file
        # change and no silent {} replacement.
        for malformed in ("not-a-dict", 42, ["bad"]):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                fixture = SingleVoicePreviewSaveTests()
                client, path, script, original = fixture.fixture(
                    Path(tmp), stack, {"type": "custom", "voice": "Serena"})
                config = json.loads(path.read_bytes())
                config["BOB"] = malformed
                path.write_text(json.dumps(config))
                before = path.read_bytes()
                listing = client.get("/api/voices")
                self.assertEqual(400, listing.status_code, (malformed, listing.text))
                self.assertIn("malformed", listing.json()["detail"])
                self.assertIn("restore", listing.json()["detail"])
                self.assertEqual(before, path.read_bytes(), "listing refusal must not write")
                snapshot_resp = client.get("/api/voice_config/snapshot")
                self.assertEqual(400, snapshot_resp.status_code, (malformed, snapshot_resp.text))
                self.assertIn("malformed", snapshot_resp.json()["detail"])
                self.assertEqual(before, path.read_bytes(), "snapshot refusal must not write")
                # Preview + direct POST need exact guards, but the snapshot
                # GET now refuses: derive them canonically under admission.
                guards = _canonical_guards(Path(tmp))
                preview = client.post("/api/voice_config/single-voice-preview", json={
                    "source_speaker": "ALICE", **guards})
                self.assertEqual(400, preview.status_code, (malformed, preview.text))
                self.assertIn("malformed", preview.json()["detail"])
                self.assertEqual(before, path.read_bytes(), "preview refusal must not write")
                guards = _canonical_guards(Path(tmp))
                response = client.post("/api/voice_config/save", json={
                    **guards, "voices": {"BOB": {"voice": "Serena"}}})
                self.assertEqual(400, response.status_code, (malformed, response.text))
                self.assertIn("malformed", response.json()["detail"])
                self.assertEqual(before, path.read_bytes(), "direct POST refusal must not write")
                self.assertEqual(malformed, json.loads(path.read_bytes())["BOB"],
                                 "malformed data must not be silently erased/normalized")

    def test_null_active_entry_is_readable_unconfigured_and_preserves_bytes(self):
        # Null is the canonical unconfigured representation (not
        # malformed): actual HTTP reads stay 200 with the null preserved
        # byte-for-byte, a null SELECTED source refuses 422, and an
        # explicit valid proposal still initializes the null target on
        # guarded save.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["BOB"] = None
            path.write_text(json.dumps(config))
            before = path.read_bytes()
            listing = client.get("/api/voices")
            self.assertEqual(200, listing.status_code, listing.text)
            rows = {row["name"]: row for row in listing.json()}
            self.assertIsNone(rows["BOB"]["config"])
            self.assertTrue(rows["BOB"]["persona_pending"])
            self.assertEqual(before, path.read_bytes(), "null read must not write")
            snapshot_resp = client.get("/api/voice_config/snapshot")
            self.assertEqual(200, snapshot_resp.status_code, snapshot_resp.text)
            self.assertIsNone(snapshot_resp.json()["config"]["BOB"])
            self.assertEqual(before, path.read_bytes(), "null snapshot read must not write")
            guards = _canonical_guards(Path(tmp))
            preview = client.post("/api/voice_config/single-voice-preview", json={
                "source_speaker": "BOB", **guards})
            self.assertEqual(422, preview.status_code, preview.text)
            self.assertEqual(before, path.read_bytes(), "null-source refusal must not write")
            _, _, _ = preview_and_save(client, "ALICE")
            saved = json.loads(path.read_bytes())
            self.assertEqual("custom", saved["BOB"]["type"],
                             "explicit valid apply must initialize the null target")
            self.assertEqual("Serena", saved["BOB"]["voice"])

    def test_adapter_replacement_with_failing_manifest_refuses_and_preserves_all_bytes(self):
        # DSF25005: identity resolution failure on DIFFERENT adapters must
        # refuse 409 BEFORE any config/invalidation side effect — never
        # assume equality (stale pin on B) and never clear/unpin on
        # unknown lookup. All original bytes remain.
        pin = "f" * 64
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["BOB"] = {"type": "lora", "adapter_id": "voice-a",
                             "adapter_path": "lora_models/voice-a",
                             "adapter_generation_sha256": pin}
            path.write_text(json.dumps(config))
            guards = _canonical_guards(Path(tmp))
            root = Path(tmp)
            before = {str(p.relative_to(root)): p.read_bytes()
                      for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
            for failure in (ValueError("adapter publication recovery is required: run x"),
                            OSError("manifest unreadable")):
                with patch.object(voices, 'get_resolved_adapter_id_mapping',
                                  side_effect=failure):
                    response = client.post('/api/voice_config/save', json={
                        **guards, "voices": {"BOB": {"type": "lora", "adapter_id": "voice-b"}}})
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertIn("could not be verified", response.json()["detail"])
            after = {str(p.relative_to(root)): p.read_bytes()
                     for p in root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
            self.assertEqual(before, after, "refused save must not write any file")
            saved = json.loads(path.read_bytes())
            self.assertEqual(pin, saved["BOB"]["adapter_generation_sha256"])
            self.assertEqual("voice-a", saved["BOB"]["adapter_id"])

    def test_adapter_identity_programmer_error_propagates(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["BOB"] = {"type": "lora", "adapter_id": "voice-a",
                             "adapter_path": "lora_models/voice-a",
                             "adapter_generation_sha256": "f" * 64}
            path.write_text(json.dumps(config))
            guards = _canonical_guards(Path(tmp))
            with patch.object(voices, 'get_resolved_adapter_id_mapping',
                              side_effect=RuntimeError("programmer boom")):
                with self.assertRaises(RuntimeError):
                    client.post('/api/voice_config/save', json={
                        **guards, "voices": {"BOB": {"type": "lora", "adapter_id": "voice-b"}}})

    def test_adapter_identity_alias_and_different_controls(self):
        pin = "f" * 64
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["BOB"] = {"type": "lora", "adapter_id": "voice-a",
                             "adapter_path": "lora_models/voice-a",
                             "adapter_generation_sha256": pin}
            path.write_text(json.dumps(config))
            # Successfully resolved alias (both spellings -> current):
            # same asset, pin kept, save succeeds.
            guards = _canonical_guards(Path(tmp))
            with patch.object(voices, 'get_resolved_adapter_id_mapping',
                              return_value={"voice-a": "current", "voice-b": "current"}):
                response = client.post('/api/voice_config/save', json={
                    **guards, "voices": {"BOB": {"type": "lora", "adapter_id": "voice-b"}}})
                self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual(pin, saved["BOB"]["adapter_generation_sha256"])
            # Successfully resolved DIFFERENT identity: pin cleared.
            guards = _canonical_guards(Path(tmp))
            with patch.object(voices, 'get_resolved_adapter_id_mapping',
                              return_value={"voice-b": "voice-b", "voice-c": "voice-c"}):
                response = client.post('/api/voice_config/save', json={
                    **guards, "voices": {"BOB": {"type": "lora", "adapter_id": "voice-c"}}})
                self.assertEqual(200, response.status_code, response.text)
            saved = json.loads(path.read_bytes())
            self.assertIsNone(saved["BOB"]["adapter_generation_sha256"])
            self.assertEqual("voice-c", saved["BOB"]["adapter_id"])

    def test_version_select_with_failing_manifest_refuses_no_writes(self):
        pin = "f" * 64
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["BOB"] = {"type": "lora", "adapter_id": "voice-a",
                             "adapter_path": "lora_models/voice-a",
                             "adapter_generation_sha256": pin,
                             "versions": {"v1": {"type": "lora", "adapter_id": "voice-b",
                                                 "adapter_path": "lora_models/voice-b"}}}
            path.write_text(json.dumps(config))
            guards = _canonical_guards(Path(tmp))
            before = path.read_bytes()
            with patch.object(voices, 'get_resolved_adapter_id_mapping',
                              side_effect=ValueError("adapter publication recovery is required")):
                response = client.post('/api/voices/BOB/versions/v1/select',
                                       json={"book_token": guards["book_token"]})
                self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, path.read_bytes())

    def test_absent_active_and_inactive_malformed_keys_load_and_preserve(self):
        # Absent active key = normal unconfigured speaker. INACTIVE
        # malformed legacy key = untouched, does not block the roster.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            del config["CAROL"]
            config["ZED_LEGACY"] = "not-a-dict"
            path.write_text(json.dumps(config))
            before = path.read_bytes()
            listing = client.get("/api/voices")
            self.assertEqual(200, listing.status_code, listing.text)
            rows = {row["name"]: row for row in listing.json()}
            self.assertEqual({}, rows["CAROL"]["config"])
            self.assertTrue(rows["CAROL"]["persona_pending"])
            self.assertNotIn("ZED_LEGACY", rows)
            snapshot_resp = client.get("/api/voice_config/snapshot")
            self.assertEqual(200, snapshot_resp.status_code, snapshot_resp.text)
            self.assertEqual(before, path.read_bytes(), "reads must not write")
            self.assertEqual("not-a-dict", json.loads(path.read_bytes())["ZED_LEGACY"],
                             "inactive malformed key stays byte-preserved")

    def test_top_level_malformed_map_snapshot_refuses_not_500(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            path.write_text(json.dumps("bad-top"))
            before = path.read_bytes()
            snapshot_resp = client.get("/api/voice_config/snapshot")
            self.assertEqual(400, snapshot_resp.status_code, snapshot_resp.text)
            self.assertNotEqual(500, snapshot_resp.status_code)
            self.assertIn("invalid", snapshot_resp.json()["detail"].lower())
            self.assertEqual(before, path.read_bytes(), "refusal must not write")

    def test_voice_approval_reset_only_where_effective_voice_changes(self):
        script = [{"speaker": "ALICE", "text": "Synthetic line."},
                  {"speaker": "BOB", "text": "Synthetic line."},
                  {"speaker": "CAROL", "text": "Synthetic line."}]
        proposal = voices.build_single_voice_proposal({
            "ALICE": {"type": "custom", "voice": "Serena",
                      "voice_status": "approved", "persona_status": "approved",
                      "seed": "7", "character_style": "calm"},
            "BOB": {"type": "custom", "voice": "Ryan",
                    "voice_status": "approved", "persona_status": "approved",
                    "seed": "-1", "character_style": "steady"},
            "CAROL": {"type": "custom", "voice": "Serena",
                      "voice_status": "approved", "persona_status": "approved",
                      "seed": "-1", "character_style": "calm"}},
            script, "ALICE")
        self.assertEqual("unassigned", proposal["voices"]["BOB"]["voice_status"],
                         "changed voice resets stale approval")
        self.assertNotIn("voice_status", proposal["voices"]["CAROL"],
                         "unchanged asset preserves approval (key omitted)")
        for speaker in ("ALICE", "BOB", "CAROL"):
            for key in ("seed", "character_style"):
                self.assertNotIn(key, proposal["voices"][speaker],
                                 "performance metadata preserved by merge, never sent")

    def test_unchanged_autosave_preserves_pin_byte_identically(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "lora", "adapter_id": "lora-1",
                                   "adapter_path": "lora_models/lora-1",
                                   "adapter_generation_sha256": "d" * 64})
            snapshot = client.get("/api/voice_config/snapshot").json()
            result = client.post("/api/voice_config/save", json={
                "revision": snapshot["revision"], "book_token": snapshot["book_token"],
                "voices": {"ALICE": {"adapter_id": "lora-1",
                                     "adapter_path": "lora_models/lora-1",
                                     "type": "lora"}}})
            self.assertEqual(200, result.status_code, result.text)
            saved = json.loads(path.read_bytes())
            self.assertEqual("d" * 64, saved["ALICE"]["adapter_generation_sha256"],
                             "same-asset autosave preserves the pin")


if __name__ == "__main__":
    unittest.main()
