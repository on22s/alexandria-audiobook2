"""Real pilot validator and both Bash reuse boundaries on known paired rows."""
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from experiments import pdnc_context_evidence as context
from tests.test_pdnc_campaign_guards import make_pilot_fixture

REPO = Path(__file__).resolve().parents[2]


class PilotCompletionTests(unittest.TestCase):
    def test_all_interventions_reuse_complete_pass_and_fail_without_mutating_evidence(self):
        for intervention in ("evidence", "sequence", "targeted_sequence"):
            for advance in (True, False):
                with self.subTest(intervention=intervention, advance=advance), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    document, bundle = make_pilot_fixture(root, advance, intervention)
                    path = root / "pilot.json"
                    path.write_text(json.dumps(document))
                    before = path.read_bytes(), bundle.read_bytes()
                    self.assertEqual("pass" if advance else "fail",
                                     context.get_context_pilot_state(path, bundle, intervention))
                    if advance:
                        self.assertTrue(context.require_passing_pilot(path, bundle, intervention)["advance"])
                    else:
                        with self.assertRaisesRegex(ValueError, "did not pass"):
                            context.require_passing_pilot(path, bundle, intervention)
                    self.assertEqual(before, (path.read_bytes(), bundle.read_bytes()))

    def test_partial_forged_and_changed_input_pilots_have_no_scientific_verdict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good, bundle = make_pilot_fixture(root)
            path = root / "pilot.json"
            changes = [lambda d: d["meta"].pop("finished"),
                       lambda d: d["rows"].pop(),
                       lambda d: d["meta"]["decision"].update(advance=False),
                       lambda d: d["meta"]["decision"].update(advance=1),
                       lambda d: d["meta"]["decision"].update(n=0)]
            for index, change in enumerate(changes):
                with self.subTest(case=index):
                    document = copy.deepcopy(good)
                    change(document)
                    path.write_text(json.dumps(document))
                    self.assertEqual("missing", context.get_context_pilot_state(path, bundle, "evidence"))
                    with self.assertRaises(ValueError):
                        context.require_passing_pilot(path, bundle, "evidence")
            path.write_text(json.dumps(good))
            bundle.write_bytes(bundle.read_bytes() + b" ")
            self.assertEqual("missing", context.get_context_pilot_state(path, bundle, "evidence"))
            with self.assertRaises(ValueError):
                context.require_passing_pilot(path, bundle, "evidence")


    def test_forged_passing_decision_cannot_open_confirmatory_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document, bundle = make_pilot_fixture(root, False)
            self.assertFalse(document["meta"]["decision"]["advance"])
            document["meta"]["decision"]["advance"] = True
            path = root / "pilot.json"
            path.write_text(json.dumps(document))
            self.assertEqual("missing", context.get_context_pilot_state(path, bundle, "evidence"))
            with self.assertRaisesRegex(ValueError, "decision does not match"):
                context.require_passing_pilot(path, bundle, "evidence")


class ResearchPilotBoundaryTests(unittest.TestCase):
    def run_boundary(self, kind, *, worker_rc=0, generated_kind="valid", pause_rc=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / "ab_test_runtime"
            runtime.mkdir()
            experiments = runtime / "experiments"
            experiments.mkdir()
            document, _ = make_pilot_fixture(runtime, kind != "negative", "sequence")
            pilot = experiments / "pdnc_sequence__pilot__local-llamacpp.json"
            if kind != "missing":
                pilot.write_text(json.dumps(document) if kind in ("positive", "negative") else
                                 '{"meta":{"decision":{"advance":true}}}')
            generated, _ = make_pilot_fixture(runtime, True, "sequence")
            payload = root / "payload.json"
            payload.write_text(json.dumps(generated) if generated_kind == "valid" else '{}')
            (root / "app/experiments").mkdir(parents=True)
            (root / "app/experiments/pdnc_context_evidence.py").symlink_to(
                REPO / "app/experiments/pdnc_context_evidence.py")
            (root / "run_chains").mkdir()
            campaign = root / "run_chains/pdnc_context_evidence.sh"
            campaign.write_text('#!/bin/bash\necho DISPATCH\ncp "$FIXTURE_PAYLOAD" "$FIXTURE_PILOT"\n'
                                'exit "$FIXTURE_WORKER_RC"\n')
            campaign.chmod(0o755)
            path = Path(os.environ.get("ALEXANDRIA_TEST_RESEARCH_SOURCE",
                                      str(REPO / "run_chains/remaining_gpu_research.sh")))
            source = path.read_text()
            start = source.index('pilot_out=') if 'pilot_out=' in source else source.index('if [ ! -f "$runtime/experiments/pdnc_')
            block = source[start:source.index('\nasr_out=', start)]
            script = ('set -uo pipefail\nrepo=$1; runtime=$2; python=$3\n'
                      'wait_if_paused() { return ' + str(pause_rc) + '; }\n' + block + '\necho NEXT_STAGE\n')
            result = subprocess.run(['bash', '-c', script, 'fixture', str(root), str(runtime), sys.executable],
                capture_output=True, text=True, timeout=10,
                env=dict(os.environ, PYTHONPATH=str(REPO / 'app'),
                         ALEXANDRIA_RUNTIME_ROOT=str(runtime), FIXTURE_PAYLOAD=str(payload),
                         FIXTURE_PILOT=str(pilot), FIXTURE_WORKER_RC=str(worker_rc)))
            return result

    def test_complete_positive_and_negative_pilots_skip_expensive_campaign(self):
        for kind in ("positive", "negative"):
            with self.subTest(kind=kind):
                result = self.run_boundary(kind)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn("DISPATCH", result.stdout)
                self.assertIn("NEXT_STAGE", result.stdout)

    def test_partial_and_missing_pilots_run_campaign_and_validate_result(self):
        for kind in ("partial", "missing"):
            with self.subTest(kind=kind):
                result = self.run_boundary(kind)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertIn("NEXT_STAGE", result.stdout)

    def test_failed_campaign_or_incomplete_success_cannot_start_later_research(self):
        for worker_rc, generated_kind in ((7, "valid"), (0, "partial")):
            with self.subTest(worker_rc=worker_rc, generated_kind=generated_kind):
                result = self.run_boundary("missing", worker_rc=worker_rc, generated_kind=generated_kind)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("DISPATCH", result.stdout)
                self.assertNotIn("NEXT_STAGE", result.stdout)

    def test_pause_refusal_blocks_campaign_before_dispatch(self):
        result = self.run_boundary("missing", pause_rc=4)
        self.assertEqual(4, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("DISPATCH", result.stdout)
        self.assertNotIn("NEXT_STAGE", result.stdout)
