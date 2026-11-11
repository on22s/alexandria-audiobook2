"""Read-aloud vs. script extraction: ruby, inserted TOC titles, split files.

Synthetic EPUBs only. The script for each case is made the way AA2 makes
it: routers.script.extract_epub_text, one entry per paragraph (dialogue
split from narration), project.group_into_chunks, speech preparation.
Qwen mode uses one simulated aligner token per spoken word, or per
character for Han/kana: these tests check mapping and SMIL timing, not the
model. Each case here refused on main at c675b04f (ruby was read twice).
"""
import io
import re
import unittest
import xml.etree.ElementTree as ET
import zipfile

import project
from epub_readaloud import build_epub_readaloud, get_spoken_text, validate_epub_readaloud
from routers.script import extract_epub_text
from ruby_text import get_ruby_spoken_text

SMIL = "{http://www.w3.org/ns/SMIL}"
QUOTE = re.compile(r"(“[^”]*”|「[^」]*」)")
HAN_OR_KANA = re.compile(r"[぀-ヿ㐀-鿿]")


def _epub(bodies, toc_label="Chapter One", lang="en"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml",
                         '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:'
                         'opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/'
                         'content.opf"/></rootfiles></container>')
        items = "".join(f'<item id="c{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
                        for i in range(len(bodies)))
        spine = "".join(f'<itemref idref="c{i}"/>' for i in range(len(bodies)))
        archive.writestr("OEBPS/content.opf",
                         '<?xml version="1.0" encoding="UTF-8"?><package xmlns="http://www.idpf.org/'
                         '2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://'
                         'purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:test:ra</dc:identifier>'
                         f'<dc:title>Synthetic</dc:title><dc:language>{lang}</dc:language></metadata>'
                         '<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
                         f'properties="nav"/>{items}</manifest><spine>{spine}</spine></package>')
        archive.writestr("OEBPS/nav.xhtml",
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub='
                         '"http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body><nav '
                         f'epub:type="toc"><ol><li><a href="ch0.xhtml">{toc_label}</a></li></ol></nav>'
                         '</body></html>')
        for i, body in enumerate(bodies):
            archive.writestr(f"OEBPS/ch{i}.xhtml",
                             '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head>'
                             f'<title>c</title></head><body>{body}</body></html>')
    return buffer.getvalue()


def _script(epub):
    text = extract_epub_text("synthetic.epub", archive_bytes=epub)
    text = text if isinstance(text, str) else text[0]
    entries = []
    for paragraph in (line.strip() for line in text.splitlines()):
        for piece in QUOTE.split(paragraph):
            if piece.strip():
                speaker = "ALICE" if QUOTE.fullmatch(piece.strip()) else "NARRATOR"
                entries.append({"speaker": speaker, "text": piece.strip(), "instruct": ""})
    return text, project.group_into_chunks(entries)


def _tokens(prepared, seconds=0.25):
    words = []
    for word in re.findall(r"[\w']+", prepared.replace("’", "")):
        words.extend(word if HAN_OR_KANA.search(word) else [word])
    return [{"text": w, "start_time": i * seconds, "end_time": (i + 1) * seconds}
            for i, w in enumerate(words)]


def _build(epub, *, qwen):
    _text, chunks = _script(epub)
    entries, aligned, frame, rate = [], [], 0, 24000
    for index, chunk in enumerate(chunks):
        prepared = get_spoken_text(chunk["text"])
        tokens = _tokens(prepared)
        length = int((len(tokens) * 0.25 + 0.05) * rate)
        entries.append({"index": index, "prepared_text": prepared, "start_frame": frame,
                        "end_frame": frame + length, "sample_rate": rate})
        aligned.append({"tokens": tokens})
        frame += length + rate // 10
    result, _report = build_epub_readaloud(
        retained_epub_bytes=epub, audio_bytes=b"\xff\xfbmp3", audio_filename="audiobook.mp3",
        script_entries=entries, aligned_rows=aligned if qwen else None)
    validate_epub_readaloud(result)
    return result


def _clips(result, name):
    with zipfile.ZipFile(io.BytesIO(result)) as archive:
        smil = ET.fromstring(archive.read(f"OEBPS/{name}.smil"))
    return [(par.find(SMIL + "text").get("src").split("#")[1],
             par.find(SMIL + "audio").get("clipBegin"), par.find(SMIL + "audio").get("clipEnd"))
            for par in smil.iter(SMIL + "par")]


