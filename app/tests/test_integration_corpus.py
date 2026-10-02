import copy
import core
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integration_corpus import build_manifest, select_passages
import integration_runner


class OwnedCorpusFixture(unittest.TestCase):
    def setUp(self):
        state = copy.deepcopy(core.process_state)
        for value in state.values():
            value['running'] = False
        for owner, name, value in ((core, 'process_state', state),
                                   (core, '_task_claims', {}), (core, '_gpu_leases', {})):
            context = patch.object(owner, name, value)
            context.start()
            self.addCleanup(context.stop)
        for context in (patch.object(core, 'acquire_gpu_lock', return_value=None),
                        patch.object(core, 'llm_is_on_this_gpu', return_value=True)):
            context.start()
            self.addCleanup(context.stop)



class IntegrationCorpusTests(OwnedCorpusFixture):
    def test_passage_selection_covers_distinct_text_features(self):
        text = ('Opening narration establishes the setting. “This dialogue passage is long '
                'enough to represent a speaking character clearly,” she said. Wait—what?! '
                + 'x' * 260 + '. café')
        categories = {item["category"] for item in select_passages(text, target_chars=200)}
        self.assertIn("dialogue", categories)
        self.assertIn("expressive_punctuation", categories)
        self.assertIn("non_ascii", categories)
        self.assertIn("long_sentence", categories)

    def test_manifest_is_deterministic_and_records_bad_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b.txt").write_text("Second book text.", encoding="utf-8")
            Path(tmp, "a.txt").write_text("First book text.", encoding="utf-8")
            Path(tmp, "empty.txt").write_text("", encoding="utf-8")

            first = build_manifest(tmp, max_books=10, target_chars=200)
            second = build_manifest(tmp, max_books=10, target_chars=200)

        self.assertEqual(first, second)
        self.assertEqual(["a.txt", "b.txt"], [book["name"] for book in first["books"]])
        self.assertEqual("empty.txt", first["errors"][0]["name"])

    def test_runner_writes_case_results_incrementally(self):
        text = "one two three four five"
        manifest = {"books": [{"name": "book.txt", "passages": [{
            "category": "opening_narration", "text": text, "sha256": "abc"}]}]}

        def process(_client, _model, chunk, _index, _total, _params,
                    max_retries, attempt_observer):
            attempt_observer({"attempt": 1, "finish_reason": "stop"})
            return [{"speaker": "NARRATOR", "text": chunk, "instruct": "neutral"}]

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(integration_runner, "load_app_config", return_value={
                 "llm": {"base_url": "http://localhost:1234/v1", "model_name": "model"}}), \
             patch.object(integration_runner, "ensure_ideal_settings",
                          return_value=(False, {"context_length": 8192}, "ready")), \
             patch.object(integration_runner, "OpenAI"), \
             patch.object(integration_runner, "process_chunk", side_effect=process):
            output = str(Path(tmp, "report.json"))
            report = integration_runner.run_manifest(manifest, output)

            self.assertTrue(Path(output).is_file())
        self.assertEqual("passed", report["cases"][0]["status"])
        self.assertEqual(1, len(report["cases"][0]["attempts"]))
        self.assertEqual({"total": 1, "passed": 1},
                         report["summary"]["by_category"]["opening_narration"])
        self.assertEqual(0, report["summary"]["retry_cases"])


if __name__ == "__main__":
    unittest.main()


