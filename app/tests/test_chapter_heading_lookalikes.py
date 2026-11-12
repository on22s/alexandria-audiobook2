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
