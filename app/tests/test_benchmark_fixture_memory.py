"""Native source reads preserve fixture hashes and global chunk normalization."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import benchmark_runner as runner
import benchmark_fixtures as fixtures


class FixtureMemoryTests(unittest.TestCase):
    def test_grouped_source_reads_are_bounded_and_normalize_once_per_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'book.txt')
            text = ('Alice said, “This is a long sentence with café and 日本語.”\r\n\r\n'
                    'The саге and пар words are surrounded by English text.\r\n\r\n') * 1000
            path.write_bytes(text.encode('utf-8'))
            specs = [{'path':str(path), 'chunk_numbers':[1, 2, 3]}]
            manifest = fixtures.build_script_generation_manifest(specs, tmp, chunk_size=200)
            other = fixtures.build_script_generation_manifest(
                [{'path':str(path), 'chunk_numbers':[4]}], tmp, chunk_size=300)['fixtures'][0]
            other['id'] = 'other-size'
            selected = manifest['fixtures'] + [other]
            before = copy.deepcopy(selected)
            reads = []
            real_readinto = runner._HashingFixtureReader.readinto
            def observed(reader, buffer):
                reads.append(len(buffer))
                return real_readinto(reader, buffer)
            with patch.object(runner._HashingFixtureReader, 'readinto', observed), \
                 patch.object(runner, 'open', wraps=open, create=True) as opened, \
                 patch.object(runner, 'get_normalized_source_chunks', wraps=fixtures.get_normalized_source_chunks) as normalized:
                result = runner.get_text_fixture_sources(selected, tmp)
            self.assertEqual(1, opened.call_count)
            self.assertEqual(2, normalized.call_count)
            self.assertTrue(reads)
            self.assertLessEqual(max(reads), 8192)
            for fixture in selected:
                chunks = fixtures.get_normalized_source_chunks(path.read_bytes(), fixture['chunk_size'])
                self.assertEqual(chunks[fixture['chunk_number']-1], result[fixture['id']])
                self.assertEqual(fixture['sha256'], hashlib.sha256(result[fixture['id']].encode()).hexdigest())
            self.assertEqual(before, selected)

    def test_plain_text_preserves_newlines_and_unicode_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = b'x'*8191 + 'é\r\n日本語\rEND'.encode()
            path = Path(tmp, 'plain.txt'); path.write_bytes(raw)
            fixture = {'id':'plain', 'path':str(path), 'sha256':hashlib.sha256(raw).hexdigest()}
            self.assertEqual(raw.decode(), runner._load_text_fixture(fixture, tmp))
            path.write_bytes(raw[:-1]+b'!')
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                runner._load_text_fixture(fixture, tmp)

    def test_invalid_sources_and_each_fixture_hash_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'book.txt'); path.write_text(('A regular sentence.\n\n')*50)
            selected = fixtures.build_script_generation_manifest(
                [{'path':str(path), 'chunk_numbers':[1,2]}], tmp, chunk_size=200)['fixtures']
            for key, value, message in (('source_sha256','0'*64,'source hash changed'),
                                       ('sha256','0'*64,'chunk hash changed'),
                                       ('chunk_number',9999,'out of range')):
                damaged = copy.deepcopy(selected); damaged[1][key] = value
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, message):
                    runner.get_text_fixture_sources(damaged, tmp)
            for raw, message in ((b'\xff','not UTF-8'), (b' \r\n','empty')):
                path.write_bytes(raw)
                with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, message):
                    runner._load_text_fixture({'id':'plain','path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}, tmp)
            with tempfile.TemporaryDirectory() as outside:
                with self.assertRaisesRegex(ValueError, 'inside uploads'):
                    runner._load_text_fixture({'id':'escape','path':str(path),'sha256':'unused'}, outside)

    def test_production_runner_reuses_selected_texts_for_all_repetitions(self):
        import benchmark_core
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'book.txt'); path.write_text(('A complete ordinary sentence.\n\n')*50)
            manifest = fixtures.build_script_generation_manifest(
                [{'path':str(path), 'chunk_numbers':[1,2]}], tmp, repetitions=3, chunk_size=200)
            environment = benchmark_core.build_environment_fingerprint('local', {
                'hostname':'fixture', 'gpu_name':'none', 'backend':'cpu',
                'python_version':'3.10', 'git_commit':'fixture'})
            state = {'cancel':False, 'logs':[], 'tasks':[
                {'fixture_id':f['id'], 'status':'pending'} for f in manifest['fixtures']]}
            def generated(client, model_name, text, *args, **kwargs):
                return [{'speaker':'NARRATOR', 'text':text, 'instruct':'Neutral.'}]
            with patch.object(runner, 'load_app_config', return_value={
                    'llm_local':{'model_name':'fixture', 'base_url':'http://fixture.invalid'}}), \
                 patch.object(runner, 'get_lmstudio_status', return_value={
                     'available':True, 'loaded':True, 'context_length':8192}), \
                 patch.object(runner, 'make_llm_client'), \
                 patch.object(runner, 'process_chunk', side_effect=generated) as generate, \
                 patch.object(runner, 'get_text_fixture_sources', wraps=runner.get_text_fixture_sources) as load, \
                 patch.object(runner, 'get_normalized_source_chunks', wraps=fixtures.get_normalized_source_chunks) as normalize:
                report = runner.run_script_generation_benchmark(
                    manifest, environment, str(Path(tmp,'report.json')), state, str(Path(tmp,'config.json')), tmp)
            self.assertEqual(1, load.call_count)
            self.assertEqual(1, normalize.call_count)
            self.assertEqual(6, generate.call_count)
            self.assertEqual(6, len(report['cases']))
            self.assertTrue(all(case['status']=='passed' for case in report['cases']))
            self.assertEqual('complete', state['status'])
