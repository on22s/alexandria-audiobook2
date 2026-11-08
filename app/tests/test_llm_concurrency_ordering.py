"""Local cache ordering with real config persistence and benchmark sweeps."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import llm_bench


class ConcurrencyCacheOrderingTests(unittest.TestCase):
    status = {'available': True, 'loaded': True, 'parallel': None, 'context_length': 8192}

    def _environment(self, status):
        return llm_bench.get_concurrency_environment(
            'http://localhost:1234/v1', 'fixture', False, None, 'fixture-host',
            'fixture-gpu', 'fixture-backend', status)

    def _call(self, path, status=None):
        with patch.object(llm_bench.platform, 'node', return_value='fixture-host'), \
             patch.object(llm_bench.lmstudio_settings, 'get_gpu_name_and_backend',
                          return_value=('fixture-gpu', 'fixture-backend')):
            return llm_bench.get_cached_or_benchmarked_concurrency(
                str(path), 'local', 'http://localhost:1234/v1', 'fixture', object(),
                max_concurrency=2, status=self.status if status is None else status)

    def _seed(self, path, existing, status=None):
        profile = {'unrelated': 'kept'}
        if existing:
            profile.update(concurrency=2, concurrency_for='http://localhost:1234/v1::fixture',
                           concurrency_environment=self._environment(status or self.status))
        config = {'llm_local': profile, 'llm_remote': {'unrelated': 'remote'}, 'other': 'kept'}
        path.write_text(json.dumps(config), encoding='utf-8')
        return config

    def test_repeated_single_slot_policy_replaces_one_bounded_revision(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "config.json")
                seed = self._seed(path, existing)
                if existing:
                    seed["llm_local"]["concurrency_for"] = "unrelated-endpoint::fixture"
                    path.write_text(json.dumps(seed), encoding="utf-8")
                revisions = set()
                sizes = set()
                with patch.object(llm_bench, "find_optimal_concurrency") as sweep:
                    for iteration in range(3):
                        self.assertEqual(1, self._call(path, dict(self.status, parallel=1)))
                        sizes.add(path.stat().st_size)
                        saved = json.loads(path.read_text(encoding="utf-8"))
                        revision = saved["llm_local"].pop("concurrency_revision")
                        self.assertIsInstance(revision, str)
                        self.assertEqual(32, len(revision))
                        self.assertNotIn(revision, revisions)
                        revisions.add(revision)
                        self.assertEqual(seed, saved)
                sweep.assert_not_called()
                self.assertEqual(3, len(revisions))
                self.assertEqual(1, len(sizes))

    def test_late_positive_cannot_restore_single_slot_retired_qualification(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, 'config.json')
                seed = self._seed(path, existing, dict(self.status, context_length=16384))
                observations = []
                calls = []

                def measure(_client, _model, concurrency):
                    calls.append(concurrency)
                    if len(calls) == 6:
                        self.assertEqual(1, self._call(path, dict(self.status, parallel=1)))
                        observations.append(json.loads(path.read_text(encoding='utf-8')))
                    return 10 * concurrency

                with patch.object(llm_bench, 'measure_throughput', side_effect=measure):
                    self.assertEqual(2, self._call(path))
                self.assertEqual([1, 1, 1, 2, 2, 2], calls)
                saved = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(observations[0], saved)
                self.assertNotIn('concurrency_environment', saved['llm_local'])
                self.assertEqual(seed['llm_remote'], saved['llm_remote'])
                self.assertEqual('kept', saved['other'])
                with patch.object(llm_bench, 'measure_throughput',
                                  side_effect=lambda _client, _model, level: 10 * level) as measure, \
                     patch.object(llm_bench, 'find_optimal_concurrency',
                                  wraps=llm_bench.find_optimal_concurrency) as sweep:
                    self.assertEqual(2, self._call(path))
                sweep.assert_called_once()
                self.assertEqual(6, measure.call_count)

    def test_delayed_policy_cannot_remove_newer_same_key_measurement_including_aba(self):
        real_lock = llm_bench.file_lock
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, 'config.json')
                self._seed(path, existing)
                newer = []

                @contextlib.contextmanager
                def delayed_lock(target):
                    if not newer:
                        newer.append(None)
                        with real_lock(target):
                            config = json.loads(path.read_text(encoding='utf-8'))
                            config['llm_local'].pop('concurrency_environment', None)
                            llm_bench.atomic_json_write(config, path)
                        self.assertEqual(2, self._call(path))
                        newer[0] = path.read_bytes()
                    with real_lock(target):
                        yield

                with patch.object(llm_bench, 'file_lock', side_effect=delayed_lock), \
                     patch.object(llm_bench, 'measure_throughput',
                                  side_effect=lambda _client, _model, level: 10 * level):
                    self.assertEqual(1, self._call(path, dict(self.status, parallel=1)))
                self.assertEqual(newer[0], path.read_bytes())
                with patch.object(llm_bench, 'find_optimal_concurrency') as sweep:
                    self.assertEqual(2, self._call(path))
                sweep.assert_not_called()

    def test_late_positive_preserves_newer_measurement_and_unrelated_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'config.json')
            self._seed(path, False)
            calls = []
            newer = []

            def measure(_client, _model, level):
                calls.append(level)
                if len(calls) == 6:
                    self.assertEqual(2, self._call(path, dict(self.status, context_length=16384)))
                    newer.append(path.read_bytes())
                return 10 * level

            with patch.object(llm_bench, 'measure_throughput', side_effect=measure):
                self.assertEqual(2, self._call(path))
            self.assertEqual(newer[0], path.read_bytes())
            self.assertEqual(16384, json.loads(path.read_text())['llm_local'][
                'concurrency_environment']['context_length'])
