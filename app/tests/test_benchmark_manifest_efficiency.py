import copy
import unittest
from unittest.mock import patch

import benchmark_core as core
from tests.test_benchmark_core import _manifest, _environment


class BenchmarkManifestEfficiencyTests(unittest.TestCase):
    def test_report_and_preflight_validate_once_and_preserve_fingerprints(self):
        manifest=_manifest(); environment=_environment()
        expected=core.get_manifest_fingerprint(manifest)
        with patch.object(core,'validate_benchmark_manifest',wraps=core.validate_benchmark_manifest) as validate:
            report=core.build_benchmark_report(manifest,environment)
            self.assertEqual(expected,report['manifest_sha256'])
            self.assertEqual(1,validate.call_count)
        with patch.object(core,'validate_benchmark_manifest',wraps=core.validate_benchmark_manifest) as validate:
            identity=core.get_benchmark_preflight_id(manifest,{'local':environment,'thunder':_environment('thunder')})
            self.assertEqual(1,validate.call_count)
        self.assertEqual(identity,core.get_benchmark_preflight_id(manifest,{'thunder':_environment('thunder'),'local':environment}))

    def test_fixture_payload_copied_once_and_each_fixture_remains_independent(self):
        counts=[]
        class CountedList(list):
            def __deepcopy__(self,memo):
                counts.append(1)
                return list(self)
        shared=CountedList(['payload'])
        manifest=_manifest();manifest['fixtures']=[{'id':'a','sha256':'x','payload':shared},{'id':'b','sha256':'y','payload':shared}]
        normalized=core.validate_benchmark_manifest(manifest)
        self.assertEqual(2,len(counts))
        normalized['fixtures'][0]['payload'].append('changed')
        self.assertEqual(['payload'],normalized['fixtures'][1]['payload'])
        self.assertEqual(['payload'],shared)

    def test_external_fingerprint_still_rejects_invalid_manifest(self):
        for mutate in (lambda m:m.update(repetitions=0),lambda m:m.update(settings=[]),
                       lambda m:m['fixtures'].append(dict(m['fixtures'][0]))):
            manifest=_manifest();mutate(manifest)
            with self.assertRaises(ValueError):core.get_manifest_fingerprint(manifest)
