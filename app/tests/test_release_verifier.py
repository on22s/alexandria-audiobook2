import contextlib
import io
import json
import os
import re
import unittest
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import ci_env
import diagnose_pr_changes
import mark_pr_ready
import verify_release


class CiEnvParityTests(unittest.TestCase):
    """The local verifier is only useful if it predicts CI, which means the set
    of libraries it hides must match the set CI actually lacks. CPU
    adapter dependencies are installed in both environments."""

    def _workflow(self):
        path = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "tests.yml"
        return path.read_text(encoding="utf-8")

    def _requirements(self):
        return (Path(__file__).resolve().parent.parent / "requirements.txt").read_text(encoding="utf-8")

    def test_blocked_modules_match_what_ci_omits(self):
        workflow = self._workflow()
        self.assertIn("python -m pip install -r requirements.txt", workflow)
        self.assertIn("python -m pip install 'torch==2.10.0+cpu' 'torchaudio==2.10.0+cpu' --index-url https://download.pytorch.org/whl/cpu", workflow)
        self.assertIn("assert torch.version.cuda is None and torch.version.hip is None", workflow)
        self.assertNotIn("grep -vE", workflow)
        self.assertEqual((), ci_env.BLOCKED_MODULES,
                         "CI installs real CPU adapter-validation dependencies")
        declared = {
            re.split(r"[=<>~\[]", line, 1)[0].strip().lower()
            for line in self._requirements().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertTrue({"peft", "transformers"} <= declared)

    def test_torch_is_absent_from_requirements(self):
        # Production gets its platform-specific Torch from torch.js.
        # The CPU CI pin must remain separate from production requirements.
        declared = {
            re.split(r"[=<>~\[]", line, 1)[0].strip().lower()
            for line in self._requirements().splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertNotIn("torch", declared)

    def test_ci_runs_the_shared_release_verifier(self):
        workflow = self._workflow()
        self.assertIn(
            'python verify_release.py --shard "${{ matrix.shard }}" --json-report release-report.json',
            workflow)
        self.assertNotIn("python -m unittest discover", workflow)
        # The unit suite is split across a matrix; a final job checks the shards
        # together ran every test. The matrix size and the checker's --shards must agree.
        matrix = re.search(r'shard:\s*\[([^\]]*)\]', workflow)
        shards = [item.strip().strip('"') for item in matrix.group(1).split(",")]
        count = len(shards)
        self.assertEqual([f"{i}/{count}" for i in range(1, count + 1)], shards)
        self.assertIn(f"python check_shard_reports.py ../shard-reports --shards {count}", workflow)
        self.assertIn("actions/upload-artifact@v6", workflow)
        self.assertIn("Diagnose whether failure exists on the base commit", workflow)
        self.assertIn("fetch-depth: 0", workflow)

    def test_changed_file_hints_name_the_missing_generator(self):
        self.assertEqual([], diagnose_pr_changes.get_snapshot_hints({
            "app/tests/test_example.py", "app/tests/unit_test_inventory.json",
            "app/routers/example.py", "app/api_contract/openapi.json",
        }))
        hints = diagnose_pr_changes.get_snapshot_hints({
            "app/tests/test_example.py", "app/routers/example.py",
        })
        self.assertEqual(2, len(hints))
        self.assertIn("update_test_inventory.py", hints[0])
        self.assertIn("update_api_contract_snapshots.py", hints[1])

    def test_mark_ready_requires_current_mergeable_head_and_successful_checks(self):
        head = "abc123"
        ready = {
            "isDraft": True, "headRefOid": head, "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{
                "name": "test", "status": "completed", "conclusion": "success",
            }],
        }
        self.assertEqual([], mark_pr_ready.get_readiness_errors(ready, head))
        ready["headRefOid"] = "stale"
        ready["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        errors = mark_pr_ready.get_readiness_errors(ready, head)
        self.assertTrue(any("does not match" in error for error in errors))
        self.assertTrue(any("not completed successfully" in error for error in errors))

    def test_mark_ready_parses_https_and_ssh_origins(self):
        expected = "on22s/alexandria-audiobook2"
        self.assertEqual(expected, mark_pr_ready.get_origin_repo(
            "https://github.com/on22s/alexandria-audiobook2.git"))
        self.assertEqual(expected, mark_pr_ready.get_origin_repo(
            "git@github.com:on22s/alexandria-audiobook2.git"))

    def test_block_ml_imports_hides_an_installed_module(self):
        # Prove the finder really blocks, using a module that IS installed here
        # (json), so the test is meaningful whether or not torch is present.
        code = (
            "import ci_env, sys\n"
            "ci_env.block_ml_imports(['json'])\n"
            "try:\n"
            "    import json\n"
            "except ImportError as e:\n"
            "    print('BLOCKED')\n"
            "else:\n"
            "    print('LEAKED')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True)
        self.assertIn("BLOCKED", result.stdout)


class ReleaseVerifierTests(unittest.TestCase):
    def test_python_compilation_includes_nonignored_untracked_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("ignored.py\nenv/\n", encoding="utf-8")
            (repo / "tracked.py").write_text("TRACKED = True\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "tracked.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "baseline"], cwd=repo, check=True)
            (repo / "staged.py").write_text("STAGED = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "staged.py"], cwd=repo, check=True)
            (repo / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")
            (repo / "ignored.py").write_text("not valid python !\n", encoding="utf-8")
            (repo / "env").mkdir()
            (repo / "env" / "ignored_env.py").write_text("not valid python !\n", encoding="utf-8")

            paths = verify_release.get_python_paths(repo)

            self.assertEqual(
                [repo / "staged.py", repo / "tracked.py", repo / "untracked.py"], paths
            )
            verify_release.compile_python_files(repo)

    @unittest.skipUnless(os.name == "posix", "process-group behavior is POSIX-specific")
    def test_failed_command_terminates_grandchild_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path = Path(tmp) / "grandchild.pid"
            code = (
                "import subprocess,sys; "
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'], "
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                f"open({str(pid_path)!r},'w').write(str(child.pid)); sys.exit(3)"
            )

            with self.assertRaisesRegex(RuntimeError, "exit status 3"):
                verify_release.run_command("failing tree", [sys.executable, "-c", code], tmp)

            grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(grandchild_pid, 0)

    def test_non_utf8_child_output_is_shown_not_fatal(self):
        """A child that writes a raw byte to the shared pipe must not abort
        the verifier: the byte is escaped into the log so its origin can be
        found, and a clean exit still counts as passing."""
        code = (
            "import sys; sys.stdout.buffer.write(b'test_x ... \\xa1 ok\\n'); "
            "sys.stdout.flush(); print('done')"
        )
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            combined = verify_release.run_command("raw bytes", [sys.executable, "-c", code], ".")
        self.assertIn("\\xa1", combined)
        self.assertIn("done", combined)
        self.assertIn("\\xa1", captured.getvalue())

    def test_keyboard_interrupt_stops_child_group_before_propagating(self):
        class InterruptingOutput:
            def __iter__(self):
                raise KeyboardInterrupt

            def close(self):
                pass

        process = unittest.mock.Mock(stdout=InterruptingOutput())
        with patch.object(verify_release, "start_owned_subprocess", return_value=process), \
             patch.object(verify_release, "stop_process_group") as stop:
            with self.assertRaises(KeyboardInterrupt):
                verify_release.run_command("interrupted", ["command"], ".")

        stop.assert_called_once_with(process, interrupt=True)

    def test_api_summary_uses_suite_inventory_for_quick_and_full_results(self):
        quick = self._api_summary("quick", "skipped")
        self.assertEqual(
            quick["counts"], verify_release.validate_api_summary(quick, False)
        )
        full = self._api_summary("full", "passed")
        self.assertEqual(
            full["counts"], verify_release.validate_api_summary(full, True)
        )

    def test_api_summary_rejects_wrong_skip_identity_with_same_counts(self):
        summary = self._api_summary("quick", "passed")
        summary["tests"][0]["status"] = "skipped"
        summary["counts"].update(passed=1, skipped=1)
        with self.assertRaisesRegex(ValueError, "Unexpected quick API skips"):
            verify_release.validate_api_summary(summary, False)

    def test_api_summary_rejects_inconsistent_counts_and_duplicate_names(self):
        summary = self._api_summary("quick", "skipped")
        summary["counts"]["passed"] = 2
        with self.assertRaisesRegex(ValueError, "do not match test records"):
            verify_release.validate_api_summary(summary, False)

        summary = self._api_summary("quick", "skipped")
        summary["tests"][1]["name"] = summary["tests"][0]["name"]
        with self.assertRaisesRegex(ValueError, "non-empty and unique"):
            verify_release.validate_api_summary(summary, False)

    @staticmethod
    def _api_summary(mode, full_only_status):
        tests = [
            {"name": "always", "requires_full": False, "status": "passed"},
            {"name": "gpu", "requires_full": True, "status": full_only_status},
        ]
        if full_only_status == "skipped":
            tests[1]["reason"] = "requires --full"
        passed = sum(test["status"] == "passed" for test in tests)
        skipped = sum(test["status"] == "skipped" for test in tests)
        return {
            "schema_version": 1, "mode": mode,
            "counts": {"passed": passed, "failed": 0, "skipped": skipped, "total": 2},
            "tests": tests,
        }

    def test_unittest_summary_rejects_skips_and_missing_success(self):
        verify_release.validate_unittest_output("Ran 1592 tests in 1.0s\n\nOK\n")
        with self.assertRaisesRegex(ValueError, "2 skipped"):
            verify_release.validate_unittest_output(
                "Ran 1592 tests in 1.0s\n\nOK (skipped=2)\n"
            )
        with self.assertRaisesRegex(ValueError, "successful summary"):
            verify_release.validate_unittest_output("process stopped")

    def test_unittest_summary_rejects_a_suite_that_collapsed_to_nothing(self):
        """The failure this gate could not see before the suite moved.

        `unittest discover` skips a directory that is not an importable
        package, so losing `tests/__init__.py` yields a perfectly well-formed
        "Ran 0 tests ... OK" that the skip check and the summary check both
        pass. A green build that ran nothing is the worst outcome available,
        so the count itself has to be asserted on.
        """
        with self.assertRaisesRegex(ValueError, "under the .* floor"):
            verify_release.validate_unittest_output("Ran 0 tests in 0.000s\n\nOK\n")
        # Not just zero - a partially-collected suite is the same failure.
        with self.assertRaisesRegex(ValueError, "ran only 12 tests"):
            verify_release.validate_unittest_output("Ran 12 tests in 0.1s\n\nOK\n")

    def test_unittest_final_summary_supersedes_nested_fixture_summaries(self):
        for nested in ("Ran 1 test in 0.01s\n\nOK\n",
                       "Ran 1600 tests in 0.01s\n\nOK (skipped=2)\n",
                       "Ran 1600 tests in 0.01s\n\nFAILED (failures=1)\n"):
            output = nested + "Ran 6296 tests in 597.0s\n\nOK\n"
            for value in (output, io.StringIO(output)):
                with self.subTest(nested=nested, streaming=not isinstance(value, str)):
                    verify_release.validate_unittest_output(value)

    def test_unittest_nested_success_cannot_hide_an_invalid_final_summary(self):
        for final, message in (
                ("Ran 0 tests in 0.0s\n\nOK\n", "under the .* floor"),
                ("Ran 12 tests in 0.0s\n\nOK\n", "under the .* floor"),
                ("Ran 1600 tests in 1.0s\n\nFAILED (failures=1)\n", "successful summary"),
                ("Ran 1600 tests in 1.0s\n\nOK (skipped=2)\n", "2 skipped"),
                ("Ran 1600 tests in 1.0s\n", "successful summary")):
            output = "Ran 1600 tests in 0.1s\n\nOK\n" + final
            for value in (output, io.StringIO(output)):
                with self.subTest(final=final, streaming=not isinstance(value, str)):
                    with self.assertRaisesRegex(ValueError, message):
                        verify_release.validate_unittest_output(value)

    def test_json_report_records_successful_gates_and_api_results(self):
        api_result = {
            "counts": {"passed": 1, "failed": 0, "skipped": 1, "total": 2},
            "skips": [{"name": "gpu", "reason": "requires --full"}],
        }
        def run_command(label, command, cwd, reject_unittest_skips=False, capture_output=True):
            # Only the unit gate reports a value (its test count); the others return nothing.
            return 600 if reject_unittest_skips else None

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(verify_release, "compile_python_files", return_value=None), \
             patch.object(verify_release, "run_command", side_effect=run_command), \
             patch.object(verify_release, "run_api_suite", return_value=api_result):
            report_path = Path(tmp) / "report.json"
            self.assertEqual(0, verify_release.main(["--json-report", str(report_path)]))

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("passed", report["status"])
        self.assertEqual("quick", report["mode"])
        # The three evidence-index gates sit between unit_tests and
        # api_contract, matching the order CI runs them in. They were added
        # after "verifier green" was followed by a red CI three times in two
        # days, every time on an index that had not been regenerated.
        self.assertEqual(
            ["compile_python", "test_inventory", "unit_tests",
             "evidence_index", "legacy_audit", "results_index",
             "api_contract", "api_tests"],
            [gate["name"] for gate in report["gates"]],
        )
        self.assertEqual(api_result, report["gates"][-1]["result"])
        # The unit gate records how many tests ran, so sharded runs can be totalled.
        gates = {gate["name"]: gate for gate in report["gates"]}
        self.assertEqual({"tests_ran": 600}, gates["unit_tests"]["result"])
        self.assertTrue(all("result" not in gate for name, gate in gates.items()
                            if name not in ("unit_tests", "api_tests")))
        self.assertNotIn("shard", report)
        self.assertTrue(all(gate["status"] == "passed" for gate in report["gates"]))

    def test_json_report_is_written_on_failure_with_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(
                 verify_release, "compile_python_files",
                 side_effect=ValueError("token=do-not-store compilation failed"),
             ):
            report_path = Path(tmp) / "report.json"
            # Suppress the simulated "RELEASE VERIFICATION FAILED" that main()
            # prints; otherwise it leaks into the suite output and reads like a
            # real failure on every run.
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, verify_release.main(["--json-report", str(report_path)]))

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("failed", report["status"])
        self.assertEqual("compile_python", report["failure"]["gate"])
        self.assertEqual("token=[REDACTED] compilation failed", report["failure"]["message"])
        self.assertNotIn("do-not-store", json.dumps(report))

    def test_json_report_marks_interrupted_gate_and_returns_130(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(verify_release, "compile_python_files", side_effect=KeyboardInterrupt):
            report_path = Path(tmp) / "report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    130, verify_release.main(["--json-report", str(report_path)])
                )
            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual("failed", report["status"])
        self.assertEqual("KeyboardInterrupt", report["failure"]["type"])
        self.assertEqual("failed", report["gates"][0]["status"])


if __name__ == "__main__":
    unittest.main()


class UnexpectedGateFailureReportTests(unittest.TestCase):
    def test_unexpected_gate_exception_is_terminal_and_reported_with_redaction(self):
        for error in (TypeError('token=secret broken gate'), AttributeError('broken gate'),
                      KeyError('missing gate key'), SystemExit(0)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp, 'report.json')
                with patch.object(verify_release, 'compile_python_files', side_effect=error), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout, \
                     contextlib.redirect_stderr(io.StringIO()) as stderr:
                    rc = verify_release.main(['--json-report', str(report_path)])
                self.assertEqual(1, rc)
                report = json.loads(report_path.read_text())
                self.assertEqual('failed', report['status'])
                self.assertEqual('failed', report['gates'][0]['status'])
                self.assertEqual('compile_python', report['failure']['gate'])
                self.assertEqual(type(error).__name__, report['failure']['type'])
                self.assertNotIn('secret', json.dumps(report))
                self.assertIn('RELEASE VERIFICATION FAILED', stderr.getvalue())
                self.assertNotIn('RELEASE VERIFICATION PASSED', stdout.getvalue())

    def test_actual_api_json_loading_cannot_leave_failed_report_running(self):
        for summary in ([], {'schema_version':1, 'mode':'quick', 'tests':[],
                             'counts':{'passed':0,'failed':0,'skipped':0,'total':0}}):
            with self.subTest(summary=summary), tempfile.TemporaryDirectory() as tmp:
                report_path = Path(tmp, 'report.json')
                def command(_label, args, cwd, **kwargs):
                    if '--json-summary' in args:
                        Path(args[args.index('--json-summary')+1]).write_text(json.dumps(summary))
                with patch.object(verify_release, 'compile_python_files', return_value=None), \
                     patch.object(verify_release, 'run_command', side_effect=command), \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(1, verify_release.main(['--json-report', str(report_path)]))
                report = json.loads(report_path.read_text())
                self.assertEqual('failed', report['status'])
                self.assertEqual('api_tests', report['failure']['gate'])
                self.assertEqual('failed', report['gates'][-1]['status'])


class CompileArtifactIsolationTests(unittest.TestCase):
    def test_actual_bytecode_is_temporary_and_source_tree_is_unchanged(self):
        import marshal
        import py_compile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / 'readonly'
            repo.mkdir()
            files = []
            for name, value in (('a/same.py', 7), ('b/same.py', 9)):
                path = repo / name
                path.parent.mkdir()
                path.write_text(f'VALUE = {value}\n', encoding='utf-8')
                path.chmod(0o444)
                path.parent.chmod(0o555)
                files.append(path)
            before = {p.relative_to(repo): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
            compiler = py_compile.compile
            outputs = []
            def compile_to_temporary(source, *args, **kwargs):
                cfile = kwargs.get('cfile')
                self.assertIsNotNone(cfile, 'Compiler would write into the read-only source checkout')
                target = Path(cfile)
                self.assertNotIn(repo, target.parents)
                result = compiler(source, *args, **kwargs)
                namespace = {}
                exec(marshal.loads(target.read_bytes()[16:]), namespace)
                outputs.append((target, namespace['VALUE']))
                return result
            try:
                with patch.object(verify_release, 'get_python_paths', return_value=files), \
                     patch.object(verify_release.py_compile, 'compile', side_effect=compile_to_temporary):
                    verify_release.compile_python_files(repo)
                self.assertEqual([7, 9], [value for _, value in outputs])
                self.assertEqual(2, len({path for path, _ in outputs}))
                self.assertTrue(all(not path.exists() for path, _ in outputs))
                self.assertEqual(before, {p.relative_to(repo): p.read_bytes() for p in repo.rglob('*') if p.is_file()})
                self.assertFalse(any(repo.rglob('__pycache__')))
            finally:
                for path in files:
                    path.parent.chmod(0o755)
                    path.chmod(0o644)

    def test_compile_error_propagates_and_removes_temporary_bytecode(self):
        import py_compile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            good, bad = repo / 'good.py', repo / 'bad.py'
            good.write_text('VALUE = 7\n', encoding='utf-8')
            bad.write_text('def broken(\n', encoding='utf-8')
            compiler = py_compile.compile
            outputs = []
            def compile_file(source, *args, **kwargs):
                outputs.append(Path(kwargs['cfile']))
                return compiler(source, *args, **kwargs)
            with patch.object(verify_release, 'get_python_paths', return_value=[good,bad]), \
                 patch.object(verify_release.py_compile, 'compile', side_effect=compile_file):
                with self.assertRaises(py_compile.PyCompileError):
                    verify_release.compile_python_files(repo)
            self.assertEqual(2, len(outputs))
            self.assertTrue(all(not output.exists() for output in outputs))
            self.assertEqual({'good.py', 'bad.py'}, {path.name for path in repo.iterdir()})
