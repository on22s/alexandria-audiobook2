"""A process verdict needs fresh, typed evidence from its isolated attempt."""
import copy
import json
import os
from pathlib import Path
import time
import unittest

from experiments.recheck_result import get_recheck_score
from tests import test_recheck_reporting as reporting


def get_document():
    return dict(adapter='adapter/alpha', median_ecapa=0.6612, lines=6, passed=True)


class RecheckResultTests(unittest.TestCase):
    def test_measured_scores_and_rounded_near_threshold_verdict_are_preserved(self):
        document = get_document()
        before = copy.deepcopy(document)
        self.assertEqual(0.6612, get_recheck_score(document, 'adapter/alpha', 0))
        self.assertEqual(before, document)
        # Producer decides on unrounded median; 0.44999 rounds to0.4500.
        document.update(median_ecapa=0.45, passed=False)
        self.assertEqual(0.45, get_recheck_score(document, 'adapter/alpha', 3))

    def test_bad_scores_wrong_adapter_empty_measurement_and_verdict_mismatch_refuse(self):
        for field, value in [('median_ecapa', True), ('median_ecapa', '0.8'),
                             ('median_ecapa', float('nan')), ('median_ecapa', float('inf')),
                             ('median_ecapa', 1.01), ('median_ecapa', -1.01),
                             ('adapter', 'adapter/other'), ('lines', 0), ('lines', True),
                             ('lines', '6'), ('passed', False), ('passed', 1)]:
            with self.subTest(field=field, value=value):
                document = get_document()
                document[field] = value
                with self.assertRaises(ValueError): get_recheck_score(document, 'adapter/alpha', 0)
        for code in (2, 4, 5, 7, 124):
            with self.subTest(exit_code=code), self.assertRaises(ValueError):
                get_recheck_score(get_document(), 'adapter/alpha', code)


class RecheckFreshnessTests(unittest.TestCase):
    setUp = reporting.RecheckReportingTest.setUp
    _run = reporting.RecheckReportingTest._run

    def test_recent_old_gate_is_not_reused_when_success_writes_nothing(self):
        path = Path(self.experiments, 'gate_recheck__alpha.json')
        old = json.dumps(get_document())
        for code in (0, 3):
            with self.subTest(exit_code=code):
                path.write_text(old)
                # Deliberately inside the old one-second tolerance.
                now = time.time()
                os.utime(path, (now, now))
                result = self._run([('alpha', '0.0342')], {'alpha': code}, write_artifact=False)
                self.assertIn('0 of 1 measured', result.stdout)
                self.assertIn('NOT MEASURED', result.stdout)
                self.assertIn('RETURN=1', result.stdout)
                self.assertNotIn('recheck PASS', result.stdout)
                self.assertNotIn('recheck BELOW', result.stdout)
                self.assertEqual(old, path.read_text())

    def test_exit_zero_or_three_without_any_artifact_is_not_a_measurement(self):
        for code in (0, 3):
            with self.subTest(exit_code=code):
                result = self._run([('alpha', '0.0342')], {'alpha': code}, write_artifact=False)
                self.assertIn('0 of 1 measured', result.stdout)
                self.assertIn('RETURN=1', result.stdout)
                self.assertFalse(Path(self.experiments, 'gate_recheck__alpha.json').exists())

    def test_missing_first_verdict_does_not_prevent_next_independent_attempt(self):
        # A refused first job has no artifact; the second actual fixture writes one.
        result = self._run([('alpha', '0.03'), ('beta', '0.04')], {'alpha': 5, 'beta': 3})
        self.assertIn('1 of 2 measured; 1 below threshold; 1 never ran', result.stdout)
        self.assertIn('recheck BELOW beta', result.stdout)
        self.assertIn('RETURN=1', result.stdout)
        self.assertEqual(0.2, json.loads(Path(self.experiments, 'gate_recheck__beta.json').read_text())['median_ecapa'])


if __name__ == '__main__':
    unittest.main()
