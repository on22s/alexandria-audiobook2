"""Chapter-by-chapter export: filenames from a template, chapters from the same
grouping the M4B uses, changed-only re-export, and the download routes."""
import asyncio
import io
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

from project import (CHAPTER_EXPORT_DIR, ProjectManager, build_chapter_filename)
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
                manifest = json.load(open(os.path.join(out, "manifest.json")))
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

    def test_subset_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            with patch.object(pm, "load_chunks", return_value=chunks):
                ok, msg = pm.export_chapters(fmt="wav", chapters=[1])
                self.assertTrue(ok); self.assertIn("1 chapter file(s) written", msg)
                self.assertEqual(["02 - Chapter 2.wav", "manifest.json"], sorted(os.listdir(os.path.join(tmp, CHAPTER_EXPORT_DIR))))
                ok, msg = pm.export_chapters(fmt="wav", cancel_check=lambda: True)
                self.assertEqual((False, "Export cancelled"), (ok, msg))

    def test_preview_needs_no_audio_and_matches_the_export_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm, chunks = _project(tmp)
            for c in chunks:
                os.remove(os.path.join(tmp, c["audio_path"]))     # no audio on disk at all
            with patch.object(pm, "load_chunks", return_value=chunks):
                names = [r["file"] for r in pm.preview_chapter_filenames(fmt="mp3")]
            self.assertEqual(["01 - Chapter 1.mp3", "02 - Chapter 2.mp3"], names)

    def test_unsupported_format_is_refused_before_any_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            with patch.object(pm, "load_chunks", side_effect=AssertionError("must not load")):
                self.assertEqual((False, "Unsupported format: ogg"), pm.export_chapters(fmt="ogg"))


class RouteTests(unittest.TestCase):
    def test_zip_404s_when_nothing_is_exported_and_serves_only_listed_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(editor_module, "DATA_DIR", tmp):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(editor_module.download_chapters_zip())
            self.assertEqual(404, ctx.exception.status_code)
            out = os.path.join(tmp, CHAPTER_EXPORT_DIR); os.makedirs(out)
            for n in ("a.mp3", "b.mp3", "stray.mp3"):
                open(os.path.join(out, n), "wb").write(b"X" + n.encode())
            json.dump({"chapters": [{"index": 0, "file": "a.mp3"}, {"index": 1, "file": "b.mp3"}]},
                      open(os.path.join(out, "manifest.json"), "w"))
            resp = asyncio.run(editor_module.download_chapters_zip(names="b.mp3"))
            async def drain():
                return b"".join([c async for c in resp.body_iterator])
            with zipfile.ZipFile(io.BytesIO(asyncio.run(drain()))) as zf:
                self.assertEqual(["b.mp3"], zf.namelist())
            listed = asyncio.run(editor_module.list_chapter_exports())
            self.assertEqual([True, True], [r["exists"] for r in listed["chapters"]])
            with self.assertRaises(HTTPException):
                asyncio.run(editor_module.download_chapter("../manifest.json"))
            with self.assertRaises(HTTPException):
                asyncio.run(editor_module.download_chapter("stray.mp3".replace("stray", "missing")))


if __name__ == "__main__":
    unittest.main()
