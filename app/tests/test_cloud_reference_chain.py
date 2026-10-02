"""Run the real cloud reference chain with CPU-only artifact producers."""
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
import wave


REPO = Path(__file__).resolve().parents[2]
CHAIN = REPO / "run_chains/cloud_reference_spread_resume_20260824.sh"


class CloudReferenceChainTests(unittest.TestCase):
    def test_repeat_skips_every_completed_arm_without_rewriting_its_artifacts(self):
        result, calls, _, _, compared = self._run(repeat=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(compared)
        self.assertEqual([('compare', 0)], [(c['stage'], c['arm']) for c in calls])
        self.assertEqual(3, result.stdout.count('SKIP complete reference arm'))

    def test_repeat_reruns_only_the_arm_with_missing_receipt_or_bad_audio(self):
        for damage in ('receipt', 'audio'):
            with self.subTest(damage=damage):
                result, calls, _, _, compared = self._run(repeat=True, damage=damage)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(compared)
                self.assertEqual([('generate', 3), ('score', 3), ('compare', 0)],
                                 [(c['stage'], c['arm']) for c in calls])

    def test_arm0_is_checked_before_any_expensive_worker(self):
        for value in ('missing', '{', '[]', '{}',
                      '{"summary":{"clone":{"ecapa":null,"n":2}}}',
                      '{"summary":{"clone":{"ecapa":NaN,"n":2}}}',
                      '{"summary":{"clone":{"ecapa":1.1,"n":2}}}',
                      '{"summary":{"clone":{"ecapa":true,"n":2}}}',
                      '{"summary":{"clone":{"ecapa":0.7,"n":0}}}'):
            with self.subTest(arm0=value):
                result, calls, prior, remaining, compared = self._run(arm0=value)
                self.assertNotEqual(0, result.returncode)
                self.assertIn('unusable reference score', result.stderr)
                self.assertEqual([], calls)
                self.assertEqual(prior, remaining)
                self.assertFalse(compared)

    def test_row_only_legacy_arm0_is_read_by_existing_metric_reader(self):
        result, calls, _, _, compared = self._run(
            arm0='{"rows":[{"clone":{"ecapa":0.7}},{"clone":{"ecapa":0.8}}]}')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(compared)
        self.assertEqual(7, len(calls))

    def _run(self, failed_stage=None, failed_arm=1, failure_code=19, arm0="valid", source=None, repeat=False, damage=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app/experiments").mkdir(parents=True)
            work = root / "ab_test_runtime/reference_spread"
            results = root / "ab_test_runtime/experiments"
            work.mkdir(parents=True)
            results.mkdir()
            (root / "app/config.json").write_text('{}')
            for name in ('experiments/generation.py', 'tts.py', 'audio_validation.py'):
                (root / 'app' / name).write_text('# CPU source fixture\n')
            for name in ('ref.wav', 'human.wav'):
                with wave.open(str(work / name), 'wb') as handle:
                    handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
                    handle.writeframes(b'\x00\x10' * 4800)
            score0 = results / "reference_spread__en_score_arm0.json"
            if arm0 != "missing":
                score0.write_text(json.dumps({"summary": {"clone": {"ecapa": .75, "n": 2}}})
                                  if arm0 == "valid" else arm0)
            builds = {}
            prior = {}
            for arm in (1, 2, 3):
                build = work / f"build_spread{arm}.json"
                build.write_text(json.dumps({"arm": arm, "ref_sample": str(work / 'ref.wav'),
                    "ref_text": "reference", "ref_source_id": "reference1",
                    "test": [{"id": "line1", "book": "speaker", "text": "line one",
                              "human_wav": str(work / 'human.wav'), "seconds": .2}]}))
                builds[build] = build.read_bytes()
                for stage in ("generate", "score"):
                    output = results / f"reference_spread__en_{stage}_arm{arm}.json"
                    output.write_text(json.dumps({"arm": arm, "old": True}))
                    prior[str(output)] = output.read_bytes()
            producer = r"""import json, os, pathlib, re, sys
root = pathlib.Path(__file__).resolve().parents[2]
args = sys.argv[1:]
if '--check-score' in args:
    os.execv(sys.executable, [sys.executable, os.environ['REAL_COMPARE']] + args)
stage = pathlib.Path(__file__).stem.removeprefix('ljspeech_')
if stage.startswith('reference_spread'):
    stage = 'compare'
    arm = 0
else:
    output = pathlib.Path(args[args.index('--out') + 1])
    arm = int(re.search(r'arm([123])', str(output)).group(1))
with (root/'calls.jsonl').open('a') as handle:
    handle.write(json.dumps({'stage': stage, 'arm': arm, 'args': args}) + '\n')
if stage == os.environ.get('FIXTURE_FAILED_STAGE') and str(arm) == os.environ.get('FIXTURE_FAILED_ARM'):
    raise SystemExit(int(os.environ['FIXTURE_FAILURE_CODE']))
output = pathlib.Path(args[args.index('--out') + 1])
if stage == 'generate':
    assert args[args.index('--arms') + 1] == 'clone'
    assert args[args.index('--limit') + 1] == '0'
    json.loads(pathlib.Path(args[args.index('--build') + 1]).read_text())
if stage == 'score':
    generated = json.loads(pathlib.Path(args[args.index('--generated') + 1]).read_text())
    assert generated['arm'] == arm
doc={'stage':stage,'arm':arm,'old':False}
if stage=='generate':
    import wave
    wav=pathlib.Path(args[args.index('--out-dir')+1])/'line1__clone.wav'
    wav.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(wav),'wb') as h:
        h.setnchannels(1);h.setsampwidth(2);h.setframerate(24000);h.writeframes(b'\x00\x10'*4800)
    build=json.loads(pathlib.Path(args[args.index('--build')+1]).read_text())
    row=dict(build['test'][0]);row['human_seconds']=row.pop('seconds');row['clone_wav']=str(wav)
    doc.update(seed=1234,arms=['clone'],reference_id=build['ref_source_id'],failures=[],rows=[row])
if stage=='score':
    row=generated['rows'][0]
    doc.update(arms=['clone'],ecapa_error=None,source=args[args.index('--generated')+1],
        rows=[dict(id=row['id'],book=row['book'],human_seconds=row['human_seconds'],clone={'ecapa':.75})],
        summary={'clone':{'ecapa':.75,'n':1}})
output.write_text(json.dumps(doc))
"""
            for name in ("ljspeech_generate.py", "ljspeech_score.py", "reference_spread_compare.py"):
                (root / "app/experiments" / name).write_text(producer)
            (root / 'app/experiments/reference_spread_resume.py').write_text(
                'import runpy\nrunpy.run_path(' + repr(str(REPO / 'app/experiments/reference_spread_resume.py'))
                + ', run_name="__main__")\n')
            source = CHAIN.read_text(encoding="utf-8") if source is None else source
            for variable, value in (('REPO', str(root)), ('PY', sys.executable)):
                literal = re.search(r'(?m)^' + variable + r'="[^$\n]*"$', source)
                if literal:
                    source = source[:literal.start()] + variable + '=' + shlex.quote(value) + source[literal.end():]
            script = root / "chain.sh"
            script.write_text(source)
            environment = dict(os.environ, REAL_COMPARE=str(REPO / "app/experiments/reference_spread_compare.py"), FIXTURE_FAILED_STAGE=failed_stage or "",
                               FIXTURE_FAILED_ARM=str(failed_arm), FIXTURE_FAILURE_CODE=str(failure_code))
            result = subprocess.run(["bash", str(script)], cwd=root, env=environment,
                                    capture_output=True, text=True, timeout=10)
            if repeat:
                before_repeat = {path: Path(path).read_bytes() for path in prior}
                if damage == 'receipt':
                    (results / 'reference_spread__en_generate_arm3.json.resume.json').unlink()
                elif damage == 'audio':
                    (work / 'arm3/line1__clone.wav').write_bytes(b'broken audio')
                (root / 'calls.jsonl').unlink()
                result = subprocess.run(["bash", str(script)], cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=10)
                if damage is None:
                    self.assertEqual(before_repeat, {path: Path(path).read_bytes() for path in prior})
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()] if (root / "calls.jsonl").exists() else []
            remaining = {path: Path(path).read_bytes() for path in prior}
            for path, content in builds.items():
                self.assertEqual(content, path.read_bytes())
            comparison = results / "reference_spread__en_compare.json"
            return result, calls, prior, remaining, comparison.exists()

    def test_generation_failure_prevents_scoring_stale_artifact_and_comparison(self):
        for arm in (1, 2, 3):
            with self.subTest(arm=arm):
                result, calls, prior, remaining, compared = self._run("generate", arm, 19)
                self.assertEqual(19, result.returncode, result.stdout + result.stderr)
                self.assertFalse(compared)
                expected = [(stage, n) for n in range(1, arm) for stage in ("generate", "score")]
                expected.append(("generate", arm))
                self.assertEqual(expected, [(c['stage'], c['arm']) for c in calls])
                for path, content in prior.items():
                    if path.endswith(f"arm{arm}.json"):
                        self.assertEqual(content, remaining[path])

    def test_score_failure_prevents_later_arms_and_comparison(self):
        for arm in (1, 2, 3):
            with self.subTest(arm=arm):
                result, calls, prior, remaining, compared = self._run("score", arm, 37)
                self.assertEqual(37, result.returncode, result.stdout + result.stderr)
                self.assertFalse(compared)
                expected = [(stage, n) for n in range(1, arm+1) for stage in ("generate", "score")]
                self.assertEqual(expected, [(c['stage'], c['arm']) for c in calls])
                path = next(p for p in prior if p.endswith(f"score_arm{arm}.json"))
                self.assertEqual(prior[path], remaining[path])

    def test_success_still_generates_scores_and_compares_all_three_arms(self):
        result, calls, prior, remaining, compared = self._run()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertTrue(compared)
        self.assertEqual([(stage, n) for n in (1, 2, 3) for stage in ("generate", "score")]
                         + [("compare", 0)], [(c['stage'], c['arm']) for c in calls])
        for path in prior:
            self.assertFalse(json.loads(remaining[path])['old'])
