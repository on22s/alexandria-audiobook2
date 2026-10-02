"""Execute the project loader with the numeric seed zero."""

from pathlib import Path
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parent.parent / "static/js/app-workbench.js"


class DatasetSeedJsTests(unittest.TestCase):
    def test_project_loader_preserves_zero_and_clears_nullish_seeds(self):
        from tests.test_dataset_ui_ownership_js import DatasetUiOwnershipJsTests
        DatasetUiOwnershipJsTests().run_scenario(r'''
run("dsbCurrentProject = 'voice';");
for (const seed of [0, 12, null, undefined, '0', '']) {
    context.API.get = async () => ({global_seed: seed, samples: [{text: 'line', seed}]});
    await context.dsbLoadProject('voice');
    assert.strictEqual(String(elements['dsb-global-seed'].value),
                       seed === null || seed === undefined ? '' : String(seed));
    assert.strictEqual(run('dsbRows[0].seed'), seed === null || seed === undefined ? '' : seed);
}
''')

    def test_cancel_handler_scopes_the_encoded_current_project_and_ignores_no_selection(self):
        script = r'''
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('window.dsbCancel =');
const end = source.indexOf('window.dsbImport =', start);
assert(start >= 0 && end > start);
const calls = [];
const result = {status: 'fixture'};
const context = {window: {}, dsbCurrentProject: '', cancelTask(url) { calls.push(url); return result; }};
vm.runInNewContext(source.slice(start, end), context);
assert.strictEqual(context.window.dsbCancel(), undefined);
assert.strictEqual(calls.length, 0);
context.dsbCurrentProject = 'Voice + Café #?';
assert.strictEqual(context.window.dsbCancel(), result);
assert.deepStrictEqual(calls, ['/api/dataset_builder/cancel?name=' + encodeURIComponent(context.dsbCurrentProject)]);
'''
        result = subprocess.run(["node", "-e", script, str(SOURCE)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
