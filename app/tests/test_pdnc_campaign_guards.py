"""Actual PDNC shell decisions, without a live model or server."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[2]
CHAIN = REPO / "run_chains/pdnc_context_evidence.sh"


def make_pilot_fixture(root, advance=True, intervention="evidence"):
    from experiments import pdnc_context_evidence as context

    books = context.get_context_books("pilot", intervention)
    arms = context.get_context_arm_names(intervention)
    entries = [dict(id=f"{book}-{index:05d}", line=f"Hello {book} {index}", expected_speaker="Alice")
               for book in books for index in range(4)]
    raw = json.dumps(dict(books=list(books), limit=context.DEFAULT_LIMIT, entries=entries)).encode()
    bundle = root / "pdnc_inputs" / f"pdnc_{intervention}__pilot.json"
    bundle.parent.mkdir(exist_ok=True)
    bundle.write_bytes(raw)
    rows = [dict(arm=arm, id=entry["id"].split("-", 1)[0]+":"+entry["id"],
                 line=entry["line"], expected="Alice",
                 predicted="Bob" if advance and arm == "baseline" else "Alice",
                 correct=not (advance and arm == "baseline"))
            for arm in arms for entry in entries]
    document = dict(meta=dict(phase="pilot", intervention=intervention, finished=1,
        validation="ok", gold_sha256=hashlib.sha256(raw).hexdigest(),
        decoding=dict(books=list(books), limit=context.DEFAULT_LIMIT),
        decision=context.get_pilot_decision(rows, intervention)),
        summary={arm:dict(n=len(entries), correct=sum(row["correct"] for row in rows if row["arm"] == arm))
                 for arm in arms}, rows=rows)
    return document, bundle



class PdncCampaignGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "app/env/bin").mkdir(parents=True)
        (self.root / "app/env/bin/python").symlink_to(sys.executable)
        self.foreign = self.root / "foreign"
        (self.foreign / "app").mkdir(parents=True)
        self.source = Path(os.environ.get("ALEXANDRIA_TEST_PDNC_SOURCE", str(CHAIN))).read_text(encoding="utf-8")

    def _bash(self, body, cwd=None):
        return subprocess.run(["bash", "-c", 'set -uo pipefail\nrepo=$1\n' + body,
                               "fixture", str(self.root)], cwd=cwd or self.root,
                              capture_output=True, text=True, timeout=10,
                              env=dict(os.environ, PYTHONPATH=str(REPO / "app")))

    def _pilot(self, payload, generated=None, worker_rc=0):
        pilot = self.root / "pilot.json"
        if payload is not None:
            pilot.write_bytes(payload)
        elif pilot.exists():
            pilot.unlink()
        worker = self.root / "worker_started"
        if worker.exists():
            worker.unlink()
        generated_file = self.root / "generated.json"
        generated_file.write_bytes(generated or b"{}")
        start = self.source.index("get_pilot_state() {")
        end = self.source.index('if [ -f "$confirmatory" ]', start)
        fixture = '\n'.join([
            'pilot=$repo/pilot.json; run_dir=$repo; runtime_root=$repo; intervention=evidence',
            'timeout() {',
            '    touch "$repo/worker_started"',
            '    cp "$repo/generated.json" "$pilot"',
            '    return ' + str(worker_rc),
            '}',
            self.source[start:end],
            'echo CONFIRMATORY_NEXT',
        ])
        before = pilot.read_bytes() if pilot.exists() else None
        result = self._bash(fixture)
        if before is not None and not worker.exists():
            self.assertEqual(before, pilot.read_bytes())
        return result, worker.exists()

    def test_corrupt_existing_pilot_is_regenerated_before_scientific_decision(self):
        document, _ = make_pilot_fixture(self.root)
        for payload in (b'{"meta":', b'[]', b'{"meta": null}',
                        b'{"meta":{"decision":{"advance":true}}}'):
            with self.subTest(payload=payload):
                result, launched = self._pilot(payload, generated=json.dumps(document).encode())
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(launched)
                self.assertIn("CONFIRMATORY_NEXT", result.stdout)
                self.assertNotIn("PILOT_GATE_FAIL", result.stdout)

    def test_corrupt_generated_pilot_is_checkpoint_error(self):
        result, launched = self._pilot(None, generated=b'{"meta":')
        self.assertTrue(launched)
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("PILOT_GATE_FAIL", result.stdout)
        self.assertNotIn("CONFIRMATORY_NEXT", result.stdout)

    def test_worker_success_with_unmeasured_decision_refuses_scientific_verdict(self):
        for advance in (True, False):
            with self.subTest(advance=advance):
                payload = json.dumps({"meta": {"decision": {"advance": advance}}}).encode()
                result, launched = self._pilot(None, generated=payload)
                self.assertTrue(launched)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("PILOT_INCOMPLETE", result.stdout)
                self.assertNotIn("PILOT_GATE_FAIL", result.stdout)
                self.assertNotIn("CONFIRMATORY_NEXT", result.stdout)

    def test_valid_pilot_keeps_scientific_gate_decision(self):
        for advance in (True, False):
            document, _ = make_pilot_fixture(self.root, advance)
            payload = json.dumps(document).encode()
            for existing in (True, False):
                with self.subTest(advance=advance, existing=existing):
                    result, launched = self._pilot(payload if existing else None, generated=payload)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(not existing, launched)
                    self.assertEqual(advance, "CONFIRMATORY_NEXT" in result.stdout)
                    self.assertEqual(not advance, "PILOT_GATE_FAIL" in result.stdout)

    def test_pilot_worker_failure_keeps_exit_status(self):
        result, launched = self._pilot(None, generated=b'{}', worker_rc=7)
        self.assertTrue(launched)
        self.assertEqual(7, result.returncode, result.stdout + result.stderr)
        self.assertIn("PILOT_FAILED rc=7", result.stdout)
        self.assertNotIn("CONFIRMATORY_NEXT", result.stdout)