class RubyTests(unittest.TestCase):
    """A kana reading is spoken in place of its base, once, on both sides."""

    def test_spoken_form_rule(self):
        self.assertEqual("かんじ", get_ruby_spoken_text([("漢字", "かんじ")]))
        self.assertEqual("マジ", get_ruby_spoken_text([("本気", "マジ")]))
        self.assertEqual("かんじ", get_ruby_spoken_text([("漢", "かん"), ("字", "じ")]))
        # A non-kana reading (pinyin, a gloss) is dropped; the base is kept.
        self.assertEqual("中国", get_ruby_spoken_text([("中国", "zhōngguó")]))
        self.assertEqual("後", get_ruby_spoken_text([("後", None)]))

    def test_extraction_reads_the_kana_once(self):
        body = '<h1>第一章</h1><p><ruby><rb>本気</rb><rp>(</rp><rt>マジ</rt><rp>)</rp></ruby>だ。</p>'
        text, _chunks = _script(_epub([body], toc_label="第一章", lang="ja"))
        self.assertIn("マジだ。", text)
        self.assertNotIn("本気", text)
        self.assertNotIn("(", text)

    def test_read_aloud_builds_in_both_modes(self):
        body = ('<h1>第一章</h1><p><ruby>漢字<rt>かんじ</rt></ruby>を読んだ。それから寝た。</p>'
                '<p>朝が来た。</p>')
        epub = _epub([body], toc_label="第一章", lang="ja")
        _build(epub, qwen=False)
        clips = _clips(_build(epub, qwen=True), "ch0")
        # Heading, the ruby paragraph as one clip (its spoken text differs
        # from its characters), and the plain paragraph.
        self.assertEqual(3, len(clips), clips)


class InsertedTocTitleTests(unittest.TestCase):
    """Extraction speaks a TOC label the chapter body does not contain."""

    def test_title_is_spoken_but_not_highlighted(self):
        epub = _epub(["<p>The hall was quiet.</p><p>Nobody moved.</p>"])
        text, chunks = _script(epub)
        self.assertTrue(text.startswith("Chapter One"), text)  # the inserted label
        for qwen in (False, True):
            clips = _clips(_build(epub, qwen=qwen), "ch0")
            # No clip for the title: the first clip starts after its audio.
            self.assertNotEqual("00:00:00.000", clips[0][1], (qwen, clips))
        self.assertGreaterEqual(len(chunks), 2)

    def test_heading_in_the_text_still_gets_its_clip(self):
        epub = _epub(["<h1>Chapter One</h1><p>The hall was quiet.</p>"])
        clips = _clips(_build(epub, qwen=False), "ch0")
        self.assertEqual("00:00:00.000", clips[0][1], clips)
        self.assertEqual(2, len(clips), clips)


class ChapterSplitAcrossFilesTests(unittest.TestCase):
    """Calibre-style split: narration merges into one chunk across files."""

    EPUB = _epub(["<h1>Chapter One</h1><p>The hall was quiet.</p><p>Nobody moved.</p>",
                  "<p>Then the bell rang.</p><p>Everyone stood up.</p>"])

    def test_one_chunk_really_crosses_the_files(self):
        _text, chunks = _script(self.EPUB)
        self.assertTrue(any("Nobody moved. Then the bell rang." in c["text"] for c in chunks),
                        [c["text"] for c in chunks])

    def test_qwen_times_each_file_from_its_own_words(self):
        result = _build(self.EPUB, qwen=True)
        first, second = _clips(result, "ch0"), _clips(result, "ch1")
        self.assertEqual(3, len(first), first)
        self.assertEqual(2, len(second), second)
        # The second file starts where the first file's words end: no gap
        # invented, no overlap.
        self.assertLessEqual(first[-1][2], second[0][1], (first, second))

    def test_chunk_timed_mode_refuses_with_the_reason(self):
        with self.assertRaisesRegex(ValueError, "continues from one EPUB file into the next"):
            _build(self.EPUB, qwen=False)

    def test_next_file_with_different_words_refuses(self):
        epub = _epub(["<h1>Chapter One</h1><p>The hall was quiet.</p>",
                      "<p>Then the bell rang.</p>"])
        _text, chunks = _script(epub)
        tampered = _epub(["<h1>Chapter One</h1><p>The hall was quiet.</p>",
                          "<p>Then the gong rang.</p>"])
        entries = []
        for index, chunk in enumerate(chunks):
            prepared = get_spoken_text(chunk["text"])
            entries.append({"index": index, "prepared_text": prepared, "start_frame": index * 24000,
                            "end_frame": (index + 1) * 24000, "sample_rate": 24000})
        aligned = [{"tokens": _tokens(entry["prepared_text"], seconds=0.1)} for entry in entries]
        with self.assertRaises(ValueError):
            build_epub_readaloud(retained_epub_bytes=tampered, audio_bytes=b"\xff\xfbmp3",
                                 audio_filename="audiobook.mp3", script_entries=entries,
                                 aligned_rows=aligned)


if __name__ == "__main__":
    unittest.main()
