"""Chapter detection ignores heading look-alikes; no headings means one chapter (#1921).

All text is synthetic (Rule 27). What these verify, through the real
script -> group_into_chunks -> _chapter_groups pipeline:
- a status window (a bracketed title and a run of short "label: value" lines),
  an interrupted sentence ending in a dash and one trailing off in an ellipsis
  are not chapter starts, so a one-chapter excerpt is one chapter;
- keyword headings are always kept, including adjacent ones;
- a script with no headings is one chapter (not one per line), while the
  explicit per_chunk_chapters option still gives one per line;
- the Editor's chapter list (/api/chunks/repair_options) agrees.
"""
import contextlib
import io
import tempfile
import unittest

import project
from project import ProjectManager, group_into_chunks, is_heading_lookalike, is_smart_chapter_text

PROSE = ("The morning bell rang across the courtyard and every student hurried "
         "toward the hall, where the instructors were already waiting in a line.")


def narrator(text):
    return {"speaker": "NARRATOR", "text": text, "instruct": ""}


def groups_for(entries, per_chunk=False):
    with contextlib.redirect_stdout(io.StringIO()):
        chunks = group_into_chunks(entries)
        return chunks, ProjectManager(tempfile.gettempdir())._chapter_groups(chunks, per_chunk)


class HeadingLookalikeTests(unittest.TestCase):
    def test_line_rules(self):
        for text in ("Chapter 3: The Weakest Student (1)", "The Beginning", "Prologue",
                     "Part Two: Rising", "Lv: 1"):
            with self.subTest(text=text):
                self.assertTrue(is_smart_chapter_text(text))
        for text in ("[Status Window]", "【System】", "He was about to answer –",
                     "He was about to answer —", "And then…", "And then...",
                     "Currently, it was,", "There was only one thing left;"):
            with self.subTest(text=text):
                self.assertTrue(is_heading_lookalike(text))
                self.assertFalse(is_smart_chapter_text(text))
        # A keyword heading is never rejected for its punctuation.
        self.assertTrue(is_smart_chapter_text("Chapter 4: And Then…"))

    def test_one_chapter_excerpt_with_status_window_and_fragments_is_one_chapter(self):
        entries = [narrator("Chapter 3: The Weakest Student (1)"), narrator(PROSE),
                   narrator("[Ian Example]"), narrator("Lv: 1"), narrator("Race: Human"),
                   narrator("Elements: Fire, Water"), narrator("Danger: Low"),
                   narrator(PROSE), narrator("He was about to answer –"), narrator(PROSE),
                   narrator("Somehow, it all felt so familiar…"), narrator(PROSE)]
        chunks, groups = groups_for(entries)
        self.assertEqual([("Chapter 3: The Weakest Student (1)", 0, len(chunks) - 1)], groups)

    def test_status_window_alone_does_not_split_a_headed_book(self):
        entries = [narrator("Chapter 1"), narrator(PROSE), narrator("[Status]"),
                   narrator("Lv: 1"), narrator("Race: Human"), narrator(PROSE),
                   narrator("Chapter 2"), narrator(PROSE)]
        chunks, groups = groups_for(entries)
        self.assertEqual(["Chapter 1", "Chapter 2"], [title for title, _, _ in groups])

    def test_adjacent_keyword_headings_and_a_lone_untitled_heading_are_kept(self):
        entries = [narrator("Part One"), narrator("Chapter 1"), narrator(PROSE),
                   narrator("The Long Night"), narrator(PROSE)]
        _, groups = groups_for(entries)
        self.assertEqual(["Part One", "Chapter 1", "The Long Night"], [title for title, _, _ in groups])

    def test_no_headings_is_one_chapter_and_per_chunk_still_splits(self):
        entries = [narrator(PROSE), {"speaker": "HERO", "text": "Wait for me.", "instruct": ""},
                   narrator(PROSE + " Then they left.")]
        chunks, groups = groups_for(entries)
        self.assertEqual([("Chapter 1", 0, len(chunks) - 1)], groups)
        _, per_line = groups_for(entries, per_chunk=True)
        self.assertEqual(len(chunks), len(per_line))
        self.assertEqual([], ProjectManager(tempfile.gettempdir())._chapter_groups([], False))

    def test_cjk_and_decorated_keyword_headings_are_kept(self):
        """Bracketed or dashed keyword headings are chapters, not status lines."""
        for text in ("第一章", "【第一章】", "[第1話]",
                     "第十二回 旅立ち", "- Chapter 1 -",
                     "— Chapter 1 —", "[Chapter 1]"):
            with self.subTest(text=text):
                self.assertTrue(project.is_keyword_chapter_heading(text))
                self.assertTrue(is_smart_chapter_text(text))
        for text in ("[Status Window]", "[1]", "Lv: 1"):
            with self.subTest(text=text):
                self.assertFalse(project.is_keyword_chapter_heading(text))
        cjk_prose = "彼は静かに扉を開けて中に入った。" * 3
        entries = [narrator("【第一章】"), narrator("旅立ち"), narrator(cjk_prose),
                   narrator("[第2話]"), narrator(cjk_prose)]
        _, groups = groups_for(entries)
        titles = [title for title, _, _ in groups]
        self.assertIn("【第一章】", titles)
        self.assertIn("[第2話]", titles)

    def test_cjk_counters_in_prose_and_loose_decoration_are_not_headings(self):
        """Re-review: 回/部 are also everyday counters, and only a matching frame
        is stripped before the keyword check."""
        for text in ("\u7b2c\u4e00\u56de\u76ee\u306e\u4f1a\u8b70\u306f\u59cb\u307e\u3063\u305f",
                     "\u7b2c\u4e00\u90e8\u968a\u304c\u5230\u7740\u3057\u305f",
                     "\u7b2c\u4e00\u7ae0\u306f\u9000\u5c48\u3060\u3063\u305f",
                     "\u7b2c\u4e00\u56de\u306e\u8a66\u5408", "--chapter", "~chapter",
                     "[Chapter 1"):
            with self.subTest(text=text):
                self.assertFalse(project.is_keyword_chapter_heading(text))
        for text in ("\u7b2c\u4e00\u56de", "\u7b2c\u4e00\u90e8 \u59cb\u307e\u308a",
                     "\u7b2c\uff11\u7ae0", "\u300c\u7b2c1\u7ae0\u300d", "~ Chapter 2 ~"):
            with self.subTest(text=text):
                self.assertTrue(project.is_keyword_chapter_heading(text))

    def test_readalong_sections_are_the_shared_chapters(self):
        """The sidecar used stored heading flags, so a status window became its
        own sections there alone (Rule 15). It now uses _chapter_groups."""
        import json
        from pathlib import Path
        from pydub import AudioSegment
        from readalong_export import build_sidecar_export, get_sidecar_output_path
        entries = [narrator("Chapter 1"), narrator(PROSE), narrator("Lv: 1"), narrator("Race: Human"),
                   narrator(PROSE), narrator("Chapter 2"), narrator(PROSE)]
        with contextlib.redirect_stdout(io.StringIO()):
            chunks = group_into_chunks(entries)
        self.assertTrue(any((c.get("chapter_origin") or {}).get("is_heading")
                            for c in chunks if c["text"].startswith("Lv")),
                        "fixture must carry the stored heading flag that misled the sidecar")
        for i, chunk in enumerate(chunks):
            chunk.update(id=i, uid=f"u{i}", audio_path=f"clip{i}.wav", audio_revision=f"r{i}")

        def clip():
            return AudioSegment(data=b"\x00\x20" * 2400, sample_width=2, frame_rate=24000, channels=1)

        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            manager = ProjectManager(tmp)
            (Path(tmp) / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
            book = Path(tmp) / "book.txt"; book.write_bytes(b"Synthetic book.")
            (Path(tmp) / "state.json").write_text(json.dumps({"input_file_path": str(book)}), encoding="utf-8")
            (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
            manager._load_chunks_with_audio = lambda **kwargs: ([(chunk, clip()) for chunk in chunks], 0)
            manager._load_pause_defaults = lambda: (10, 10)
            manager.load_chunks = lambda: chunks
            (Path(tmp) / "cloned_audiobook.mp3").write_bytes(b"mp3")
            ok, message = build_sidecar_export(manager, lambda: False, includes_text=False,
                                               audio_path=str(Path(tmp) / "cloned_audiobook.mp3"))
            self.assertTrue(ok, message)
            data = json.loads(Path(get_sidecar_output_path(tmp)).read_text(encoding="utf-8"))
        expected = manager._chapter_groups(chunks, False)
        self.assertEqual(["Chapter 1", "Chapter 2"], [title for title, _, _ in expected])
        self.assertEqual(len(expected), len(data["sections"]))
        starts = [next(e["index"] for e in data["entries"] if e["section"] == section["id"])
                  for section in data["sections"]]
        self.assertEqual([first for _, first, _ in expected], starts)

    def test_editor_chapter_list_agrees(self):
        from routers import editor
        entries = [narrator(PROSE), narrator("Lv: 1"), narrator("Race: Human"), narrator(PROSE)]
        chunks, _ = groups_for(entries)
        with contextlib.redirect_stdout(io.StringIO()):
            options = editor.get_audio_repair_options(chunks)
        self.assertEqual([{"index": 0, "title": "Chapter 1", "first": 0, "last": len(chunks) - 1}],
                         options["chapters"])
        self.assertIn("whole script is one chapter", options["grouping_note"])


if __name__ == "__main__":
    unittest.main()
