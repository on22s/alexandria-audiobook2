"""Whisper download publishes streamed files without constructing weight tensors."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from tests import test_model_download_publication as publication


class WhisperDownloadStreamTests(unittest.TestCase):
    load=publication.ModelDownloadPublicationTests.load

    def test_streamed_weights_unchanged_local_processor_and_cache_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'models/whisper-base';target.mkdir(parents=True);(target/'old').write_bytes(b'prior')
            module=self.load(root);observed={};block=b'known fixture weights'*4096
            def stream(repo_id,local_dir,allow_patterns):
                self.assertEqual(b'prior',(target/'old').read_bytes())
                observed.update(repo=repo_id,patterns=allow_patterns,stage=local_dir)
                stage=Path(local_dir);(stage/'config.json').write_text(json.dumps({'model_type':'whisper'}))
                (stage/'preprocessor_config.json').write_text('{}')
                (stage/'tokenizer_config.json').write_text('{}')
                cache=stage/'.cache/huggingface';cache.mkdir(parents=True);(cache/'metadata').write_text('owned cache')
                with (stage/'model.safetensors').open('wb') as handle:
                    for _ in range(8):handle.write(block)
                return local_dir
            factory=module.AutoProcessor.from_pretrained
            def processor(path,**kwargs):
                self.assertEqual(observed['stage'],path)
                self.assertEqual({'local_files_only':True},kwargs)
                self.assertEqual(b'prior',(target/'old').read_bytes())
                return factory(path,**kwargs)
            forbidden=types.SimpleNamespace(from_pretrained=lambda *a,**k:(_ for _ in ()).throw(MemoryError('full weights allocation forbidden')))
            with patch.object(module,'snapshot_download',side_effect=stream) as downloaded, \
                 patch.object(module.AutoProcessor,'from_pretrained',side_effect=processor), \
                 patch.object(module,'AutoModelForSpeechSeq2Seq',forbidden,create=True), \
                 contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0,module.download_model())
            downloaded.assert_called_once()
            self.assertEqual(hashlib.sha256(block*8).hexdigest(),hashlib.sha256((target/'model.safetensors').read_bytes()).hexdigest())
            self.assertEqual('owned cache',(target/'.cache/huggingface/metadata').read_text())
            self.assertFalse((target/'old').exists())
            self.assertEqual([],list((root/'models').glob('.whisper-base-download-*')))

    def test_snapshot_missing_or_empty_required_files_never_replaces_prior(self):
        for missing in ('config.json','model.safetensors','empty_weights'):
            with self.subTest(missing=missing),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);target=root/'models/whisper-base';target.mkdir(parents=True);(target/'old').write_bytes(b'prior')
                module=self.load(root)
                def stream(repo_id,local_dir,allow_patterns):
                    stage=Path(local_dir)
                    if missing!='config.json':(stage/'config.json').write_text('{}')
                    if missing!='model.safetensors':(stage/'model.safetensors').write_bytes(b'' if missing=='empty_weights' else b'weights')
                    return local_dir
                with patch.object(module,'snapshot_download',side_effect=stream), \
                     patch.object(module.AutoProcessor,'from_pretrained',side_effect=AssertionError('incomplete model reached processor')), \
                     contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(1,module.download_model())
                self.assertEqual({'old':b'prior'},{p.name:p.read_bytes() for p in target.iterdir()})
                self.assertEqual([],list((root/'models').glob('.whisper-base-download-*')))

    def test_success_does_not_claim_model_parameter_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            module=self.load(Path(tmp));output=io.StringIO()
            with contextlib.redirect_stdout(output):self.assertEqual(0,module.download_model())
            self.assertIn('Model weights downloaded',output.getvalue())
            self.assertNotIn('parameters',output.getvalue())
