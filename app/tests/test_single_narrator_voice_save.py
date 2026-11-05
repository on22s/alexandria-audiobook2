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
import tts

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


class SingleVoiceAssetDescriptorTests(unittest.TestCase):
    """get_voice_rows single_voice_asset is the server completeness authority."""
    def rows(self, config):
        script = [{"speaker": "ALICE", "text": "Synthetic line."}]
        return voices.get_voice_rows(script, {"ALICE": config})

    def test_valid_custom_without_persona_is_eligible(self):
        row = self.rows({"type": "custom", "voice": "Serena"})[0]
        self.assertTrue(row["persona_pending"], "persona_pending stays persona-scoped")
        self.assertEqual({"type": "custom", "voice": "Serena", "ref_audio": None,
                          "ref_text": None, "adapter_id": None, "adapter_path": None,
                          "members": None}, row["single_voice_asset"])

    def test_complete_clone_and_lora_are_eligible(self):
        clone = self.rows({"type": "clone", "ref_audio": "r.wav", "ref_text": "text"})[0]
        self.assertEqual({"type": "clone", "voice": None, "ref_audio": "r.wav",
                          "ref_text": "text", "adapter_id": None, "adapter_path": None,
                          "members": None}, clone["single_voice_asset"])
        lora = self.rows({"type": "lora", "adapter_id": "lora-1"})[0]
        self.assertEqual("lora_models/lora-1", lora["single_voice_asset"]["adapter_path"])
        builtin = self.rows({"type": "builtin_lora", "adapter_id": "b-1"})[0]
        self.assertEqual("builtin_lora/b-1", builtin["single_voice_asset"]["adapter_path"])

    def test_empty_clone_lora_and_refusals_have_no_descriptor(self):
        cases = [
            {"type": "clone"},
            {"type": "clone", "ref_audio": "r.wav"},
            {"type": "lora"},
            {"type": "lora", "adapter_path": "lora_models/x"},
            {"type": "custom", "voice": "Serena", "alias_of": "BOB"},
            {"type": "custom", "voice": "Serena", "alias": "BOB"},
            {"type": "custom", "voice": "Serena", "active_version": "v1"},
            {"type": "custom", "voice": "Serena", "version_timeline": [{"from_index": 0, "version_id": "v1"}]},
            {"type": "custom", "voice": "Serena", "narrator_strategy": "focus"},
            {"type": "design", "description": "Synthetic design."},
            {"type": "ensemble", "members": ["A"]},
            {"type": "custom"},
            {},
            None,
        ]
        for config in cases:
            with self.subTest(config=config):
                row = self.rows(config)[0]
                self.assertIsNone(row["single_voice_asset"], config)

    def test_snapshot_carries_descriptors_and_preview_apply_is_usable(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            fixture = SingleVoicePreviewSaveTests()
            client, path, script, original = fixture.fixture(
                Path(tmp), stack, {"type": "custom", "voice": "Serena"})
            config = json.loads(path.read_bytes())
            config["ALICE"] = {"type": "custom", "voice": "Serena"}
            config["BOB"] = {"type": "custom", "voice": "Serena"}
            config["CAROL"] = {"type": "custom", "voice": "Serena"}
            path.write_text(json.dumps(config))
            snapshot = client.get("/api/voice_config/snapshot").json()
            for row in snapshot["voices"]:
                self.assertIsNotNone(row["single_voice_asset"], row)
                self.assertTrue(row["persona_pending"], "no persona text still pending")
            _, proposal, _ = preview_and_save(client, "ALICE")
            saved = json.loads(path.read_bytes())
            rows = voices.get_voice_rows(json.loads(script.read_bytes()), saved)
            self.assertTrue(all(r["single_voice_asset"] == rows[0]["single_voice_asset"] for r in rows))


if __name__ == "__main__":
    unittest.main()
