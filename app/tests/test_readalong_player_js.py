"""AA2 read-along standalone player R3: real-handler Node harness (#1720/#1779).

Every behavioral test extracts the shipped inline <script> from
``static/readalong-player.html``, installs a *stateful* DOM (live
classList / children / textContent / listeners / querySelectorAll,
DocumentFragment flattening, blob-URL registry, cancellable
setTimeout), then invokes the ACTUAL registered change / click /
keydown / input / timeupdate handlers with realistic File-like
objects (real sizes, real bytes, real Node crypto digest for
fixtures, deferred arrayBuffer/text promises for async interleavings).

No source-substring assertions are used as behavior proof. The only
structural check is the local-only guard (no external <script src>,
no network URLs) which cannot be observed from the DOM stub alone;
it is labelled structural and never stands in for handler coverage.

Measured behavior (what a GREEN run proves):
- audio-first and sidecar-first orders both reach verified,
- wrong audio keeps generic playback (play/pause + seek-range work)
  but entry/section/bookmark seeks + add/import/export are gated at
  handler execution and synchronized highlights are cleared,
- per-input generations: bookmark/sidecar events do not cancel a
  legitimate in-flight audio hash and stale same-input completions
  cannot overwrite current files/status,
- bad/oversize/malformed sidecar and oversize audio reset the
  appropriate input immediately and clear stale highlights/markup,
  while hash/read failures keep generic audio playback unverified
  without unhandled rejections,
- half-open entry/word spans, pause/end/unverified clearing,
- Enter/Space activation for focusable entries, buttons as standard
  <button> click activation, textContent-only rendering,
- Python-codepoint word offsets rendered correctly for astral /
  combining text, JS validator cross-checked against REAL
  ``build_readalong_sidecar`` output (include_text false + true),
- portable bookmark document: schema/version + full timeline
  binding, bounded count/bytes/id/label, unique ids, section refs,
  atomic rejection, empty-list clearing, preserved pair, clamped
  add-bookmark, export roundtrip.

Limits: Node DOM is a hand-rolled stub (it does not parse the HTML
or implement a layout / media pipeline). Timing is therefore
simulated by invoking timeupdate directly; no browser, reader,
listening or full-suite claim is made here.
"""
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


STUBS = r"""
var _NodeCrypto = require('crypto');
var _realSetTimeout = require('timers').setTimeout;
var _pendingTimers = [];
var setTimeout = function(fn, ms) { _pendingTimers.push(fn); return _pendingTimers.length; };
function _flushTimers() { var fns = _pendingTimers.splice(0); for (var i = 0; i < fns.length; i++) { fns[i](); } }
var _blobUrls = {};
var _revokedUrls = [];
var _blobCounter = 0;
var URL = {
  createObjectURL: function(obj) { _blobCounter++; var u = 'blob:fake-' + _blobCounter; _blobUrls[u] = obj; return u; },
  revokeObjectURL: function(u) { _revokedUrls.push(u); }
};
var Blob = function(parts, opts) { this.parts = parts || []; this.type = (opts && opts.type) || ''; };
var _windowCrypto = _NodeCrypto.webcrypto;
var window = { crypto: _windowCrypto };
global.crypto = _windowCrypto;
function _makeClassList(classSet) {
  return {
    toggle: function(name, force) {
      var should = (force === undefined) ? !classSet[name] : !!force;
      if (should) { classSet[name] = true; } else { delete classSet[name]; }
    },
    add: function(name) { classSet[name] = true; },
    remove: function(name) { delete classSet[name]; },
    contains: function(name) { return !!classSet[name]; }
  };
}
function _makeElement(tag, id) {
  var classSet = {};
  var el = {
    id: id || '', tag: tag, tagName: String(tag).toUpperCase(),
    children: [], style: {}, dataset: {},
    type: '', tabIndex: 0, value: '0', src: '', currentTime: 0, duration: 0, paused: true,
    files: [], disabled: false, href: '', download: '',
    onclick: null, onkeydown: null, oninput: null,
    _attrs: {}, _listeners: {}, _playCount: 0, _pauseCount: 0,
    _text: '', _innerHTML: '', _isFragment: false, _isTextNode: false
  };
  Object.defineProperty(el, 'className', {
    configurable: true, enumerable: true,
    get: function() { return Object.keys(classSet).join(' '); },
    set: function(v) {
      for (var k in classSet) { delete classSet[k]; }
      if (v) { var parts = String(v).split(/\s+/); for (var i = 0; i < parts.length; i++) { if (parts[i]) { classSet[parts[i]] = true; } } }
    }
  });
  el.classList = _makeClassList(classSet);
  Object.defineProperty(el, 'textContent', {
    configurable: true, enumerable: true,
    get: function() { return el._text; },
    set: function(v) { el._text = String(v); el.children = []; }
  });
  Object.defineProperty(el, 'innerHTML', {
    configurable: true, enumerable: true,
    get: function() { return el._innerHTML; },
    set: function(v) { el._innerHTML = String(v); el.children = []; el._text = ''; }
  });
  el.setAttribute = function(name, value) { el._attrs[String(name)] = String(value); };
  el.getAttribute = function(name) { return Object.prototype.hasOwnProperty.call(el._attrs, String(name)) ? el._attrs[String(name)] : null; };
  el.appendChild = function(child) {
    if (child && child._isFragment) {
      var kids = child.children.slice();
      child.children = [];
      for (var i = 0; i < kids.length; i++) { el.children.push(kids[i]); }
      return child;
    }
    el.children.push(child);
    return child;
  };
  el.querySelectorAll = function(selector) {
    var results = [];
    var want = String(selector).trim();
    var classes = [];
    if (want.charAt(0) === '.') { classes = want.slice(1).split('.'); }
    function walk(node) {
      for (var i = 0; i < node.children.length; i++) {
        var c = node.children[i];
        if (c && c._isTextNode) { continue; }
        if (c && c.classList && typeof c.classList.contains === 'function') {
          var match = true;
          for (var j = 0; j < classes.length; j++) { if (classes[j] && !c.classList.contains(classes[j])) { match = false; break; } }
          if (match && classes.length > 0) { results.push(c); }
        }
        if (c && c.children && c.children.length) { walk(c); }
      }
    }
    walk(el);
    return results;
  };
  el.addEventListener = function(name, fn) { el._listeners[String(name)] = fn; };
  el.removeEventListener = function(name, fn) { if (el._listeners[String(name)] === fn) { delete el._listeners[String(name)]; } };
  el.click = function() { if (typeof el.onclick === 'function') { el.onclick({ target: el }); } };
  el.play = function() { el.paused = false; el._playCount++; return Promise.resolve(); };
  el.pause = function() { el.paused = true; el._pauseCount++; };
  el.focus = function() { el._focused = true; };
  return el;
}
var _els = {};
function _getEl(id) {
  if (_els[id]) { return _els[id]; }
  var tag = 'div';
  if (id === 'audio') { tag = 'audio'; }
  else if (id === 'audio-file' || id === 'sidecar-file' || id === 'bm-file' || id === 'seek-range') { tag = 'input'; }
  else if (id === 'add-bookmark' || id === 'save-bookmarks' || id === 'play-pause') { tag = 'button'; }
  else if (id === 'bookmarks') { tag = 'ul'; }
  var el = _makeElement(tag, id);
  if (id === 'entries') {
    el.tabIndex = 0;
    el.setAttribute('aria-label', 'Synchronized text');
    el.setAttribute('tabindex', '0');
  }
  if (id === 'notice') { el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite'); }
  if (id === 'audio-status') { el.setAttribute('role', 'status'); el.setAttribute('aria-live', 'polite'); }
  _els[id] = el;
  return el;
}
var document = {
  getElementById: _getEl,
  createElement: function(tag) { return _makeElement(tag, ''); },
  createTextNode: function(t) { return { text: t, _isTextNode: true, tag: '#text', children: [], _text: t }; },
  createDocumentFragment: function() { var f = { _isFragment: true, children: [], appendChild: function(c) { f.children.push(c); return c; } }; return f; }
};
var FileReader = function() {};
var File = function() {};
"""

