"""Titled headings glued to their chapter's first paragraph still start chapters (#1921).

Synthetic text only (Rule 27). The reported 5-chapter book came back from
annotation with every heading ("Chapter 3: Some Title") as the first paragraph
of a long narrator entry, and two of those entries marked spoken because later
paragraphs quote speech. The splitter knew only bare "Chapter 3", and never
split spoken entries, so the book lost every boundary. What these verify,
through the real group_into_chunks -> _chapter_groups pipeline:
- a standalone titled keyword heading is split out and starts a chapter, also
  when its entry is marked spoken; the rest of a spoken entry stays spoken;
- prose that merely begins with a keyword, a heading that is not its own
  paragraph, and a heading in the middle of a spoken entry are not split;
- a five-chapter book in that shape yields five chapters whose text is exactly
  the text between consecutive headings.
"""
import contextlib
import io
import re
import tempfile
import unittest

import project
from project import ProjectManager, group_into_chunks, is_titled_chapter_heading

PROSE = ("The morning bell rang across the courtyard and every student hurried toward "
         "the hall, where the instructors were already waiting in a line.")
SPEECH = "“You are late again,” the instructor said, and nobody laughed."


def narrator(text, spoken=False):
    return {"speaker": "NARRATOR", "text": text, "instruct": "", "spoken": spoken}


def chapters_for(entries):
    with contextlib.redirect_stdout(io.StringIO()):
        chunks = group_into_chunks(entries)
        return chunks, ProjectManager(tempfile.gettempdir())._chapter_groups(chunks, False)


class TitledChapterHeadingTests(unittest.TestCase):
    def test_titled_heading_rule(self):
        for text in ("Chapter 3: The Gate (2)", "Chapter Five - The Gate", "Chapter twenty-one: Ash",
                     "Part II: Rising", "Prologue: Before the Fall", "Chapter 12 — Night", "Book 1. Embers"):
            with self.subTest(text=text):
                self.assertTrue(is_titled_chapter_heading(text))
        for text in ("Chapter 3 was the hardest part of the book.", "Chapter 3: he said it was over.",
                     "Part of the problem: nobody listened", "Chapter 3: Title\nand more",
                     "Chapter 3: " + "very long title " * 10):
            with self.subTest(text=text):
                self.assertFalse(is_titled_chapter_heading(text))

    def test_heading_glued_to_its_paragraph_is_split_out(self):
        for spoken in (False, True):
            with self.subTest(spoken=spoken):
                chunks, groups = chapters_for([
                    narrator("Chapter 1: The Gate\n\n" + PROSE + "\n\n" + SPEECH, spoken),
                    narrator("Chapter 2: The Hall (1)\n\n" + PROSE, spoken)])
                self.assertEqual(["Chapter 1: The Gate", "Chapter 2: The Hall (1)"],
                                 [title for title, _, _ in groups])
                heading = chunks[groups[0][1]]
                self.assertEqual("Chapter 1: The Gate", heading["text"])
                self.assertIsNot(True, heading.get("spoken"))
                if spoken:
                    # Only the heading becomes narration; the body keeps the annotator's flag.
                    self.assertTrue(all(c.get("spoken") is True for c in chunks[groups[0][1] + 1:groups[0][2] + 1]))

    def test_lookalikes_are_not_split(self):
        cases = {
            "prose that starts with a keyword": narrator("Chapter 3: he said it was over.\n\n" + PROSE),
            "heading not its own paragraph": narrator("Chapter 3: The Gate\n" + PROSE),
            "heading inside a spoken entry": narrator(SPEECH + "\n\nChapter 3: The Gate\n\n" + PROSE, True),
        }
        for name, entry in cases.items():
            with self.subTest(case=name):
                _, groups = chapters_for([narrator(PROSE), entry])
                self.assertEqual([("Chapter 1", 0, groups[0][2])], groups)

    def test_five_chapter_book_has_exact_boundaries(self):
        titles = ["Chapter 1: Into the Gate", "Chapter 2: Evaluation (1)", "Chapter 3: Evaluation (2)",
                  "Chapter 4: Goal", "Chapter 5: Realm of Winter"]
        entries, expected = [], []
        for number, title in enumerate(titles, 1):
            body = [f"Paragraph {number}-{k}. " + PROSE for k in range(3)]
            spoken = number in (3, 4)  # as reported: two heading entries marked spoken
            entries.append(narrator(title + "\n\n" + body[0] + ("\n\n" + SPEECH if spoken else ""), spoken))
            entries += [narrator(text) for text in body[1:]]
            expected.append(" ".join([title, body[0]] + ([SPEECH] if spoken else []) + body[1:]))
        chunks, groups = chapters_for(entries)
        self.assertEqual(titles, [title for title, _, _ in groups])
        words = lambda text: re.findall(r"\w+", text.lower())
        for (title, first, last), text in zip(groups, expected):
            with self.subTest(chapter=title):
                self.assertEqual(words(text), words(" ".join(c["text"] for c in chunks[first:last + 1])))


if __name__ == "__main__":
    unittest.main()
