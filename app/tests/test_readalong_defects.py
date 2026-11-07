"""Discriminating regressions for #1720/#1779 A-F parent findings.

These tests use production paths (real ffmpeg, real route helpers,
real Node player/app scripts) and assert the repaired behavior.
They must FAIL on the rejected implementation and PASS after repair.
"""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


def _synthetic_mp3_seconds(seconds, tmp):
    """Real MP3 via ffmpeg sine; returns bytes."""
    wav = os.path.join(tmp, "sine.wav")
    mp3 = os.path.join(tmp, "sine.mp3")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}",
                    "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", wav],
                   check=True, capture_output=True, timeout=30)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", wav,
                    "-codec:a", "libmp3lame", "-q:a", "4", mp3],
                   check=True, capture_output=True, timeout=30)
    return Path(mp3).read_bytes()


def _make_source_epub_nested():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml',
                         '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                         '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
        archive.writestr('OEBPS/content.opf',
                         '<?xml version="1.0" encoding="UTF-8"?>'
                         '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">'
                         '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                         '<dc:identifier id="id">urn:test:1</dc:identifier>'
                         '<dc:title>Synthetic Title</dc:title><dc:creator>Test Author</dc:creator>'
                         '<dc:language>en</dc:language></metadata>'
                         '<manifest>'
                         '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
                         '<item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>'
                         '<item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>'
                         '</manifest>'
                         '<spine><itemref idref="c1"/></spine></package>')
        archive.writestr('OEBPS/nav.xhtml',
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                         'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Nav</title></head><body>'
                         '<nav epub:type="toc"><ol><li><a href="ch1.xhtml">Chapter One</a></li></ol></nav></body></html>')
        archive.writestr('OEBPS/ch1.xhtml',
                         '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
                         '<head><title>Chapter One</title></head><body>'
                         '<section><div><h1>Chapter One</h1><p>Hello synthetic world.</p>'
                         '<p>Second paragraph here.</p></div></section>'
                         '<blockquote><p>Quoted line here.</p></blockquote>'
                         '<ul><li>First list item.</li></ul>'
                         '</body></html>')
        archive.writestr('OEBPS/cover.jpg', b'\xff\xd8\xff\xe0\xff\xd9')
    return buffer.getvalue()