HELPERS = r"""
function ok(cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } }
function flush() {
  return new Promise(function(r) { _realSetTimeout(r, 0); }).then(function() {
    return new Promise(function(r) { _realSetTimeout(r, 0); });
  }).then(function() {
    return new Promise(function(r) { _realSetTimeout(r, 0); });
  });
}
function shaHex(str) { return _NodeCrypto.createHash('sha256').update(str).digest('hex'); }
function makeDeferred() {
  var res, rej;
  var p = new Promise(function(resolve, reject) { res = resolve; rej = reject; });
  return { promise: p, resolve: res, reject: rej };
}
function makeFile(name, content, opts) {
  opts = opts || {};
  var bytes = new TextEncoder().encode(content);
  var size = (opts.size !== undefined ? opts.size : bytes.length);
  var file = { name: name, size: size, type: opts.type || 'application/octet-stream', _content: content };
  if (opts.deferredText) {
    var d = makeDeferred();
    file._deferText = d;
    file.text = function() { return d.promise; };
  } else if (opts.failText) {
    file.text = function() { return Promise.reject(new Error('text read fail')); };
  } else {
    file.text = function() { return Promise.resolve(content); };
  }
  if (opts.deferredBuf) {
    var d2 = makeDeferred();
    file._deferBuf = d2;
    file.arrayBuffer = function() { return d2.promise; };
  } else if (opts.failBuf) {
    file.arrayBuffer = function() { return Promise.reject(new Error('buffer read fail')); };
  } else {
    file.arrayBuffer = function() { return Promise.resolve(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength)); };
  }
  return file;
}
function fireChange(el, file) {
  el.files = file ? [file] : [];
  var fn = el._listeners['change'];
  ok(typeof fn === 'function', 'change handler missing for ' + el.id);
  fn({ target: el });
  return fn;
}
function fireTimeUpdate() {
  var a = document.getElementById('audio');
  var fn = a._listeners['timeupdate'];
  ok(typeof fn === 'function', 'timeupdate handler missing');
  fn({});
}
function getEntries() { return document.getElementById('entries').children; }
function entryWords(entryEl) {
  var all = [];
  for (var i = 0; i < entryEl.children.length; i++) {
    var c = entryEl.children[i];
    if (c && c.classList && c.classList.contains('word')) { all.push(c); }
  }
  return all;
}
function entryText(entryEl) {
  if (!entryEl.children || entryEl.children.length === 0) { return entryEl.textContent || ''; }
  var s = '';
  for (var i = 0; i < entryEl.children.length; i++) {
    var c = entryEl.children[i];
    if (c && c._isTextNode) { s += c.text; }
    else if (c && typeof c.textContent === 'string') { s += c.textContent; }
  }
  return s;
}
function countActive() {
  var e = document.getElementById('entries');
  var n = 0;
  for (var i = 0; i < e.children.length; i++) {
    var en = e.children[i];
    if (en.classList && en.classList.contains('active')) { n++; }
    var ws = entryWords(en);
    for (var j = 0; j < ws.length; j++) { if (ws[j].classList.contains('active')) { n++; } }
  }
  return n;
}
function getSections() { return document.getElementById('sections').children; }
function getBookmarks() { return document.getElementById('bookmarks').children; }
function getBookmarkButtons() {
  var lis = getBookmarks();
  var out = [];
  for (var i = 0; i < lis.length; i++) {
    var li = lis[i];
    if (li.children && li.children[0]) { out.push(li.children[0]); }
  }
  return out;
}
function makeSidecar(audioBytes, opts) {
  opts = opts || {};
  var sha = (opts.sha !== undefined ? opts.sha : shaHex(audioBytes));
  var frames = (opts.frames || 48000);
  var rate = (opts.rate || 24000);
  var doc = {
    schema: 'aa2-readalong-sidecar', version: 1,
    binding: {
      audio_sha256: sha, audio_format: 'mp3',
      audio_bytes: (opts.audioBytes !== undefined ? opts.audioBytes : new TextEncoder().encode(audioBytes).length),
      sample_rate: rate, channels: 1, sample_width: 2, frames: frames,
      duration_ms: Math.round(1000 * frames / rate * 1000) / 1000
    },
    includes_text: !!opts.includes_text,
    sections: [{ id: 'sec-0001', title: (opts.sectionTitle !== undefined ? opts.sectionTitle : null), start_frame: 0, end_frame: frames, entry_count: 2 }],
    bookmarks: [{ id: 'bm-0000', label: 'Section 0001', start_frame: 0, end_frame: 24000, section: 'sec-0001' }],
    entries: opts.entries || [
      { index: 0, section: 'sec-0001', speaker: null, text: opts.includes_text ? 'Hello world.' : null, start_frame: 0, end_frame: 24000, pause_frames_after: 0, status: 'missing', words: null },
      { index: 1, section: 'sec-0001', speaker: null, text: opts.includes_text ? 'Second line.' : null, start_frame: 24000, end_frame: 48000, pause_frames_after: 0, status: 'missing', words: null }
    ]
  };
  if (opts.extra) { for (var k in opts.extra) { doc[k] = opts.extra[k]; } }
  return doc;
}
async function loadPair(audioBytes, sidecarDoc, order) {
  var audioFile = makeFile('audiobook.mp3', audioBytes, { type: 'audio/mpeg' });
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(sidecarDoc), { type: 'application/json' });
  var aEl = document.getElementById('audio');
  aEl.duration = 2;
  if (order === 'audio-first') {
    fireChange(document.getElementById('audio-file'), audioFile);
    await flush();
    fireChange(document.getElementById('sidecar-file'), sidecarFile);
    await flush();
  } else {
    fireChange(document.getElementById('sidecar-file'), sidecarFile);
    await flush();
    fireChange(document.getElementById('audio-file'), audioFile);
    await flush();
  }
  await flush();
}
function audioBytesBuffer(content) {
  var bytes = new TextEncoder().encode(content);
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}
"""


def _player_js():
    player = Path(__file__).parent.parent / "static" / "readalong-player.html"
    html = player.read_text(encoding="utf-8")
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    if not match:
        raise AssertionError("player inline script missing")
    return match.group(1)


