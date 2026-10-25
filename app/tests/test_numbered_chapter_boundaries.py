"""Real chapter lines must survive annotation-to-chunk preparation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from delivery_review import get_delivery_retry_chunks
from literal_policy import (bind_literal_policy, ensure_literal_chunk,
                            get_literal_policy, get_word_identity)
from project import (EXPLICIT_SILENCE_MS, ProjectManager, get_speakable_entries,
                     group_into_chunks)
from subprocess_ownership import run_owned_capture


def get_script():
    return [{"speaker": "NARRATOR", "text": text, "instruct": "neutral"}
            for text in ("CHAPTER I.", "The first room was quiet.",
                         "CHAPTER II.", "The second room was empty.")]


class NumberedChapterBoundariesTests(unittest.TestCase):
    def test_numbered_narrator_rows_keep_native_chapter_groups(self):
        for first, second in (("CHAPTER I.", "CHAPTER II."),
                              ("Chapter 1.", "Chapter 2."),
                              ("part i!", "part ii?"),
                              ("Section 1。", "Section 2！")):
            with self.subTest(first=first):
                rows = get_script()
                rows[0]["text"], rows[2]["text"] = first, second
                before = copy.deepcopy(rows)
                chunks = group_into_chunks(rows)
                self.assertEqual([row["text"] for row in rows],
                                 [chunk["text"] for chunk in chunks])
                manager = ProjectManager.__new__(ProjectManager)
                self.assertEqual([(first, 0, 1), (second, 2, 3)],
                                 manager._chapter_groups(chunks, False))
                self.assertEqual(before, rows)

    def test_embedded_heading_lines_preserve_source_rows_context_and_pause(self):
        text = "Before.\r\n\r\nCHAPTER II.\r\nThe next room was empty."
        rows = [{"speaker": "NARRATOR", "text": text, "instruct": "calm",
                 "pause_after": 1300, "narrator_version": "second",
                 "focus_speaker": "MARA", "source_span": [0, len(text)]}]
        before = copy.deepcopy(rows)
        prepared = get_speakable_entries(rows)
        self.assertEqual(["Before.", "CHAPTER II.", "The next room was empty."],
                         [part["text"] for part in prepared])
        self.assertTrue(all(part["source_span"] == [0, len(text)] for part in prepared))
        chunks = group_into_chunks(rows, include_source_indices=True)
        self.assertEqual([1300], [c["pause_after"] for c in chunks if "pause_after" in c])
        self.assertEqual(1300, chunks[-1]["pause_after"])
        for chunk in chunks:
            self.assertEqual([0], chunk["source_entry_indices"])
            self.assertEqual("second", chunk["narrator_version"])
            self.assertEqual("MARA", chunk["focus_speaker"])
            self.assertEqual("calm", chunk["instruct"])
        self.assertEqual(before, rows)

    def test_dialogue_and_inline_mentions_do_not_add_heading_chunks(self):
        for speaker, text in (
                ("MARA", "Before.\nCHAPTER II.\nAfter."),
                ("NARRATOR", "Before. CHAPTER II. After."),
                ("NARRATOR", 'Before.\n"CHAPTER II."\nAfter.'),
                ("NARRATOR", "Before.\nPart of her wanted to leave.\nAfter.")):
            with self.subTest(speaker=speaker, text=text):
                chunks = group_into_chunks([{"speaker": speaker, "text": text}])
                self.assertEqual(1, len(chunks))
                self.assertEqual(" ".join(text.split()), chunks[0]["text"])

    def test_review_indices_stay_bound_to_original_rows(self):
        rows = [{"speaker": "NARRATOR", "text": "CHAPTER I.\nBefore."},
                {"speaker": "NARRATOR", "text": "CHAPTER II.\nA symbol ⌘."}]
        review = []
        chunks = group_into_chunks(rows, include_source_indices=True, review_sink=review)
        self.assertEqual([{"entry_index": 1, "character": "⌘"}], review)
        self.assertEqual([[0], [0], [1], [1]],
                         [chunk["source_entry_indices"] for chunk in chunks])

    def test_literal_policy_and_chunk_size_limits_stay_enforced(self):
        text = "CHAPTER I.\n" + "An ordinary sentence. " * 15 + "\nCHAPTER II.\nEnd."
        rows = bind_literal_policy(text, [{"speaker": "NARRATOR", "text": text,
                                         "pause_after": 1700}])
        before = copy.deepcopy(rows)
        chunks = group_into_chunks(rows, max_chars=20, include_source_indices=True)
        self.assertTrue(all(0 < len(chunk["text"]) <= 20 for chunk in chunks))
        self.assertEqual(get_word_identity([text]),
                         get_word_identity(chunk["text"] for chunk in chunks))
        self.assertEqual([1700], [c["pause_after"] for c in chunks if "pause_after" in c])
        for chunk in chunks:
            self.assertEqual([0], chunk["source_entry_indices"])
            ensure_literal_chunk(chunk, get_literal_policy(rows))
        self.assertEqual(before, rows)
        rows[0]["text"] = rows[0]["text"].replace("End.", "Changed.")
        with self.assertRaisesRegex(ValueError, "changed ordered source words"):
            group_into_chunks(rows)

    def test_trailing_heading_keeps_only_final_pause(self):
        chunks = group_into_chunks([{"speaker": "NARRATOR",
                                    "text": "Before.\nCHAPTER II.\n\n",
                                    "pause_after": 1600}])
        self.assertEqual(["Before.", "CHAPTER II."], [c["text"] for c in chunks])
        self.assertNotIn("pause_after", chunks[0])
        self.assertEqual(1600, chunks[-1]["pause_after"])

    def test_heading_split_preserves_punctuation_and_native_final_pause(self):
        for marks, expected in (("---", "Before. CHAPTER II. ---"),
                                ("***", "Before. CHAPTER II.")):
            with self.subTest(marks=marks):
                text = "Before.\nCHAPTER II.\n" + marks
                rows = bind_literal_policy(text, [{"speaker": "NARRATOR", "text": text,
                                                  "pause_after": 1700}])
                before = copy.deepcopy(rows)
                parts = get_speakable_entries(rows)
                self.assertEqual(expected, " ".join(part["text"] for part in parts))
                self.assertEqual(1700, parts[-1]["pause_after"])
                self.assertTrue(all("pause_after" not in part for part in parts[:-1]))
                chunks = group_into_chunks(rows)
                self.assertEqual(expected, " ".join(chunk["text"] for chunk in chunks))
                self.assertEqual(1700, chunks[-1]["pause_after"])
                for chunk in chunks:
                    ensure_literal_chunk(chunk, get_literal_policy(rows))
                self.assertEqual(before, rows)

    def test_quoted_and_spoken_headings_keep_their_existing_context(self):
        contexts = [(opening, closing, False) for opening, closing in (
            ('"', '"'), ('“', '”'), ('「', '」'), ('<', '>'),
            ('(', ')'), ('[', ']'), ('{', '}'), ("'", "'"), ('‘', '’'))]
        contexts.append(("", "", True))
        for opening, closing, spoken in contexts:
            with self.subTest(opening=opening, spoken=spoken):
                rows = [{"speaker": "NARRATOR", "text": "CHAPTER I"},
                        {"speaker": "NARRATOR", "spoken": spoken,
                         "text": f"Before. {opening}A quote\nCHAPTER II.\nMore quote.{closing} After."}]
                chunks = group_into_chunks(rows)
                self.assertFalse(any(chunk["text"].strip() == "CHAPTER II." for chunk in chunks))
                groups = ProjectManager.__new__(ProjectManager)._chapter_groups(chunks, False)
                self.assertEqual([("CHAPTER I", 0, len(chunks) - 1)], groups)
        # An unlocated entry (no `spoken`) is not speech: it joins a narration
        # run instead of splitting it, and never joins confirmed speech.
        narration = [{"speaker": "NARRATOR", "text": "One.", "spoken": False},
                     {"speaker": "NARRATOR", "text": "Two."},
                     {"speaker": "NARRATOR", "text": "Three.", "spoken": False}]
        self.assertEqual(["One. Two. Three."], [chunk["text"] for chunk in group_into_chunks(narration)])
        speech = [{"speaker": "TOM", "text": "Hi.", "spoken": True}, {"speaker": "TOM", "text": "there."}]
        self.assertEqual(["Hi.", "there."], [chunk["text"] for chunk in group_into_chunks(speech)])
        # Source facts describe where the old text sits; an edited text keeps none.
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            manager.save_chunks([{"id": 0, "uid": "u0", "speaker": "NARRATOR", "text": "said Tom.",
                                  "instruct": "", "status": "pending", "spoken": False,
                                  "source_speaker": "Tom", "source_span": [10, 19],
                                  "dialogue_ambiguity": "other_convention"}])
            manager.update_chunk(0, {"speaker": "TOM", "text": "said Tom."})
            self.assertEqual(("Tom", [10, 19]), tuple(manager.load_chunks()[0].get(k)
                                                      for k in ("source_speaker", "source_span")))
            manager.update_chunk(0, {"text": "CHAPTER II."})
            row = manager.load_chunks()[0]
            self.assertFalse({"spoken", "source_speaker", "source_span", "dialogue_ambiguity"} & set(row))

    def test_balanced_context_away_from_heading_keeps_its_boundary(self):
        for opening, closing in (('(', ')'), ('[', ']'), ('{', '}'), ('<', '>'),
                                 ('"', '"'), ('“', '”'), ('‘', '’'), ('「', '」')):
            with self.subTest(opening=opening):
                text = (f"Before {opening}an aside{closing} continued.\nCHAPTER II.\n"
                        f"After {opening}another aside{closing} ended.")
                rows = [{"speaker": "NARRATOR", "text": text}]
                before = copy.deepcopy(rows)
                chunks = group_into_chunks(rows)
                heads = [i for i, chunk in enumerate(chunks) if chunk["text"] == "CHAPTER II."]
                self.assertEqual(1, len(heads))
                groups = ProjectManager.__new__(ProjectManager)._chapter_groups(chunks, False)
                self.assertTrue(any(title == "CHAPTER II." and first == heads[0]
                                    for title, first, last in groups))
                self.assertEqual(before, rows)

    def test_malformed_context_after_heading_defers_the_whole_row(self):
        for suffix in ('After (', 'After ]', 'After ([)].', 'After "', 'After ‘'):
            with self.subTest(suffix=suffix):
                text = "Before.\nCHAPTER II.\n" + suffix
                parts = get_speakable_entries([{"speaker": "NARRATOR", "text": text}])
                self.assertEqual([" ".join(text.split())], [part["text"] for part in parts])

    def test_apostrophes_before_a_heading_do_not_block_the_split(self):
        # A possessive or an elision is not an open quote (found in review of #1326).
        for apostrophe in ("'", "’"):
            for prose in (f"James{apostrophe} room was dark.", f"The dogs{apostrophe} bowls were empty.",
                          f"It was the {apostrophe}90s then."):
                with self.subTest(prose=prose):
                    parts = get_speakable_entries([{
                        "speaker": "NARRATOR", "text": f"{prose}\nCHAPTER II.\nIt was empty."}])
                    self.assertEqual([prose, "CHAPTER II.", "It was empty."],
                                     [part["text"] for part in parts])

    def test_a_heading_inside_an_open_single_quote_still_stays_joined(self):
        for opener, closer in (("'", "'"), ("‘", "’")):
            with self.subTest(opener=opener):
                parts = get_speakable_entries([{
                    "speaker": "NARRATOR", "text": f"He said, {opener}wait\nCHAPTER II.\nfor me.{closer}"}])
                self.assertEqual(1, len(parts))

    def test_balanced_single_quotes_before_a_heading_still_split(self):
        for opener, closer in (("'", "'"), ("‘", "’")):
            with self.subTest(opener=opener):
                prose = f"He said, {opener}wait{closer} and paused."
                parts = get_speakable_entries([{
                    "speaker": "NARRATOR", "text": f"{prose}\nCHAPTER II.\nIt was empty."}])
                self.assertEqual([prose, "CHAPTER II.", "It was empty."],
                                 [part["text"] for part in parts])

    def test_scene_break_endpoints_keep_existing_pause_when_heading_splits(self):
        for pause in (700, 1700):
            with self.subTest(pause=pause):
                parts = get_speakable_entries([{
                    "speaker": "NARRATOR", "pause_after": pause,
                    "text": "Before.\nCHAPTER II.\nDuring. ■After."}])
                self.assertEqual(["Before.", "CHAPTER II.", "During.", "After."],
                                 [part["text"] for part in parts])
                # The entry's pause is the silence after the whole entry (#1323), so the
                # scene break keeps its own silence and only the last part gets `pause`.
                self.assertEqual([EXPLICIT_SILENCE_MS, pause],
                                 [part["pause_after"] for part in parts if "pause_after" in part])

    def test_existing_saved_chunks_and_audio_are_not_regenerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            root = Path(tmp)
            (root / "annotated_script.json").write_text(json.dumps(get_script()))
            audio = root / "old.wav"
            sf.write(audio, np.full(2400, .1), 24000)
            saved = [{"id": 0, "uid": "existing", "speaker": "NARRATOR",
                      "text": "CHAPTER I. The first room was quiet.",
                      "status": "done", "audio_path": "old.wav"}]
            manager.save_chunks(saved)
            watched = [root / "annotated_script.json", root / "chunks.json", audio]
            before = {p: p.read_bytes() for p in watched}
            self.assertEqual(saved, manager.load_chunks())
            self.assertEqual(before, {p: p.read_bytes() for p in watched})

    def test_delivery_retry_preserves_unaffected_heading_audio(self):
        rows = get_script()
        rows[1]["instruct_unchecked"] = True
        chunks = [dict(chunk, id=i, uid=f"u{i}", status="done", audio_path=f"a{i}.wav")
                  for i, chunk in enumerate(group_into_chunks(rows))]
        after = copy.deepcopy(rows)
        after[1]["instruct"] = "calm"
        result = get_delivery_retry_chunks(rows, after, chunks)
        self.assertEqual(4, len(result))
        self.assertIsNone(result[1]["audio_path"])
        for index in (0, 2, 3):
            self.assertEqual(chunks[index], result[index])

    def test_native_preview_and_m4b_follow_the_grouped_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = ProjectManager(tmp)
            (root / "annotated_script.json").write_text(json.dumps(get_script()))
            chunks = manager.load_chunks()
            self.assertEqual(4, len(chunks))
            for index, chunk in enumerate(chunks):
                chunk.update(status="done", audio_path=f"line{index}.wav", pause_after=0)
                sf.write(root / chunk["audio_path"], np.full(2400, .1), 24000)
            manager.save_chunks(chunks)
            source_bytes = (root / "annotated_script.json").read_bytes()
            with patch.object(manager, "_load_pause_defaults", return_value=(0, 0)):
                preview = manager.preview_chapter_filenames(fmt="wav")
                self.assertEqual(["CHAPTER I.", "CHAPTER II."],
                                 [row["title"] for row in preview])
                ok, message = manager.merge_m4b(metadata={"title": "Synthetic chapters"})
            self.assertTrue(ok, message)
            probe = run_owned_capture(
                ["ffprobe", "-v", "error", "-show_chapters", "-show_streams",
                 "-of", "json", str(root / "audiobook.m4b")], timeout=20, text=True)
            probe.check_returncode()
            artifact = json.loads(probe.stdout)
            self.assertEqual(["aac"], [s["codec_name"] for s in artifact["streams"]
                                      if s["codec_type"] == "audio"])
            self.assertEqual(["CHAPTER I.", "CHAPTER II."],
                             [chapter["tags"]["title"] for chapter in artifact["chapters"]])
            self.assertEqual(2, len(artifact["chapters"]))
            for chapter, start, end in zip(artifact["chapters"], (0, .2), (.2, .4)):
                self.assertAlmostEqual(start, float(chapter["start_time"]), places=3)
                self.assertAlmostEqual(end, float(chapter["end_time"]), places=3)
            self.assertEqual(source_bytes, (root / "annotated_script.json").read_bytes())
