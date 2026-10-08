import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


class ModelDownloadPublicationTests(unittest.TestCase):
    def test_post_publish_backup_cleanup_failure_warns_without_false_download_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'models/whisper-base'
            target.mkdir(parents=True)
            (target / 'old').write_bytes(b'prior bundle')
            module = self.load(root)
            original = module.shutil.rmtree
            def remove(path, *args, **kwargs):
                if str(path).endswith('.previous'):
                    raise PermissionError('backup cleanup refused')
                return original(path, *args, **kwargs)
            output = io.StringIO()
            with patch.object(module.shutil, 'rmtree', side_effect=remove), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(0, module.download_model())
            self.assertEqual(b'fixture weights', (target / 'model.safetensors').read_bytes())
            backups = list((root / 'models').glob('*.previous'))
            self.assertEqual(1, len(backups))
            self.assertEqual(b'prior bundle', (backups[0] / 'old').read_bytes())
            self.assertIn('WARNING', output.getvalue())
            self.assertIn(str(backups[0]), output.getvalue())
            self.assertIn('SUCCESS', output.getvalue())
            self.assertNotIn('✗ ERROR', output.getvalue())

    def load(self, root, failure=None, source=None):
        target = root / 'models/whisper-base'
        published_before = {p.name:p.read_bytes() for p in target.glob('*') if p.is_file()}
        def assert_unpublished():
            assert published_before == {p.name:p.read_bytes() for p in target.glob('*') if p.is_file()}
        class Model:
            def save_pretrained(self, path):
                assert_unpublished()
                Path(path,'config.json').write_text('fixture config')
                if failure == 'weights': raise OSError('weights interrupted')
                Path(path,'model.safetensors').write_bytes(b'fixture weights')
            def parameters(self): return [object()]
        class Processor:
            def save_pretrained(self, path):
                assert_unpublished()
                Path(path,'tokenizer_config.json').write_text('fixture tokenizer')
                if failure == 'processor': raise OSError('processor interrupted')
        transformers = types.ModuleType('transformers')
        transformers.AutoModelForSpeechSeq2Seq = types.SimpleNamespace(from_pretrained=lambda *_args,**_kwargs:Model())
        transformers.AutoProcessor = types.SimpleNamespace(from_pretrained=lambda *_args,**_kwargs:Processor())
        hub = types.ModuleType('huggingface_hub')
        def snapshot_download(repo_id, local_dir, allow_patterns):
            assert repo_id == 'openai/whisper-base'
            assert set(allow_patterns) == {'*.json', '*.txt', 'model.safetensors'}
            Model().save_pretrained(local_dir)
            return local_dir
        hub.snapshot_download = snapshot_download
        module=types.ModuleType('download_fixture');module.__file__=str(root/'download_model.py')
        with patch.dict(sys.modules,{'transformers':transformers,'huggingface_hub':hub}):
            exec(compile(source or (ROOT/'download_model.py').read_text(),module.__file__,'exec'),module.__dict__)
        return module

    def test_failed_model_or_processor_save_never_publishes_or_damages_prior_bundle(self):
        for previous in (False, True):
            for failure in ('weights','processor'):
                with self.subTest(previous=previous,failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);target=root/'models/whisper-base'
                    if previous:
                        target.mkdir(parents=True);(target/'old').write_bytes(b'prior bundle')
                    module=self.load(root,failure)
                    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(1,module.download_model())
                    if previous: self.assertEqual([('old',b'prior bundle')],[(p.name,p.read_bytes()) for p in target.iterdir()])
                    else: self.assertFalse(target.exists())
                    self.assertEqual([],list((root/'models').glob('.whisper-base-download-*')))

    def test_success_publishes_both_files_and_failed_replace_restores_previous(self):
        for fail_replace in (False,True):
            with self.subTest(fail_replace=fail_replace),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);target=root/'models/whisper-base';target.mkdir(parents=True)
                (target/'old').write_bytes(b'prior bundle')
                module=self.load(root)
                real_replace=os.replace
                def replace(source,destination):
                    if fail_replace and Path(destination)==target and not str(source).endswith('.previous'):
                        raise PermissionError('publication blocked')
                    return real_replace(source,destination)
                with patch.object(module.os,'replace',side_effect=replace), \
                     contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(1 if fail_replace else 0,module.download_model())
                if fail_replace: self.assertEqual(b'prior bundle',(target/'old').read_bytes())
                else: self.assertEqual({'config.json','model.safetensors','tokenizer_config.json'},{p.name for p in target.iterdir()})
                self.assertEqual([],list((root/'models').glob('.whisper-base-download-*')))

    def test_failed_rollback_preserves_previous_bundle_with_visible_recovery_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);target=root/'models/whisper-base';target.mkdir(parents=True)
            (target/'old').write_bytes(b'prior bundle')
            module=self.load(root); real_replace=os.replace
            def replace(source,destination):
                if Path(destination)==target: raise PermissionError('destination blocked')
                return real_replace(source,destination)
            output=io.StringIO()
            with patch.object(module.os,'replace',side_effect=replace), \
                 contextlib.redirect_stdout(output),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1,module.download_model())
            backups=list((root/'models').glob('*.previous'))
            self.assertEqual(1,len(backups));self.assertEqual(b'prior bundle',(backups[0]/'old').read_bytes())
            self.assertIn(str(backups[0]),output.getvalue())

    def test_native_cli_processor_failure_leaves_no_final_directory(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'download_model.py').write_text((ROOT/'download_model.py').read_text())
            (root/'transformers.py').write_text('''from pathlib import Path
class Model:
    def save_pretrained(self,path):
        Path(path,'config.json').write_text('fixture')
        Path(path,'model.safetensors').write_bytes(b'fixture')
    def parameters(self):return [1]
class Processor:
    def save_pretrained(self,path):
        Path(path,'tokenizer_config.json').write_text('partial')
        raise OSError('processor interrupted')
class AutoModelForSpeechSeq2Seq:
    @staticmethod
    def from_pretrained(*args,**kwargs):return Model()
class AutoProcessor:
    @staticmethod
    def from_pretrained(*args,**kwargs):return Processor()
''')
            (root/'huggingface_hub.py').write_text('''from pathlib import Path
def snapshot_download(repo_id,local_dir,allow_patterns):
    Path(local_dir,'config.json').write_text('fixture')
    Path(local_dir,'model.safetensors').write_bytes(b'fixture')
    return local_dir
''')
            result=subprocess.run([sys.executable,'download_model.py'],cwd=root,capture_output=True,text=True,timeout=20)
            self.assertEqual(1,result.returncode,result.stderr)
            self.assertIn('processor interrupted',result.stdout)
            self.assertFalse((root/'models/whisper-base').exists())
            self.assertEqual([],list((root/'models').iterdir()))