class ReadalongPlayerJsTests(unittest.TestCase):
    def _run(self, program, timeout=30):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as tmp:
            tmp.write(program)
            tmp.flush()
            tmp_path = tmp.name
        try:
            try:
                return subprocess.run(
                    ["node", tmp_path], capture_output=True, text=True, timeout=timeout
                )
            except subprocess.TimeoutExpired as exc:
                out = ""
                err = ""
                try:
                    out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                except Exception:
                    out = repr(exc.stdout)
                try:
                    err = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
                except Exception:
                    err = repr(exc.stderr)
                raise AssertionError(
                    f"Node harness timed out after {timeout}s (file {tmp_path}): "
                    f"stdout={out!r} stderr={err!r}"
                ) from exc
            except FileNotFoundError as exc:
                raise AssertionError(f"Node executable not found on PATH: {exc}") from exc
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass
            except OSError:
                Path(tmp_path).unlink(missing_ok=True)

    def _check(self, scenario, marker, timeout=30):
        program = STUBS + "\n" + _player_js() + "\n" + HELPERS + "\n" + scenario
        result = self._run(program, timeout=timeout)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertIn(marker, result.stdout)

    # ---- validators cross-checked against the real Python builder ----
    def test_validators_against_real_python_sidecar(self):
        from readalong_sidecar import build_readalong_sidecar

        binding = {
            "audio_sha256": hashlib.sha256(b"exported-mp3").hexdigest(),
            "audio_bytes": 12,
            "sample_rate": 24000,
            "channels": 1,
            "sample_width": 2,
            "frames": 48000,
        }
        entries_plain = [
            {"index": 0, "section": "sec-0001", "speaker": "Narrator",
             "text": "Hello world.", "start_frame": 0, "end_frame": 24000,
             "pause_frames_after": 240, "status": "missing", "words": None},
            {"index": 1, "section": "sec-0001", "speaker": "Narrator",
             "text": "Second line.", "start_frame": 24240, "end_frame": 48000,
             "pause_frames_after": 0, "status": "missing", "words": None},
        ]
        doc_plain, _raw_plain = build_readalong_sidecar(
            entries=entries_plain, binding=binding, includes_text=False
        )
        entries_words = [
            {"index": 0, "section": "sec-0001", "speaker": "Narrator",
             "text": "Hello world.", "start_frame": 0, "end_frame": 24000,
             "pause_frames_after": 0, "status": "aligned-estimated",
             "words": [{"from": 0, "to": 5, "start_frame": 100, "end_frame": 1000},
                       {"from": 6, "to": 11, "start_frame": 1200, "end_frame": 2000}]},
            {"index": 1, "section": "sec-0001", "speaker": "Narrator",
             "text": "Second line.", "start_frame": 24000, "end_frame": 48000,
             "pause_frames_after": 0, "status": "chunk-estimated", "words": None},
        ]
        doc_words, _raw_words = build_readalong_sidecar(
            entries=entries_words, binding=binding, includes_text=True
        )
        scenario = r"""
async function main() {
  var realPlain = __DOC_PLAIN__;
  var realWords = __DOC_WORDS__;
  var r1 = window.AA2Readalong.validateSidecar(realPlain);
  ok(r1.ok === true, 'real Python sidecar (text off) must validate, got ' + r1.reason);
  var r2 = window.AA2Readalong.validateSidecar(realWords);
  ok(r2.ok === true, 'real Python sidecar (text + aligned words) must validate, got ' + r2.reason);
  ok(window.AA2Readalong.checkAudioHash('a', 'b') === false, 'wrong audio must be detected');
  ok(window.AA2Readalong.checkAudioHash('a', 'a') === true, 'matching audio must pass');
  ok(window.AA2Readalong.isVerified() === false, 'must start unverified');
  console.log('validators-real-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        scenario = scenario.replace("__DOC_PLAIN__", json.dumps(doc_plain, ensure_ascii=False))
        scenario = scenario.replace("__DOC_WORDS__", json.dumps(doc_words, ensure_ascii=False))
        self._check(scenario, "validators-real-ok")

    # ---- pair verification in both orders ----
    def test_audio_first_then_sidecar_verifies(self):
        scenario = r"""
