"""Read-along download links show the server's refusal (#1906).

A plain <a download> turns a 409 refusal into Chrome's "Failed - Unknown
server error" and the reason is lost. The real app-core.js handler is run
under Node with stubbed fetch/showToast; nothing is re-implemented here.
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

async function run(fetchImpl) {
    const toasts = [];
    const saved = [];
    const revoked = [];
    let handler = null;
    let fetches = 0;
    const link = {href: '/api/readaloud_epub', getAttribute: (name) => name === 'download' ? 'readaloud.epub' : null};
    const context = {
        document: {
            addEventListener: (name, fn) => { if (name === 'click') { handler = fn; } },
            createElement: () => ({click() { saved.push([this.href, this.download]); }}),
        },
        fetch: async (...args) => { fetches += 1; return fetchImpl(...args); },
        URL: {createObjectURL: (blob) => 'blob:' + blob.body, revokeObjectURL: (url) => revoked.push(url)},
        setTimeout: (fn) => fn(),
        showToast: (message, type) => toasts.push([message, type]),
        t: (key, english, values = {}) => english.replace(/\{(\w+)\}/g, (_m, name) => values[name]),
    };
    vm.createContext(context);
    vm.runInContext(handlerSource, context);
    const event = {target: {closest: (selector) => selector === '[data-checked-download]' ? link : null},
                   defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    await handler(event);
    return {toasts, saved, revoked, fetches, prevented: event.defaultPrevented};
}

(async () => {
    const refused = await run(async () => ({ok: false, status: 409,
        json: async () => ({detail: 'EPUB read-aloud timing settings changed; rebuild it.'})}));
    const ok = await run(async () => ({ok: true, status: 200, blob: async () => ({body: 'epub-bytes'})}));
    const down = await run(async () => { throw new TypeError('Failed to fetch'); });
    const noJson = await run(async () => ({ok: false, status: 500, json: async () => { throw new Error('html'); }}));
    console.log(JSON.stringify({refused, ok, down, noJson}));
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
        self.assertEqual([], refused['saved'])
        self.assertEqual([['Download refused: EPUB read-aloud timing settings changed; rebuild it.',
                           'error']], refused['toasts'])

    def test_success_saves_the_one_response_without_a_second_request(self):
        ok = self.results['ok']
        self.assertEqual([], ok['toasts'])
        self.assertEqual(1, ok['fetches'])
        self.assertEqual([['blob:epub-bytes', 'readaloud.epub']], ok['saved'])
        self.assertEqual(['blob:epub-bytes'], ok['revoked'])

    def test_refusal_and_outage_make_one_request_and_save_nothing(self):
        for name in ('refused', 'down', 'noJson'):
            with self.subTest(name=name):
                self.assertEqual(1, self.results[name]['fetches'])
                self.assertEqual([], self.results[name]['saved'])

    def test_unreachable_server_says_so(self):
        down = self.results['down']
        self.assertEqual([], down['saved'])
        self.assertIn('did not respond', down['toasts'][0][0])

    def test_non_json_error_falls_back_to_the_status(self):
        self.assertEqual('Download refused by the server (status 500).', self.results['noJson']['toasts'][0][0])

    def test_every_read_along_export_link_is_checked(self):
        html = (STATIC / 'index.html').read_text(encoding='utf-8')
        for path in ('/api/readalong/sidecar', '/api/readalong/sidecar/audio',
                     '/api/readalong/player', '/api/readaloud_epub', '/api/readaloud_epub/audio'):
            with self.subTest(path=path):
                tag = re.search(r'<a [^>]*href="%s"[^>]*>' % re.escape(path), html).group(0)
                self.assertIn('data-checked-download', tag)


if __name__ == '__main__':
    unittest.main()
