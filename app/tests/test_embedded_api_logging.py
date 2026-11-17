"""Embedded native HTTP fixtures retain foreign FileHandler ownership."""
import subprocess
import sys
import unittest


class EmbeddedApiLoggingTests(unittest.TestCase):
    def test_server_creation_does_not_close_a_foreign_log_after_directory_cleanup(self):
        code = r"""
import logging,os,tempfile
from pathlib import Path
from fastapi import FastAPI
from tests.test_support import create_test_api_server
root=tempfile.TemporaryDirectory();path=Path(root.name)/'foreign.log'
handler=logging.FileHandler(path);logger=logging.getLogger('alexandria')
logger.addHandler(handler);logger.setLevel(logging.INFO)
try:
 logger.info('before server config');stream=handler.stream
 server=create_test_api_server(FastAPI())
 assert server.config.log_config is None
 assert handler.stream is stream and not stream.closed
 logger.info('after server config');assert 'after server config' in path.read_text()
 assert handler.stream is stream and not stream.closed
 assert os.fstat(stream.fileno()).st_size>0
finally:
 logger.removeHandler(handler);handler.close();root.cleanup()
assert not path.parent.exists()
"""
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(0, result.returncode, result.stderr)
