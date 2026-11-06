"""Voice-library Trash lifecycle tests for issue 1532.

These tests exercise the real ASGI routers against the existing temporary
fixture. They cover the persistent lifecycle contract end to end and are
expected to fail meaningfully before the Trash implementation exists.
"""
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


class CastingTrashTests(unittest.TestCase):
    def setUp(self):
        from tests import test_casting_api
        test_casting_api.CastingApiTests.setUp(self)

    def read_document(self):
        return json.loads(self.marks.read_text(encoding="utf-8"))

    def write_document(self, document):
        self.marks.write_text(json.dumps(document), encoding="utf-8")

    def lifecycle(self, changes):
        return self.client.post("/api/casting/lifecycle/batch", json={"changes": changes})

    def trash(self, voice_id, expected="active"):
        return self.lifecycle([{"voice_id": voice_id, "to": "trashed", "expected_lifecycle": expected}])

    def restore(self, voice_id, expected="trashed"):
        return self.lifecycle([{"voice_id": voice_id, "to": "active", "expected_lifecycle": expected}])

    def delete(self, voice_id, expected="trashed"):
        return self.lifecycle([{"voice_id": voice_id, "to": "deleted", "expected_lifecycle": expected}])

    def add_persona(self, folder):
        voice_dir = self.dir / "persona" / "dh_1" / folder
        voice_dir.mkdir(parents=True, exist_ok=True)
        (voice_dir / "preview.wav").write_bytes(b"RIFF" + folder.encode("utf-8"))
        (voice_dir / "meta.json").write_text(json.dumps({
            "book_id": "DH", "speaker": folder.upper(), "description": "Young male; calm.",
            "ref_text": "Synthetic line.", "preview": f"designed_voices/persona/dh_1/{folder}/preview.wav"}),
            encoding="utf-8")
        return f"persona/dh_1/{folder}"

    def test_legacy_marks_default_to_active_and_trash_preserves_annotations(self):
        seeded = {"status": "callback", "priority": "high", "notes": "Keep this exact note.",
                  "tags": ["warm", "mentor"], "tag_colors": {"warm": "red"}, "stars": ["blue"],
                  "circles": ["green"], "collections": ["Leads"]}
        self.write_document({"persona/dh_1/abel_1": dict(seeded)})
        listed = self.client.get("/api/casting/voices")
        self.assertEqual(200, listed.status_code, listed.text)
        body = listed.json()
        self.assertEqual(["persona/dh_1/abel_1", "persona/dh_1/mute_1"], [voice["id"] for voice in body["voices"]])
        self.assertEqual(seeded, body["marks"]["persona/dh_1/abel_1"])
        for voice in body["voices"]:
            self.assertNotIn("lifecycle", voice)
        trashed = self.trash("persona/dh_1/abel_1")
        self.assertEqual(200, trashed.status_code, trashed.text)
        stored = self.read_document()["persona/dh_1/abel_1"]
        self.assertEqual({**seeded, "lifecycle": "trashed"}, stored)
        self.assertEqual({**seeded, "lifecycle": "trashed"}, trashed.json()["marks"]["persona/dh_1/abel_1"])
        self.assertEqual(1, len(trashed.json()["activity"]))
        self.assertEqual(1, trashed.json()["activity"][0]["count"])
        self.assertNotIn("Keep this exact note.", trashed.json()["activity"][0]["action"])
        listed = self.client.get("/api/casting/voices").json()
        self.assertIn("persona/dh_1/abel_1", [voice["id"] for voice in listed["voices"]])
        self.assertEqual({**seeded, "lifecycle": "trashed"}, listed["marks"]["persona/dh_1/abel_1"])

    def test_malformed_lifecycle_is_refused_without_overwriting_bytes(self):
        import casting_catalog
        for bad_value in ("gone", 5, None, ["trashed"], {"state": "trashed"}):
            with self.subTest(bad_value=bad_value):
                self.write_document({"persona/dh_1/abel_1": {"status": "shortlist", "lifecycle": bad_value}})
                before = self.marks.read_bytes()
                with self.assertRaises(casting_catalog.CastingMarksCorrupt):
                    casting_catalog.get_casting_marks(str(self.marks))
                single = self.client.post("/api/casting/marks", json={
                    "voice_id": "persona/dh_1/abel_1", "status": "reject"})
                self.assertEqual(500, single.status_code, single.text)
                batch = self.client.post("/api/casting/marks/batch", json={
                    "changes": [{"voice_id": "persona/dh_1/abel_1", "patch": {"status": "reject"},
                                 "expected": {"status": "shortlist"}}], "action": "reject"})
                self.assertEqual(500, batch.status_code, batch.text)
                lifecycle = self.trash("persona/dh_1/abel_1")
                self.assertEqual(500, lifecycle.status_code, lifecycle.text)
                self.assertEqual(before, self.marks.read_bytes())

    def test_full_trash_restore_trash_delete_persistence_and_minimal_tombstone(self):
        seeded = {"status": "shortlist", "notes": "Survives until delete.", "tags": ["keep"],
                  "tag_colors": {"keep": "blue"}, "collections": ["Leads"]}
        self.write_document({"persona/dh_1/abel_1": dict(seeded)})
        preview_before = (self.dir / "persona" / "dh_1" / "abel_1" / "preview.wav").read_bytes()
        meta_before = (self.dir / "persona" / "dh_1" / "abel_1" / "meta.json").read_bytes()
        trashed = self.trash("persona/dh_1/abel_1")
        self.assertEqual(200, trashed.status_code, trashed.text)
        self.assertEqual({**seeded, "lifecycle": "trashed"}, self.read_document()["persona/dh_1/abel_1"])
        restored = self.restore("persona/dh_1/abel_1")
        self.assertEqual(200, restored.status_code, restored.text)
        self.assertEqual({**seeded, "lifecycle": "active"}, self.read_document()["persona/dh_1/abel_1"])
        self.assertEqual({**seeded, "lifecycle": "active"}, restored.json()["marks"]["persona/dh_1/abel_1"])
        trashed_again = self.trash("persona/dh_1/abel_1")
        self.assertEqual(200, trashed_again.status_code, trashed_again.text)
        deleted = self.delete("persona/dh_1/abel_1")
        self.assertEqual(200, deleted.status_code, deleted.text)
        self.assertEqual({"lifecycle": "deleted"}, deleted.json()["marks"]["persona/dh_1/abel_1"])
        self.assertEqual({"lifecycle": "deleted"}, self.read_document()["persona/dh_1/abel_1"])
        self.assertEqual(preview_before, (self.dir / "persona" / "dh_1" / "abel_1" / "preview.wav").read_bytes())
        self.assertEqual(meta_before, (self.dir / "persona" / "dh_1" / "abel_1" / "meta.json").read_bytes())
        for refresh in range(3):
            listed = self.client.get("/api/casting/voices")
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual(["persona/dh_1/mute_1"], [voice["id"] for voice in listed.json()["voices"]])
            self.assertEqual({}, listed.json()["marks"])

    def test_source_files_manifest_library_and_saved_cast_bytes_survive_delete(self):
        designer_wav = self.dir / "designer.wav"
        designer_wav.write_bytes(b"RIFF designer synthetic bytes")
        manifest = self.dir / "manifest.json"
        manifest.write_text(json.dumps([{"id": "desk", "name": "Desk", "description": "Calm female voice.",
                                         "sample_text": "Synthetic line.", "filename": "designer.wav"}]),
                            encoding="utf-8")
        created = self.client.post("/api/voice_library/casts", json={"name": "Collection"})
        self.assertEqual(200, created.status_code, created.text)
        saved = self.client.post("/api/voice_library/catalog_member", json={
            "cast": "Collection", "voice_id": "persona/dh_1/abel_1", "name": "ADAM"})
        self.assertEqual(200, saved.status_code, saved.text)
        matched = self.client.post("/api/voice_library/match", json={"name": "Collection"})
        self.assertEqual(200, matched.status_code, matched.text)
        applied = self.client.post("/api/voice_library/apply", json={
            "cast": "Collection", "mapping": {"ADAM": "adam"}, "book_token": matched.json()["book_token"]})
        self.assertEqual(200, applied.status_code, applied.text)
        protected = {
            "preview": (self.dir / "persona" / "dh_1" / "abel_1" / "preview.wav").read_bytes(),
            "meta": (self.dir / "persona" / "dh_1" / "abel_1" / "meta.json").read_bytes(),
            "designer_wav": designer_wav.read_bytes(), "manifest": manifest.read_bytes(),
            "library": self.library.read_bytes(), "config": self.config.read_bytes(),
            "script": self.script.read_bytes(),
        }
        for voice_id in ("persona/dh_1/abel_1", "designer/desk"):
            self.assertEqual(200, self.trash(voice_id).status_code)
            self.assertEqual(200, self.delete(voice_id).status_code)
        self.assertEqual(protected["preview"], (self.dir / "persona" / "dh_1" / "abel_1" / "preview.wav").read_bytes())
        self.assertEqual(protected["meta"], (self.dir / "persona" / "dh_1" / "abel_1" / "meta.json").read_bytes())
        self.assertEqual(protected["designer_wav"], designer_wav.read_bytes())
        self.assertEqual(protected["manifest"], manifest.read_bytes())
        self.assertEqual(protected["library"], self.library.read_bytes())
        self.assertEqual(protected["config"], self.config.read_bytes())
        self.assertEqual(protected["script"], self.script.read_bytes())
        listed = self.client.get("/api/casting/voices").json()
        self.assertEqual(["persona/dh_1/mute_1"], [voice["id"] for voice in listed["voices"]])

    def test_lifecycle_batch_is_all_or_none_for_stale_unknown_duplicate_and_malformed(self):
        self.write_document({"persona/dh_1/abel_1": {"status": "callback", "lifecycle": "trashed"},
                             "persona/dh_1/mute_1": {"notes": "keep"}})
        before = self.marks.read_bytes()
        cases = [
            ([{"voice_id": "persona/dh_1/mute_1", "to": "trashed", "expected_lifecycle": "trashed"}], 409),
            ([{"voice_id": "persona/dh_1/nobody", "to": "trashed", "expected_lifecycle": "active"}], 404),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "deleted", "expected_lifecycle": "active"}], 409),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "active", "expected_lifecycle": "active"}], 409),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "trashed", "expected_lifecycle": "active"},
              {"voice_id": "persona/dh_1/mute_1", "to": "trashed", "expected_lifecycle": "active"}], 400),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "gone", "expected_lifecycle": "active"}], 422),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "trashed", "expected_lifecycle": "gone"}], 422),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "trashed", "expected_lifecycle": "active"},
              {"voice_id": "persona/dh_1/abel_1", "to": "active", "expected_lifecycle": "trashed"}], 400),
            ([{"voice_id": "persona/dh_1/mute_1", "to": "trashed"}], 422),
            ([{"voice_id": "persona/dh_1/mute_1", "expected_lifecycle": "active"}], 422),
            ([], 422),
        ]
        for changes, expected_code in cases:
            with self.subTest(code=expected_code, changes=str(changes)[:80]):
                response = self.lifecycle(changes)
                self.assertEqual(expected_code, response.status_code, response.text)
                self.assertEqual(before, self.marks.read_bytes())
        self.assertNotIn("__studio__", self.read_document())

    def test_deleted_cannot_be_restored_or_edited_through_any_route(self):
        self.write_document({"persona/dh_1/abel_1": {"status": "callback", "notes": "drop on delete"}})
        self.assertEqual(200, self.trash("persona/dh_1/abel_1").status_code)
        self.assertEqual(200, self.delete("persona/dh_1/abel_1").status_code)
        before = self.marks.read_bytes()
        single = self.client.post("/api/casting/marks", json={
            "voice_id": "persona/dh_1/abel_1", "status": "reject", "expected_lifecycle": "deleted"})
        self.assertEqual(409, single.status_code, single.text)
        batch = self.client.post("/api/casting/marks/batch", json={
            "changes": [{"voice_id": "persona/dh_1/abel_1", "patch": {"status": "reject"},
                          "expected": {"status": "unreviewed"}, "expected_lifecycle": "deleted"}],
            "action": "undo attempt"})
        self.assertEqual(409, batch.status_code, batch.text)
        restore = self.restore("persona/dh_1/abel_1", expected="deleted")
        self.assertEqual(409, restore.status_code, restore.text)
        lifecycle_only = self.client.post("/api/casting/marks", json={
            "voice_id": "persona/dh_1/abel_1", "lifecycle": "active"})
        self.assertEqual(400, lifecycle_only.status_code, lifecycle_only.text)
        self.assertEqual(before, self.marks.read_bytes())
        self.assertEqual({"lifecycle": "deleted"}, self.read_document()["persona/dh_1/abel_1"])

    def test_ordinary_single_and_batch_patches_cannot_set_lifecycle(self):
        self.write_document({"persona/dh_1/abel_1": {"status": "shortlist", "notes": "keep"}})
        before = self.marks.read_bytes()
        single = self.client.post("/api/casting/marks", json={
            "voice_id": "persona/dh_1/abel_1", "lifecycle": "trashed"})
        self.assertEqual(400, single.status_code, single.text)
        batch_patch = self.client.post("/api/casting/marks/batch", json={
            "changes": [{"voice_id": "persona/dh_1/abel_1", "patch": {"lifecycle": "trashed"},
                          "expected": {"status": "shortlist"}}], "action": "trash via patch"})
        self.assertEqual(400, batch_patch.status_code, batch_patch.text)
        batch_expected = self.client.post("/api/casting/marks/batch", json={
            "changes": [{"voice_id": "persona/dh_1/abel_1", "patch": {"status": "reject"},
                          "expected": {"status": "shortlist", "lifecycle": "active"}}],
            "action": "trash via expected"})
        self.assertEqual(400, batch_expected.status_code, batch_expected.text)
        self.assertEqual(before, self.marks.read_bytes())

    def test_stale_active_mark_on_trashed_rejects_and_explicit_trashed_update_succeeds(self):
        self.write_document({"persona/dh_1/abel_1": {"status": "callback", "notes": "before trash"}})
        self.assertEqual(200, self.trash("persona/dh_1/abel_1").status_code)
        before = self.marks.read_bytes()
        legacy = self.client.post("/api/casting/marks", json={
            "voice_id": "persona/dh_1/abel_1", "notes": "stale active edit"})
        self.assertEqual(409, legacy.status_code, legacy.text)
        explicit_active = self.client.post("/api/casting/marks", json={
            "voice_id": "persona/dh_1/abel_1", "notes": "stale active edit", "expected_lifecycle": "active"})
        self.assertEqual(409, explicit_active.status_code, explicit_active.text)
        stale_batch = self.client.post("/api/casting/marks/batch", json={
            "changes": [{"voice_id": "persona/dh_1/abel_1", "patch": {"notes": "stale active edit"},
                          "expected": {"notes": "before trash"}, "expected_lifecycle": "active"}],
            "action": "edit trashed as active"})
        self.assertEqual(409, stale_batch.status_code, stale_batch.text)
        self.assertEqual(before, self.marks.read_bytes())
        explicit_trashed = self.client.post("/api/casting/marks", json={
            "voice_id": "persona/dh_1/abel_1", "notes": "edited while trashed", "expected_lifecycle": "trashed"})
        self.assertEqual(200, explicit_trashed.status_code, explicit_trashed.text)
        self.assertEqual("edited while trashed", explicit_trashed.json()["mark"]["notes"])
        self.assertEqual("trashed", explicit_trashed.json()["mark"]["lifecycle"])
        batch_trashed = self.client.post("/api/casting/marks/batch", json={
            "changes": [{"voice_id": "persona/dh_1/abel_1", "patch": {"priority": "high"},
                          "expected": {"priority": "none"}, "expected_lifecycle": "trashed"}],
            "action": "edit trashed"})
        self.assertEqual(200, batch_trashed.status_code, batch_trashed.text)
        stored = self.read_document()["persona/dh_1/abel_1"]
        self.assertEqual(("edited while trashed", "high", "trashed"),
                         (stored["notes"], stored["priority"], stored["lifecycle"]))

    def test_clone_and_catalog_member_refuse_trashed_and_deleted_but_saved_cast_still_applies(self):
        created = self.client.post("/api/voice_library/casts", json={"name": "Collection"})
        self.assertEqual(200, created.status_code, created.text)
        saved = self.client.post("/api/voice_library/catalog_member", json={
            "cast": "Collection", "voice_id": "persona/dh_1/abel_1", "name": "ADAM"})
        self.assertEqual(200, saved.status_code, saved.text)
        library_before = self.library.read_bytes()
        self.assertEqual(200, self.trash("persona/dh_1/abel_1").status_code)
        clone_trashed = self.client.get("/api/casting/clone_config", params={
            "voice_id": "persona/dh_1/abel_1", "speaker": "ADAM"})
        self.assertEqual(409, clone_trashed.status_code, clone_trashed.text)
        member_trashed = self.client.post("/api/voice_library/catalog_member", json={
            "cast": "Collection", "voice_id": "persona/dh_1/abel_1", "name": "EVE"})
        self.assertEqual(409, member_trashed.status_code, member_trashed.text)
        self.assertEqual(library_before, self.library.read_bytes())
        matched = self.client.post("/api/voice_library/match", json={"name": "Collection"})
        self.assertEqual(200, matched.status_code, matched.text)
        applied = self.client.post("/api/voice_library/apply", json={
            "cast": "Collection", "mapping": {"ADAM": "adam"}, "book_token": matched.json()["book_token"]})
        self.assertEqual(200, applied.status_code, applied.text)
        self.assertEqual(["ADAM"], applied.json()["applied"])
        self.assertEqual(200, self.delete("persona/dh_1/abel_1").status_code)
        clone_deleted = self.client.get("/api/casting/clone_config", params={
            "voice_id": "persona/dh_1/abel_1", "speaker": "EVE"})
        self.assertEqual(404, clone_deleted.status_code, clone_deleted.text)
        member_deleted = self.client.post("/api/voice_library/catalog_member", json={
            "cast": "Collection", "voice_id": "persona/dh_1/abel_1", "name": "EVE"})
        self.assertEqual(404, member_deleted.status_code, member_deleted.text)
        self.assertEqual(library_before, self.library.read_bytes())
        matched_again = self.client.post("/api/voice_library/match", json={"name": "Collection"})
        applied_again = self.client.post("/api/voice_library/apply", json={
            "cast": "Collection", "mapping": {"ADAM": "adam"}, "book_token": matched_again.json()["book_token"]})
        self.assertEqual(200, applied_again.status_code, applied_again.text)
        self.assertEqual(["ADAM"], applied_again.json()["applied"])

    def test_atomic_write_failure_preserves_original_bytes(self):
        import casting_catalog
        self.write_document({"persona/dh_1/abel_1": {"status": "callback", "notes": "keep"}})
        before = self.marks.read_bytes()
        with patch.object(casting_catalog, "atomic_json_write", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                self.trash("persona/dh_1/abel_1")
        self.assertEqual(before, self.marks.read_bytes())
        recovered = self.trash("persona/dh_1/abel_1")
        self.assertEqual(200, recovered.status_code, recovered.text)
        self.assertEqual("trashed", self.read_document()["persona/dh_1/abel_1"]["lifecycle"])

    def test_lifecycle_bounds_and_path_traversal_are_refused_without_writing(self):
        self.assertFalse(self.marks.exists())
        traversal = self.lifecycle([{"voice_id": "../../secret", "to": "trashed", "expected_lifecycle": "active"}])
        self.assertEqual(422, traversal.status_code, traversal.text)
        too_long = self.lifecycle([{"voice_id": "persona/dh_1/" + "x" * 400, "to": "trashed",
                                     "expected_lifecycle": "active"}])
        self.assertEqual(422, too_long.status_code, too_long.text)
        oversize_changes = [{"voice_id": "persona/dh_1/abel_1", "to": "trashed",
                             "expected_lifecycle": "active"} for _unused in range(1001)]
        oversize = self.lifecycle(oversize_changes)
        self.assertIn(oversize.status_code, (400, 422), oversize.text)
        not_an_object = self.client.post("/api/casting/lifecycle/batch", json={"changes": ["persona/dh_1/abel_1"]})
        self.assertIn(not_an_object.status_code, (400, 422), not_an_object.text)
        self.assertFalse(self.marks.exists())

    def test_normal_mark_writes_reject_invalid_expected_lifecycle(self):
        self.write_document({"persona/dh_1/abel_1": {"notes": "keep"}})
        before = self.marks.read_bytes()
        for expected in ("", "unknown"):
            with self.subTest(expected=expected):
                single = self.client.post("/api/casting/marks", json={
                    "voice_id": "persona/dh_1/abel_1", "status": "callback",
                    "expected_lifecycle": expected})
                self.assertIn(single.status_code, (400, 422), single.text)
                self.assertEqual(before, self.marks.read_bytes())
                batch = self.client.post("/api/casting/marks/batch", json={
                    "action": "Set status", "changes": [{"voice_id": "persona/dh_1/abel_1",
                    "patch": {"status": "callback"}, "expected": {"status": "unreviewed"},
                    "expected_lifecycle": expected}]})
                self.assertIn(batch.status_code, (400, 422), batch.text)
                self.assertEqual(before, self.marks.read_bytes())

    def test_batch_uses_each_voices_lifecycle_precondition(self):
        self.write_document({"persona/dh_1/abel_1": {"notes": "keep"}})
        before = self.marks.read_bytes()
        response = self.client.post("/api/casting/marks/batch", json={
            "action": "Set status", "expected_lifecycle": "active", "changes": [{
                "voice_id": "persona/dh_1/abel_1", "patch": {"status": "callback"},
                "expected": {"status": "unreviewed"}, "expected_lifecycle": "trashed"}]})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self.marks.read_bytes())

    def test_deleted_voiceless_member_returns_not_found_without_mutation(self):
        created = self.client.post("/api/voice_library/casts", json={"name": "Collection"})
        self.assertEqual(200, created.status_code, created.text)
        self.assertEqual(200, self.trash("persona/dh_1/mute_1").status_code)
        self.assertEqual(200, self.delete("persona/dh_1/mute_1").status_code)
        before = self.library.read_bytes()
        response = self.client.post("/api/voice_library/catalog_member", json={
            "cast": "Collection", "voice_id": "persona/dh_1/mute_1", "name": "MUTE"})
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual(before, self.library.read_bytes())

    def test_bulk_lifecycle_publishes_every_change_and_one_activity_event(self):
        voice_ids = ["persona/dh_1/abel_1", "persona/dh_1/mute_1"]
        self.write_document({voice_id: {"notes": voice_id} for voice_id in voice_ids})
        for target, expected in (("trashed", "active"), ("active", "trashed"),
                                 ("trashed", "active"), ("deleted", "trashed")):
            with self.subTest(target=target):
                result = self.lifecycle([{"voice_id": voice_id, "to": target,
                                          "expected_lifecycle": expected} for voice_id in voice_ids])
                self.assertEqual(200, result.status_code, result.text)
                self.assertEqual(set(voice_ids), set(result.json()["marks"]))
                self.assertEqual(2, result.json()["activity"][-1]["count"])
                for voice_id in voice_ids:
                    expected_mark = {"lifecycle": "deleted"} if target == "deleted" else {
                        "lifecycle": target, "notes": voice_id}
                    self.assertEqual(expected_mark, self.read_document()[voice_id])
        self.assertEqual(4, len(result.json()["activity"]))

    def test_mid_batch_stale_precondition_preserves_every_byte(self):
        self.write_document({"persona/dh_1/abel_1": {"notes": "active"},
                             "persona/dh_1/mute_1": {"lifecycle": "trashed", "notes": "stale"}})
        before = self.marks.read_bytes()
        response = self.lifecycle([{"voice_id": voice_id, "to": "trashed", "expected_lifecycle": "active"}
                                   for voice_id in ("persona/dh_1/abel_1", "persona/dh_1/mute_1")])
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(before, self.marks.read_bytes())

    def test_conflicting_same_voice_batches_have_exactly_one_winner(self):
        self.write_document({"persona/dh_1/abel_1": {"notes": "keep"}})
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def trash_same_voice():
            try:
                barrier.wait(timeout=10)
                results.append(self.trash("persona/dh_1/abel_1").status_code)
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=trash_same_voice) for _unused in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], failures)
        self.assertEqual([200, 409], sorted(results))
        self.assertEqual({"notes": "keep", "lifecycle": "trashed"}, self.read_document()["persona/dh_1/abel_1"])
        self.assertEqual(1, len(self.client.get("/api/casting/voices").json()["activity"]))

    def test_lifecycle_schema_is_closed_and_bad_enums_return_422(self):
        from routers.casting import CastingLifecycleChange
        properties = CastingLifecycleChange.model_json_schema()["properties"]
        for field in ("to", "expected_lifecycle"):
            self.assertEqual(["active", "trashed", "deleted"], properties[field]["enum"])
            change = {"voice_id": "persona/dh_1/abel_1", "to": "trashed", "expected_lifecycle": "active"}
            change[field] = "unknown"
            response = self.lifecycle([change])
            self.assertEqual(422, response.status_code, response.text)
        self.assertFalse(self.marks.exists())

    def test_concurrent_lifecycle_batches_lose_no_marks(self):
        voice_ids = [self.add_persona(f"voice_{index}") for index in range(4)]
        self.write_document({voice_id: {"notes": f"note {voice_id}"} for voice_id in voice_ids})
        results = {}
        failures = []

        def trash_one(voice_id):
            try:
                results[voice_id] = self.trash(voice_id).status_code
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=trash_one, args=(voice_id,)) for voice_id in voice_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assertEqual({voice_id: 200 for voice_id in voice_ids}, results)
        document = self.read_document()
        for voice_id in voice_ids:
            self.assertEqual({"notes": f"note {voice_id}", "lifecycle": "trashed"}, document[voice_id])
        listed = self.client.get("/api/casting/voices").json()
        for voice_id in voice_ids:
            self.assertEqual("trashed", listed["marks"][voice_id]["lifecycle"])


if __name__ == "__main__":
    unittest.main()
