"""The book test with a Manual LLM (#1920): requests reach the page that answers them.

What these verify:
- a child process set up exactly as the book test sets it up (private
  ALEXANDRIA_DATA_DIR, manual transport) writes its request where
  /api/manual_llm/pending reads, /api/status/script reports it, an answer posted
  through /api/manual_llm/response reaches the child, and the private directory
  never gets a manual_llm/ queue;
- without the parent directory the same child queues privately, which is the
  reported hang (the control that keeps the first test meaningful);
- the book test's worker hands the child that parent directory.
"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import llm_provider
from routers import script

APP = Path(__file__).resolve().parent.parent
CHILD = (
    "import llm_provider\n"
    "client = llm_provider.make_llm_client({'transport': 'manual'}, 10)\n"
    "reply = client.chat.completions.create(model='m', messages=[{'role': 'user', 'content': 'Sample question'}])\n"
    "print('REPLY=' + reply.choices[0].message.content, flush=True)\n"
)


def start_child(private, parent=None):
    env = {**os.environ, 'ALEXANDRIA_DATA_DIR': str(private), 'PYTHONPATH': str(APP)}
    env.pop(llm_provider.MANUAL_DATA_DIR_ENV, None)
    if parent is not None:
        env[llm_provider.MANUAL_DATA_DIR_ENV] = str(parent)
    return subprocess.Popen([sys.executable, '-c', CHILD], cwd=str(APP), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_for(path, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


class BookPreflightManualLlmTests(unittest.TestCase):
    def test_child_request_is_shown_and_answered_through_the_page(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as private:
            child = start_child(private, parent)
            try:
                pending = Path(llm_provider.manual_llm_dir(parent), 'pending.json')
                self.assertTrue(wait_for(pending), child.stderr.read() if child.poll() is not None else 'no request')
                state = {'running': True, 'logs': ['Step 1: sample first - asking the model']}
                with patch.object(script, 'DATA_DIR', parent), \
                        patch.dict(script.process_state, {'script': state}):
                    status = asyncio.run(script.get_status('script'))
                    self.assertEqual(1, status['manual_request']['sequence'])
                    shown = asyncio.run(script.manual_llm_pending())['pending']
                    self.assertEqual([{'role': 'user', 'content': 'Sample question'}], shown['messages'])
                    asyncio.run(script.manual_llm_response(script.ManualReply(id=shown['id'], content='The answer')))
                out, err = child.communicate(timeout=20)
                self.assertEqual(0, child.returncode, err)
                self.assertIn('REPLY=The answer', out)
                self.assertFalse(Path(llm_provider.manual_llm_dir(private)).exists())
            finally:
                if child.poll() is None:
                    child.kill(); child.communicate()

    def test_without_the_parent_directory_the_request_is_hidden(self):
        with tempfile.TemporaryDirectory() as parent, tempfile.TemporaryDirectory() as private:
            child = start_child(private)
            try:
                self.assertTrue(wait_for(Path(llm_provider.manual_llm_dir(private), 'pending.json')))
                with patch.object(script, 'DATA_DIR', parent):
                    self.assertIsNone(asyncio.run(script.manual_llm_pending())['pending'])
            finally:
                child.kill(); child.communicate()

    def test_data_dir_precedence(self):
        from direct_path_admission import get_direct_spelling
        with tempfile.TemporaryDirectory() as parent,                 patch.dict(os.environ, {llm_provider.MANUAL_DATA_DIR_ENV: '  ' + parent + '  '}):
            self.assertEqual(get_direct_spelling(parent), llm_provider.get_manual_llm_data_dir())
        environment = {key: value for key, value in os.environ.items() if key != llm_provider.MANUAL_DATA_DIR_ENV}
        with patch.dict(os.environ, environment, clear=True), \
                patch('utils.get_runtime_data_dir', return_value='/runtime') as runtime:
            self.assertEqual('/runtime', llm_provider.get_manual_llm_data_dir())
            runtime.assert_called_once()


if __name__ == '__main__':
    unittest.main()