class IntegrationRunnerBoundaryTests(OwnedCorpusFixture):
    def test_empty_manifest_cli_saves_zero_case_summary(self):
        for manifest in ({'books': []}, {'books': [{'name': 'empty.txt', 'passages': []}]}):
            with self.subTest(manifest=manifest), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp, 'manifest.json')
                source.write_text(json.dumps(manifest))
                before = source.read_bytes()
                output = Path(tmp, 'nested', 'report.json')
                with patch.object(integration_runner, 'load_app_config', return_value={}), \
                     patch.object(integration_runner, 'ensure_ideal_settings',
                                  return_value=(False, {}, 'ready')), \
                     patch.object(integration_runner, 'OpenAI'), \
                     patch.object(integration_runner, 'process_chunk') as process, \
                     contextlib.redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(0, integration_runner.main([
                        '--manifest', str(source), '--output', str(output)]))
                process.assert_not_called()
                report = json.loads(output.read_text())
                self.assertEqual([], report['cases'])
                self.assertEqual(integration_runner.summarize_cases([]), report['summary'])
                self.assertIn('Completed 0 case(s): 0 passed, 0 retried', stdout.getvalue())
                self.assertEqual(before, source.read_bytes())

    def test_current_profile_controls_provider_generation_and_report(self):
        text = 'one two three four five'
        manifest = {'books': [{'name': 'book.txt', 'passages': [{
            'category': 'opening', 'text': text, 'sha256': 'abc'}]}]}
        for mode in ('local', 'remote'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                profile = {'base_url': f'http://{mode}:1234/v1',
                           'model_name': f'{mode}-model', 'api_key': f'{mode}-key'}
                config = {'llm_mode': mode, f'llm_{mode}': profile,
                          'llm': {'base_url': 'http://stale', 'model_name': 'stale', 'api_key': 'stale'},
                          'llm_remote_ssh': 'fixture-ssh'}
                original = copy.deepcopy(config)
                output = Path(tmp, 'report.json')
                with patch.object(integration_runner, 'load_app_config', return_value=config), \
                     patch.object(integration_runner, 'ensure_ideal_settings',
                                  return_value=(False, {}, 'ready')) as ensure, \
                     patch.object(integration_runner, 'OpenAI') as provider, \
                     patch.object(integration_runner, 'process_chunk', return_value=[{
                         'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}]) as process:
                    integration_runner.run_manifest(manifest, str(output))
                self.assertEqual(profile['base_url'], provider.call_args.kwargs['base_url'])
                self.assertEqual(profile['api_key'], provider.call_args.kwargs['api_key'])
                self.assertEqual((mode, profile['base_url'], profile['model_name']), ensure.call_args.args)
                self.assertEqual('fixture-ssh', ensure.call_args.kwargs['ssh_alias'])
                self.assertEqual(profile['model_name'], process.call_args.args[1])
                report = json.loads(output.read_text())
                self.assertEqual(profile['base_url'], report['base_url'])
                self.assertEqual(profile['model_name'], report['model'])
                self.assertEqual(1, report['summary']['passed'])
                self.assertEqual(original, config)


class IntegrationAdapterProvenanceTests(OwnedCorpusFixture):
    def test_adapter_check_result_is_persisted_beside_measured_cases(self):
        import attribution_adapter
        text = 'one two three four five'
        manifest = {'books': [{'name': 'book.txt', 'passages': [{
            'category': 'opening', 'text': text, 'sha256': 'abc'}]}]}
        cases = (([], False), ([{'path': '/configured/adapter.gguf', 'scale': 0}], False),
                 (None, False), ([{'path': '/server/adapter.gguf', 'scale': 1}], False),
                 ([{'path': '/configured/adapter.gguf', 'scale': 1}], True))
        for served, expected in cases:
            with self.subTest(served=served), tempfile.TemporaryDirectory() as tmp:
                config = {'llm_mode': 'remote', 'llm_remote': {
                    'base_url': 'http://fixture/v1', 'model_name': 'model',
                    'attribution_adapter': {'path': '/configured/adapter.gguf', 'scale': 1}}}
                before = copy.deepcopy(config)
                output = Path(tmp, 'report.json')
                with patch.object(integration_runner, 'load_app_config', return_value=config), \
                     patch.object(integration_runner, 'ensure_ideal_settings',
                                  return_value=(False, {}, 'ready')), \
                     patch.object(integration_runner, 'OpenAI'), \
                     patch.object(attribution_adapter, 'served_adapters', return_value=served) as probe, \
                     patch.object(integration_runner, 'process_chunk', return_value=[{
                         'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}]), \
                     contextlib.redirect_stdout(io.StringIO()) as stdout:
                    report = integration_runner.run_manifest(manifest, str(output))
                probe.assert_called_once_with('http://fixture/v1', api_key='local')
                evidence = json.loads(output.read_text())['attribution_adapter']
                self.assertIs(expected, evidence['verified'])
                self.assertEqual(attribution_adapter.describe(config), evidence['expected'])
                self.assertTrue(evidence['message'])
                self.assertEqual(report['attribution_adapter'], evidence)
                self.assertEqual(1, report['summary']['passed'])
                if not expected:
                    self.assertIn('WARNING:', stdout.getvalue())
                self.assertEqual(before, config)

    def test_required_missing_adapter_refuses_before_work_and_preserves_prior_report(self):
        import attribution_adapter
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'report.json')
            output.write_bytes(b'prior report must survive')
            config = {'llm_local': {'base_url': 'http://fixture/v1', 'model_name': 'model',
                'attribution_adapter': {'path': '/adapter.gguf', 'require': True}}}
            with patch.object(integration_runner, 'load_app_config', return_value=config), \
                 patch.object(attribution_adapter, 'served_adapters', return_value=[]), \
                 patch.object(integration_runner, 'ensure_ideal_settings',
                              return_value=(False, {}, 'ready')) as ensure, \
                 patch.object(integration_runner, 'OpenAI') as provider, \
                 patch.object(integration_runner, 'process_chunk') as process:
                with self.assertRaises(attribution_adapter.AdapterError):
                    integration_runner.run_manifest({'books': []}, str(output))
            ensure.assert_called_once()
            provider.assert_not_called()
            process.assert_not_called()
            self.assertEqual(b'prior report must survive', output.read_bytes())


class IntegrationAdapterAfterHealTests(OwnedCorpusFixture):
    def test_adapter_evidence_reflects_server_state_after_self_healing(self):
        import attribution_adapter
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'report.json')
            config = {'llm_local': {'base_url': 'http://fixture/v1', 'model_name': 'model',
                'attribution_adapter': {'path': '/adapter.gguf'}}}
            served = [{'path': '/adapter.gguf', 'scale': 1}]
            def heal(*args, **kwargs):
                served.clear()
                return True, {}, 'reloaded'
            with patch.object(integration_runner, 'load_app_config', return_value=config), \
                 patch.object(integration_runner, 'ensure_ideal_settings', side_effect=heal), \
                 patch.object(attribution_adapter, 'served_adapters', side_effect=lambda _url, **_kwargs: list(served)), \
                 patch.object(integration_runner, 'OpenAI'), \
                 contextlib.redirect_stdout(io.StringIO()):
                integration_runner.run_manifest({'books': []}, str(output))
            evidence = json.loads(output.read_text())['attribution_adapter']
            self.assertIs(False, evidence['verified'])
            self.assertIn('NOT loaded', evidence['message'])



class CorpusSourceSnapshotTests(unittest.TestCase):
    def test_txt_and_epub_passages_are_from_the_exact_hashed_source_snapshot(self):
        import hashlib
        import integration_corpus
        import zipfile
        for extension in ('.txt','.epub'):
            with self.subTest(extension=extension), tempfile.TemporaryDirectory() as tmp:
                source=Path(tmp,'book'+extension)
                original_text='Original version has the passages selected for this manifest.'
                changed_text='Replacement version arrived while the corpus was being built.'
                def write_book(text):
                    if extension=='.txt':
                        source.write_text(text,encoding='utf-8')
                    else:
                        with zipfile.ZipFile(source,'w') as archive:
                            archive.writestr('mimetype','application/epub+zip')
                            archive.writestr('META-INF/container.xml',
                                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                                '<rootfile full-path="book.opf" media-type="application/oebps-package+xml"/>'
                                '</rootfiles></container>')
                            archive.writestr('book.opf',
                                '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                                '<item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>'
                                '</manifest><spine><itemref idref="body"/></spine></package>')
                            archive.writestr('body.xhtml','<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'+text+'</p></body></html>')
                write_book(original_text)
                original_bytes=source.read_bytes()
                read_bytes=Path.read_bytes
                original_reader=integration_corpus._read_book
                source_reads=[]
                read_paths=[]
                def read_and_replace(path):
                    data=read_bytes(path)
                    if path==source:
                        source_reads.append(path)
                        write_book(changed_text)
                    return data
                def parse_book(path, raw):
                    read_paths.append((path, raw))
                    return original_reader(path, raw)
                with patch.object(Path,'read_bytes',read_and_replace), \
                     patch.object(integration_corpus,'_read_book',side_effect=parse_book):
                    manifest=integration_corpus.build_manifest(tmp,target_chars=200)
                self.assertEqual([],manifest['errors'])
                self.assertEqual(1,len(manifest['books']))
                book=manifest['books'][0]
                self.assertEqual(source.name,book['name'])
                self.assertEqual(hashlib.sha256(original_bytes).hexdigest(),book['source_sha256'])
                self.assertEqual(len(original_bytes),book['source_size_bytes'])
                self.assertIn(original_text,[p['text'] for p in book['passages']])
                self.assertNotIn(changed_text,[p['text'] for p in book['passages']])
                self.assertEqual([source],source_reads)
                self.assertEqual([(source, original_bytes)], read_paths,
                                 'Parsing must use captured bytes even if the source changes')
                self.assertNotEqual(original_bytes,source.read_bytes())
                self.assertEqual([source],list(Path(tmp).iterdir()))
