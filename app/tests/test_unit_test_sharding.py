"""Unit-test sharding must run every test exactly once; a dropped test cannot hide."""
import io
import json
import math
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import check_shard_reports as checker
import unit_test_sharding as sharding
import verify_release

APP = Path(__file__).resolve().parent.parent
INVENTORY = json.loads((APP / "tests" / "unit_test_inventory.json").read_text(encoding="utf-8"))
MODULES = sorted(INVENTORY)


def fake_test(identifier):
    class Fake(unittest.TestCase):
        def runTest(self):
            pass

        def id(self):
            return identifier
    return Fake()


class ShardSpecTests(unittest.TestCase):
    def test_valid_specs(self):
        self.assertEqual((2, 3), sharding.parse_shard_spec("2/3"))
        self.assertEqual((1, 1), sharding.parse_shard_spec(" 1/1 "))

    def test_invalid_specs_are_refused(self):
        for spec in ("0/3", "4/3", "a/b", "1/0", "2", "1/3/5", "", None, 3, "-1/3"):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                sharding.parse_shard_spec(spec)


class AssignmentTests(unittest.TestCase):
    durations = sharding.get_module_durations()

    def test_real_modules_are_covered_exactly_once_for_every_shard_count(self):
        for count in (1, 2, 3, 4, 7, 20):
            with self.subTest(count=count):
                shards = sharding.get_test_shards(MODULES, self.durations, count)
                self.assertEqual(list(range(1, count + 1)), sorted(shards))
                flat = [name for modules in shards.values() for name in modules]
                self.assertEqual(MODULES, sorted(flat))          # complete
                self.assertEqual(len(flat), len(set(flat)))      # disjoint

    def test_result_does_not_depend_on_input_order(self):
        expected = sharding.get_test_shards(MODULES, self.durations, 3)
        shuffled = list(MODULES)
        random.Random(7).shuffle(shuffled)
        self.assertEqual(expected, sharding.get_test_shards(shuffled, self.durations, 3))

    def test_one_shard_is_everything(self):
        self.assertEqual({1: MODULES}, sharding.get_test_shards(MODULES, self.durations, 1))

    def test_shards_are_balanced_within_the_greedy_bound(self):
        weight = lambda name: self.durations.get(name, sharding.DEFAULT_WEIGHT_SECONDS)
        for count in (2, 3, 4):
            totals = [sum(weight(n) for n in modules)
                      for modules in sharding.get_test_shards(MODULES, self.durations, count).values()]
            average = sum(totals) / count
            # Longest-processing-time-first never exceeds the average by more than the largest item.
            self.assertLessEqual(max(totals), average + max(weight(n) for n in MODULES) + 1e-9, count)

    def test_a_module_with_no_recorded_time_is_still_assigned_once(self):
        names = MODULES + ["test_brand_new_module_without_a_weight"]
        shards = sharding.get_test_shards(names, self.durations, 3)
        holders = [number for number, modules in shards.items() if "test_brand_new_module_without_a_weight" in modules]
        self.assertEqual(1, len(holders))

    def test_bad_inputs_are_refused(self):
        with self.assertRaises(ValueError):
            sharding.get_test_shards(["a", "a"], {}, 2)
        for count in (0, -1, True, 2.5):
            with self.subTest(count=count), self.assertRaises(ValueError):
                sharding.get_test_shards(["a"], {}, count)

    def test_duration_file_rejects_non_numbers_negatives_and_nan(self):
        for bad in ('{"a": -1}', '{"a": "3"}', '{"a": true}', '[1]', '{"a": NaN}'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "d.json"
                path.write_text(bad, encoding="utf-8")
                with self.assertRaises(ValueError):
                    sharding.get_module_durations(path)
        self.assertTrue(all(math.isfinite(v) and v >= 0 for v in self.durations.values()))


class SuiteFilteringTests(unittest.TestCase):
    def build(self):
        ids = [f"tests.test_{m}.Case.test_{n}" for m in "abcde" for n in (1, 2, 3)]
        return ids, unittest.TestSuite([unittest.TestSuite([fake_test(i) for i in ids[:7]]),
                                        unittest.TestSuite([fake_test(i) for i in ids[7:]])])

    def test_every_test_lands_in_exactly_one_shard_in_original_order(self):
        ids, suite = self.build()
        durations = {"test_a": 9, "test_b": 5, "test_c": 4, "test_d": 3, "test_e": 1}
        seen = []
        for index in (1, 2, 3):
            part = [t.id() for t in sharding.get_leaf_tests(sharding.get_sharded_suite(suite, index, 3, durations))]
            self.assertEqual(part, [i for i in ids if i in set(part)])   # order preserved
            seen += part
        self.assertEqual(sorted(ids), sorted(seen))

    def test_a_module_is_never_split_across_shards(self):
        ids, suite = self.build()
        owners = {}
        for index in (1, 2):
            for test in sharding.get_leaf_tests(sharding.get_sharded_suite(suite, index, 2, {})):
                owners.setdefault(sharding.get_test_module_key(test), set()).add(index)
        self.assertTrue(all(len(shards) == 1 for shards in owners.values()), owners)

    def test_a_module_that_fails_to_import_is_kept_not_dropped(self):
        broken = unittest.defaultTestLoader.loadTestsFromName("tests.test_zz_does_not_exist_for_sharding")
        failed = next(sharding.get_leaf_tests(broken))
        self.assertEqual("test_zz_does_not_exist_for_sharding", sharding.get_test_module_key(failed))
        suite = unittest.TestSuite([failed, fake_test("tests.test_ok.Case.test_1")])
        kept = [sharding.get_sharded_suite(suite, i, 2, {}) for i in (1, 2)]
        self.assertEqual(2, sum(len(list(sharding.get_leaf_tests(s))) for s in kept))
        self.assertEqual(1, sum(failed in set(sharding.get_leaf_tests(s)) for s in kept))


class CiEnvCommandLineTests(unittest.TestCase):
    """The real `python -m ci_env discover ... --shard I/N` on throwaway test modules."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for name in "abcd":
            (self.root / f"test_shardfixture_{name}.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n")

    def run_ci_env(self, *extra):
        return subprocess.run([sys.executable, "-m", "ci_env", "discover", "-s", str(self.root),
                               "-p", "test_shardfixture_*.py", "-v", *extra],
                              cwd=APP, capture_output=True, text=True, timeout=120)

    @staticmethod
    def ran(result):
        match = re.search(r"^Ran (\d+) tests? in ", result.stderr, re.M)
        return int(match.group(1)) if match else None

    def test_shards_partition_the_run_and_unsharded_runs_everything(self):
        self.assertEqual(4, self.ran(self.run_ci_env()))
        counts = [self.ran(self.run_ci_env("--shard", f"{i}/2")) for i in (1, 2)]
        self.assertEqual([2, 2], counts)

    def test_equals_form_works_and_bad_specs_exit_nonzero_with_a_message(self):
        self.assertEqual(2, self.ran(self.run_ci_env("--shard=1/2")))
        for spec in ("3/2", "x", "0/1"):
            with self.subTest(spec=spec):
                result = self.run_ci_env("--shard", spec)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("Shard", result.stderr + result.stdout)
        self.assertNotEqual(0, self.run_ci_env("--shard").returncode)

    def test_a_broken_test_file_fails_exactly_one_shard(self):
        (self.root / "test_shardfixture_broken.py").write_text("def oops(:\n")
        codes = [self.run_ci_env("--shard", f"{i}/2").returncode for i in (1, 2)]
        self.assertEqual([0, 1], sorted(codes))


class VerifierShardTests(unittest.TestCase):
    def test_validator_returns_the_count_and_keeps_its_floor_and_skip_rule(self):
        self.assertEqual(600, verify_release.validate_unittest_output("Ran 600 tests in 1.0s\n\nOK\n"))
        with self.assertRaisesRegex(ValueError, "floor"):
            verify_release.validate_unittest_output("Ran 10 tests in 1.0s\n\nOK\n")
        with self.assertRaisesRegex(ValueError, "skipped"):
            verify_release.validate_unittest_output("Ran 600 tests in 1.0s\n\nOK (skipped=2)\n")

    def run_main(self, *args):
        names, commands = [], []

        def gate(report, name, callback):
            names.append(name)
            if name == "unit_tests":
                report["gates"].append({"name": name, "status": "passed", "result": callback()})
            return None

        def command(label, cmd, cwd, **kwargs):
            commands.append(cmd)
            return 1234

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(verify_release, "run_report_gate", side_effect=gate), \
             patch.object(verify_release, "run_report_command", side_effect=command), \
             patch.object(sys, "stdout", io.StringIO()):
            path = Path(directory) / "report.json"
            code = verify_release.main([*args, "--json-report", str(path)])
            report = json.loads(path.read_text(encoding="utf-8"))
        return code, names, commands, report

    def test_unsharded_and_first_shard_run_every_gate_later_shards_only_the_unit_gate(self):
        everything = ["compile_python", "test_inventory", "unit_tests", "evidence_index", "legacy_audit",
                      "results_index", "api_contract", "api_tests"]
        for args in ((), ("--shard", "1/3")):
            with self.subTest(args=args):
                code, names, _, _ = self.run_main(*args)
                self.assertEqual((0, everything), (code, names))
        for shard in ("2/3", "3/3"):
            with self.subTest(shard=shard):
                code, names, commands, report = self.run_main("--shard", shard)
                self.assertEqual((0, ["unit_tests"]), (code, names))
                self.assertEqual(["--shard", shard], commands[0][-2:])
                self.assertEqual(shard, report["shard"])
                self.assertEqual({"tests_ran": 1234}, report["gates"][0]["result"])

    def test_bad_shard_is_refused_before_any_gate_runs(self):
        with patch.object(verify_release, "run_report_gate") as gate, patch.object(sys, "stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                verify_release.main(["--shard", "5/3"])
        self.assertEqual(2, caught.exception.code)
        gate.assert_not_called()


def passing_report(index, count, ran, **changes):
    report = {"status": "passed", "shard": f"{index}/{count}",
              "gates": [{"name": "unit_tests", "status": "passed", "result": {"tests_ran": ran}}]}
    report.update(changes)
    return report


class ShardReportCheckTests(unittest.TestCase):
    good = [passing_report(1, 3, 2000), passing_report(2, 3, 2000), passing_report(3, 3, 2326)]

    def errors(self, reports, expected=6326, count=3):
        return checker.get_shard_report_errors(reports, count, expected)

    def test_a_complete_set_is_accepted(self):
        self.assertEqual([], self.errors(self.good))

    def test_every_way_of_losing_or_doubling_a_test_is_refused(self):
        cases = {
            "missing shard": self.good[:2],
            "duplicate shard number": [self.good[0], self.good[0], self.good[2]],
            "one module dropped (short count)": [self.good[0], self.good[1], passing_report(3, 3, 2325)],
            "one module run twice (long count)": [self.good[0], self.good[1], passing_report(3, 3, 2327)],
            "failed shard": [self.good[0], passing_report(2, 3, 2000, status="failed"), self.good[2]],
            "no tests_ran recorded": [self.good[0], self.good[1],
                                      {"status": "passed", "shard": "3/3", "gates": [{"name": "unit_tests", "status": "passed"}]}],
            "no unit gate": [self.good[0], self.good[1], {"status": "passed", "shard": "3/3", "gates": []}],
            "unsharded report mixed in": [self.good[0], self.good[1], {"status": "passed", "gates": []}],
            "run as a different shard count": [self.good[0], self.good[1], passing_report(3, 4, 2326)],
        }
        for name, reports in cases.items():
            with self.subTest(name):
                self.assertTrue(self.errors(reports), name)

    def test_command_line_reads_reports_at_any_depth_and_sets_the_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.json"
            inventory.write_text(json.dumps({"test_a": ["x"] * 4, "test_b": ["y"] * 2}), encoding="utf-8")
            for index, ran in ((1, 2), (2, 4)):
                folder = root / f"artifact-{index}" / "app"
                folder.mkdir(parents=True)
                (folder / "release-report.json").write_text(json.dumps(passing_report(index, 2, ran)), encoding="utf-8")
            (root / "artifact-1" / "app" / "base-release-report.json").write_text("{}", encoding="utf-8")  # ignored
            with patch.object(sys, "stdout", io.StringIO()):
                self.assertEqual(0, checker.main([str(root), "--shards", "2", "--inventory", str(inventory)]))
            inventory.write_text(json.dumps({"test_a": ["x"] * 4, "test_b": ["y"] * 3}), encoding="utf-8")
            with patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(1, checker.main([str(root), "--shards", "2", "--inventory", str(inventory)]))

    def test_real_inventory_total_matches_its_modules(self):
        self.assertEqual(sum(len(v) for v in INVENTORY.values()), checker.get_inventory_total())


if __name__ == "__main__":
    unittest.main()
