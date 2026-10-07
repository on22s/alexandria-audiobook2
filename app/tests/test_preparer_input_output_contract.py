import ast
from contextlib import ExitStack
import io
import json
import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import preparer
import preparer_numeric_settings as numeric
from tests import test_preparer_batch_uploads as batch_fixture

CLI_SOURCE = Path(__file__).resolve().parents[2] / 'alexandria_preparer_rocm_compatible.py'


class PreparerInputOutputContractTests(unittest.TestCase):
    def test_numeric_booleans_refuse_before_request_coercion_or_launch(self):
        app=FastAPI();app.include_router(preparer.router)
        fields=('chunk_size','min_chunk_duration','min_confidence','source_threshold',
                'batch_size','source_start','min_snr')
        with patch.object(preparer,'_save_upload_limited') as save, \
             patch.object(preparer,'reserve_background_task') as reserve, \
             patch.object(preparer,'_resolve_preparer_interpreter',
                          side_effect=HTTPException(503,'validation reached interpreter')) as interpreter,TestClient(app) as client:
            for field in fields:
                for value in (False,True):
                    with self.subTest(field=field,value=value):
                        response=client.post('/api/preparer/start',data={
                            'config_json':json.dumps({'audio_filename':'book.wav',field:value})},
                            files={'audio_file':('book.wav',b'not staged','audio/wav')})
                        self.assertEqual(422,response.status_code,response.text)
                        save.assert_not_called();reserve.assert_not_called();interpreter.assert_not_called()
            for field in ('min_confidence','min_snr'):
                for value in (False,True):
                    with self.subTest(batch=field,value=value):
                        response=client.post('/api/preparer/batch/start',json={
                            'tasks':[{'audio_filename':'book.wav','output_filename':'book.zip'}],field:value})
                        self.assertEqual(422,response.status_code,response.text)
                        interpreter.assert_not_called();reserve.assert_not_called()
        for field in (*fields,'val_split','zip_max_files'):
            with self.subTest(shared_validator=field):
                with self.assertRaises(ValueError):numeric.validate_preparer_numeric_settings({field:True})
        payload={'audio_filename':'book.wav','chunk_size':'10','min_snr':'25','resume':True}
        config=preparer.PreparerConfig(**payload)
        self.assertEqual(10.0,config.chunk_size);self.assertEqual(25,config.min_snr)
        self.assertTrue(config.resume);self.assertEqual('10',payload['chunk_size'])

    def test_single_bad_numeric_values_refuse_before_staging_or_claim(self):
        cases = [('source_threshold', -1), ('source_threshold', 10), ('min_confidence', -1),
                 ('min_confidence', 2), ('chunk_size', 0), ('chunk_size', 'NaN'),
                 ('min_chunk_duration', -1), ('min_chunk_duration', 'Infinity'),
                 ('batch_size', 0), ('source_start', -1), ('min_snr', -100)]
        app = FastAPI(); app.include_router(preparer.router)
        with patch.object(preparer, '_save_upload_limited') as save, \
             patch.object(preparer, 'reserve_background_task') as reserve, \
             patch.object(preparer, '_resolve_preparer_interpreter',
                          side_effect=HTTPException(503, 'validation reached interpreter')) as interpreter, TestClient(app) as client:
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    response = client.post('/api/preparer/start', data={
                        'config_json': json.dumps({'audio_filename': 'book.wav', field: value})},
                        files={'audio_file': ('book.wav', b'not staged', 'audio/wav')})
                    self.assertEqual(422, response.status_code, response.text)
                    save.assert_not_called(); reserve.assert_not_called(); interpreter.assert_not_called()

    def test_batch_both_input_modes_share_quality_validation_before_staging_or_claim(self):
        app = FastAPI(); app.include_router(preparer.router)
        with patch.object(preparer, '_save_upload_limited') as save, \
             patch.object(preparer, 'reserve_background_task') as reserve, \
             patch.object(preparer, '_resolve_preparer_interpreter',
                          side_effect=HTTPException(503, 'validation reached interpreter')), TestClient(app) as client:
            for field, value in [('min_confidence', -1), ('min_confidence', 2),
                                 ('min_confidence', 'NaN'), ('min_snr', -100)]:
                payload = {'tasks': [{'audio_filename': 'book.wav', 'output_filename': 'book.zip'}], field: value}
                for upload in (False, True):
                    with self.subTest(field=field, value=value, upload=upload):
                        if upload:
                            response = client.post('/api/preparer/batch/upload_start', data={'config_json': json.dumps(payload)},
                                                   files=[('audio_files', ('book.wav', b'not staged', 'audio/wav'))])
                        else:
                            response = client.post('/api/preparer/batch/start', json=payload)
                        self.assertEqual(422, response.status_code, response.text)
                        save.assert_not_called(); reserve.assert_not_called()

    def test_valid_boundary_values_are_preserved_without_mutating_input(self):
        data = {'audio_filename': 'book.wav', 'source_threshold': 0, 'min_confidence': 1,
                'chunk_size': 0.01, 'min_chunk_duration': 0.01, 'batch_size': 1, 'source_start': 0, 'min_snr': 0}
        before = dict(data)
        item = preparer.PreparerConfig(**data)
        self.assertEqual(before, data)
        self.assertEqual(0, item.source_threshold); self.assertEqual(1, item.min_confidence)
        self.assertEqual(0.01, item.chunk_size); self.assertEqual(0, item.min_snr)
        numeric.validate_preparer_numeric_settings(data)
        self.assertEqual(before, data)
        self.assertEqual(0, preparer.BatchPreparerRequest(tasks=[{'audio_filename':'b.wav','output_filename':'b.zip'}],
                                                       min_confidence=0, min_snr=0).min_confidence)

    def test_actual_cli_validation_block_calls_shared_rules_before_pipeline(self):
        tree = ast.parse(CLI_SOURCE.read_text())
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'main')
        start = next(i for i, node in enumerate(main.body) if isinstance(node, ast.Assign) and
                     any(isinstance(target, ast.Name) and target.id == 'WAV2VEC2_MODEL_REVISION' for target in node.targets)) + 1
        end = next(i for i, node in enumerate(main.body) if isinstance(node, ast.If) and
                   'args.skip_annotation' in ast.unparse(node.test))
        block = compile(ast.Module(body=main.body[start:end], type_ignores=[]), str(CLI_SOURCE), 'exec')
        def error(message): raise ValueError(message)
        defaults = {'chunk_size': 10.0, 'min_chunk_duration': 2.0, 'min_confidence': 0.85, 'val_split': 0.1,
                    'source_threshold': 0.65, 'batch_size': 1, 'zip_max_files': 1000,
                    'source_start': None, 'min_snr': 25, 'limit': None}
        for field, value in [('chunk_size', math.inf), ('min_confidence', math.nan), ('zip_max_files', 0),
                             ('source_start', -1), ('min_snr', -100), ('limit', 0)]:
            with self.subTest(field=field), patch.object(numeric, 'validate_preparer_numeric_settings',
                                                        wraps=numeric.validate_preparer_numeric_settings) as validate:
                with self.assertRaises(ValueError):
                    exec(block, {'args': SimpleNamespace(**{**defaults, field: value}), 'parser': SimpleNamespace(error=error),
                                 'sys': sys, 'os': __import__('os'), 'math': math, 'script_dir': str(CLI_SOURCE.parent)})
                validate.assert_called_once()
        exec(block, {'args': SimpleNamespace(**defaults), 'parser': SimpleNamespace(error=error),
                     'sys': sys, 'os': __import__('os'), 'math': math, 'script_dir': str(CLI_SOURCE.parent)})

    def test_download_refuses_non_zip_and_resolved_non_zip_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive: archive.writestr('metadata.json', '{}')
            (root / 'dataset.zip').write_bytes(data.getvalue())
            (root / 'metadata.json').write_bytes(b'private metadata')
            (root / '.upload.tmp').write_bytes(b'private staging')
            (root / 'alias.zip').symlink_to(root / 'metadata.json')
            app = FastAPI(); app.include_router(preparer.router)
            with patch.object(preparer, 'PREPARER_OUTPUT_DIR', str(root)), TestClient(app) as client:
                for name in ('metadata.json', '.upload.tmp', 'alias.zip'):
                    with self.subTest(name=name):
                        self.assertEqual(400, client.get('/api/preparer/download/' + name).status_code)
                response = client.get('/api/preparer/download/dataset.zip')
                self.assertEqual(200, response.status_code)
                self.assertEqual(data.getvalue(), response.content)
                self.assertEqual(404, client.get('/api/preparer/download/missing.zip').status_code)

    def test_zip_listing_and_download_admit_all_extension_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);data=io.BytesIO()
            with zipfile.ZipFile(data,'w') as archive:
                archive.writestr('metadata.json','{}')
            names=('lower.zip','upper.ZIP','mixed.Zip')
            for name in names:
                (root/name).write_bytes(data.getvalue())
            (root/'other.txt').write_text('not a dataset')
            app=FastAPI();app.include_router(preparer.router)
            with patch.object(preparer,'PREPARER_OUTPUT_DIR',str(root)),TestClient(app) as client:
                response=client.get('/api/preparer/list')
                self.assertEqual(200,response.status_code)
                self.assertEqual(sorted(names),[row['filename'] for row in response.json()['files']])
                for name in names:
                    with self.subTest(name=name):
                        response=client.get('/api/preparer/download/'+name)
                        self.assertEqual(200,response.status_code)
                        self.assertEqual(data.getvalue(),response.content)

    def test_native_batch_collision_tries_one_and_retains_prior_outputs(self):
        with tempfile.TemporaryDirectory() as root, ExitStack() as contexts:
            fixture = batch_fixture.PreparerBatchUploadTests()
            client, uploads, captures, state, claim, release = fixture.setup_api(Path(root), contexts)
            outputs = Path(root, 'outputs')
            (outputs / 'book.zip').write_bytes(b'old natural')
            (outputs / 'book_2.zip').write_bytes(b'old suffix')
            config = fixture.config(['one.wav', 'two.wav'])
            for task in config['tasks']: task['output_filename'] = 'book.zip'
            response = fixture.post(client, ['one.wav', 'two.wav'], [b'one', b'two'], config)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(['book_1.zip', 'book_3.zip'], [Path(cmd[cmd.index('--output')+1]).name for _, _, cmd in captures])
            self.assertEqual(b'old natural', (outputs / 'book.zip').read_bytes())
            self.assertEqual(b'old suffix', (outputs / 'book_2.zip').read_bytes())
            self.assertTrue((outputs / 'book_1.zip').is_file()); self.assertTrue((outputs / 'book_3.zip').is_file())
