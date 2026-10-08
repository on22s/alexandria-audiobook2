"""Real repair CLI reports describe publication, and failed writes preserve outputs."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import repair_source_encoding as deterministic
import repair_source_llm as llm
from tests.test_repair_source_llm import _Client


class SourceRepairPublicationTests(unittest.TestCase):
    @contextlib.contextmanager
    def cli(self, module, root, source, apply=True):
        source_path = root / 'source.txt'; source_path.write_text(source, encoding='utf-8')
        output = root / 'output.txt'; output.write_text('Previous complete output.', encoding='utf-8')
        report = root / 'report.json'; (root / 'config.json').write_text('{}')
        args = ['repair.py', str(source_path), '--out', str(output), '--report', str(report)] + (['--apply'] if apply else [])
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, 'argv', args))
            stack.enter_context(patch.dict(os.environ, {'ALEXANDRIA_DATA_DIR': str(root)}))
            stack.enter_context(patch('lmstudio_settings.get_active_llm_config', return_value={'model_name': 'fixture'}))
            stack.enter_context(patch('llm_provider.make_llm_client', return_value=_Client()))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            yield source_path, output, report

    def test_structural_refusal_reports_requested_but_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.cli(deterministic, Path(tmp), '“Synthetic sentence—') as (source, output, report):
                before = output.read_bytes()
                self.assertEqual(1, deterministic.main())
                self.assertEqual(before, output.read_bytes())
                data = json.loads(report.read_text())
                self.assertFalse(data['applied'])
                self.assertTrue(data['apply_requested'])
                self.assertEqual('“Synthetic sentence—', source.read_text())

    def test_failed_output_write_or_replace_keeps_complete_output_and_false_report_then_retries(self):
        real_open = open; real_temp = tempfile.NamedTemporaryFile; real_replace = Path.replace
        class BrokenWriter:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def __getattr__(self, name): return getattr(self.stream, name)
            def write(self, text):
                self.stream.write(text[:5]); self.stream.flush()
                raise OSError('injected output write failure')
        for module in (deterministic, llm):
            for failure in ('write', 'replace'):
                with self.subTest(module=module.__name__, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    with self.cli(module, root, 'Synthetic clean text.') as (source, output, report):
                        before = output.read_bytes(); source_before = source.read_bytes()
                        def fail_open(path, mode='r', *args, **kwargs):
                            stream = real_open(path, mode, *args, **kwargs)
                            return BrokenWriter(stream) if Path(path) == output and mode == 'w' else stream
                        def fail_stage(*args, **kwargs):
                            stream = real_temp(*args, **kwargs)
                            return BrokenWriter(stream) if kwargs.get('prefix') == '.output.txt.' and failure == 'write' else stream
                        def fail_replace(path, destination):
                            if Path(destination) == output: raise OSError('injected output replace failure')
                            return real_replace(path, destination)
                        with contextlib.ExitStack() as stack:
                            stack.enter_context(patch('builtins.open', side_effect=fail_open))
                            stack.enter_context(patch.object(tempfile, 'NamedTemporaryFile', side_effect=fail_stage))
                            if failure == 'replace': stack.enter_context(patch.object(Path, 'replace', autospec=True, side_effect=fail_replace))
                            with self.assertRaisesRegex(OSError, 'injected'):
                                module.main()
                        self.assertEqual(before, output.read_bytes())
                        self.assertEqual(source_before, source.read_bytes())
                        self.assertFalse(json.loads(report.read_text())['applied'])
                        self.assertEqual([], list(root.glob('.*.tmp')))
                        module.main()
                        self.assertEqual('Synthetic clean text.', output.read_text())
                        self.assertTrue(json.loads(report.read_text())['applied'])
                        self.assertEqual([], list(root.glob('.*.tmp')))

    def test_dry_runs_and_successful_apply_have_truthful_reports(self):
        for module in (deterministic, llm):
            for apply in (False, True):
                with self.subTest(module=module.__name__, apply=apply), tempfile.TemporaryDirectory() as tmp:
                    with self.cli(module, Path(tmp), 'Synthetic clean text.', apply) as (source, output, report):
                        module.main(); data = json.loads(report.read_text())
                        self.assertEqual(apply, data['applied'])
                        self.assertEqual(apply, data['apply_requested'])
                        self.assertEqual('Synthetic clean text.' if apply else 'Previous complete output.', output.read_text())
                        self.assertEqual('Synthetic clean text.', source.read_text())

    def test_llm_review_sample_includes_repair_site_at_start_and_after_expanded_newlines(self):
        for prefix in ('', 'x' * llm.CONTEXT, '\n' * llm.CONTEXT):
            with self.subTest(prefix=repr(prefix)), tempfile.TemporaryDirectory() as tmp:
                with self.cli(llm, Path(tmp), prefix + '�' + 'x' * 200, False) as (source, output, report):
                    llm.main(); sample = json.loads(report.read_text())['samples'][0]
                    self.assertIn('�', sample['passage'])
                    self.assertEqual(len(prefix), sample['source_offset'])
                    self.assertEqual(1, sample['run_length'])
                    self.assertEqual('é', sample['chars'])
