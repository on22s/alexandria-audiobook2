"""CPU-only external-probe launch counts during a benchmark invocation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import tts_benchmark as benchmark


class BenchmarkProbeRateTests(unittest.TestCase):
    def test_default_and_short_requested_intervals_do_not_launch_high_rate_probes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worker = root / 'probe.py'
            worker.write_text(
                "import json,sys,time\n"
                "with open(sys.argv[1],'a') as f:f.write(json.dumps(time.monotonic())+'\\n')\n"
                "print('50')\n")
            for interval in (None, .01):
                with self.subTest(interval=interval):
                    log = root / ('default.jsonl' if interval is None else 'short.jsonl')
                    first_sample = threading.Event()
                    def sample():
                        result = subprocess.run([sys.executable, str(worker), str(log)],
                                                capture_output=True, text=True, check=True, timeout=2)
                        first_sample.set()
                        return float(result.stdout)
                    def generate():
                        self.assertTrue(first_sample.wait(2))
                        time.sleep(1.25)
                        return 'generated'
                    kwargs = {} if interval is None else {'poll_interval': interval}
                    with patch.object(benchmark, 'sample_gpu_utilization', side_effect=sample):
                        self.assertEqual(('generated', 50.0),
                                         benchmark._run_with_utilization_sampling(generate, **kwargs))
                    stamps = [json.loads(row) for row in log.read_text().splitlines()]
                    print(f'CPU external probe interval={interval}: {len(stamps)} launches')
                    self.assertLessEqual(len(stamps), 2)
                    self.assertGreaterEqual(len(stamps), 1)
                    self.assertTrue(all(b-a >= .95 for a,b in zip(stamps, stamps[1:])))

    def test_generation_exception_stops_waiting_poller_without_a_second_probe(self):
        sampled = threading.Event()
        def sample():
            sampled.set()
            return 70
        def generate():
            self.assertTrue(sampled.wait(2))
            raise RuntimeError('generation failed')
        with patch.object(benchmark, 'sample_gpu_utilization', side_effect=sample) as probe:
            with self.assertRaisesRegex(RuntimeError, 'generation failed'):
                benchmark._run_with_utilization_sampling(generate)
            count = probe.call_count
            time.sleep(.25)
            self.assertEqual(count, probe.call_count)
            self.assertEqual(1, count)
