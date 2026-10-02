"""Reject accidental CPU enrichment before model loading or publication."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.test_enricher_preflight_json import load_enricher

class EnricherCpuAdmissionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = patch.dict('os.environ', GPU_LOCK=str(self.root/'gpu.lock'), ALEXANDRIA_GPU_LOCK_HELD='0')
        env.start(); self.addCleanup(env.stop)
        self.module, self.provider = load_enricher()
        self.module.llama_supports_gpu_offload = lambda: False

    def test_gpu_cpu_build_refuses_before_model_and_preserves_output(self):
        source, output = self.root/'input.json', self.root/'output.json'
        source.write_text('[{"text":"hello"}]'); output.write_text('[{"prior":true}]')
        with patch.object(self.module, 'system_has_gpu', return_value=(True, 'AMD/ROCm')), \
             patch.object(sys, 'argv', ['enricher', '--model-path', 'fixture.gguf', '--input-file', str(source), '--output-file', str(output), '--emotional-tone']), \
             self.assertLogs(self.module.logger, level='ERROR') as logs:
            with self.assertRaises(SystemExit) as raised: self.module.main()
        self.assertEqual(1, raised.exception.code)
        self.provider.Llama.assert_not_called()
        self.assertEqual([{'prior':True}], json.loads(output.read_text()))
        for fragment in ('AMD/ROCm', 'no GPU support', 'GGML_HIP', '--allow-cpu-fallback'):
            self.assertIn(fragment, '\n'.join(logs.output))
        with patch.object(self.module, 'system_has_gpu', return_value=(False, None)):
            enricher = self.module.LLMEnricher('fixture.gguf'); enricher.close()

    def test_explicit_cli_override_loads_and_publishes(self):
        source, output = self.root/'input.json', self.root/'output.json'
        source.write_text('[{"text":"hello"}]')
        self.provider.Llama.return_value.return_value = {'choices':[{'text':'{"emotional_tone":"calm"}'}]}
        with patch.object(self.module, 'system_has_gpu', return_value=(True, 'NVIDIA')), \
             patch.object(sys, 'argv', ['enricher', '--model-path', 'fixture.gguf', '--input-file', str(source), '--output-file', str(output), '--emotional-tone', '--allow-cpu-fallback']):
            try:
                self.module.main()
            except SystemExit as error:
                self.fail(f"Explicit CPU opt-in was refused with exit {error.code}")
        self.assertEqual([{'text':'hello','emotional_tone':'calm'}], json.loads(output.read_text()))
        self.provider.Llama.assert_called_once()
        self.provider.Llama.return_value.close.assert_called_once()

    def test_no_gpu_and_offload_build_keep_model_loading(self):
        for offload, has_gpu in ((False, False), (True, True)):
            with self.subTest(offload=offload), patch.object(self.module, 'llama_supports_gpu_offload', return_value=offload), patch.object(self.module, 'system_has_gpu', return_value=(has_gpu, 'NVIDIA')):
                self.provider.Llama.reset_mock()
                enricher = self.module.LLMEnricher('fixture.gguf'); enricher.close()
                self.provider.Llama.assert_called_once()

    def test_no_fields_never_probes_or_loads_model(self):
        with patch.object(self.module, 'system_has_gpu') as probe:
            enricher = self.module.LLMEnricher('fixture.gguf', []); enricher.close()
        probe.assert_not_called(); self.provider.Llama.assert_not_called()

    def test_preparer_worker_forwards_only_explicit_opt_in(self):
        import asyncio
        import io
        from fastapi import BackgroundTasks, UploadFile
        from routers import preparer
        from tests.test_preparer_admission_cancel import PreparerAdmissionCancelTests
        fixture = PreparerAdmissionCancelTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        for allowed in (False, True):
            with self.subTest(allowed=allowed), patch.object(preparer, '_stream_subprocess_to_logs', return_value=(0, [])) as stream:
                async def start():
                    tasks = BackgroundTasks()
                    await preparer.preparer_start(tasks, json.dumps({'audio_filename':'book.wav', 'enrich_with_llm':True, 'llm_model_path':'fixture.gguf', 'enrich_emotional_tone':True, 'allow_cpu_fallback':allowed}), UploadFile(filename='book.wav', file=io.BytesIO(b'audio')), None)
                    await tasks()
                asyncio.run(start())
                self.assertEqual(allowed, '--allow-cpu-fallback' in stream.call_args.args[0])

    def test_native_preparer_forwards_opt_in_to_enricher(self):
        import os
        from types import SimpleNamespace
        from tests.test_preparer_run_state import preparer, PreparerRunStateTests
        fixture = PreparerRunStateTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        previous = Path.cwd(); os.chdir(fixture.root); self.addCleanup(os.chdir, previous)
        (fixture.root/'book.wav').write_bytes(b'audio')
        (fixture.work/'asr_segments.json').write_text(json.dumps({'word_segments':[{'word':'hello','start':0,'end':1}], 'detected_lang':'en'}))
        for allowed in (False, True):
            def run(command):
                self.assertEqual(allowed, '--allow-cpu-fallback' in command)
                chunks = json.loads((fixture.work/'asr_chunks_for_enrich.json').read_text())
                (fixture.work/'enriched_segments.json').write_text(json.dumps(chunks))
                return SimpleNamespace(returncode=0)
            argv = ['preparer','--audio','book.wav','--model','model.gguf','--phase','enrich','--llm-model-path','llm.gguf']
            if allowed: argv.append('--allow-cpu-fallback')
            with self.subTest(allowed=allowed), patch.object(sys, 'argv', argv), patch.object(preparer, 'LLAMA_CPP_AVAILABLE', True), patch.object(preparer, 'acquire_run_lock', return_value=1), patch.object(preparer, 'get_run_identity', return_value={}), patch.object(preparer, 'ensure_run_manifest'), patch.object(preparer, 'is_verified_artifact', return_value=True), patch.object(preparer, 'mark_artifact_complete'), patch.object(preparer.subprocess, 'run', side_effect=run):
                self.assertEqual(0, preparer.main())