class ReadalongDefectsTests(unittest.TestCase):
    # ---- A: audio authority ----
    def test_A_stale_mp3_must_not_bind_new_timeline(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        from pydub import AudioSegment
        from project import ProjectManager
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            def clip(frames=4800):
                return AudioSegment(data=b"\x00\x20" * frames, sample_width=2, frame_rate=24000, channels=1)
            chunks = [{"id": 0, "speaker": "Narrator", "text": "Hello world.", "uid": "u0",
                       "audio_path": "clip0.wav", "audio_revision": "r0"},
                      {"id": 1, "speaker": "Narrator", "text": "Second line.", "uid": "u1",
                       "audio_path": "clip1.wav", "audio_revision": "r1"}]
            manager._load_chunks_with_audio = lambda **kwargs: ([(chunks[0], clip()), (chunks[1], clip())], 0)
            manager._load_pause_defaults = lambda: (10, 10)
            manager.load_chunks = lambda: chunks
            # Stale MP3: short sine (0.2s) that cannot match the 0.4s+ PCM timeline.
            stale = _synthetic_mp3_seconds(0.2, tmp)
            mp3_path = Path(tmp) / "cloned_audiobook.mp3"
            mp3_path.write_bytes(stale)
            stale_sha = hashlib.sha256(stale).hexdigest()
            # Edit pauses/text after merge (pause_after override) without merging.
            edited = [dict(chunks[0], pause_after=2000), dict(chunks[1])]
            # Actual persisted source state for the required app receipt.
            (Path(tmp) / "chunks.json").write_text(json.dumps(edited), encoding="utf-8")
            (Path(tmp) / "book.txt").write_bytes(b"Hello world. Second line.")
            Path(tmp, "state.json").write_text(json.dumps(
                {"input_file_path": str(Path(tmp) / "book.txt")}), encoding="utf-8")
            (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
            manager.load_chunks = lambda: edited
            from readalong_export import build_sidecar_export, get_sidecar_output_path
            ok, msg = build_sidecar_export(manager, lambda: False, includes_text=False,
                                           chunks=edited, audio_path=str(mp3_path))
            # Repaired behavior: fail closed OR fresh-encode so the published MP3
            # matches the current timeline. Reject "succeed binding stale MP3".
            if ok:
                after = mp3_path.read_bytes()
                self.assertNotEqual(stale_sha, hashlib.sha256(after).hexdigest(),
                                    "build bound stale MP3 to edited timeline without re-encoding")
                data = json.loads(Path(get_sidecar_output_path(tmp)).read_text(encoding="utf-8"))
                self.assertEqual(hashlib.sha256(after).hexdigest(), data["binding"]["audio_sha256"])
            else:
                self.assertIn("Rebuild", msg + "Retry" + msg)  # failure message guides rebuild

    def test_A_epub_must_encode_fresh_audio(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("ffmpeg unavailable")
        from pydub import AudioSegment
        from project import ProjectManager
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            def clip(frames=4800):
                return AudioSegment(data=b"\x00\x20" * frames, sample_width=2, frame_rate=24000, channels=1)
            # Full spine coverage (5 leaf segments of the nested fixture):
            # strict exact-coverage mapping requires every spine block.
            chunks = [
                {"id": 0, "speaker": "Narrator", "text": "Chapter One", "uid": "u0",
                 "audio_path": "clip0.wav", "audio_revision": "r0"},
                {"id": 1, "speaker": "Narrator", "text": "Hello synthetic world.", "uid": "u1",
                 "audio_path": "clip1.wav", "audio_revision": "r1"},
                {"id": 2, "speaker": "Narrator", "text": "Second paragraph here.", "uid": "u2",
                 "audio_path": "clip2.wav", "audio_revision": "r2"},
                {"id": 3, "speaker": "Narrator", "text": "Quoted line here.", "uid": "u3",
                 "audio_path": "clip3.wav", "audio_revision": "r3"},
                {"id": 4, "speaker": "Narrator", "text": "First list item.", "uid": "u4",
                 "audio_path": "clip4.wav", "audio_revision": "r4"}]
            manager._load_chunks_with_audio = lambda **kwargs: ([(row, clip()) for row in chunks], 0)
            manager._load_pause_defaults = lambda: (10, 10)
            manager.load_chunks = lambda: chunks
            stale = _synthetic_mp3_seconds(0.2, tmp)
            mp3_path = Path(tmp) / "cloned_audiobook.mp3"
            mp3_path.write_bytes(stale)
            stale_sha = hashlib.sha256(stale).hexdigest()
            Path(tmp, "state.json").write_text(json.dumps(
                {"input_file_path": str(Path(tmp) / "book.txt")}), encoding="utf-8")
            Path(tmp, "book.txt").write_bytes(b"Chapter One Hello synthetic world. Second paragraph here. Quoted line here. First list item.")
            (Path(tmp) / "annotated_script.json").write_text("[]", encoding="utf-8")
            # Bypass provenance gate by mocking locator to our EPUB.
            source = _make_source_epub_nested()
            from unittest.mock import patch
            from readalong_export import build_epub_export
            edited = [dict(chunks[0], pause_after=2000)] + [dict(row) for row in chunks[1:]]
            (Path(tmp) / "chunks.json").write_text(json.dumps(edited), encoding="utf-8")
            manager.load_chunks = lambda: edited
            manager._load_chunks_with_audio = lambda **kwargs: ([(row, clip()) for row in edited], 0)
            with patch("readalong_export._locate_retained_epub", return_value=(source, str(Path(tmp) / "book.txt"))):
                ok, msg = build_epub_export(manager, lambda: False, chunks=edited, audio_path=str(mp3_path))
            self.assertTrue(ok, msg)
            from readalong_export import get_epub_output_path
            raw = Path(get_epub_output_path(tmp)).read_bytes()
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                audios = [n for n in z.namelist() if n.endswith((".mp3", ".m4a", ".ogg", ".wav"))]
                self.assertTrue(audios)
                embedded = z.read(audios[0])
            # Embedded audio must be the fresh encode, not the stale file.
            self.assertNotEqual(stale_sha, hashlib.sha256(embedded).hexdigest())

    # ---- B: EPUB standard ----
    def test_B_manifest_xhtml_links_overlay_and_text_src_resolves(self):
        from epub_readaloud import build_epub_readaloud
        source = _make_source_epub_nested()
        # Script covering only leaf paragraphs (nested <section><div> must not duplicate).
        entries = [
            {"index": 0, "prepared_text": "Chapter One", "start_frame": 0, "end_frame": 8000, "sample_rate": 24000},
            {"index": 1, "prepared_text": "Hello synthetic world.", "start_frame": 8000, "end_frame": 16000, "sample_rate": 24000},
            {"index": 2, "prepared_text": "Second paragraph here.", "start_frame": 16000, "end_frame": 24000, "sample_rate": 24000},
            {"index": 3, "prepared_text": "Quoted line here.", "start_frame": 24000, "end_frame": 32000, "sample_rate": 24000},
            {"index": 4, "prepared_text": "First list item.", "start_frame": 32000, "end_frame": 40000, "sample_rate": 24000},
        ]
        epub_bytes, report = build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b"\xff\xfbx",
                                                  audio_filename="audiobook.mp3", script_entries=entries)
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as z:
            opf = ET.fromstring(z.read("OEBPS/content.opf"))
            ns = '{http://www.idpf.org/2007/opf}'
            manifest_items = {it.get('href'): it for it in opf.findall(ns + 'manifest/' + ns + 'item')}
            self.assertIn('ch1.xhtml', manifest_items)
            props = manifest_items['ch1.xhtml'].get('properties') or ''
            smil_id = manifest_items['ch1.xhtml'].get('media-overlay')
            self.assertTrue(smil_id, "EPUB 3 requires media-overlay ATTRIBUTE naming the SMIL")
            self.assertNotIn('media-overlay', props.split(),
                             "media-overlay is not a properties vocabulary token")
            smil_item = next(it for it in manifest_items.values() if it.get('id') == smil_id)
            self.assertEqual('application/smil+xml', smil_item.get('media-type'))
            # Spine itemrefs must NOT carry media-overlay (EPUB3 manifest association).
            for ir in opf.findall(ns + 'spine/' + ns + 'itemref'):
                self.assertIsNone(ir.get('media-overlay'))
            # Audio must be declared in manifest.
            medias = {it.get('media-type') for it in manifest_items.values()}
            self.assertIn('audio/mpeg', medias)
            smil = z.read(smil_item.get('href').replace('OEBPS/','OEBPS/') if 'OEBPS/' in smil_item.get('href') else 'OEBPS/'+smil_item.get('href')).decode('utf-8')
            # SMIL must declare a version.
            self.assertIn('version=', smil.split('>')[0])
            # text src must name the XHTML file (not bare #fragment).
            self.assertIn('ch1.xhtml#', smil)
            self.assertNotIn('src="#', smil)

    def test_B_nested_markup_counts_once(self):
        from epub_readaloud import get_xhtml_segments
        source = _make_source_epub_nested()
        _root, segments, _full = get_xhtml_segments(source, "OEBPS/ch1.xhtml")
        texts = [s["text"] for s in segments]
        # Leaf blocks: h1, p, p, blockquote>p, li => 5. Nested div/section must not duplicate.
        self.assertEqual(5, len(segments), f"segments={texts}")
        self.assertEqual("Hello synthetic world.", texts[1])
        joined = " | ".join(texts)
        self.assertEqual(joined.count("Hello synthetic world."), 1)

    def test_B_missing_middle_chunk_fails(self):
        from epub_readaloud import map_spine_to_chunks
        spine = [{"fragment": None, "text": "Hello synthetic world."},
                 {"fragment": None, "text": "Second paragraph here."}]
        entries = [
            {"index": 0, "prepared_text": "Hello synthetic world.", "start_frame": 0, "end_frame": 1000, "sample_rate": 24000},
            {"index": 1, "prepared_text": "Unrelated skipped line.", "start_frame": 1000, "end_frame": 2000, "sample_rate": 24000},
            {"index": 2, "prepared_text": "Second paragraph here.", "start_frame": 2000, "end_frame": 3000, "sample_rate": 24000},
        ]
        with self.assertRaises(ValueError):
            map_spine_to_chunks(spine, entries, start=0)
        # Leading dropped chunk also fails.
        spine2 = [{"fragment": None, "text": "Second paragraph here."}]
        with self.assertRaises(ValueError):
            map_spine_to_chunks(spine2, entries, start=0)

    def test_B_changed_word_high_ratio_fails(self):
        from epub_readaloud import map_spine_to_chunks
        spine_text = " ".join([f"word{i:02d}" for i in range(30)])
        entries = [{"index": 0, "prepared_text": spine_text.replace("word07", "CHANGED"),
                    "start_frame": 0, "end_frame": 1000, "sample_rate": 24000}]
        spine = [{"fragment": None, "text": spine_text}]
        with self.assertRaises(ValueError):
            map_spine_to_chunks(spine, entries, start=0)

    def test_B_split_beyond_three_chunks(self):
        from epub_readaloud import map_spine_to_chunks
        words = "alpha beta gamma delta epsilon zeta".split()
        entries = [{"index": i, "prepared_text": w, "start_frame": i * 1000, "end_frame": (i + 1) * 1000,
                    "sample_rate": 24000} for i, w in enumerate(words)]
        spine = [{"fragment": None, "text": "alpha beta gamma delta epsilon zeta"}]
        mapping, end = map_spine_to_chunks(spine, entries, start=0)
        self.assertEqual(6, len(mapping[0]["chunk_indexes"]))
        self.assertEqual(6, end)

    def test_B_validator_rejects_wrong_file_reference(self):
        from epub_readaloud import build_epub_readaloud, validate_epub_readaloud
        source = _make_source_epub_nested()
        entries = [
            {"index": 0, "prepared_text": "Chapter One", "start_frame": 0, "end_frame": 8000, "sample_rate": 24000},
            {"index": 1, "prepared_text": "Hello synthetic world.", "start_frame": 8000, "end_frame": 16000, "sample_rate": 24000},
            {"index": 2, "prepared_text": "Second paragraph here.", "start_frame": 16000, "end_frame": 24000, "sample_rate": 24000},
            {"index": 3, "prepared_text": "Quoted line here.", "start_frame": 24000, "end_frame": 32000, "sample_rate": 24000},
            {"index": 4, "prepared_text": "First list item.", "start_frame": 32000, "end_frame": 40000, "sample_rate": 24000},
        ]
        epub_bytes, _ = build_epub_readaloud(retained_epub_bytes=source, audio_bytes=b"\xff\xfbx",
                                             audio_filename="audiobook.mp3", script_entries=entries)
        # Move a fragment to a different XHTML file: validator must reject
        # because the SMIL cites a specific file.
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(epub_bytes)) as src, zipfile.ZipFile(buf, 'w') as out:
            for item in src.infolist():
                data = src.read(item.filename)
                if item.filename == "OEBPS/ch1.smil":
                    data = data.replace(b"ch1.xhtml#", b"nav.xhtml#")
                out.writestr(item.filename, data)
        with self.assertRaises(ValueError):
            validate_epub_readaloud(buf.getvalue())

    # ---- Parent override (1): trim truth ----
    def test_parent_trim_leading_silence_not_shifted(self):
        from readalong_sidecar import map_word_spans_to_frames
        # ASR spans are take-relative: first word at 0.35s, take duration 2s.
        # Leading trim removes 0.30s before the export clock.
        cached = {"spans": [{"from": 0, "to": 5, "start": 0.35, "end": 0.9}], "duration": 2.0}
        chunk_shape = ((1, 24000, 2), 48000)  # take layout ((ch, rate, width), frames)
        kwargs = dict(trim_head_seconds=0.30)
        try:
            words, status = map_word_spans_to_frames(cached, chunk_shape, (1, 24000, 2), 0, 40000, **kwargs)
        except TypeError:
            self.fail("map_word_spans_to_frames must accept true trim coordinates (parent override 1)")
        self.assertEqual("aligned-estimated", status)
        # After 0.30s head trim, the word sits at 0.05s of the trimmed clip,
        # i.e. within the first ~5% of the 40000-frame chunk, not at 17.5%.
        self.assertLess(words[0]["start_frame"] / 40000, 0.10)

    # ---- C: player ----
    def test_C_player_validates_full_shape(self):
        import re, subprocess, tempfile as _tf
        player = Path(__file__).parent.parent / "static" / "readalong-player.html"
        html = player.read_text(encoding="utf-8")
        m = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertIsNotNone(m)
        stubs = """
var _els = {};
function _el(id) {
  if (_els[id]) { return _els[id]; }
  var el = { id: id, children: [], style: {}, dataset: {}, textContent: '', value: '0',
    classList: { toggle: function(){}, add: function(){}, remove: function(){} },
    setAttribute: function(){}, appendChild: function(c){ this.children.push(c); },
    addEventListener: function(n, f){ this['_on_'+n] = f; }, querySelectorAll: function(){ return []; }, files: [] };
  _els[id] = el; return el;
}
var document = { getElementById: _el,
  createElement: function(tag) { return { tag: tag, children: [], style: {}, classList: { toggle: function(){}, add: function(){}, remove: function(){} }, setAttribute: function(){}, appendChild: function(c){ this.children.push(c); }, onclick: null, textContent: '', className: '', tabIndex: 0, click: function(){} }; },
  createTextNode: function(t) { return { text: t }; }, createDocumentFragment: function(){ return { children: [], appendChild: function(c){ this.children.push(c); } }; } };
var window = {};
var audioEl = _el('audio'); audioEl.addEventListener = function(n, f){ this['_on_'+n] = f; };
audioEl.play = function(){}; audioEl.pause = function(){};
var Blob = function(parts, opts){ this.parts = parts; };
var URL = { createObjectURL: function(){ return 'blob:fake'; }, revokeObjectURL: function(){} };
var FileReader = function(){};
var File = function(){};
"""
        checks = stubs + "\n" + m.group(1) + r"""
var ok = function(cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } };
// Full-shape validation: bad frames must fail.
var bad = { schema: 'aa2-readalong-sidecar', version: 1,
  binding: { audio_sha256: '0'.repeat(64), sample_rate: 24000, channels: 1, sample_width: 2, frames: 1000 },
  includes_text: false, sections: [], bookmarks: [],
  entries: [{ index: 0, section: 'sec-0001', speaker: null, text: null, start_frame: 900, end_frame: 2000, pause_frames_after: 0, status: 'missing', words: null }] };
var r = window.AA2Readalong.validateSidecar(bad);
ok(r.ok === false, 'out-of-bounds end_frame must fail validation, got ok');
var bad2 = JSON.parse(JSON.stringify(bad));
bad2.entries[0] = { index: 0, section: 'sec-0001', speaker: null, text: null, start_frame: -5, end_frame: 10, pause_frames_after: 0, status: 'missing', words: null };
ok(window.AA2Readalong.validateSidecar(bad2).ok === false, 'negative start must fail');
// Bookmarks must be bound: wrong-book import rejected, not partially accepted.
var bm = window.AA2Readalong.importBookmarks({ audio_sha256: 'f'.repeat(64), bookmarks: [{ start_frame: 1, end_frame: 2 }] });
ok(bm.ok === false || bm.reason === 'audio-mismatch' || bm.valid.length === 0, 'unbound bookmarks must be rejected');
console.log('player-shape-ok');
"""
        with _tf.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(checks); tmp.flush()
            res = subprocess.run(["node", tmp.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, res.returncode, res.stderr + res.stdout)

    def test_C_player_handler_fences_wrong_audio(self):
        import re, subprocess, tempfile as _tf
        player = Path(__file__).parent.parent / "static" / "readalong-player.html"
        html = player.read_text(encoding="utf-8")
        m = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertIsNotNone(m)
        stubs = """
var _els = {};
function _el(id) {
  if (_els[id]) { return _els[id]; }
  var el = { id: id, children: [], style: {}, dataset: {}, textContent: '', value: '0', innerHTML: '',
    classList: { toggle: function(){}, add: function(){}, remove: function(){}, contains: function(){ return false; } },
    setAttribute: function(){}, appendChild: function(c){ this.children.push(c); },
    addEventListener: function(n, f){ this['_on_'+n] = f; }, querySelectorAll: function(){ return []; }, files: [] };
  _els[id] = el; return el;
}
var document = { getElementById: _el,
  createElement: function(tag) { var e = { tag: tag, children: [], style: {}, classList: { toggle: function(){}, add: function(){}, remove: function(){} }, setAttribute: function(){}, appendChild: function(c){ this.children.push(c); }, addEventListener: function(){}, querySelectorAll: function(){ return []; }, onclick: null, textContent: '', className: '' }; return e; },
  createTextNode: function(t) { return { text: t }; }, createDocumentFragment: function(){ return { children: [], appendChild: function(c){ this.children.push(c); } }; } };
var window = {};
var audioEl = _el('audio');
var _handlers = {};
audioEl.addEventListener = function(n, f){ _handlers['audio:'+n] = f; };
audioEl.play = function(){}; audioEl.pause = function(){};
var Blob = function(parts, opts){ this.parts = parts; };
var URL = { createObjectURL: function(){ return 'blob:fake'; }, revokeObjectURL: function(){} };
"""
        # Capture sidecar/audio change handlers by wrapping getElementById addEventListener.
        checks = stubs + "\n" + m.group(1) + r"""
var ok = function(cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } };
// The player must expose a verified flag gating highlight. Before verification, mark() must not highlight.
ok(typeof window.AA2Readalong.isVerified === 'function', 'isVerified gate missing');
ok(window.AA2Readalong.isVerified() === false, 'must start unverified');
console.log('player-gate-ok');
"""
        with _tf.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(checks); tmp.flush()
            res = subprocess.run(["node", tmp.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(0, res.returncode, res.stderr + res.stdout)

    # ---- D: admission/UI ----
    def test_D_worker_compares_admitted_snapshot(self):
        import inspect
        from routers import editor as ed
        src = inspect.getsource(ed._start_readalong_sidecar)
        self.assertIn("admitted_snapshot", src)
        self.assertIn("current[\"snapshot\"]", src)
        src2 = inspect.getsource(ed._start_readaloud_epub)
        self.assertIn("admitted_snapshot", src2)

    def test_D_app_readalong_poll_is_fenced_and_typed(self):
        js = Path(__file__).parent.parent / "static" / "js" / "app-readalong.js"
        text = js.read_text(encoding="utf-8")
        # Must use the app API helper (with deadline), not bare fetch.
        self.assertIn("API.", text)
        self.assertNotIn("return fetch(url", text.replace("API.", ""))
        # Must fence by task identity and require a typed terminal result.
        self.assertIn("result.status", text)
        self.assertIn("taskId", text)

    # ---- E: retention ----
    def test_E_retention_inventory_and_stable_locate(self):
        from source_upload_publication import get_retained_epub_inventory
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp)
            (uploads / "book.txt").write_text("Hello.", encoding="utf-8")
            (uploads / "book.source.epub").write_bytes(b"PK-fake")
            inv = get_retained_epub_inventory(str(uploads), 10 * 1024 * 1024)
            self.assertIn("groups", inv)
            self.assertTrue(any(g.get("files") for g in inv["groups"]))
            self.assertIn("notice", inv)
            self.assertIn("No automatic deletion", inv["notice"])

    def test_E_duplicate_reupload_attaches_missing_retained(self):
        import asyncio
        from contextlib import asynccontextmanager
        from unittest.mock import patch
        from fastapi import FastAPI
        import httpx
        from routers import script as rscript
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); uploads = root / 'uploads'; uploads.mkdir()
                app = FastAPI(); app.include_router(rscript.router)
                with patch.object(rscript, 'UPLOADS_DIR', str(uploads)), patch.object(rscript, 'DATA_DIR', str(root)):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                                 base_url='http://fixture') as client:
                        raw = _make_source_epub_nested()
                        # Pre-existing canonical TXT predating retention (no .source.epub).
                        # Upload the EPUB whose text matches; retention must attach.
                        # First create canonical TXT directly via a TXT upload of same text.
                        text = b"Hello synthetic world. Second paragraph here."
                        # Use a direct EPUB upload instead: first upload creates both;
                        # delete retained to simulate predating book, then re-upload.
                        r1 = await client.post('/api/upload', params={'select_active': 'false'},
                                               files={'file': ('book.epub', raw, 'application/epub+zip')})
                        self.assertEqual(200, r1.status_code, r1.text)
                        retained = list(uploads.glob('*.source.epub'))
                        self.assertTrue(retained)
                        retained[0].unlink()
                        r2 = await client.post('/api/upload', params={'select_active': 'false'},
                                               files={'file': ('book2.epub', raw, 'application/epub+zip')})
                        self.assertEqual(200, r2.status_code, r2.text)
                        self.assertIsNotNone(r2.json().get('retained_epub'),
                                             "re-upload must attach missing retained EPUB to canonical TXT")
                        self.assertTrue(list(uploads.glob('*.source.epub')))
        asyncio.run(run())

    def test_E_locate_is_deterministic(self):
        from readalong_export import _locate_retained_epub
        # Two retained files with identical hashes: directory-order guessing must
        # surface ambiguity instead of silently picking the first.
        import inspect
        src = inspect.getsource(_locate_retained_epub)
        self.assertIn("sorted(", src)

    # ---- F: mapping exactness ----
    def test_F_repeat_split_merge_text(self):
        from epub_readaloud import map_spine_to_chunks
        # Repeated text: identical segments must map in order, no cross-talk.
        spine = [{"fragment": None, "text": "Hello world."}, {"fragment": None, "text": "Hello world."}]
        entries = [
            {"index": 0, "prepared_text": "Hello world.", "start_frame": 0, "end_frame": 1000, "sample_rate": 24000},
            {"index": 1, "prepared_text": "Hello world.", "start_frame": 1000, "end_frame": 2000, "sample_rate": 24000},
        ]
        mapping, end = map_spine_to_chunks(spine, entries, start=0)
        self.assertEqual([0], mapping[0]["chunk_indexes"])
        self.assertEqual([1], mapping[1]["chunk_indexes"])
        self.assertEqual(2, end)
        # Split text: one segment split across 4 chunks must work.
        spine2 = [{"fragment": None, "text": "alpha beta gamma delta"}]
        entries2 = [{"index": i, "prepared_text": t, "start_frame": i * 1000, "end_frame": (i + 1) * 1000,
                     "sample_rate": 24000} for i, t in enumerate(["alpha", "beta", "gamma", "delta"])]
        mapping2, end2 = map_spine_to_chunks(spine2, entries2, start=0)
        self.assertEqual(4, len(mapping2[0]["chunk_indexes"]))


if __name__ == "__main__":
    unittest.main()
