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
    const aborted = [];
    let handler = null;
    let downloads = 0;
    const link = {
        href: '/api/readaloud_epub', dataset: {},
        click() {
            // A synthetic click re-enters the listener; it must pass through.
            const event = {target: {closest: () => link}, defaultPrevented: false,
                           preventDefault() { this.defaultPrevented = true; }};
            handler(event);
            if (!event.defaultPrevented) { downloads += 1; }
        },
    };
    class FakeAbortController {
        constructor() { this.signal = {}; }
        abort() { aborted.push(true); }
    }
    const context = {
        document: {addEventListener: (name, fn) => { if (name === 'click') { handler = fn; } }},
        fetch: fetchImpl, AbortController: FakeAbortController,
        showToast: (message, type) => toasts.push([message, type]),
        t: (key, english, values = {}) => english.replace(/\{(\w+)\}/g, (_m, name) => values[name]),
    };
    vm.createContext(context);
    vm.runInContext(handlerSource, context);
    const event = {target: {closest: (selector) => selector === '[data-checked-download]' ? link : null},
                   defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
    await handler(event);
    return {toasts, aborted: aborted.length, downloads, prevented: event.defaultPrevented,
            leftMarked: 'downloadChecked' in link.dataset};
}

(async () => {
    const refused = await run(async () => ({ok: false, status: 409,
        json: async () => ({detail: 'EPUB read-aloud timing settings changed; rebuild it.'})}));
    const ok = await run(async () => ({ok: true, status: 200}));
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
        self.assertEqual(0, refused['downloads'])
        self.assertEqual([['Download refused: EPUB read-aloud timing settings changed; rebuild it.',
                           'error']], refused['toasts'])

    def test_success_cancels_the_probe_and_lets_the_browser_download(self):
        ok = self.results['ok']
        self.assertEqual([], ok['toasts'])
        self.assertEqual(1, ok['aborted'])
        self.assertEqual(1, ok['downloads'])
        self.assertFalse(ok['leftMarked'])

    def test_unreachable_server_says_so(self):
        down = self.results['down']
        self.assertEqual(0, down['downloads'])
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
