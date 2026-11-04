from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from docx_retention import get_retained_docx_inventory
from tests.test_ui_help_js import DOM_FIXTURE


class RetentionTests(unittest.TestCase):
    def test_complete_partial_invalid_and_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, suffixes in enumerate((('.docx', '.txt', '.docx-receipt.json'),
                                               ('.docx',), ('.docx', '.docx-receipt.json'))):
                for suffix in suffixes:
                    (root / ('book-aa2docx-' + str(number) * 32 + suffix)).write_bytes(b'invalid content')
            (root / 'bad-aa2docx-marker.txt').write_bytes(b'private')
            (root / ('changed-aa2docx-' + 'a' * 32 + '.docx')).mkdir()
            before = sorted(item.name for item in root.iterdir())
            with patch('builtins.open', side_effect=AssertionError('No contents read')):
                result = get_retained_docx_inventory(root, 1000)
            self.assertEqual(before, sorted(item.name for item in root.iterdir()))
            self.assertCountEqual(['unverified', 'incomplete/unverified', 'incomplete/unverified',
                                   'unavailable', 'unavailable'], [g['state'] for g in result['groups']])
            self.assertEqual(6 * len(b'invalid content') + 7, result['observed_named_bytes'])
            self.assertNotIn('private', str(result))
            self.assertTrue(result['scan_complete'])

    def test_limits_and_missing_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number in range(4):
                (root / ('book-aa2docx-' + str(number) * 32 + '.docx')).touch()
            result = get_retained_docx_inventory(root, 1000, scan_limit=3, group_limit=1)
            self.assertEqual(1, len(result['groups']))
            self.assertEqual(2, result['omitted_marker_entries'])
            self.assertFalse(result['scan_complete'])
            result = get_retained_docx_inventory(root / 'missing', 1000)
            self.assertFalse(result['scan_complete'])
            self.assertTrue(result['unavailable_entries'])
            self.assertFalse((root / 'missing').exists())

    def test_configured_endpoint(self):
        from routers import script
        with tempfile.TemporaryDirectory() as directory, patch.object(script, 'UPLOADS_DIR', directory):
            result = script.get_docx_retained_uploads()
            self.assertEqual(str(Path(directory).absolute()), result['location'])
            self.assertEqual(script.MAX_UPLOAD_STORAGE_BYTES, result['quota_limit_bytes'])

    def test_ui_plain_text_only(self):
        source = Path(__file__).resolve().parent.parent / 'static/js/app-core.js'
        code = DOM_FIXTURE + r'''
const source=fs.readFileSync(process.argv[1],'utf8');
const details=new Element('pre',{id:'docx-retained-details'});body.appendChild(details);
Object.defineProperty(details,'innerHTML',{set(){throw Error('HTML sink');}});
let calls=0;
context.API={get:async(url)=>{assert.equal(url,'/api/uploads/docx-retained');calls++;return {location:'<img onerror=boom>',groups:[]};}};
vm.runInContext(source.slice(source.indexOf('window.openRetainedDocxDetails ='),source.indexOf('let docxPreviewCandidate =')),context);
assert.equal(calls,0);
(async()=>{await context.window.openRetainedDocxDetails();assert.equal(calls,1);assert(details.textContent.includes('<img onerror=boom>'));assert.equal(details.hidden,false);})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
