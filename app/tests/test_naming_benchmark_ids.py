import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import naming_benchmark


class NamingBenchmarkIdTests(unittest.TestCase):
    def test_path_ids_reject_before_worker_and_do_not_create_outside_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp, 'outside')
            for adapter_id in (str(outside), '../outside', 'nested/voice', '..\\outside',
                               'drive:name', 'null\x00name', '.', '..', '', None, 42):
                with self.subTest(adapter_id=adapter_id):
                    fixture={'entries':[{'id':adapter_id}]}
                    failure=subprocess.CompletedProcess([],1,'','fixture worker must not run')
                    with patch.object(naming_benchmark.subprocess,'run',return_value=failure) as worker:
                        with self.assertRaises(ValueError):
                            naming_benchmark.execute_payload({'fixture':fixture,'python':'unused','script':'unused'})
                        worker.assert_not_called()
                    self.assertFalse(outside.exists())
