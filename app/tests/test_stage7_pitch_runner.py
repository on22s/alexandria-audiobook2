import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import run_stage7_pitch as runner


class Stage7PitchRunnerTests(unittest.TestCase):
    def test_pilot_gate_requires_three_real_measurements(self):
        good = {"rows": [{"pitch_status": "measured"}] * 3
                         + [{"pitch_status": "tracker_failure"}]}
        runner.require_pilot_gate(good)
        with self.assertRaisesRegex(RuntimeError, "only 2/4"):
            runner.require_pilot_gate({
                "rows": [{"pitch_status": "measured"}] * 2
                        + [{"pitch_status": "tracker_failure"}] * 2})

    def test_full_job_is_all_adapters_seeds_and_passages(self):
        with patch.object(runner.os.path, "exists", return_value=False), \
                patch.object(runner, "run_gpu_job") as run_gpu, \
                patch.object(runner, "validate_artifact") as validate:
            runner.ensure_full(75)
        arguments = run_gpu.call_args.args[6]
        self.assertEqual(["--out-dir", runner.FULL_DIR, "--out", runner.FULL],
                         arguments)
        validate.assert_called_once_with(runner.FULL, expected_count=1350)


if __name__ == "__main__":
    unittest.main()


class PilotReportingTests(unittest.TestCase):
    def test_success_does_not_claim_four_pitch_tracks_when_only_three_measured(self):
        import contextlib
        import io
        doc={'rows':[{'pitch_status':'measured'}]*3+[{'pitch_status':'tracker_failure'}]}
        output=io.StringIO()
        with patch.object(runner.os.path,'exists',return_value=True), \
             patch.object(runner,'run_gpu_job') as gpu, \
             patch.object(runner,'validate_artifact',return_value=doc) as validate, \
             contextlib.redirect_stdout(output):
            runner.ensure_pilot('fixture-adapter')
        gpu.assert_not_called()
        validate.assert_called_once_with(runner.PILOT,expected_count=4)
        self.assertIn('validated strictly and cleared',output.getvalue())
        self.assertNotIn('4/4',output.getvalue())

    def test_failed_measurement_gate_does_not_print_success(self):
        import contextlib
        import io
        output=io.StringIO()
        doc={'rows':[{'pitch_status':'measured'}]*2+[{'pitch_status':'tracker_failure'}]*2}
        with patch.object(runner.os.path,'exists',return_value=True), \
             patch.object(runner,'validate_artifact',return_value=doc), \
             contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError,'only 2/4'):
                runner.ensure_pilot('fixture-adapter')
        self.assertEqual('',output.getvalue())


class Stage7IndexRefreshTests(unittest.TestCase):
    def test_checkpoint_refreshes_all_canonical_dependencies_and_checks_outputs(self):
        import contextlib
        import io
        import json
        import shutil
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "repository")
            root.mkdir()
            shutil.copyfile(Path(runner.REPO, "refresh_indexes.py"), root / "refresh_indexes.py")
            scripts = {
                "tools/audit/audit_experiment_artifacts.py":
                    "text='current structural audit'\noutput=Path('structural.json')\n",
                "tools/audit/audit_legacy_attribution.py":
                    "text='current legacy audit'\noutput=Path('legacy.json')\n",
                "tools/audit/collect_results.py":
                    "audit=Path('structural.json').read_text()+' '+Path('legacy.json').read_text()\n"
                    "names=('pitch_profile_matrix_pilot.json','pitch_profile_matrix.json')\n"
                    "text='artifact,note\\n'+''.join(name+','+audit+'\\n' for name in names)\n"
                    "output=Path('results_index.csv')\n"
                    "if '--check' not in sys.argv:Path('RESULTS_INDEX.md').write_text("
                    "'| artifact | why |\\n|---|---|\\n'+''.join('| `'+name+'` | '+audit+' |\\n' for name in names))\n",
                "app/experiments/goal_evidence_audit.py":
                    "text='goal current|'+Path('results_index.csv').read_text()\n"
                    "output=Path('goal.json')\n",
            }
            for relative, body in scripts.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("import sys\nfrom pathlib import Path\n" + body
                    + "if '--check' in sys.argv:\n"
                    + "    raise SystemExit(0 if output.read_text()==text else 1)\n"
                    + "output.write_text(text)\n")
            # A real isolated unit-suite child checks the final published files.
            suite = root / "app/test_refresh_fixture.py"
            suite.write_text("import unittest\nfrom pathlib import Path\n"
                "class PublishedFiles(unittest.TestCase):\n"
                "    def test_goal_retains_both_audits_and_pitch_rows(self):\n"
                "        text=Path('goal.json').read_text()\n"
                "        for value in ('current structural audit','current legacy audit',"
                "'pitch_profile_matrix_pilot.json','pitch_profile_matrix.json'):\n"
                "            self.assertIn(value,text)\n")
            adapters = [{'adapter':f'voice{index}'} for index in range(75)]
            output = io.StringIO()
            with patch.object(runner, 'REPO', str(root)), \
                 patch.object(runner, 'PYTHON', sys.executable), \
                 patch.object(runner, 'load_adapters', return_value=adapters), \
                 patch.object(runner, 'ensure_pilot') as pilot, \
                 patch.object(runner, 'ensure_full') as full, \
                 contextlib.redirect_stdout(output):
                runner.main()
            pilot.assert_called_once_with('voice0')
            full.assert_called_once_with(75)
            self.assertIn('Stage 7 pitch checkpoint complete.', output.getvalue())
            self.assertEqual('current legacy audit', (root / 'legacy.json').read_text())
            goal = (root / 'goal.json').read_text()
            for value in ('current structural audit', 'current legacy audit',
                          'pitch_profile_matrix_pilot.json', 'pitch_profile_matrix.json'):
                self.assertIn(value, goal)


