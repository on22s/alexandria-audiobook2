"""Standalone reporting uses the robust decision samples, never a second sweep."""
import contextlib
import io
import unittest
from unittest.mock import patch
import llm_bench as bench


class StandaloneSweepTests(unittest.TestCase):
    def run_cli(self, measurements):
        output=io.StringIO();client=object();calls=[]
        def measure(given,model,level):
            self.assertIs(client,given);self.assertEqual('fixture',model)
            offset=calls.count(level);calls.append(level)
            values=measurements[level];return values[offset % len(values)]
        with patch.object(bench,'make_llm_client',return_value=client),patch.object(bench,'measure_throughput',side_effect=measure),patch('sys.argv',['llm_bench.py','--model','fixture','--max-concurrency','8']),contextlib.redirect_stdout(output):
            bench._main()
        return calls,output.getvalue()

    def test_cli_reports_decision_medians_and_stops_without_preliminary_samples(self):
        calls,output=self.run_cli({1:[10,30,20],2:[23,1,24],4:[24,40,23],8:[100]})
        self.assertEqual([1,1,1,2,2,2,4,4,4],calls)
        self.assertIn('Optimal concurrency: 2',output)
        for value in ('20.0 tok/s','23.0 tok/s','24.0 tok/s'):self.assertIn(value,output)
        self.assertNotIn('100.0 tok/s',output)

    def test_failed_trial_ends_the_single_sweep_and_retains_safe_prior_level(self):
        calls,output=self.run_cli({1:[10],2:[None],4:[100],8:[100]})
        self.assertEqual([1,1,1,2],calls)
        self.assertIn('FAILED',output);self.assertIn('Optimal concurrency: 1',output)

    def test_reporting_observer_receives_only_capped_decision_levels_and_does_not_change_policy(self):
        measurements=[]
        with patch.object(bench,'measure_throughput',side_effect=[10,12,11,12,14,13]):
            result=bench.find_optimal_concurrency(object(),'fixture',8,server_parallel_limit=2,
                on_measurement=lambda level,throughput,elapsed:measurements.append((level,throughput,elapsed)))
        self.assertEqual(2,result);self.assertEqual([(1,11),(2,13)],[(n,t) for n,t,_ in measurements])
        self.assertTrue(all(elapsed>=0 for _,_,elapsed in measurements))
        with patch.object(bench,'measure_throughput') as measure:
            self.assertEqual(1,bench.find_optimal_concurrency(object(),'fixture',8,server_parallel_limit=1,on_measurement=lambda *args:self.fail('unsafe sweep')))
        measure.assert_not_called()