async function main() {
  var audioBytes = 'fake-mp3-bytes-audio-first';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'audio-first');
  ok(window.AA2Readalong.isVerified() === true, 'audio-first then sidecar must verify');
  ok(document.getElementById('notice').textContent.indexOf('matches') >= 0, 'notice must report match, got: ' + document.getElementById('notice').textContent);
  ok(getEntries().length === 2, 'entries must render');
  ok(getSections().length === 1, 'sections must render');
  ok(getBookmarkButtons().length === 1, 'sidecar bookmarks must render');
  var a = document.getElementById('audio');
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'highlight must be active inside first entry');
  console.log('audio-first-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "audio-first-ok")

    def test_sidecar_first_then_audio_verifies(self):
        scenario = r"""
async function main() {
  var audioBytes = 'fake-mp3-bytes-sidecar-first';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'sidecar-first then audio must verify');
  ok(document.getElementById('notice').textContent.indexOf('matches') >= 0, 'notice must report match');
  var a = document.getElementById('audio');
  a.currentTime = 1.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'highlight must be active inside second entry');
  console.log('sidecar-first-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "sidecar-first-ok")

    def test_wrong_audio_generic_playback_but_sync_gated(self):
        scenario = r"""
async function main() {
  var goodBytes = 'correct-mp3-bytes';
  var doc = makeSidecar(goodBytes, {});
  var badFile = makeFile('wrong.mp3', 'different-mp3-bytes', { type: 'audio/mpeg' });
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(doc), { type: 'application/json' });
  var a = document.getElementById('audio');
  a.duration = 2;
  fireChange(document.getElementById('sidecar-file'), sidecarFile);
  await flush();
  fireChange(document.getElementById('audio-file'), badFile);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'wrong audio must stay unverified');
  ok(typeof a.src === 'string' && a.src.length > 0, 'generic audio src must be set for wrong audio');
  ok(document.getElementById('notice').textContent.indexOf('Wrong audio') >= 0, 'notice must report wrong audio, got: ' + document.getElementById('notice').textContent);
  // Generic playback still works.
  a.paused = true;
  document.getElementById('play-pause').onclick();
  ok(a.paused === false, 'play-pause must play as audio-only');
  document.getElementById('play-pause').onclick();
  ok(a.paused === true, 'play-pause must pause as audio-only');
  var range = document.getElementById('seek-range');
  range.value = '500';
  range.oninput({ target: range });
  ok(Math.abs(a.currentTime - 1.0) < 1e-9, 'seek-range general playback must work, got ' + a.currentTime);
  // Value for a seek must exceed tolerance: reset to a known time then check gating.
  a.currentTime = 0.25;
  // Entry seek gated.
  var entries = getEntries();
  ok(entries.length === 2, 'entries render even when unverified');
  var before = a.currentTime;
  entries[0].onclick();
  ok(a.currentTime === before, 'entry seek must be gated when unverified, got ' + a.currentTime);
  if (entries[0].onkeydown) { entries[0].onkeydown({ key: 'Enter', preventDefault: function() {} }); ok(a.currentTime === before, 'entry Enter seek must be gated'); }
  // Section seek gated.
  var secs = getSections();
  ok(secs.length === 1, 'section renders');
  secs[0].onclick();
  ok(a.currentTime === before, 'section seek must be gated when unverified');
  // Bookmark seek + add + export gated.
  var bmBtns = getBookmarkButtons();
  ok(bmBtns.length === 1, 'bookmark renders');
  bmBtns[0].onclick();
  ok(a.currentTime === before, 'bookmark seek must be gated when unverified');
  var nBefore = getBookmarkButtons().length;
  document.getElementById('add-bookmark').onclick();
  ok(getBookmarkButtons().length === nBefore, 'add-bookmark must be gated');
  var blobKeysBefore = Object.keys(_blobUrls).length;
  document.getElementById('save-bookmarks').onclick();
  ok(Object.keys(_blobUrls).length === blobKeysBefore, 'export must be gated (no blob created)');
  // Highlights cleared when unverified even on timeupdate.
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() === 0, 'no highlights when unverified');
  console.log('wrong-audio-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "wrong-audio-ok")

    def test_replacement_clears_active_immediately(self):
        scenario = r"""
async function main() {
  var audioBytes = 'replace-audio-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair must verify first');
  var a = document.getElementById('audio');
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'highlight active before replacement');
  // Replace the audio input (new file whose hash is deferred, so the
  // pair is unverified synchronously) -- actives must clear at once.
  var deferred = makeFile('new.mp3', 'stale-bytes-xyz', { type: 'audio/mpeg', deferredBuf: true });
  fireChange(document.getElementById('audio-file'), deferred);
  ok(window.AA2Readalong.isVerified() === false, 'replacement must unverified immediately');
  ok(countActive() === 0, 'replacement must clear .active synchronously before async hash');
  // Resolve the stale file: it is a wrong file bound to no sidecar, so
  // it must land as wrong-audio unverified, never re-verifying.
  deferred._deferBuf.resolve(audioBytesBuffer('stale-bytes-xyz'));
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'wrong replacement must not verify');
  ok(countActive() === 0, 'highlights stay cleared for wrong replacement');
  console.log('replacement-clears-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "replacement-clears-ok")

    def test_malformed_sidecar_clears_previous(self):
        scenario = r"""
async function main() {
  var audioBytes = 'malformed-test-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  ok(getEntries().length === 2, 'entries present');
  var a = document.getElementById('audio');
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'highlight present');
  var oldSrc = a.src;
  var badText = makeFile('broken.json', '{not-json', { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), badText);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'malformed sidecar must unverified');
  ok(getEntries().length === 0, 'malformed sidecar must clear entries, got ' + getEntries().length);
  ok(getSections().length === 0, 'malformed sidecar must clear sections');
  ok(getBookmarkButtons().length === 0, 'malformed sidecar must clear bookmarks');
  ok(countActive() === 0, 'malformed sidecar must clear highlights');
  ok(document.getElementById('notice').textContent.length > 0, 'malformed sidecar must show a notice');
  ok(a.src === oldSrc && a.src.length > 0, 'audio playback must survive malformed sidecar');
  console.log('malformed-clears-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "malformed-clears-ok")

    def test_oversize_inputs_reset_appropriate_input(self):
        scenario = r"""
async function main() {
  var audioBytes = 'oversize-audio-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  var a = document.getElementById('audio');
  var goodSrc = a.src;
  ok(goodSrc.length > 0, 'audio src present');
  // Oversize sidecar: resets sidecar only, audio remains playable.
  var bigSidecar = makeFile('big.json', '{}', { type: 'application/json', size: 64 * 1024 * 1024 + 1 });
  fireChange(document.getElementById('sidecar-file'), bigSidecar);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'oversize sidecar unverified');
  ok(getEntries().length === 0, 'oversize sidecar clears entries');
  ok(getBookmarks().length === 0, 'oversize sidecar clears bookmarks');
  ok(a.src === goodSrc, 'oversize sidecar must not touch audio src');
  // Reload the good pair, then oversize audio.
  var doc2 = makeSidecar(audioBytes, {});
  var sidecarFile2 = makeFile('sidecar.json', JSON.stringify(doc2), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), sidecarFile2);
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'pair re-verifies');
  var bigAudio = makeFile('big.mp3', 'x', { type: 'audio/mpeg', size: 2 * 1024 * 1024 * 1024 + 1 });
  fireChange(document.getElementById('audio-file'), bigAudio);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'oversize audio unverified');
  ok(a.src === '', 'oversize audio clears audio src (reset), got src=' + a.src);
  ok(_revokedUrls.indexOf(goodSrc) >= 0, 'old audio URL revoked on audio reset, revoked=' + _revokedUrls.join(','));
  ok(getEntries().length === 2, 'oversize audio preserves sidecar markup');
  ok(getBookmarkButtons().length === 1, 'oversize audio preserves bookmark list (gated)');
  ok(countActive() === 0, 'oversize audio clears highlights');
  console.log('oversize-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "oversize-ok")

    def test_independent_generations_bookmark_does_not_cancel_audio(self):
        scenario = r"""
async function main() {
  var audioBytes = 'indep-audio-bytes';
  var doc = makeSidecar(audioBytes, {});
  var aEl = document.getElementById('audio');
  aEl.duration = 2;
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(doc), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), sidecarFile);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'sidecar alone unverified');
  // Deferred audio hash in flight, then a bookmark import starts.
  var deferredAudio = makeFile('audiobook.mp3', audioBytes, { type: 'audio/mpeg', deferredBuf: true });
  fireChange(document.getElementById('audio-file'), deferredAudio);
  var bmDoc = {
    schema: 'aa2-readalong-bookmarks', version: 1,
    binding: doc.binding,
    bookmarks: [{ id: 'bm-imp-1', label: 'Imported one', start_frame: 4800, end_frame: 4800, section: null }]
  };
  var bmFile = makeFile('bm.json', JSON.stringify(bmDoc), { type: 'application/json' });
  fireChange(document.getElementById('bm-file'), bmFile);
  await flush();
  ok(getBookmarkButtons().length === 1, 'bookmark import applied while audio hash pending');
  ok(window.AA2Readalong.isVerified() === false, 'still unverified until audio hash completes');
  // Now the audio hash completes -- it must not have been cancelled.
  deferredAudio._deferBuf.resolve(audioBytesBuffer(audioBytes));
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'bookmark import must not cancel audio hash');
  ok(document.getElementById('notice').textContent.indexOf('matches') >= 0, 'notice must report match');
  // Imported bookmark must still be there and its seek now works.
  ok(getBookmarkButtons().length === 1, 'imported bookmark preserved after verify');
  console.log('indep-bookmark-audio-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "indep-bookmark-audio-ok")

    def test_sidecar_selection_does_not_cancel_audio_hash(self):
        scenario = r"""
async function main() {
  var audioBytes = 'indep2-audio-bytes';
  var doc = makeSidecar(audioBytes, {});
  var aEl = document.getElementById('audio');
  aEl.duration = 2;
  // Audio hash deferred, then sidecar selected while it is pending.
  var deferredAudio = makeFile('audiobook.mp3', audioBytes, { type: 'audio/mpeg', deferredBuf: true });
  fireChange(document.getElementById('audio-file'), deferredAudio);
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(doc), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), sidecarFile);
  await flush();
  ok(getEntries().length === 2, 'sidecar loaded while audio pending');
  ok(window.AA2Readalong.isVerified() === false, 'unverified until hash completes');
  deferredAudio._deferBuf.resolve(audioBytesBuffer(audioBytes));
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'sidecar selection must not cancel audio hash');
  console.log('indep-sidecar-audio-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "indep-sidecar-audio-ok")

    def test_successive_out_of_order_stale_does_not_overwrite(self):
        scenario = r"""
async function main() {
  var bytesOne = 'audio-one-stale';
  var bytesTwo = 'audio-two-current';
  var doc = makeSidecar(bytesTwo, {});
  var aEl = document.getElementById('audio');
  aEl.duration = 2;
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(doc), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), sidecarFile);
  await flush();
  // Slow first audio choice, then fast second choice.
  var slow = makeFile('a1.mp3', bytesOne, { type: 'audio/mpeg', deferredBuf: true });
  var fast = makeFile('a2.mp3', bytesTwo, { type: 'audio/mpeg' });
  fireChange(document.getElementById('audio-file'), slow);
  fireChange(document.getElementById('audio-file'), fast);
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'fast current audio must verify');
  var noticeAfterFast = document.getElementById('notice').textContent;
  // The stale slow completion arrives late: must not overwrite status.
  slow._deferBuf.resolve(audioBytesBuffer(bytesOne));
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'stale audio must not unverified the current pair');
  ok(document.getElementById('notice').textContent === noticeAfterFast, 'stale audio must not overwrite status');
  // Same for sidecars: slow first, fast second.
  var docSlow = makeSidecar(bytesTwo, { entries: [
    { index: 0, section: 'sec-0001', speaker: null, text: null, start_frame: 0, end_frame: 48000, pause_frames_after: 0, status: 'missing', words: null }
  ] });
  var docFast = makeSidecar(bytesTwo, {});
  var slowSidecar = makeFile('s1.json', JSON.stringify(docSlow), { type: 'application/json', deferredText: true });
  var fastSidecar = makeFile('s2.json', JSON.stringify(docFast), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), slowSidecar);
  fireChange(document.getElementById('sidecar-file'), fastSidecar);
  await flush();
  ok(getEntries().length === 2, 'fast sidecar must win, got ' + getEntries().length);
  ok(window.AA2Readalong.isVerified() === true, 'fast sidecar + current audio must verify');
  slowSidecar._deferText.resolve(JSON.stringify(docSlow));
  await flush();
  ok(getEntries().length === 2, 'stale sidecar must not overwrite current entries');
  ok(window.AA2Readalong.isVerified() === true, 'stale sidecar must not unverified the pair');
  console.log('stale-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "stale-ok")

    def test_halfopen_spans_pause_boundary_end(self):
        scenario = r"""
async function main() {
  var audioBytes = 'halfopen-bytes';
  var doc = makeSidecar(audioBytes, {
    includes_text: true,
    entries: [
      { index: 0, section: 'sec-0001', speaker: null, text: 'Hello world.', start_frame: 0, end_frame: 24000, pause_frames_after: 0, status: 'aligned-estimated',
        words: [{ from: 0, to: 5, start_frame: 0, end_frame: 12000 }, { from: 6, to: 11, start_frame: 12000, end_frame: 24000 }] },
      { index: 1, section: 'sec-0001', speaker: null, text: 'Later line.', start_frame: 36000, end_frame: 48000, pause_frames_after: 0, status: 'missing', words: null }
    ]
  });
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  var a = document.getElementById('audio');
  var entries = getEntries();
  ok(entries.length === 2, 'two entries render');
  // Inside first word.
  a.currentTime = 0.25;
  fireTimeUpdate();
  ok(entries[0].classList.contains('active'), 'entry 0 active at 0.25s');
  ok(!entries[1].classList.contains('active'), 'entry 1 inactive at 0.25s');
  var w0 = entryWords(entries[0]);
  ok(w0.length === 2, 'two words render');
  ok(w0[0].classList.contains('active') && !w0[1].classList.contains('active'), 'word 0 active only');
  // Exact word boundary (12000 frames = 0.5s): only the second word.
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(!w0[0].classList.contains('active') && w0[1].classList.contains('active'), 'word boundary half-open (only second word)');
  ok(entries[0].classList.contains('active'), 'entry still active at word boundary');
  // Pause gap 24000..36000 frames = 1.0..1.5s: nothing active.
  a.currentTime = 1.25;
  fireTimeUpdate();
  ok(countActive() === 0, 'pause gap must have no active spans, got ' + countActive());
  // Exact entry boundary: first entry end == pause; second starts at 1.5s.
  a.currentTime = 1.5;
  fireTimeUpdate();
  ok(!entries[0].classList.contains('active') && entries[1].classList.contains('active'), 'entry boundary half-open (only second entry)');
  // End of timeline (2.0s == frames): nothing active.
  a.currentTime = 2.0;
  fireTimeUpdate();
  ok(countActive() === 0, 'timeline end must have no active spans');
  // Unverified clears even when timeupdate fires.
  var wrong = makeFile('wrong.mp3', 'other-bytes', { type: 'audio/mpeg' });
  fireChange(document.getElementById('audio-file'), wrong);
  await flush();
  a.currentTime = 0.25;
  fireTimeUpdate();
  ok(countActive() === 0, 'unverified must have no active spans');
  console.log('halfopen-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "halfopen-ok")

    def test_entry_section_bookmark_keyboard_and_click_gating(self):
        scenario = r"""
async function main() {
  var audioBytes = 'kbd-bytes';
  var doc = makeSidecar(audioBytes, { includes_text: true });
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  var a = document.getElementById('audio');
  var entries = getEntries();
  ok(entries[0].tabIndex === 0, 'entry div must be focusable (tabIndex 0)');
  a.currentTime = 5;
  entries[0].onclick();
  ok(Math.abs(a.currentTime - 0) < 1e-9, 'entry click seeks to entry start, got ' + a.currentTime);
  a.currentTime = 5;
  ok(typeof entries[0].onkeydown === 'function', 'entry must register a keydown handler');
  entries[0].onkeydown({ key: 'Enter', preventDefault: function() {} });
  ok(Math.abs(a.currentTime - 0) < 1e-9, 'entry Enter seeks, got ' + a.currentTime);
  a.currentTime = 5;
  entries[0].onkeydown({ key: ' ', preventDefault: function() {} });
  ok(Math.abs(a.currentTime - 0) < 1e-9, 'entry Space seeks, got ' + a.currentTime);
  a.currentTime = 7;
  entries[0].onkeydown({ key: 'ArrowLeft', preventDefault: function() {} });
  ok(a.currentTime === 7, 'entry unrelated key must not seek');
  // Section button: standard button element, click seeks.
  var secs = getSections();
  ok(secs[0].tag === 'button' && secs[0].type === 'button', 'section control must be a standard button');
  a.currentTime = 9;
  secs[0].onclick();
  ok(Math.abs(a.currentTime - 0) < 1e-9, 'section click seeks, got ' + a.currentTime);
  // Bookmark button: standard button, click seeks.
  var bmBtns = getBookmarkButtons();
  ok(bmBtns[0].tag === 'button' && bmBtns[0].type === 'button', 'bookmark control must be a standard button');
  a.currentTime = 8;
  bmBtns[0].onclick();
  ok(Math.abs(a.currentTime - 0) < 1e-9, 'bookmark click seeks, got ' + a.currentTime);
  // Gated when unverified: replace with wrong audio.
  var wrong = makeFile('wrong.mp3', 'wrong-bytes', { type: 'audio/mpeg' });
  fireChange(document.getElementById('audio-file'), wrong);
  await flush();
  a.currentTime = 3.25;
  entries[0].onkeydown({ key: 'Enter', preventDefault: function() {} });
  ok(a.currentTime === 3.25, 'entry keyboard gated when unverified');
  secs[0].onclick();
  ok(a.currentTime === 3.25, 'section gated when unverified');
  bmBtns[0].onclick();
  ok(a.currentTime === 3.25, 'bookmark gated when unverified');
  console.log('kbd-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "kbd-ok")

    def test_bookmark_document_schema_binding_and_atomic(self):
        scenario = r"""
async function main() {
  var audioBytes = 'bm-doc-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  ok(getBookmarkButtons().length === 1, 'one sidecar bookmark initially');
  function tryImport(obj, label) {
    var f = makeFile('bm.json', JSON.stringify(obj), { type: 'application/json' });
    fireChange(document.getElementById('bm-file'), f);
    return flush();
  }
  var goodBinding = doc.binding;
  // Missing schema / version.
  await tryImport({ bookmarks: [], audio_sha256: doc.binding.audio_sha256 }, 'old-shape');
  ok(getBookmarkButtons().length === 1, 'old-shape import must be rejected atomically');
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 2, binding: goodBinding, bookmarks: [] }, 'v2');
  ok(getBookmarkButtons().length === 1, 'version 2 must be rejected');
  // Wrong audio (same text different export) and changed timeline.
  var wrongBinding = JSON.parse(JSON.stringify(goodBinding));
  wrongBinding.audio_sha256 = 'f'.repeat(64);
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: wrongBinding, bookmarks: [{ id: 'x1', label: 'x', start_frame: 0, end_frame: 0, section: null }] }, 'wrong-audio');
  ok(getBookmarkButtons().length === 1, 'wrong-audio import must be rejected');
  ok(document.getElementById('notice').textContent.length > 0, 'rejection must be visible');
  var movedBinding = JSON.parse(JSON.stringify(goodBinding));
  movedBinding.frames = 99999;
  movedBinding.sample_rate = 48000;
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: movedBinding, bookmarks: [{ id: 'x1', label: 'x', start_frame: 0, end_frame: 0, section: null }] }, 'moved-timeline');
  ok(getBookmarkButtons().length === 1, 'changed-timeline import must be rejected');
  var bytesBinding = JSON.parse(JSON.stringify(goodBinding));
  bytesBinding.audio_bytes = goodBinding.audio_bytes + 1;
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: bytesBinding, bookmarks: [{ id: 'x1', label: 'x', start_frame: 0, end_frame: 0, section: null }] }, 'bytes');
  ok(getBookmarkButtons().length === 1, 'audio_bytes mismatch must be rejected');
  // Duplicate ids / bad frames / bad section / oversize id+label / count.
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [
    { id: 'dup', label: 'a', start_frame: 0, end_frame: 0, section: null },
    { id: 'dup', label: 'b', start_frame: 10, end_frame: 10, section: null }
  ] }, 'dup');
  ok(getBookmarkButtons().length === 1, 'duplicate ids must be rejected atomically');
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [
    { id: 'badf', label: 'a', start_frame: 0, end_frame: 48001, section: null }
  ] }, 'badf');
  ok(getBookmarkButtons().length === 1, 'out-of-timeline frames must be rejected');
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [
    { id: 'bads', label: 'a', start_frame: 0, end_frame: 0, section: 'nope' }
  ] }, 'bads');
  ok(getBookmarkButtons().length === 1, 'unknown section ref must be rejected');
  var longId = 'x'.repeat(65);
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [
    { id: longId, label: 'a', start_frame: 0, end_frame: 0, section: null }
  ] }, 'longid');
  ok(getBookmarkButtons().length === 1, 'oversize id must be rejected');
  var longLabel = 'y'.repeat(501);
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [
    { id: 'okl', label: longLabel, start_frame: 0, end_frame: 0, section: null }
  ] }, 'longlabel');
  ok(getBookmarkButtons().length === 1, 'oversize label must be rejected');
  var many = [];
  for (var i = 0; i < 10001; i++) { many.push({ id: 'bm-many-' + i, label: 'm', start_frame: 0, end_frame: 0, section: null }); }
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: many }, 'many');
  ok(getBookmarkButtons().length === 1, 'over-count list must be rejected');
  // Forbidden field.
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [], audio_path: '/secret/book.mp3' }, 'forbidden');
  ok(getBookmarkButtons().length === 1, 'forbidden field must be rejected');
  // Valid import including a malicious label renders safely via textContent.
  var evilLabel = '<img src=x onerror=alert(1)>';
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [
    { id: 'bm-imp-1', label: evilLabel, start_frame: 4800, end_frame: 4800, section: 'sec-0001' },
    { id: 'bm-imp-2', label: 'Plain', start_frame: 9600, end_frame: 9600, section: null, unknown: '<script>bad</script>' }
  ] }, 'evil');
  ok(getBookmarkButtons().length === 2, 'valid import with unknown fields must succeed');
  ok(getBookmarkButtons()[0].textContent === evilLabel, 'malicious label must be rendered as text, got: ' + getBookmarkButtons()[0].textContent);
  ok(window.AA2Readalong.isVerified() === true, 'valid import must preserve verified pair');
  // Empty list clears intentionally.
  await tryImport({ schema: 'aa2-readalong-bookmarks', version: 1, binding: goodBinding, bookmarks: [] }, 'empty');
  ok(getBookmarkButtons().length === 0, 'valid empty list must clear bookmarks');
  ok(window.AA2Readalong.isVerified() === true, 'empty import must preserve verified pair');
  console.log('bm-doc-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "bm-doc-ok")

    def test_bookmark_import_preserves_verified_and_invalid_preserves_previous(self):
        scenario = r"""
async function main() {
  var audioBytes = 'bm-preserve-bytes';
  var doc = makeSidecar(audioBytes, { includes_text: true });
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  var a = document.getElementById('audio');
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'highlight before bookmark work');
  // Add a custom bookmark.
  document.getElementById('add-bookmark').onclick();
  ok(getBookmarkButtons().length === 2, 'custom bookmark added');
  // Valid import replaces the list but keeps verified + marks.
  var bmDoc = {
    schema: 'aa2-readalong-bookmarks', version: 1, binding: doc.binding,
    bookmarks: [{ id: 'bm-imp-9', label: 'Imported nine', start_frame: 12000, end_frame: 12000, section: null }]
  };
  fireChange(document.getElementById('bm-file'), makeFile('bm.json', JSON.stringify(bmDoc), { type: 'application/json' }));
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'valid import preserves verified');
  ok(getBookmarkButtons().length === 1 && getBookmarkButtons()[0].textContent === 'Imported nine', 'valid import replaces list');
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'marks still work after valid import');
  // Invalid import preserves the previous list and the verified pair.
  var badDoc = { schema: 'aa2-readalong-bookmarks', version: 1, binding: doc.binding,
    bookmarks: [{ id: 'bad', label: 'bad', start_frame: -1, end_frame: 0, section: null }] };
  fireChange(document.getElementById('bm-file'), makeFile('bm.json', JSON.stringify(badDoc), { type: 'application/json' }));
  await flush();
  ok(getBookmarkButtons().length === 1 && getBookmarkButtons()[0].textContent === 'Imported nine', 'invalid import preserves previous bookmarks');
  ok(window.AA2Readalong.isVerified() === true, 'invalid import preserves verified');
  a.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() >= 1, 'marks still work after invalid import');
  // Malformed JSON also preserves.
  fireChange(document.getElementById('bm-file'), makeFile('bm.json', '{oops', { type: 'application/json' }));
  await flush();
  ok(getBookmarkButtons().length === 1, 'malformed bookmark file preserves previous');
  ok(window.AA2Readalong.isVerified() === true, 'malformed bookmark file preserves verified');
  console.log('bm-preserve-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "bm-preserve-ok")

    def test_add_bookmark_clamps_and_export_roundtrip(self):
        scenario = r"""
async function main() {
  var audioBytes = 'bm-export-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  var a = document.getElementById('audio');
  a.currentTime = 10; // MP3 codec padding: beyond the 2.0s PCM timeline.
  document.getElementById('add-bookmark').onclick();
  ok(getBookmarkButtons().length === 2, 'bookmark added with clamped time');
  // Export: payload must be a portable bookmark document, blob captured.
  var keysBefore = Object.keys(_blobUrls).length;
  document.getElementById('save-bookmarks').onclick();
  var keys = Object.keys(_blobUrls);
  ok(keys.length === keysBefore + 1, 'export creates one blob');
  var exportUrl = keys[keys.length - 1];
  ok(_revokedUrls.indexOf(exportUrl) < 0, 'export URL must not be revoked synchronously');
  var blob = _blobUrls[exportUrl];
  ok(blob && typeof blob.parts[0] === 'string', 'export blob holds JSON text');
  var payload = JSON.parse(blob.parts[0]);
  ok(payload.schema === 'aa2-readalong-bookmarks', 'export schema, got ' + payload.schema);
  ok(payload.version === 1, 'export version');
  ok(payload.binding && payload.binding.audio_sha256 === doc.binding.audio_sha256, 'export binds audio hash');
  ok(payload.binding.sample_rate === 24000 && payload.binding.frames === 48000, 'export binds timeline');
  ok(Array.isArray(payload.bookmarks) && payload.bookmarks.length === 2, 'export has both bookmarks');
  for (var i = 0; i < payload.bookmarks.length; i++) {
    ok(payload.bookmarks[i].start_frame >= 0 && payload.bookmarks[i].end_frame >= payload.bookmarks[i].start_frame && payload.bookmarks[i].end_frame <= 48000, 'exported bookmark in timeline, got ' + JSON.stringify(payload.bookmarks[i]));
  }
  var clamped = payload.bookmarks[1];
  ok(clamped.start_frame === 48000, 'padding time must clamp to timeline end, got ' + clamped.start_frame);
  // Roundtrip: clear via empty import, then re-import the export.
  fireChange(document.getElementById('bm-file'),
    makeFile('bm.json', JSON.stringify({ schema: 'aa2-readalong-bookmarks', version: 1, binding: doc.binding, bookmarks: [] }), { type: 'application/json' }));
  await flush();
  ok(getBookmarkButtons().length === 0, 'empty import clears');
  fireChange(document.getElementById('bm-file'), makeFile('bm.json', blob.parts[0], { type: 'application/json' }));
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'roundtrip import preserves verified');
  ok(getBookmarkButtons().length === 2, 'roundtrip restores bookmarks');
  _flushTimers();
  ok(_revokedUrls.indexOf(exportUrl) >= 0, 'export URL revoked after the deferred timer');
  console.log('bm-export-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "bm-export-ok")

    def test_webcrypto_unavailable_and_hash_failure_unverified(self):
        scenario = r"""
async function main() {
  var audioBytes = 'nocrypto-bytes';
  var doc = makeSidecar(audioBytes, {});
  var aEl = document.getElementById('audio');
  aEl.duration = 2;
  window.crypto = undefined;
  global.crypto = undefined;
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(doc), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), sidecarFile);
  await flush();
  var audioFile = makeFile('audiobook.mp3', audioBytes, { type: 'audio/mpeg' });
  fireChange(document.getElementById('audio-file'), audioFile);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'no WebCrypto must stay unverified');
  ok(typeof aEl.src === 'string' && aEl.src.length > 0, 'generic playback allowed without WebCrypto');
  ok(document.getElementById('notice').textContent.indexOf('unavailable') >= 0 || document.getElementById('audio-status').textContent.indexOf('unavailable') >= 0, 'unverified status must be truthful, notice=' + document.getElementById('notice').textContent + ' status=' + document.getElementById('audio-status').textContent);
  aEl.currentTime = 0.5;
  fireTimeUpdate();
  ok(countActive() === 0, 'no highlights without verification');
  var entries = getEntries();
  entries[0].onclick();
  ok(aEl.currentTime === 0.5, 'entry seek gated without WebCrypto');
  // Generic playback still works.
  aEl.paused = true;
  document.getElementById('play-pause').onclick();
  ok(aEl.paused === false, 'generic play works without WebCrypto');
  console.log('nocrypto-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "nocrypto-ok")

    def test_hash_failure_unverified_but_playable(self):
        scenario = r"""
async function main() {
  var audioBytes = 'hashfail-bytes';
  var doc = makeSidecar(audioBytes, {});
  var aEl = document.getElementById('audio');
  aEl.duration = 2;
  var sidecarFile = makeFile('sidecar.json', JSON.stringify(doc), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), sidecarFile);
  await flush();
  var badAudio = makeFile('audiobook.mp3', audioBytes, { type: 'audio/mpeg', failBuf: true });
  fireChange(document.getElementById('audio-file'), badAudio);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'hash failure must stay unverified');
  ok(aEl.src.length > 0, 'generic playback allowed after hash failure');
  ok(getEntries().length === 2, 'sidecar markup preserved after hash failure');
  ok(countActive() === 0, 'no highlights after hash failure');
  console.log('hashfail-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "hashfail-ok")

    def test_file_read_failures_are_handled(self):
        scenario = r"""
async function main() {
  var audioBytes = 'readfail-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  // Sidecar text() rejection clears the previous sidecar.
  var failSidecar = makeFile('s.json', '{}', { type: 'application/json', failText: true });
  fireChange(document.getElementById('sidecar-file'), failSidecar);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'sidecar read failure unverified');
  ok(getEntries().length === 0, 'sidecar read failure clears entries');
  ok(getBookmarkButtons().length === 0, 'sidecar read failure clears bookmarks');
  ok(document.getElementById('notice').textContent.length > 0, 'sidecar read failure is visible');
  ok(document.getElementById('audio').src.length > 0, 'audio survives sidecar read failure');
  // Restore a good pair, then bookmark text() rejection preserves.
  await loadPair(audioBytes, doc, 'audio-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair re-verifies');
  var failBm = makeFile('bm.json', '{}', { type: 'application/json', failText: true });
  var before = getBookmarkButtons().length;
  fireChange(document.getElementById('bm-file'), failBm);
  await flush();
  ok(getBookmarkButtons().length === before, 'bookmark read failure preserves previous bookmarks');
  ok(window.AA2Readalong.isVerified() === true, 'bookmark read failure preserves verified');
  console.log('readfail-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "readfail-ok")

    def test_unicode_word_spans_codepoints(self):
        scenario = r"""
async function main() {
  var audioBytes = 'unicode-bytes';
  var text = 'A\u{1F600}B e\u0301lan';
  var doc = makeSidecar(audioBytes, {
    includes_text: true,
    entries: [
      { index: 0, section: 'sec-0001', speaker: null, text: text, start_frame: 0, end_frame: 24000, pause_frames_after: 0, status: 'aligned-estimated',
        words: [
          { from: 0, to: 1, start_frame: 0, end_frame: 8000 },
          { from: 1, to: 2, start_frame: 8000, end_frame: 16000 },
          { from: 2, to: 9, start_frame: 16000, end_frame: 24000 }
        ] },
      { index: 1, section: 'sec-0001', speaker: null, text: 'Done.', start_frame: 24000, end_frame: 48000, pause_frames_after: 0, status: 'missing', words: null }
    ]
  });
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'unicode pair verifies');
  var entries = getEntries();
  var words = entryWords(entries[0]);
  ok(words.length === 3, 'three word spans render, got ' + words.length);
  ok(words[0].textContent === 'A', 'word 0 must be A, got ' + JSON.stringify(words[0].textContent));
  ok(words[1].textContent === '\u{1F600}', 'word 1 must be the astral emoji (codepoints), got ' + JSON.stringify(words[1].textContent));
  ok(words[2].textContent === 'B e\u0301lan', 'word 2 must render combining text, got ' + JSON.stringify(words[2].textContent));
  ok(entryText(entries[0]) === text, 'entry text reassembled must equal source');
  // Validator must use codepoint bounds: to=10 exceeds 9 codepoints even
  // though the UTF-16 length is 10.
  var bad = JSON.parse(JSON.stringify(doc));
  bad.entries[0].words[2] = { from: 2, to: 10, start_frame: 16000, end_frame: 24000 };
  ok(window.AA2Readalong.validateSidecar(bad).ok === false, 'word offset beyond codepoints must fail validation');
  console.log('unicode-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "unicode-ok")

    def test_unknown_metadata_safe_and_unsupported_version_fails_closed(self):
        scenario = r"""
async function main() {
  var audioBytes = 'unknown-bytes';
  var evilTitle = '<script>alert(1)</' + 'script>';
  var doc = makeSidecar(audioBytes, {
    includes_text: true,
    sectionTitle: evilTitle,
    extra: { x_note: '<b>unknown metadata</b>' }
  });
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'unknown metadata must not break verification');
  var secs = getSections();
  ok(secs[0].textContent === evilTitle, 'section title must render as text, got ' + secs[0].textContent);
  ok(getEntries().length === 2, 'entries render with unknown metadata');
  // Unsupported version fails closed and clears the previous sidecar.
  var bad = makeSidecar(audioBytes, {});
  bad.version = 2;
  var badFile = makeFile('s.json', JSON.stringify(bad), { type: 'application/json' });
  fireChange(document.getElementById('sidecar-file'), badFile);
  await flush();
  ok(window.AA2Readalong.isVerified() === false, 'unsupported version unverified');
  ok(getEntries().length === 0, 'unsupported version clears entries');
  ok(document.getElementById('notice').textContent.indexOf('validation') >= 0 || document.getElementById('notice').textContent.length > 0, 'unsupported version is visible');
  console.log('unknown-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "unknown-ok")

    def test_text_off_no_hidden_text_and_status_explicit(self):
        scenario = r"""
async function main() {
  var audioBytes = 'textoff-bytes';
  var doc = makeSidecar(audioBytes, { includes_text: false });
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'text-off pair verifies');
  var entries = getEntries();
  ok(entries.length === 2, 'entries render');
  var t0 = entryText(entries[0]);
  ok(t0.indexOf('Hello') < 0, 'text-off must not disclose book text, got ' + JSON.stringify(t0));
  ok(t0.indexOf('Line 1') >= 0, 'text-off placeholder names the line');
  ok(t0.indexOf('missing') >= 0, 'text-off placeholder keeps the timing status explicit');
  ok(entries[0].getAttribute('aria-label').indexOf('missing') >= 0, 'aria-label keeps status explicit');
  var bmBtns = getBookmarkButtons();
  ok(bmBtns[0].textContent === 'Section 0001', 'automatic bookmark labels must not contain book text');
  console.log('textoff-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "textoff-ok")

    def test_input_cancel_preserves_current_state(self):
        scenario = r"""
async function main() {
  var audioBytes = 'cancel-bytes';
  var doc = makeSidecar(audioBytes, {});
  await loadPair(audioBytes, doc, 'sidecar-first');
  ok(window.AA2Readalong.isVerified() === true, 'pair verifies');
  var a = document.getElementById('audio');
  var srcBefore = a.src;
  // A cancelled chooser fires change with no file: must preserve all.
  fireChange(document.getElementById('audio-file'), null);
  fireChange(document.getElementById('sidecar-file'), null);
  fireChange(document.getElementById('bm-file'), null);
  await flush();
  ok(window.AA2Readalong.isVerified() === true, 'cancel must preserve verified pair');
  ok(a.src === srcBefore, 'cancel must preserve audio src');
  ok(getEntries().length === 2, 'cancel must preserve entries');
  ok(getBookmarkButtons().length === 1, 'cancel must preserve bookmarks');
  console.log('cancel-ok');
}
main().catch(function(e) { console.error(e && e.stack || e); process.exit(1); });
"""
        self._check(scenario, "cancel-ok")

    def test_parent_bookmark_edge_controls(self):
        cases = {
            "limit": r"""
var bytes='bookmark-limit', doc=makeSidecar(bytes);
await loadPair(bytes,doc,'audio-first');
var list=Array.from({length:10000},function(_,i){return {id:'id-'+i,label:null,start_frame:0,end_frame:0,section:null};});
var payload={schema:'aa2-readalong-bookmarks',version:1,binding:doc.binding,bookmarks:list};
fireChange(document.getElementById('bm-file'),makeFile('bm.json',JSON.stringify(payload)));
await flush();
ok(getBookmarkButtons().length===10000,'precondition: full list imported');
document.getElementById('add-bookmark').onclick();
ok(getBookmarkButtons().length===10000,'add must refuse atomically at bookmark limit');
document.getElementById('save-bookmarks').onclick();
ok(_blobCounter>1,'full list must remain exportable');
""",
            "prototype": r"""
var bytes='bookmark-prototype',doc=makeSidecar(bytes);
await loadPair(bytes,doc,'audio-first');
var bad=JSON.parse(JSON.stringify(doc));bad.entries[0].status='toString';
ok(!window.AA2Readalong.validateSidecar(bad).ok,'prototype member is not a status');
bad=JSON.parse(JSON.stringify(doc));bad.entries[0].section='constructor';
ok(!window.AA2Readalong.validateSidecar(bad).ok,'prototype member is not a section');
var payload={schema:'aa2-readalong-bookmarks',version:1,binding:doc.binding,bookmarks:[{id:'constructor',label:null,start_frame:0,end_frame:0,section:null}]};
fireChange(document.getElementById('bm-file'),makeFile('bm.json',JSON.stringify(payload)));
await flush();
document.getElementById('save-bookmarks').onclick();
ok(_blobCounter>1,'valid prototype-named bookmark ID must round-trip');
""",
            "embedded": r"""
var doc=makeSidecar('embedded');
var variants=[{id:'x'.repeat(65)},{label:'x'.repeat(501)},{label:{bad:true}}];
for(var v of variants){var bad=JSON.parse(JSON.stringify(doc));Object.assign(bad.bookmarks[0],v);ok(!window.AA2Readalong.validateSidecar(bad).ok,'embedded bookmarks must obey export bounds');}
var bad=JSON.parse(JSON.stringify(doc));bad.bookmarks.push(bad.bookmarks[0]);
ok(!window.AA2Readalong.validateSidecar(bad).ok,'embedded duplicate bookmark IDs must fail');
""",
            "stale": r"""
var bytes='bookmark-stale',doc=makeSidecar(bytes);
await loadPair(bytes,doc,'audio-first');
var payload={schema:'aa2-readalong-bookmarks',version:1,binding:doc.binding,bookmarks:[{id:'old',label:'Old sidecar label',start_frame:0,end_frame:0,section:null}]};
var slow=makeFile('bm.json',JSON.stringify(payload),{deferredText:true});
fireChange(document.getElementById('bm-file'),slow);
var replacement=makeSidecar(bytes);replacement.bookmarks[0].label='New sidecar label';
fireChange(document.getElementById('sidecar-file'),makeFile('new.json',JSON.stringify(replacement)));
await flush();
slow._deferText.resolve(JSON.stringify(payload));await flush();
ok(getBookmarkButtons()[0].textContent==='New sidecar label','stale bookmark import must not overwrite replacement sidecar');
ok(window.AA2Readalong.isVerified(),'stale import must preserve current verified pair');
""",
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                self._check("async function main(){\n" + body + "\nconsole.log('parent-edge-ok');\n}\n"
                            "main().catch(function(e){console.error(e.stack||e);process.exit(1);});",
                            "parent-edge-ok")

    def test_local_only_no_external_scripts(self):
        # Structural guard only (cannot be observed from the DOM stub).
        # Handler behavior is proven by the real-handler tests above.
        player = Path(__file__).parent.parent / "static" / "readalong-player.html"
        html = player.read_text(encoding="utf-8")
        self.assertNotIn("<script src=", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org/1999/xhtml", ""))
        self.assertNotIn("https://", html.replace("https://www.w3.org/1999/xhtml", ""))
        self.assertIn('id="audio"', html)


if __name__ == "__main__":
    unittest.main()