class Stage7CheckpointResumeTests(unittest.TestCase):
    def test_interrupted_producer_resumes_checkpoint_before_final_publication(self):
        import contextlib
        import hashlib
        import io
        import json
        import shutil
        import tempfile
        import types
        import numpy as np
        import soundfile as sf
        from experiments import generation, pitch_profile_matrix as pitch, provenance
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, 'repository')
            app = root / 'app'
            (app / 'experiments').mkdir(parents=True)
            for relative in ('app/experiments/generation.py', 'app/tts.py'):
                shutil.copyfile(Path(runner.REPO, relative), root / relative)
            chunks = root / 'source_chunks.json'
            texts = ('A short narrative passage.', 'Hello, how are you today?')
            chunks.write_text(json.dumps([{'text':text} for text in texts]))
            source = root / 'passages.json'
            source.write_text(json.dumps({'source_artifact':'source_chunks.json',
                'selection_rule':'two fixed CPU fixture passages',
                'passages':[{'text':text, 'category':category, 'source_index':index,
                             'source_sha256':hashlib.sha256(text.encode()).hexdigest()}
                            for index,(text,category) in enumerate(zip(texts,('narration','plain_dialogue')))]}))
            config = app / 'config.json'
            config.write_text('{}')
            models = root / 'lora_models'
            adapter = models / 'fixture-adapter'
            adapter.mkdir(parents=True)
            (adapter / 'adapter_config.json').write_text('{}')
            (adapter / 'adapter_model.safetensors').write_bytes(b'CPU identity marker')
            manifest = models / 'manifest.json'
            manifest.write_text(json.dumps([{'id':'fixture-adapter', 'voice_features':{'mean_f0':120}}]))
            pilot = root / 'results/pilot.json'
            wavs = root / 'pilot_audio'
            calls = []
            interrupted = True

            def render(engine, text, emotion, speaker, voices, entry, path):
                nonlocal interrupted
                calls.append(Path(path).name)
                if interrupted and len(calls) == 2:
                    raise KeyboardInterrupt('controlled interruption after one checkpoint row')
                rate = 16000
                audio = 0.2*np.sin(2*np.pi*120*np.arange(rate*2)/rate)
                sf.write(path, audio, rate, subtype='PCM_16')

            def run_cpu_producer(*args):
                flags = ['pitch_profile_matrix.py', '--source', str(source),
                         '--config', str(config), '--manifest', str(manifest)] + args[6]
                with patch.object(sys, 'argv', flags):
                    pitch.main()

            def fixture_provenance(script, args, **extra):
                # Git/environment provenance is outside this CPU resume fixture;
                # actual input/adapter hashes, row identity and PCM validation run.
                values = dict(vars(args))
                for key in ('source','config','manifest','out_dir','out'):
                    values[key] = os.path.relpath(values[key], root)
                return {'script':'pitch_profile_matrix.py', 'args':values, **extra}

            original_tts = sys.modules.get('tts')
            sys.modules['tts'] = types.SimpleNamespace(TTSEngine=lambda cfg:object())
            def restore_tts():
                if original_tts is None:
                    sys.modules.pop('tts', None)
                else:
                    sys.modules['tts'] = original_tts
            self.addCleanup(restore_tts)

            with patch.object(runner, 'PILOT', str(pilot)), \
                 patch.object(runner, 'PILOT_DIR', str(wavs)), \
                 patch.object(runner, 'run_gpu_job', side_effect=run_cpu_producer) as dispatch, \
                 patch.object(pitch, 'REPO', str(root)), \
                 patch.object(pitch, 'APP', str(app)), \
                 patch.object(provenance, 'REPO', str(root)), \
                 patch.object(provenance, 'provenance', side_effect=fixture_provenance), \
                 patch.object(provenance, 'get_reproducible_harness_source', return_value='CPU-FIXTURE'), \
                 patch.object(generation, 'render', side_effect=render), \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(KeyboardInterrupt, 'controlled interruption'):
                    runner.ensure_pilot('fixture-adapter')
                self.assertFalse(pilot.exists())
                checkpoint = Path(str(pilot)+'.ckpt')
                saved = json.loads(checkpoint.read_text())
                self.assertEqual('in_progress', saved['status'])
                self.assertEqual(1, len(saved['rows']))
                first = root / saved['rows'][0]['wav']
                prior_bytes = first.read_bytes()
                calls.clear()
                interrupted = False
                runner.ensure_pilot('fixture-adapter')
                self.assertEqual(3, len(calls))
                self.assertNotIn(first.name, calls)
                self.assertEqual(prior_bytes, first.read_bytes())
                final = json.loads(pilot.read_text())
                self.assertEqual('complete', final['status'])
                self.assertEqual(4, len(final['rows']))
                self.assertEqual(4, len({(r['seed'],r['passage']) for r in final['rows']}))
                self.assertTrue(all(r['pitch_status']=='measured' for r in final['rows']))
                self.assertFalse(checkpoint.exists())
                runner.ensure_pilot('fixture-adapter')
                self.assertEqual(2, dispatch.call_count)
                self.assertEqual(3, len(calls))
