"""EPUB 3 normative conformance + source coverage (R1/R6 RED regressions).

These tests parse the emitted archive independently (OPF/SMIL/XHTML XML)
and assert W3C EPUB 3.3 normative mechanisms, never the builder's helpers:
- Manifest XHTML ``media-overlay`` *attribute* (not a ``properties`` token).
- SMIL clock durations (not ISO-8601 PT), per-overlay active-clip sum and
  one unrefined publication sum.
- Exactly one ``dcterms:modified`` UTC timestamp.
- Valid SMIL structure (default SMIL namespace, no stray attrs, ordered
  bounded clips including very short spans).
- EPUB 2 NCX -> generated EPUB 3 nav; guide removal; NCX preserved.
- Full body text coverage once in order (div direct text, mixed sections),
  repeated texts by structural identity, coarser/refusal for one chunk
  spanning segments, no-text cover spine skipped, regeneration safety.
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET
import unittest

from epub_readaloud import (build_epub_readaloud, get_xhtml_segments,
                            map_spine_to_chunks, validate_epub_readaloud)

OPF_NS = "{http://www.idpf.org/2007/opf}"
SMIL_NS = "{http://www.w3.org/ns/SMIL}"


def _container(opf_path):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            f'<rootfiles><rootfile full-path="{opf_path}" media-type="application/oebps-package+xml"/></rootfiles></container>')


def _opf3(*, extra_manifest="", extra_metadata="", spine='<itemref idref="c1"/><itemref idref="c2"/>'):
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:identifier id="id">urn:test:1</dc:identifier>'
            '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
            '<dc:language>en</dc:language>'
            '<meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>'
            + extra_metadata + '</metadata><manifest>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>'
            + extra_manifest + '</manifest><spine>' + spine + '</spine></package>')


def _nav3():
    return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="en" lang="en">'
            '<head><title>Contents</title></head><body>'
            '<nav epub:type="toc"><ol>'
            '<li><a href="ch1.xhtml">Chapter One</a></li>'
            '<li><a href="ch2.xhtml">Chapter Two</a></li>'
            '</ol></nav></body></html>')


def _ch1(body='<h1>Chapter One</h1><p>Hello synthetic world.</p><p>Second paragraph here.</p>'):
    return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">'
            '<head><title>Chapter One</title><script>var x = "do not narrate";</script>'
            '<style>p { color: red; }</style></head><body>' + body + '</body></html>')


def _ch2():
    return ('<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">'
            '<head><title>Chapter Two</title></head><body>'
            '<h1>Chapter Two</h1><p>Final synthetic paragraph.</p></body></html>')


def make_epub3(*, ch1_body=None, extra_manifest="", extra_metadata=""):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as a:
        a.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        a.writestr('META-INF/container.xml', _container('OEBPS/content.opf'))
        a.writestr('OEBPS/content.opf',
                   _opf3(extra_manifest=extra_manifest, extra_metadata=extra_metadata))
        a.writestr('OEBPS/nav.xhtml', _nav3())
        a.writestr('OEBPS/ch1.xhtml', _ch1(ch1_body) if ch1_body is not None else _ch1())
        a.writestr('OEBPS/ch2.xhtml', _ch2())
        a.writestr('OEBPS/cover.png', b'\x89PNG\r\n\x1a\nfake-png-bytes')
    return buf.getvalue()


def make_epub2(*, ch1_body=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as a:
        a.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        a.writestr('META-INF/container.xml', _container('OEBPS/content.opf'))
        a.writestr('OEBPS/content.opf',
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id" version="2.0">'
                   '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                   '<dc:identifier id="id">urn:test:1</dc:identifier>'
                   '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                   '<dc:language>en</dc:language></metadata><manifest>'
                   '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
                   '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>'
                   '<item id="cover" href="cover.png" media-type="image/png"/>'
                   '</manifest><spine toc="ncx"><itemref idref="c1"/><itemref idref="c2"/></spine>'
                   '<guide><reference type="cover" title="Cover" href="cover.png"/></guide></package>')
        a.writestr('OEBPS/toc.ncx',
                   '<?xml version="1.0" encoding="UTF-8"?>'
                   '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                   '<head><meta name="dtb:uid" content="urn:test:1"/></head>'
                   '<docTitle><text>Synthetic Title</text></docTitle>'
                   '<navMap>'
                   '<navPoint id="n1" playOrder="1"><navLabel><text>Chapter One</text></navLabel>'
                   '<content src="ch1.xhtml"/></navPoint>'
                   '<navPoint id="n2" playOrder="2"><navLabel><text>Chapter Two</text></navLabel>'
                   '<content src="ch2.xhtml"/></navPoint>'
                   '</navMap></ncx>')
        a.writestr('OEBPS/ch1.xhtml', _ch1(ch1_body) if ch1_body is not None else _ch1())
        a.writestr('OEBPS/ch2.xhtml', _ch2())
        a.writestr('OEBPS/cover.png', b'\x89PNG\r\n\x1a\nfake-png-bytes')
    return buf.getvalue()


def entries():
    return [
        {"index": 0, "prepared_text": "Chapter One", "start_frame": 0, "end_frame": 12000, "sample_rate": 24000},
        {"index": 1, "prepared_text": "Hello synthetic world.", "start_frame": 12240, "end_frame": 24000, "sample_rate": 24000},
        {"index": 2, "prepared_text": "Second paragraph here.", "start_frame": 24240, "end_frame": 36000, "sample_rate": 24000},
        {"index": 3, "prepared_text": "Chapter Two", "start_frame": 36240, "end_frame": 48000, "sample_rate": 24000},
        {"index": 4, "prepared_text": "Final synthetic paragraph.", "start_frame": 48240, "end_frame": 72000, "sample_rate": 24000},
    ]


def _parse_clock_to_ms(raw):
    m = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", raw.strip())
    assert m, raw
    hh, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
    frac = (m.group(4) or "0").ljust(3, "0")
    return ((hh * 3600 + mm * 60 + ss) * 1000 + int(frac))


def _clip_ms(smil_bytes, smil_path="OEBPS/ch1.smil"):
    root = ET.fromstring(smil_bytes)
    total = 0
    for audio in root.iter(SMIL_NS + "audio"):
        total += _parse_clock_to_ms(audio.get("clipEnd")) - _parse_clock_to_ms(audio.get("clipBegin"))
    return total


class R1ManifestAndDurationTests(unittest.TestCase):
    def test_media_overlay_attribute_not_property(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            opf = ET.fromstring(z.read("OEBPS/content.opf"))
            items = {it.get("href"): it for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")}
            by_id = {it.get("id"): it for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")}
            xhtml = items["ch1.xhtml"]
            self.assertEqual("application/xhtml+xml", xhtml.get("media-type"))
            smil_id = xhtml.get("media-overlay")
            self.assertTrue(smil_id, "EPUB3 requires media-overlay ATTRIBUTE naming the SMIL")
            self.assertIn(smil_id, by_id)
            self.assertEqual("application/smil+xml", by_id[smil_id].get("media-type"))
            self.assertNotIn("media-overlay", (xhtml.get("properties") or "").split(),
                             "media-overlay is not a properties vocabulary token")
            for ir in opf.findall(OPF_NS + "spine/" + OPF_NS + "itemref"):
                self.assertIsNone(ir.get("media-overlay"))

    def test_durations_smil_clock_active_sum_unrefined_publication(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            opf = ET.fromstring(z.read("OEBPS/content.opf"))
            meta = opf.find(OPF_NS + "metadata")
            durations = [m for m in meta.findall(OPF_NS + "meta")
                         if m.get("property") == "media:duration"]
            unrefined = [m for m in durations if not m.get("refines")]
            refined = [m for m in durations if m.get("refines")]
            self.assertEqual(1, len(unrefined), "exactly one unrefined publication duration")
            self.assertEqual(2, len(refined), "one refined duration per overlay")
            for m in durations:
                self.assertNotIn("PT", (m.text or "").strip(),
                                 "media:duration must be a SMIL clock value, not ISO-8601 PT")
            by_id = {it.get("id"): it for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")}
            href_of = {i: it.get("href") for i, it in by_id.items()}
            # Per-overlay: sum of (clipEnd-clipBegin) as emitted in the SMIL.
            total = 0
            for m in refined:
                ref = (m.get("refines") or "").lstrip("#")
                self.assertIn(ref, by_id)
                smil_href = href_of[ref]
                smil_rel = posix_join("OEBPS", smil_href)
                got = _clip_ms(z.read(smil_rel))
                declared = _parse_clock_to_ms(m.text or "")
                self.assertEqual(got, declared, f"overlay duration must equal active-clip sum for {ref}")
                total += declared
            self.assertEqual(total, _parse_clock_to_ms(unrefined[0].text or ""),
                             "publication duration must equal sum of overlay durations")
            # No audio-refined duration may exist.
            self.assertFalse(any((m.get("refines") or "").lstrip("#") not in
                                 [it.get("id") for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")
                                  if it.get("media-type") == "application/smil+xml"]
                                 for m in refined), "refined durations must name SMIL ids only")

    def test_dcterms_modified_exactly_one_and_epub2_gets_one(self):
        for source in (make_epub3(), make_epub2()):
            out, _ = build_epub_readaloud(retained_epub_bytes=source,
                                          audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                          script_entries=entries())
            with zipfile.ZipFile(io.BytesIO(out)) as z:
                opf = ET.fromstring(z.read("OEBPS/content.opf"))
                meta = opf.find(OPF_NS + "metadata")
                mods = [m for m in meta.findall(OPF_NS + "meta")
                        if m.get("property") == "dcterms:modified"]
                self.assertEqual(1, len(mods), "exactly one dcterms:modified")
                self.assertRegex((mods[0].text or "").strip(),
                                 r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_smil_structure_default_ns_no_stray_attrs(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            raw = z.read("OEBPS/ch1.smil").decode("utf-8")
            self.assertNotIn("ns0:", raw, "SMIL must use the default SMIL namespace")
            self.assertNotIn("xml:lang", raw, "SMIL root must not carry xml:lang")
            root = ET.fromstring(raw)
            self.assertEqual(SMIL_NS + "smil", root.tag)
            self.assertTrue(root.get("version"))
            text_srcs = [t.get("src") for t in root.iter(SMIL_NS + "text")]
            self.assertTrue(text_srcs)
            for src in text_srcs:
                self.assertTrue(src.startswith("ch1.xhtml#"), f"text src must name the specific XHTML: {src}")
            # Any <seq> present (if any) must carry epub:textref.
            for seq in root.iter(SMIL_NS + "seq"):
                self.assertTrue(seq.get("{http://www.idpf.org/2007/ops}textref"),
                                "EPUB 3.3 <seq> requires epub:textref")

    def test_very_short_clip_refused_not_padded(self):
        # R4 clock truth: a 1-frame span at 24 kHz (~0.04 ms) is below the
        # 1 ms SMIL quantum. Padding it to 1 ms would invent drifted
        # durations, so the build refuses rather than publishing success.
        full = entries()
        full[0] = dict(full[0], start_frame=0, end_frame=1)
        full[1] = dict(full[1], start_frame=2, end_frame=24000)
        with self.assertRaises(ValueError) as ctx:
            build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                 audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                 script_entries=full)
        self.assertIn("quantum", str(ctx.exception).lower())

    def test_validator_rejects_legacy_properties_mechanism(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        # Rewrite the output to the legacy mechanism: validator must refuse.
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(out)) as src, zipfile.ZipFile(buf, 'w') as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "OEBPS/content.opf":
                    text = data.decode("utf-8")
                    text = text.replace('media-overlay="aa2-smil-001"',
                                        'properties="media-overlay aa2-smil-001"')
                    text = text.replace('media-overlay="aa2-smil-002"',
                                        'properties="media-overlay aa2-smil-002"')
                    data = text.encode("utf-8")
                dst.writestr(info.filename, data)
        with self.assertRaises(ValueError):
            validate_epub_readaloud(buf.getvalue())

    def test_validator_rejects_pt_duration(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(out)) as src, zipfile.ZipFile(buf, 'w') as dst:
            for info in src.infolist():
                data = src.read(info.filename)
                if info.filename == "OEBPS/content.opf":
                    text = data.decode("utf-8")
                    text = re.sub(r"(<[^>]*property=\"media:duration\"[^>]*>)[^<]*(</[^>]*>)",
                                  r"\g<1>PT1S\g<2>", text, count=1)
                    data = text.encode("utf-8")
                dst.writestr(info.filename, data)
        with self.assertRaises(ValueError):
            validate_epub_readaloud(buf.getvalue())

    def test_epub2_nav_generated_guide_removed_ncx_kept(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub2(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            names = z.namelist()
            opf = ET.fromstring(z.read("OEBPS/content.opf"))
            manifest = opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")
            navs = [it for it in manifest if "nav" in (it.get("properties") or "").split()]
            self.assertEqual(1, len(navs), "EPUB 2 conversion must generate exactly one nav")
            nav_href = navs[0].get("href")
            nav_path = posix_join("OEBPS", nav_href)
            self.assertIn(nav_path, names)
            root = ET.fromstring(z.read(nav_path))
            OPS = "{http://www.idpf.org/2007/ops}"
            toc = root.find(".//{http://www.w3.org/1999/xhtml}nav[@epub:type='toc']") \
                if False else None
            links = []
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] == "nav" and (
                        el.get(OPS + "type") == "toc"):
                    for a in el.iter():
                        if a.tag.rsplit("}", 1)[-1] == "a":
                            links.append((a.get("href"),
                                          " ".join("".join(a.itertext()).split())))
            self.assertEqual([("ch1.xhtml", "Chapter One"), ("ch2.xhtml", "Chapter Two")],
                             links, "generated nav must preserve NCX labels/order/hrefs")
            self.assertIsNone(opf.find(OPF_NS + "guide"), "EPUB 2 guide must be removed for EPUB 3")
            self.assertIn("OEBPS/toc.ncx", names, "source NCX bytes must be preserved")
            self.assertEqual(
                ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                 "<ncx xmlns=\"http://www.daisy.org/z3986/2005/ncx/\" version=\"2005-1\">"
                 "<head><meta name=\"dtb:uid\" content=\"urn:test:1\"/></head>"
                 "<docTitle><text>Synthetic Title</text></docTitle>"
                 "<navMap>"
                 "<navPoint id=\"n1\" playOrder=\"1\"><navLabel><text>Chapter One</text></navLabel>"
                 "<content src=\"ch1.xhtml\"/></navPoint>"
                 "<navPoint id=\"n2\" playOrder=\"2\"><navLabel><text>Chapter Two</text></navLabel>"
                 "<content src=\"ch2.xhtml\"/></navPoint>"
                 "</navMap></ncx>"),
                z.read("OEBPS/toc.ncx").decode("utf-8"))
            validate_epub_readaloud(out)

    def test_epub3_nav_preserved(self):
        out, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            self.assertEqual(_nav3(), z.read("OEBPS/nav.xhtml").decode("utf-8"))

    def test_regeneration_with_existing_overlay_no_stale(self):
        once, _ = build_epub_readaloud(retained_epub_bytes=make_epub3(),
                                       audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                       script_entries=entries())
        twice, _ = build_epub_readaloud(retained_epub_bytes=once,
                                        audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                        script_entries=entries())
        validate_epub_readaloud(twice)
        with zipfile.ZipFile(io.BytesIO(twice)) as z:
            opf = ET.fromstring(z.read("OEBPS/content.opf"))
            ids = [it.get("id") for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")]
            self.assertEqual(len(ids), len(set(ids)), "manifest ids must stay unique")
            meta = opf.find(OPF_NS + "metadata")
            durs = [m for m in meta.findall(OPF_NS + "meta") if m.get("property") == "media:duration"]
            self.assertEqual(3, len(durs), f"no stale duplicate durations: {len(durs)}")

    def test_root_level_opf_audio_in_manifest(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as a:
            a.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            a.writestr('META-INF/container.xml', _container('content.opf'))
            a.writestr('content.opf', _opf3())
            a.writestr('nav.xhtml', _nav3())
            a.writestr('ch1.xhtml', _ch1())
            a.writestr('ch2.xhtml', _ch2())
            a.writestr('cover.png', b'\x89PNG\r\n\x1a\nfake-png-bytes')
        out, _ = build_epub_readaloud(retained_epub_bytes=buf.getvalue(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        validate_epub_readaloud(out)
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            opf = ET.fromstring(z.read("content.opf"))
            medias = {it.get("media-type") for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")}
            self.assertIn("audio/mpeg", medias)
            self.assertIn("audio/audiobook.mp3", z.namelist())


class R6CoverageTests(unittest.TestCase):
    def test_div_direct_text_segment(self):
        src = make_epub3(ch1_body='<div>Text here</div><p>More text follows.</p>')
        _root, segs, _full = get_xhtml_segments(src, "OEBPS/ch1.xhtml")
        texts = [s["text"] for s in segs]
        self.assertIn("Text here", texts, f"div direct text must not vanish: {texts}")
        self.assertEqual("Text here", texts[0])

    def test_mixed_section_lead_tail_once(self):
        src = make_epub3(ch1_body='<section>lead <em>inline</em><p>paragraph</p>tail</section>')
        # ch2 still needs coverage at build time; segment check is standalone.
        _root, segs, _full = get_xhtml_segments(src, "OEBPS/ch1.xhtml")
        joined = " ".join(s["text"] for s in segs)
        self.assertEqual("lead inline paragraph tail", joined,
                         "mixed direct + block text must be counted once in order")
        self.assertEqual(1, joined.count("paragraph"))
        self.assertEqual(1, joined.count("lead"))

    def test_repeated_paragraphs_structural_identity(self):
        src = make_epub3(ch1_body='<p id="a">Same repeated words.</p><p id="b">Same repeated words.</p>')
        _root, segs, _full = get_xhtml_segments(src, "OEBPS/ch1.xhtml")
        self.assertEqual(["a", "b"], [s["fragment"] for s in segs])
        two = [
            {"index": 0, "prepared_text": "Same repeated words.", "start_frame": 0, "end_frame": 1000, "sample_rate": 24000},
            {"index": 1, "prepared_text": "Same repeated words.", "start_frame": 2000, "end_frame": 3000, "sample_rate": 24000},
        ]
        mapping, end = map_spine_to_chunks(segs, two, start=0)
        self.assertEqual("a", mapping[0]["fragment"])
        self.assertEqual("b", mapping[1]["fragment"])
        self.assertEqual([0], mapping[0]["chunk_indexes"])
        self.assertEqual([1], mapping[1]["chunk_indexes"])
        self.assertEqual(2, end)
        # Build must not overwrite the first element's id with the second's.
        full = entries()
        # Adapt script to this ch1 body: replace first three entries.
        full[0] = {"index": 0, "prepared_text": "Same repeated words.", "start_frame": 0, "end_frame": 12000, "sample_rate": 24000}
        full[1] = {"index": 1, "prepared_text": "Same repeated words.", "start_frame": 12240, "end_frame": 24000, "sample_rate": 24000}
        full[2] = {"index": 2, "prepared_text": "Chapter Two", "start_frame": 36240, "end_frame": 48000, "sample_rate": 24000}
        full[3] = {"index": 3, "prepared_text": "Final synthetic paragraph.", "start_frame": 48240, "end_frame": 72000, "sample_rate": 24000}
        full = full[:4]
        for i, row in enumerate(full):
            row["index"] = i
        out, _ = build_epub_readaloud(retained_epub_bytes=src,
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=full)
        validate_epub_readaloud(out)
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            ch1 = z.read("OEBPS/ch1.xhtml").decode("utf-8")
            self.assertEqual(1, ch1.count('id="a"'), ch1)
            self.assertEqual(1, ch1.count('id="b"'), ch1)

    def test_one_chunk_spanning_two_segments_explicitly_refused(self):
        spine = [{"fragment": None, "text": "First paragraph here."},
                 {"fragment": None, "text": "Second paragraph here."}]
        entries1 = [
            {"index": 0, "prepared_text": "First paragraph here. Second paragraph here.",
             "start_frame": 0, "end_frame": 2000, "sample_rate": 24000},
        ]
        with self.assertRaises(ValueError) as cm:
            map_spine_to_chunks(spine, entries1, start=0)
        self.assertIn("spans", str(cm.exception).lower(),
                      f"refusal must name the unrepresentable mapping, got: {cm.exception}")

    def test_many_chunks_one_segment_and_normalization(self):
        spine = [{"fragment": None, "text": "Hello, WORLD!"}]
        split = [
            {"index": 0, "prepared_text": "hello", "start_frame": 0, "end_frame": 1000, "sample_rate": 24000},
            {"index": 1, "prepared_text": "world", "start_frame": 1000, "end_frame": 2000, "sample_rate": 24000},
        ]
        mapping, end = map_spine_to_chunks(spine, split, start=0)
        self.assertEqual(2, len(mapping[0]["chunk_indexes"]),
                         "case/punctuation-insensitive exact coverage must still map split chunks")

    def test_no_text_cover_spine_skipped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as a:
            a.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            a.writestr('META-INF/container.xml', _container('OEBPS/content.opf'))
            a.writestr('OEBPS/content.opf',
                       _opf3(spine='<itemref idref="coverdoc"/><itemref idref="c1"/><itemref idref="c2"/>',
                             extra_manifest='<item id="coverdoc" href="coverdoc.xhtml" media-type="application/xhtml+xml"/>'))
            a.writestr('OEBPS/nav.xhtml', _nav3())
            a.writestr('OEBPS/coverdoc.xhtml',
                       '<?xml version="1.0" encoding="utf-8"?><!DOCTYPE html>'
                       '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">'
                       '<head><title>Cover</title></head><body><img src="cover.png" alt=""/></body></html>')
            a.writestr('OEBPS/ch1.xhtml', _ch1())
            a.writestr('OEBPS/ch2.xhtml', _ch2())
            a.writestr('OEBPS/cover.png', b'\x89PNG\r\n\x1a\nfake-png-bytes')
        out, _ = build_epub_readaloud(retained_epub_bytes=buf.getvalue(),
                                      audio_bytes=b"\xff\xfbx", audio_filename="audiobook.mp3",
                                      script_entries=entries())
        validate_epub_readaloud(out)
        with zipfile.ZipFile(io.BytesIO(out)) as z:
            opf = ET.fromstring(z.read("OEBPS/content.opf"))
            cover = next(it for it in opf.findall(OPF_NS + "manifest/" + OPF_NS + "item")
                         if it.get("id") == "coverdoc")
            self.assertIsNone(cover.get("media-overlay"),
                              "image-only cover spine must not require narration")
            self.assertNotIn("OEBPS/coverdoc.smil", z.namelist())


def posix_join(base, href):
    import posixpath
    return posixpath.normpath(posixpath.join(base, href))


if __name__ == "__main__":
    unittest.main()
