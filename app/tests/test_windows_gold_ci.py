from pathlib import Path
import unittest

from tests.test_windows_native_ci import _job_lines, _key_block, _step, _unquote


STEP_NAME = 'Verify materialized Git gold links and repeat scoring on Windows'
COMMAND = ('python -m unittest tests.test_legacy_attribution_audit '
           'tests.test_pipeline_repeat_scoring -v')


class WindowsGoldCiTests(unittest.TestCase):
    def assert_windows_gold_gate(self, workflow):
        job = _job_lines(workflow, 'windows-owner')
        self.assertIn('    runs-on: windows-latest', job)
        checkout = [index for index, line in enumerate(job)
                    if line.strip().startswith('- uses: actions/checkout@')]
        self.assertEqual(1, len(checkout))
        following = job[checkout[0] + 1:]
        stop = next((index for index, line in enumerate(following)
                     if line.startswith('      - ')), len(following))
        _, options = _key_block(following[:stop], 8, 'with')
        depth, _ = _key_block(options, 10, 'fetch-depth')
        self.assertEqual('0', _unquote(depth))
        _, defaults = _key_block(job, 4, 'defaults')
        _, run_defaults = _key_block(defaults, 6, 'run')
        directory, _ = _key_block(run_defaults, 8, 'working-directory')
        self.assertEqual('app', _unquote(directory))
        step = _step(job, STEP_NAME)
        self.assertEqual('app', step.get('working-directory', 'app'))
        self.assertEqual(step.get('run'), COMMAND)
        self.assertNotIn('if', step)
        self.assertNotIn('continue-on-error', step)

    def test_gold_regressions_run_unconditionally_on_windows(self):
        workflow = (Path(__file__).resolve().parents[2] /
                    '.github/workflows/tests.yml').read_text(encoding='utf-8')
        self.assert_windows_gold_gate(workflow)

    def test_missing_or_conditional_gold_step_is_refused(self):
        source = ('jobs:\n  windows-owner:\n    runs-on: windows-latest\n'
                  '    defaults:\n      run:\n        working-directory: app\n'
                  '    steps:\n      - uses: actions/checkout@v6\n'
                  '        with:\n          fetch-depth: 0\n'
                  '      - name: ' + STEP_NAME + '\n'
                  '        run: ' + COMMAND + '\n')
        self.assert_windows_gold_gate(source)
        for workflow in (
                source.replace('          fetch-depth: 0\n', ''),
                source.replace('fetch-depth: 0', 'fetch-depth: 1'),
                source.replace('      - uses: actions/checkout@v6\n'
                               '        with:\n          fetch-depth: 0\n', ''),
                source.replace('    defaults:\n      run:\n'
                               '        working-directory: app\n', ''),
                source.replace('working-directory: app', 'working-directory: .'),
                source.replace('        run:',
                               '        working-directory: .\n        run:'),
                source.replace('        run:', '        if: false\n        run:'),
                source.replace('        run:',
                               '        continue-on-error: true\n        run:'),
                source.replace('tests.test_pipeline_repeat_scoring ', ''),
                source.replace(STEP_NAME, 'An unrelated step'),
                source.replace('    runs-on: windows-latest',
                               '    # runs-on: windows-latest\n    runs-on: ubuntu-24.04')):
            with self.subTest(workflow=workflow), self.assertRaises(AssertionError):
                self.assert_windows_gold_gate(workflow)
