"""Chapter-by-chapter export: filenames from a template, chapters from the same
grouping the M4B uses, changed-only re-export, and the download routes."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

from project import (CHAPTER_EXPORT_DIR, ProjectManager, build_chapter_filename,
                     build_chapter_filenames)
from routers import editor as editor_module

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))


class FilenameTemplateTests(unittest.TestCase):
    def test_default_template_pads_and_adds_the_extension(self):
        self.assertEqual("03 - The Storm.mp3",
                         build_chapter_filename("{chapter_number} - {chapter_name}", 3, "The Storm", "mp3"))

    def test_every_field_and_padding_widths(self):
        t = "{series_name} {volume_number} {book_name} ch{chapter_number} {chapter_name}"
        self.assertEqual("Grimgar 3 Volume Three ch012 Dawn.wav",
                         build_chapter_filename(t, 12, "Dawn", "wav", padding=3, book_name="Volume Three",
                                                series_name="Grimgar", volume_number=3))
        self.assertEqual("7 - x.mp3", build_chapter_filename("{chapter_number} - {chapter_name}", 7, "x", "mp3", padding=0))

    def test_filesystem_hostile_titles_cannot_escape_or_break(self):
        name = build_chapter_filename("{chapter_name}", 1, '../../etc/passwd: "Part 1/2"?', "mp3")
        self.assertNotIn("/", name); self.assertNotIn("..", name.split(".mp3")[0][:2])
        self.assertTrue(name.endswith(".mp3"))

    def test_unknown_fields_and_empty_titles_still_yield_a_name(self):
        self.assertEqual("_nope_.mp3", build_chapter_filename("{nope}", 1, "x", "mp3"))
        self.assertEqual("chapter 4.mp3", build_chapter_filename("{chapter_name}", 4, "   ", "mp3"))
        self.assertEqual("02 - chapter 2.mp3", build_chapter_filename("", 2, "", "mp3"))
        self.assertTrue(build_chapter_filename("???", 2, "", "mp3").endswith(".mp3"))

    def test_duplicate_names_are_rejected_before_an_export_can_overwrite_audio(self):
        groups = [("Chapter 1", 0, 0), ("Chapter 2", 1, 1)]
        with self.assertRaisesRegex(ValueError, "duplicate filenames"):
            build_chapter_filenames(groups, "{book_name}", "mp3", book_name="Novel")

    def test_case_only_name_collisions_are_rejected_for_portable_exports(self):
        groups = [("Part One", 0, 0), ("part one", 1, 1)]
        with self.assertRaisesRegex(ValueError, "duplicate filenames"):
            build_chapter_filenames(groups, "{chapter_name}", "wav")
        self.assertEqual(["01 - Part One.wav", "02 - part one.wav"],
                         build_chapter_filenames(groups, "{chapter_number} - {chapter_name}", "wav"))


def _tone(path, seconds, hz=220.0, rate=24000):
    t = np.arange(int(seconds * rate)) / rate
    sf.write(path, (0.3 * np.sin(2 * np.pi * hz * t)).astype(np.float32), rate)


def _project(tmp):
    """A heading, two lines, a second heading, a line. Narration lines are
    over 80 chars because the (pre-existing) heading rule treats any shorter
    unquoted line as a heading."""
    pm = ProjectManager(tmp)
    os.makedirs(pm.voicelines_dir, exist_ok=True)
    chunks = []
    for i, (speaker, text) in enumerate([("NARRATOR", "Chapter 1"), ("NARRATOR", "It began at night, as these things do, with the lamps guttering and the long road out of town empty of anyone."),
                                          ("MIA", "\"Are you awake?\" she asked, twice."), ("NARRATOR", "Chapter 2"),
                                          ("NARRATOR", "Morning came late and grey over the valley road, and nobody in the house had slept enough to say so.")]):
        rel = os.path.join("voicelines", f"c{i}.wav")
        _tone(os.path.join(tmp, rel), 0.4 + 0.1 * i, hz=200 + 40 * i)
        chunks.append({"index": i, "speaker": speaker, "text": text, "audio_path": rel})
    return pm, chunks


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg needed for mp3 export")
class ExportChaptersTests(unittest.TestCase):
    def test_same_size_subsecond_audio_replacement_rebuilds_changed_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            source = Path(tmp, "voicelines", "c4.wav")
            first_ns = 1700000000100000000
            os.utime(source, ns=(first_ns, first_ns))
            before_stat = source.stat()
            out = Path(tmp, CHAPTER_EXPORT_DIR)
            with patch.object(pm, "load_chunks", return_value=chunks):
                ok, msg = pm.export_chapters(fmt="wav")
                self.assertTrue(ok, msg)
                first_chapter = (out / "01 - Chapter 1.wav").read_bytes()
                old_second = (out / "02 - Chapter 2.wav").read_bytes()
                old_manifest = json.loads((out / "manifest.json").read_text())
                _tone(str(source), 0.8, hz=550)
                second_ns = first_ns + 100000000
                os.utime(source, ns=(second_ns, second_ns))
                after_stat = source.stat()
                self.assertEqual(before_stat.st_size, after_stat.st_size)
                self.assertEqual(int(before_stat.st_mtime), int(after_stat.st_mtime))
                self.assertNotEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
                ok, msg = pm.export_chapters(fmt="wav", changed_only=True)
                self.assertTrue(ok, msg)
                self.assertIn("1 chapter file(s) written, 1 unchanged and kept", msg)
                self.assertEqual(first_chapter, (out / "01 - Chapter 1.wav").read_bytes())
                new_second = (out / "02 - Chapter 2.wav").read_bytes()
                self.assertNotEqual(old_second, new_second)
                new_manifest = json.loads((out / "manifest.json").read_text())
                self.assertNotEqual(old_manifest["chapters"][1]["fingerprint"],
                                    new_manifest["chapters"][1]["fingerprint"])
                ok, msg = pm.export_chapters(fmt="wav", changed_only=True)
                self.assertTrue(ok, msg)
                self.assertIn("0 chapter file(s) written, 2 unchanged and kept", msg)
                self.assertEqual(new_second, (out / "02 - Chapter 2.wav").read_bytes())

    def test_changed_pause_defaults_rebuild_actual_audio_and_then_reuse_it(self):
        for field in ("pause_between_speakers_ms", "pause_same_speaker_ms"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                pm, chunks = _project(tmp)
                pm.config_path = os.path.join(tmp, "config.json")
                settings = {"pause_between_speakers_ms": 300, "pause_same_speaker_ms": 100}
                Path(pm.config_path).write_text(json.dumps({"tts": settings}))
                out = Path(tmp, CHAPTER_EXPORT_DIR)
                with patch.object(pm, "load_chunks", return_value=chunks):
                    ok, msg = pm.export_chapters(fmt="wav")
                    self.assertTrue(ok, msg)
                    original = sf.info(str(out / "01 - Chapter 1.wav")).duration
                    old_manifest = json.loads((out / "manifest.json").read_text())
                    settings[field] += 400
                    Path(pm.config_path).write_text(json.dumps({"tts": settings}))
                    ok, msg = pm.export_chapters(fmt="wav", changed_only=True)
                    self.assertTrue(ok, msg)
                    current = sf.info(str(out / "01 - Chapter 1.wav")).duration
                    self.assertAlmostEqual(0.4, current - original, places=3)
                    new_manifest = json.loads((out / "manifest.json").read_text())
                    self.assertNotEqual(old_manifest["chapters"][0]["fingerprint"],
                                        new_manifest["chapters"][0]["fingerprint"])
                    ok, msg = pm.export_chapters(fmt="wav", changed_only=True)
                    self.assertTrue(ok, msg)
                    self.assertIn("0 chapter file(s) written, 2 unchanged and kept", msg)

    def test_grouping_matches_the_m4b_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            groups = pm._chapter_groups(chunks, per_chunk_chapters=False)
            self.assertEqual([("Chapter 1", 0, 2), ("Chapter 2", 3, 4)], groups)
            with patch.object(pm, "load_chunks", return_value=chunks):
                loaded, _ = pm._load_chunks_with_audio()
            from tts import compute_timeline
            timeline = compute_timeline(loaded, 500, 200)
            m4b = pm._build_m4b_chapters(timeline, per_chunk_chapters=False)
            self.assertEqual(["Chapter 1", "Chapter 2"], [c[0] for c in m4b])
            self.assertEqual(timeline[3][2], m4b[1][1], "chapter 2 starts where its heading chunk starts")

    def test_writes_one_file_per_chapter_with_a_manifest_and_reuses_unchanged_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, "load_chunks", return_value=chunks):
                ok, msg = pm.export_chapters(fmt="wav", book_name="Test Book",
                                             template="{book_name} {chapter_number} {chapter_name}")
                self.assertTrue(ok, msg); self.assertIn("2 chapter file(s) written", msg)
                out = os.path.join(tmp, CHAPTER_EXPORT_DIR)
                self.assertEqual(sorted(["Test Book 01 Chapter 1.wav", "Test Book 02 Chapter 2.wav", "manifest.json"]),
                                 sorted(os.listdir(out)))
                info = sf.info(os.path.join(out, "Test Book 02 Chapter 2.wav"))
                self.assertGreater(info.frames / info.samplerate, 1.0)   # 0.7 s + 0.8 s of audio + a pause
                with open(os.path.join(out, "manifest.json")) as handle:
                    manifest = json.load(handle)
                self.assertEqual([1, 2], [r["number"] for r in manifest["chapters"]])
                # Second export with nothing changed: nothing rewritten.
                ok, msg = pm.export_chapters(fmt="wav", book_name="Test Book",
                                             template="{book_name} {chapter_number} {chapter_name}", changed_only=True)
                self.assertIn("0 chapter file(s) written, 2 unchanged and kept", msg)
                # Re-render chunk 4 (chapter 2's audio changes): only chapter 2 is rewritten.
                _tone(os.path.join(tmp, "voicelines", "c4.wav"), 1.5, hz=330)
                os.utime(os.path.join(tmp, "voicelines", "c4.wav"), (0, 10 ** 9))
                ok, msg = pm.export_chapters(fmt="wav", book_name="Test Book",
                                             template="{book_name} {chapter_number} {chapter_name}", changed_only=True)
                self.assertIn("1 chapter file(s) written, 1 unchanged and kept", msg)

    def test_changed_only_decodes_only_the_changed_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, "load_chunks", return_value=chunks):
                self.assertTrue(pm.export_chapters(fmt="wav")[0])
                _tone(os.path.join(tmp, "voicelines", "c4.wav"), 1.5, hz=330)
                os.utime(os.path.join(tmp, "voicelines", "c4.wav"), (0, 10 ** 9))
                original = pm._load_chunks_with_audio
                loaded_groups = []

                def record_load(*args, **kwargs):
                    loaded_groups.append(kwargs.get("chunks"))
                    return original(*args, **kwargs)

                with patch.object(pm, "_load_chunks_with_audio", side_effect=record_load):
                    ok, msg = pm.export_chapters(fmt="wav", changed_only=True)
            self.assertTrue(ok, msg)
            self.assertEqual([2], [len(group) for group in loaded_groups])
            self.assertIn("1 chapter file(s) written, 1 unchanged and kept", msg)

    def test_subset_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, "load_chunks", return_value=chunks):
                ok, msg = pm.export_chapters(fmt="wav", chapters=[1])
                self.assertTrue(ok); self.assertIn("1 chapter file(s) written", msg)
                self.assertEqual(["02 - Chapter 2.wav", "manifest.json"], sorted(os.listdir(os.path.join(tmp, CHAPTER_EXPORT_DIR))))
                ok, msg = pm.export_chapters(fmt="wav", cancel_check=lambda: True)
                self.assertEqual((False, "Export cancelled"), (ok, msg))

    def test_preview_matches_export_names_and_drops_missing_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, "load_chunks", return_value=chunks):
                names = [r["file"] for r in pm.preview_chapter_filenames(fmt="mp3")]
                self.assertEqual(["01 - Chapter 1.mp3", "02 - Chapter 2.mp3"], names)
                for chunk in chunks:
                    os.remove(os.path.join(tmp, chunk["audio_path"]))
                self.assertEqual([], pm.preview_chapter_filenames(fmt="mp3"))
                self.assertEqual((False, "No audio segments found"), pm.export_chapters())

    def test_unsupported_format_is_refused_before_any_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            with patch.object(pm, "load_chunks", side_effect=AssertionError("must not load")):
                self.assertEqual((False, "Unsupported format: ogg"), pm.export_chapters(fmt="ogg"))

    def test_duplicate_template_refuses_before_it_can_write_over_a_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, "load_chunks", return_value=chunks):
                ok, message = pm.export_chapters(fmt="wav", template="{book_name}",
                                                  book_name="Novel")
            self.assertFalse(ok)
            self.assertIn("duplicate filenames", message)
            self.assertFalse(os.path.exists(os.path.join(tmp, CHAPTER_EXPORT_DIR)))


class RouteTests(unittest.TestCase):
    def test_export_can_require_every_speaker_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "script.json")
            voices = os.path.join(tmp, "voice_config.json")
            Path(script).write_text(json.dumps([{"speaker": "Hero"}, {"speaker": "NARRATOR"}]), encoding="utf-8")
            Path(voices).write_text(json.dumps({"Hero": {"ready": True}}), encoding="utf-8")
            with patch.object(editor_module, "SCRIPT_PATH", script), \
                 patch.object(editor_module, "DATA_DIR", tmp), \
                 patch.object(editor_module, "claim_gpu_task"):
                with self.assertRaises(HTTPException) as ctx:
                    asyncio.run(editor_module.export_chapters(
                        editor_module.ChapterExportRequest(require_ready=True),
                        editor_module.BackgroundTasks()))
            self.assertEqual(409, ctx.exception.status_code)
            self.assertIn("NARRATOR", ctx.exception.detail["speakers"])

    def test_preview_rejects_the_same_invalid_padding_as_export(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(editor_module.preview_chapter_filenames(padding=7))
        self.assertEqual(400, ctx.exception.status_code)
        with self.assertRaises(Exception):
            editor_module.ChapterExportRequest(padding=7)

    def test_zip_404s_when_nothing_is_exported_and_serves_only_listed_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(editor_module, "DATA_DIR", tmp):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(editor_module.download_chapters_zip())
            self.assertEqual(404, ctx.exception.status_code)
            out = os.path.join(tmp, CHAPTER_EXPORT_DIR); os.makedirs(out)
            for n in ("a.mp3", "b.mp3", "stray.mp3"):
                with open(os.path.join(out, n), "wb") as handle:
                    handle.write(b"X" + n.encode())
            with open(os.path.join(out, "manifest.json"), "w") as handle:
                json.dump({"chapters": [{"index": 0, "file": "a.mp3"},
                                         {"index": 1, "file": "b.mp3"}]}, handle)
            resp = asyncio.run(editor_module.download_chapters_zip(names="b.mp3"))
            with zipfile.ZipFile(resp.path) as zf:
                self.assertEqual(["b.mp3"], zf.namelist())
            asyncio.run(resp.background())
            self.assertFalse(os.path.exists(resp.path))
            listed = asyncio.run(editor_module.list_chapter_exports())
            self.assertEqual([True, True], [r["exists"] for r in listed["chapters"]])
            with self.assertRaises(HTTPException):
                asyncio.run(editor_module.download_chapter("../manifest.json"))
            with self.assertRaises(HTTPException):
                asyncio.run(editor_module.download_chapter("stray.mp3".replace("stray", "missing")))


if __name__ == "__main__":
    unittest.main()


class DeferredExportCancelTests(unittest.TestCase):
    def run_export(self, chapter, cancel_before_start):
        import copy
        from contextlib import ExitStack
        from fastapi import BackgroundTasks
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        import core
        state = copy.deepcopy(core.process_state)
        for entry in state.values():
            entry["running"] = False
        key = "chapter_export" if chapter else "audio"
        state[key]["cancel"] = True
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            pm, chunks = _project(tmp)
            pm.save_chunks(chunks)
            pm.load_chunks()
            output = Path(tmp, CHAPTER_EXPORT_DIR) if chapter else Path(tmp, "cloned_audiobook.mp3")
            if chapter:
                output.mkdir()
                (output / "previous.wav").write_bytes(b"previous chapter")
                (output / "manifest.json").write_text('{"chapters": []}')
            else:
                output.write_bytes(b"previous merged output")
            before = {str(path.relative_to(tmp)): path.read_bytes()
                      for path in Path(tmp).rglob("*") if path.is_file()}
            stack.enter_context(patch.object(core, "process_state", state))
            stack.enter_context(patch.object(editor_module, "process_state", state))
            stack.enter_context(patch.object(core, "_gpu_leases", {}))
            stack.enter_context(patch.object(core, "acquire_gpu_lock", return_value=None))
            stack.enter_context(patch.object(editor_module, "project_manager", pm))
            background = BackgroundTasks()
            if chapter:
                response = asyncio.run(editor_module.export_chapters(
                    editor_module.ChapterExportRequest(format="wav"), background))
            else:
                response = asyncio.run(editor_module.merge_audio_endpoint(background))
            self.assertEqual("started", response["status"])
            self.assertTrue(state[key]["running"])
            self.assertFalse(state[key]["cancel"], "claim must clear only the earlier run's cancel")
            if cancel_before_start:
                app = FastAPI()
                app.include_router(editor_module.router)
                endpoint = "/api/export_chapters/cancel" if chapter else "/api/cancel_audio"
                with TestClient(app) as client:
                    cancelled = client.post(endpoint)
                self.assertEqual(200, cancelled.status_code, cancelled.text)
                self.assertEqual("cancelling", cancelled.json()["status"])
                self.assertTrue(state[key]["cancel"])
            with patch("project._load_audio_segment", wraps=__import__("project")._load_audio_segment) as decoder:
                asyncio.run(background())
            self.assertFalse(state[key]["running"])
            self.assertFalse(state[key]["cancel"])
            if cancel_before_start:
                decoder.assert_not_called()
                self.assertTrue(any("cancelled" in line.lower() for line in state[key]["logs"]))
                self.assertFalse(any("complete" in line.lower() for line in state[key]["logs"]))
                after = {str(path.relative_to(tmp)): path.read_bytes()
                         for path in Path(tmp).rglob("*") if path.is_file()}
                self.assertEqual(before, after)
            elif chapter:
                self.assertEqual(len(chunks), decoder.call_count)
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(2, len(manifest["chapters"]))
                for row in manifest["chapters"]:
                    self.assertGreater(sf.info(output / row["file"]).frames, 0)
                self.assertTrue(any("complete" in line.lower() for line in state[key]["logs"]))
            else:
                from pydub import AudioSegment
                self.assertEqual(len(chunks), decoder.call_count)
                self.assertNotEqual(before[output.name], output.read_bytes())
                self.assertGreater(len(AudioSegment.from_mp3(output)), 0)
                self.assertTrue(any("complete" in line.lower() for line in state[key]["logs"]))
            for name, data in before.items():
                if name.startswith("voicelines/") or name == "chunks.json":
                    self.assertEqual(data, Path(tmp, name).read_bytes())

    def test_cancel_between_claim_and_merge_worker_preserves_audio_artifacts(self):
        self.run_export(chapter=False, cancel_before_start=True)

    def test_cancel_between_claim_and_chapter_worker_preserves_audio_artifacts(self):
        self.run_export(chapter=True, cancel_before_start=True)

    def test_new_merge_and_chapter_runs_still_clear_previous_run_cancel_and_export(self):
        for chapter in (False, True):
            with self.subTest(chapter=chapter):
                self.run_export(chapter=chapter, cancel_before_start=False)


class MalformedChapterManifestTests(unittest.TestCase):
    MALFORMED = ({"chapters": ["bad"]}, {"chapters": None}, {"chapters": {}},
                 {"chapters": [{}]}, {"chapters": [{"index": 0, "file": None}]},
                 {"chapters": [{"index": [], "file": "01 - Chapter 1.wav"}]},
                 {"chapters": [{"index": True, "file": "01 - Chapter 1.wav"}]},
                 {"chapters": [{"index": -1, "file": "01 - Chapter 1.wav"}]},
                 {"chapters": [{"index": 0, "file": ""}]},
                 {"chapters": [{"index": 0, "file": "01 - Chapter 1.wav"}, "bad"]},
                 {"chapters": [{"index": 0, "file": "01 - Chapter 1.wav"},
                               {"index": 0, "file": "02 - Chapter 2.wav"}]}, ["bad-root"])

    def test_corrupt_previous_rows_render_complete_audio_then_reuse_it(self):
        timing_cases = ({"chapters": [{"index": 0, "file": "01 - Chapter 1.wav"}]},
                        {"chapters": [{"index": 0, "file": "01 - Chapter 1.wav",
                                       "start_ms": None, "end_ms": 100}]},
                        {"chapters": [{"index": 0, "file": "01 - Chapter 1.wav",
                                       "start_ms": 0, "end_ms": "not a time"}]})
        for malformed in self.MALFORMED + timing_cases:
            for changed in (False, True):
                with self.subTest(manifest=malformed, changed=changed), tempfile.TemporaryDirectory() as tmp:
                    pm, chunks = _project(tmp)
                    with patch.object(pm, "load_chunks", return_value=chunks), \
                         patch.object(pm, "_load_pause_defaults", return_value=(10, 5)):
                        ok, msg = pm.export_chapters(fmt="wav")
                        self.assertTrue(ok, msg)
                        output = Path(tmp, CHAPTER_EXPORT_DIR)
                        expected_manifest = json.loads((output / "manifest.json").read_text())
                        expected_audio = {row["file"]: sf.read(output / row["file"], dtype="int16")
                                          for row in expected_manifest["chapters"]}
                        (output / "manifest.json").write_text(json.dumps(malformed))
                        ok, msg = pm.export_chapters(fmt="wav", changed_only=changed)
                        self.assertTrue(ok, msg)
                        self.assertIn("2 chapter file(s) written", msg)
                        self.assertEqual(expected_manifest, json.loads((output / "manifest.json").read_text()))
                        for name, (samples, rate) in expected_audio.items():
                            actual, actual_rate = sf.read(output / name, dtype="int16")
                            self.assertEqual(rate, actual_rate)
                            np.testing.assert_array_equal(samples, actual)
                        before = {name: (output / name).read_bytes() for name in expected_audio}
                        ok, msg = pm.export_chapters(fmt="wav", changed_only=True)
                        self.assertTrue(ok, msg)
                        self.assertIn("0 chapter file(s) written, 2 unchanged and kept", msg)
                        self.assertEqual(before, {name: (output / name).read_bytes() for name in expected_audio})

    def test_http_listing_and_zip_reject_corrupt_whole_set_without_rewriting_manifest(self):
        import io
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(editor_module.router)
        with tempfile.TemporaryDirectory() as tmp, patch.object(editor_module, "DATA_DIR", tmp), TestClient(app) as client:
            output = Path(tmp, CHAPTER_EXPORT_DIR)
            output.mkdir()
            for name in ("01 - Chapter 1.wav", "02 - Chapter 2.wav"):
                (output / name).write_bytes(b"existing audio " + name.encode())
            path = output / "manifest.json"
            for malformed in self.MALFORMED:
                with self.subTest(manifest=malformed):
                    path.write_text(json.dumps(malformed))
                    before = path.read_bytes()
                    listed = client.get("/api/chapter_exports")
                    self.assertEqual(200, listed.status_code, listed.text)
                    self.assertEqual([], listed.json()["chapters"])
                    zipped = client.get("/api/chapter_exports/zip")
                    self.assertEqual(404, zipped.status_code, zipped.text)
                    self.assertEqual(before, path.read_bytes())
            valid = {"custom": "keep", "chapters": [{"index": 0, "file": "01 - Chapter 1.wav"},
                                                    {"index": 1, "file": "02 - Chapter 2.wav"}]}
            path.write_text(json.dumps(valid))
            before = path.read_bytes()
            listed = client.get("/api/chapter_exports")
            self.assertEqual(200, listed.status_code, listed.text)
            self.assertEqual("keep", listed.json()["custom"])
            self.assertEqual([True, True], [row["exists"] for row in listed.json()["chapters"]])
            zipped = client.get("/api/chapter_exports/zip", params={"names": "02 - Chapter 2.wav"})
            self.assertEqual(200, zipped.status_code, zipped.text)
            with zipfile.ZipFile(io.BytesIO(zipped.content)) as archive:
                self.assertEqual(["02 - Chapter 2.wav"], archive.namelist())
                self.assertEqual((output / "02 - Chapter 2.wav").read_bytes(), archive.read("02 - Chapter 2.wav"))
            self.assertEqual(before, path.read_bytes())
