"""Interpreter admission for the configured optional cp310 NVIDIA wheels."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]


class LauncherAttentionPythonTests(unittest.TestCase):
    def commands(self, platform, **options):
        script = r'''
const vm = require('node:vm');
const root = process.argv[1];
const platform = process.argv[2];
const args = JSON.parse(process.argv[3]);
const context = {platform, gpu:'nvidia', args};
function render(value) {
  if (typeof value !== 'string') { return value; }
  return value.replace(/\{\{([\s\S]*?)\}\}/g, (_, expression) => vm.runInNewContext(expression, context));
}
const result = [];
for (const step of require(root + '/torch.js').run) {
  if (step.when && !vm.runInNewContext(step.when.slice(2,-2), context)) { continue; }
  const messages = Array.isArray(step.params.message) ? step.params.message : [step.params.message];
  result.push(...messages.map(render).filter(Boolean));
  if (step.next === null) { break; }
}
console.log(JSON.stringify(result));
'''
        result = subprocess.run(['node', '-e', script, str(ROOT), platform, json.dumps(options)],
                                capture_output=True, text=True, timeout=10, check=True)
        return json.loads(result.stdout)

    def test_fresh_installer_selects_the_required_python(self):
        script = "console.log(JSON.stringify(require(process.argv[1]).run))"
        steps = json.loads(subprocess.check_output(
            ['node', '-e', script, str(ROOT / 'install.js')], text=True))
        create = next(step for step in steps if step.get('method') == 'shell.run'
                      and step['params'].get('path') == 'app')
        self.assertEqual('uv venv --python 3.10 env', create['params']['message'])
        self.assertEqual("{{!exists('app/env')}}", create['when'])

    def test_sage_and_flash_validate_before_any_package_install(self):
        for platform in ('linux', 'win32'):
            for option in ('sageattention', 'flashattention'):
                with self.subTest(platform=platform, option=option):
                    commands = self.commands(platform, **{option: True})
                    self.assertTrue(commands[0].startswith('python -c '), commands)
                    payload = shlex.split(commands[0])[2]
                    for implementation, version, expected in (
                            ('cpython', (3, 10), 0), ('cpython', (3, 11), 1),
                            ('cpython', (3, 12), 1), ('pypy', (3, 10), 1)):
                        probe = ("import sys,types;sys.version_info=" + repr(version) +
                                 ";sys.implementation=types.SimpleNamespace(name=" + repr(implementation) + ");" + payload)
                        result = subprocess.run([sys.executable, '-c', probe],
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(expected, result.returncode, result.stderr)
                        if expected:
                            self.assertIn('CPython 3.10', result.stderr)
                    self.assertTrue(any(option.replace('flashattention', 'flash_attn') in command
                                        and 'cp310-cp310' in command for command in commands[1:]))

    def test_without_optional_wheels_has_no_interpreter_restriction(self):
        for platform in ('linux', 'win32'):
            commands = self.commands(platform)
            self.assertTrue(commands[0].startswith('uv pip install torch==2.7.0'))
            self.assertFalse(any(command.startswith('python -c ') for command in commands))
