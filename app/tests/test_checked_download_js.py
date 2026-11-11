"""Read-along download links validate once and download from the response (#1906).

A plain <a download> turns a 409 refusal into Chrome's "Failed - Unknown
server error" and the reason is lost. The real app-core.js handler is run
under Node with stubbed fetch/showToast/URL/document.createElement; nothing is
re-implemented here. The handler must fetch once, check the status, and — on
success — download from that same response body instead of re-clicking the link
(which made the server run its heavy validation twice).
"""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'static'
MARKER = '// Export links the server may refuse'

HARNESS = r'''
const vm = require('vm');
const fs = require('fs');
const assert = require('assert');
const source = fs.readFileSync(process.argv[2], 'utf8');
const start = source.indexOf(%(marker)s);
const handlerSource = source.slice(start, source.indexOf('// --- Toast & Confirm utilities ---', start));

const OBJECT_URL = 'blob:http://test/object-url';
const BLOB = { kind: 'blob' };

function setup() {
    const state = {
        toasts: [], downloads: [], revoked: [], blobsRead: [], timers: [],
        fetchCalls: 0, blobCalls: 0, aborted: 0, nativeDownloads: 0, handler: null,
    };
    const link = {
        href: '/api/readaloud_epub', download: 'book.epub', dataset: {},
        // Models the browser: a synthetic click re-enters the listener and
        // the native download happens only if it is not prevented.
        click() {
            const ev = { target: { closest: () => link }, defaultPrevented: false,
                         preventDefault() { this.defaultPrevented = true; } };
            state.handler(ev);
            if (!ev.defaultPrevented) { state.nativeDownloads += 1; }
        },
    };
    const anchor = {
        href: '', download: '',
        click() { state.downloads.push({ href: this.href, download: this.download }); },
        remove() {},
    };
    const context = {
        document: {
            addEventListener: (name, fn) => { if (name === 'click') { state.handler = fn; } },
            createElement: (tag) => { assert.strictEqual(tag, 'a'); return anchor; },
            body: { appendChild: () => {} },
        },
        URL: {
            createObjectURL: (blob) => { state.blobsRead.push(blob); return OBJECT_URL; },
            revokeObjectURL: (url) => { state.revoked.push(url); },
        },
        fetch: async (url, options) => {
            state.fetchCalls += 1;
            const response = await context.fetchImpl(url, options);
            if (response && !response.headers) {
                response.headers = { get: (name) => name.toLowerCase() === 'content-length' ? String(context.length) : null };
            }
            return response;
        },
        length: 1000, isSecureContext: true,
        AbortController: class { constructor() { this.signal = {}; } abort() { state.aborted += 1; } },
        showToast: (message, type) => state.toasts.push([message, type]),
        t: (key, english, values = {}) => english.replace(/\{(\w+)\}/g, (_m, name) => values[name]),
        setTimeout: (fn) => { state.timers.push(fn); },
    };
    vm.createContext(context);
    vm.runInContext(handlerSource, context);
    const event = () => ({
        target: { closest: (selector) => selector === '[data-checked-download]' ? link : null },
        defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; },
    });
    return {
        context, state, event, link,
        flush() { state.timers.splice(0).forEach((fn) => fn()); },
    };
}

function snapshot(s, ev, extra) {
    return Object.assign({
        toasts: s.state.toasts,
        downloads: s.state.downloads,
        revoked: s.state.revoked,
        blobsRead: s.state.blobsRead,
        fetchCalls: s.state.fetchCalls,
        aborted: s.state.aborted,
        nativeDownloads: s.state.nativeDownloads,
        blobCalls: s.state.blobCalls,
        prevented: ev.defaultPrevented,
        leftMarked: 'downloadChecked' in s.link.dataset,
    }, extra || {});
}

// A 200 whose body must not be buffered: the check request is cancelled and
// the browser downloads the link itself.
async function nativeCase(configure) {
    const s = setup();
    configure(s.context);
    s.context.fetchImpl = async () => ({ ok: true, status: 200,
        blob: async () => { s.state.blobCalls += 1; return BLOB; } });
    const ev = s.event();
    await s.state.handler(ev);
    return snapshot(s, ev);
}

(async () => {
    // (a) 409 with a detail: refusal toast, no download, blob never read.
    const refused = setup();
    refused.context.fetchImpl = async () => ({ ok: false, status: 409,
        json: async () => ({ detail: 'EPUB read-aloud timing settings changed; rebuild it.' }),
        blob: async () => { refused.state.blobCalls += 1; return BLOB; } });
    const evRefused = refused.event();
    await refused.state.handler(evRefused);

    // (b) 200: one fetch, one download from the response, object URL revoked.
    const ok = setup();
    ok.context.fetchImpl = async () => ({ ok: true, status: 200,
        blob: async () => { ok.state.blobCalls += 1; return BLOB; } });
    const evOk = ok.event();
    await ok.state.handler(evOk);
    ok.flush();

    // Fallback filename: no download attribute, use the last path segment.
    const fallback = setup();
    fallback.link.download = '';
    fallback.context.fetchImpl = async () => ({ ok: true, status: 200,
        blob: async () => { fallback.state.blobCalls += 1; return BLOB; } });
    const evFallback = fallback.event();
    await fallback.state.handler(evFallback);
    fallback.flush();

    // (c) fetch rejects: unreachable toast.
    const down = setup();
    down.context.fetchImpl = async () => { throw new TypeError('Failed to fetch'); };
    const evDown = down.event();
    await down.state.handler(evDown);

    // (d) non-JSON 500: status toast.
    const noJson = setup();
    noJson.context.fetchImpl = async () => ({ ok: false, status: 500,
        json: async () => { throw new Error('html'); } });
    const evNoJson = noJson.event();
    await noJson.state.handler(evNoJson);

    // (e) response.blob() rejects: unreachable toast, no download.
    const blobRejects = setup();
    blobRejects.context.fetchImpl = async () => ({ ok: true, status: 200,
        blob: async () => { blobRejects.state.blobCalls += 1; throw new Error('dropped'); } });
    const evBlobRejects = blobRejects.event();
    await blobRejects.state.handler(evBlobRejects);

    // (f) a second click while the first is pending makes no second fetch.
    const double = setup();
    let resolveFetch;
    double.context.fetchImpl = () => new Promise((resolve) => { resolveFetch = resolve; });
    const evDouble = double.event();
    const first = double.state.handler(evDouble);
    const markedDuringFlight = 'downloadChecked' in double.link.dataset;
    const evDouble2 = double.event();
    await double.state.handler(evDouble2);
    const secondPrevented = evDouble2.defaultPrevented;
    resolveFetch({ ok: true, status: 200,
        blob: async () => { double.state.blobCalls += 1; return BLOB; } });
    await first;
    double.flush();

    console.log(JSON.stringify({
        refused: snapshot(refused, evRefused),
        ok: snapshot(ok, evOk),
        fallback: snapshot(fallback, evFallback),
        down: snapshot(down, evDown),
        noJson: snapshot(noJson, evNoJson),
        blobRejects: snapshot(blobRejects, evBlobRejects),
        double: snapshot(double, evDouble, { markedDuringFlight, secondPrevented }),
        big: await nativeCase((c) => { c.length = 300 * 1024 * 1024; }),
        noLength: await nativeCase((c) => { c.length = ''; }),
        insecure: await nativeCase((c) => { c.isSecureContext = false; }),
    }));
})().catch(error => { console.error(error); process.exit(1); });
'''


class CheckedDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as handle:
            handle.write(HARNESS % {'marker': json.dumps(MARKER)})
        result = subprocess.run(['node', handle.name, str(STATIC / 'js' / 'app-core.js')],
                                capture_output=True, text=True, encoding='utf-8', timeout=60)
        Path(handle.name).unlink()
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.results = json.loads(result.stdout)

    def test_refusal_shows_the_server_reason_and_does_not_download(self):
        refused = self.results['refused']
        self.assertTrue(refused['prevented'])
        self.assertEqual(1, refused['fetchCalls'])
        self.assertEqual([], refused['downloads'])
        self.assertEqual(0, refused['blobCalls'])
        self.assertEqual([], refused['blobsRead'])
        self.assertEqual(['Download refused: EPUB read-aloud timing settings changed; rebuild it.',
                          'error'], refused['toasts'][-1])

    def test_success_downloads_once_from_the_response_body(self):
        ok = self.results['ok']
        self.assertTrue(ok['prevented'])
        self.assertEqual(1, ok['fetchCalls'])
        self.assertEqual(1, ok['blobCalls'])
        self.assertEqual([{'href': 'blob:http://test/object-url', 'download': 'book.epub'}],
                         ok['downloads'])
        self.assertEqual(['blob:http://test/object-url'], ok['revoked'])
        self.assertEqual(['Preparing download…', 'info'], ok['toasts'][0])
        self.assertFalse(ok['leftMarked'])

    def test_success_falls_back_to_the_last_path_segment_for_the_filename(self):
        fallback = self.results['fallback']
        self.assertEqual(1, fallback['fetchCalls'])
        self.assertEqual([{'href': 'blob:http://test/object-url', 'download': 'readaloud_epub'}],
                         fallback['downloads'])

    def test_unreachable_server_says_so(self):
        down = self.results['down']
        self.assertEqual(1, down['fetchCalls'])
        self.assertEqual([], down['downloads'])
        self.assertIn('did not respond', down['toasts'][-1][0])

    def test_non_json_error_falls_back_to_the_status(self):
        self.assertEqual('Download refused by the server (status 500).',
                         self.results['noJson']['toasts'][-1][0])

    def test_blob_read_failure_reports_unreachable_and_does_not_download(self):
        blobRejects = self.results['blobRejects']
        self.assertEqual([], blobRejects['downloads'])
        self.assertEqual([], blobRejects['blobsRead'])
        self.assertIn('did not respond', blobRejects['toasts'][-1][0])

    def test_a_second_click_while_pending_makes_no_second_fetch(self):
        double = self.results['double']
        self.assertTrue(double['markedDuringFlight'])
        # The guarded click is still prevented, so the browser does not start
        # its own download (and a second server validation) meanwhile.
        self.assertTrue(double['secondPrevented'])
        self.assertEqual(0, double['nativeDownloads'])
        self.assertEqual(1, double['fetchCalls'])
        self.assertEqual(1, len(double['downloads']))
        self.assertFalse(double['leftMarked'])

    def test_success_buffers_nothing_else(self):
        ok = self.results['ok']
        self.assertEqual(0, ok['aborted'])
        self.assertEqual(0, ok['nativeDownloads'])

    def test_large_unknown_or_insecure_bodies_use_the_browser_download(self):
        # Over the in-memory limit, no Content-Length, or a plain-HTTP LAN
        # page: one check request, cancelled, then exactly one native download
        # of the link; the body is never read into a blob.
        for name in ('big', 'noLength', 'insecure'):
            with self.subTest(case=name):
                result = self.results[name]
                self.assertEqual(1, result['fetchCalls'])
                self.assertEqual(1, result['aborted'])
                self.assertEqual(0, result['blobCalls'])
                self.assertEqual([], result['downloads'])
                self.assertEqual(1, result['nativeDownloads'])
                self.assertFalse(result['leftMarked'])

    def test_every_read_along_export_link_is_checked(self):
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        for path in ('/api/readalong/sidecar', '/api/readalong/sidecar/audio',
                     '/api/readalong/player', '/api/readaloud_epub', '/api/readaloud_epub/audio'):
            with self.subTest(path=path):
                tag = re.search(r'<a [^>]*href="%s"[^>]*>' % re.escape(path), html).group(0)
                self.assertIn('data-checked-download', tag)


if __name__ == '__main__':
    unittest.main()
