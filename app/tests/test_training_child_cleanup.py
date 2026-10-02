"""Exercise real owned child processes without starting training or GPU inference."""
import contextlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from tests.test_voicelab_pipeline_scripts import batch_train


class TrainingChildCleanupTests(unittest.TestCase):
    def run_fault(self, error_type, ignore_term=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'dummy_training.py'
            script.write_text('import signal, time\n'
                              + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore_term else '')
                              + 'print("[DATA] Found dummy input", flush=True)\ntime.sleep(120)\n')
            archive = root / 'speaker.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('metadata.jsonl', '{"audio":"sample.wav","text":"Words"}\n')
            args = SimpleNamespace(datasets_dir=str(root/'datasets'), models_dir=str(root/'models'),
                python=sys.executable, train_script=str(script), max_epochs=1, lr=1e-6,
                lora_r=4,lora_alpha=8,grad_accum=1,language='english',target_loss=4.0,keep_datasets=False)
            Path(args.models_dir).mkdir()
            prior = Path(args.models_dir, 'old')
            prior.mkdir()
            (prior/'keep.bin').write_bytes(b'prior adapter')
            real_popen = subprocess.Popen
            real_rmtree = batch_train.shutil.rmtree
            children, cleanup_states, streams = [], [], []
            unrelated = real_popen([sys.executable, '-c', 'import time;time.sleep(120)'])

            class BrokenOutput:
                def __init__(self, stream):
                    self.stream = stream

                def __iter__(self):
                    self.stream.readline()  # Confirm child is running and TERM policy installed.
                    raise error_type('injected output processing failure')

                def close(self):
                    self.stream.close()

            def start(*params, **kwargs):
                child = real_popen(*params, **kwargs)
                streams.append(child.stdout)
                child.stdout = BrokenOutput(child.stdout)
                children.append(child)
                return child

            def remove(path, *params, **kwargs):
                if str(path).startswith(str(root/'datasets')):
                    cleanup_states.append(children[0].poll())
                return real_rmtree(path, *params, **kwargs)

            try:
                with patch.object(batch_train.subprocess, 'Popen', side_effect=start), \
                     patch.object(batch_train.shutil, 'rmtree', side_effect=remove), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if error_type is KeyboardInterrupt:
                        with self.assertRaises(KeyboardInterrupt):
                            batch_train.train_one(str(archive),'speaker','old',args)
                    else:
                        self.assertIsNone(batch_train.train_one(str(archive),'speaker','old',args))
                self.assertEqual(1,len(children))
                self.assertIsNotNone(children[0].poll(),'train_one left owned child alive')
                self.assertTrue(streams[0].closed,'owned stdout descriptor leaked')
                self.assertIsNone(unrelated.poll(),'cleanup killed an unrelated process')
                self.assertEqual(b'prior adapter',(prior/'keep.bin').read_bytes())
                self.assertTrue(all(state is not None for state in cleanup_states),
                                'dataset removed before child stopped')
                if ignore_term:
                    self.assertEqual(-signal.SIGKILL,children[0].returncode)
                if error_type is OSError:
                    self.assertTrue(cleanup_states)
                    self.assertFalse(Path(args.datasets_dir,'speaker').exists())
            finally:
                for child in children+[unrelated]:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=10)
                for stream in streams:
                    stream.close()

    def test_output_exception_stops_child_before_dataset_cleanup(self):
        self.run_fault(OSError)

    def test_keyboard_interrupt_reaps_child_and_preserves_interruption(self):
        self.run_fault(KeyboardInterrupt)

    @unittest.skipIf(os.name == 'nt','POSIX TERM-ignore escalation fixture')
    def test_unresponsive_owned_child_is_killed_and_reaped(self):
        self.run_fault(OSError,ignore_term=True)


    def test_unparsed_epoch_count_is_unknown_and_parsed_early_stop_is_measured(self):
        for log, expected in (("training finished without epoch summaries", None),
                              ("[EPOCH] unknown/6 avg_loss=bad", None),
                              ("[EPOCH] 2/6 avg_loss=2.5000", 2)):
            with self.subTest(log=log), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                script = root / 'cpu_epoch_fixture.py'
                script.write_text("import argparse,json,pathlib\n"
                    "p=argparse.ArgumentParser();p.add_argument('--output_dir');p.add_argument('--epochs',type=int)\n"
                    "args,_=p.parse_known_args();out=pathlib.Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)\n"
                    "(out/'training_meta.json').write_text(json.dumps({'epochs':args.epochs,'final_loss':2.5,'best_loss':2.5}))\n"
                    + "from safetensors.numpy import save_file;import numpy as np\n"
                    + "(out/'adapter_config.json').write_text(json.dumps({'peft_type':'LORA','r':2,'lora_alpha':4,'target_modules':['q_proj']}))\n"
                    + "save_file({'layer.lora_A.weight':np.ones((2,3),dtype=np.float32)},str(out/'adapter_model.safetensors'))\n"
                    + f"print({log!r},flush=True)\n")
                archive = root / 'speaker.zip'
                with zipfile.ZipFile(archive, 'w') as output:
                    output.writestr('metadata.jsonl', '{"audio":"sample.wav","text":"Words"}\n')
                args = SimpleNamespace(datasets_dir=str(root/'datasets'),models_dir=str(root/'models'),
                    python=sys.executable,train_script=str(script),max_epochs=6,lr=1e-6,
                    lora_r=4,lora_alpha=8,grad_accum=1,language='english',target_loss=4.0,keep_datasets=False)
                Path(args.models_dir).mkdir()
                logs = io.StringIO()
                with contextlib.redirect_stdout(logs):
                    result = batch_train.train_one(str(archive), 'speaker', 'new', args)
                self.assertIsNotNone(result)
                self.assertEqual(expected, result['epochs_run'])
                self.assertEqual({} if expected is None else {2:2.5}, result['epoch_losses'])
                self.assertEqual(2.5, result['best_loss'])
                self.assertEqual(6, json.loads(Path(args.models_dir,'new/training_meta.json').read_text())['epochs'])
                manifest = root / 'manifest.json'
                batch_train.save_manifest(str(manifest), [result])
                saved = json.loads(manifest.read_text())[0]
                self.assertEqual(expected, saved['epochs_run'])
                if expected is None:
                    self.assertIn('Epoch count unavailable', logs.getvalue())
                else:
                    self.assertNotIn('Epoch count unavailable', logs.getvalue())
                self.assertFalse(Path(args.datasets_dir, 'speaker').exists())
                self.assertTrue(Path(args.models_dir, 'new/adapter_model.safetensors').exists())

    def test_normal_finished_child_keeps_training_result_and_closes_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root/'dummy_finished.py'
            script.write_text("import argparse,json,pathlib\n"
                "p=argparse.ArgumentParser();p.add_argument('--output_dir');args,_=p.parse_known_args()\n"
                "out=pathlib.Path(args.output_dir);out.mkdir(parents=True,exist_ok=True)\n"
                "(out/'training_meta.json').write_text(json.dumps({'final_loss':2.5,'best_loss':2.5}))\n"
                "from safetensors.numpy import save_file;import numpy as np\n"
                "(out/'adapter_config.json').write_text(json.dumps({'peft_type':'LORA','r':2,'lora_alpha':4,'target_modules':['q_proj']}))\n"
                "save_file({'layer.lora_A.weight':np.ones((2,3),dtype=np.float32)},str(out/'adapter_model.safetensors'))\n"
                "print('[EPOCH] 1/1 avg_loss=2.5000',flush=True)\n")
            archive = root/'speaker.zip'
            with zipfile.ZipFile(archive,'w') as output:
                output.writestr('metadata.jsonl','{"audio":"sample.wav"}\n')
            args = SimpleNamespace(datasets_dir=str(root/'datasets'),models_dir=str(root/'models'),
                python=sys.executable,train_script=str(script),max_epochs=1,lr=1e-6,
                lora_r=4,lora_alpha=8,grad_accum=1,language='english',target_loss=4.0,keep_datasets=False)
            Path(args.models_dir).mkdir()
            real_popen = subprocess.Popen
            children = []
            def start(*params,**kwargs):
                child = real_popen(*params,**kwargs)
                child.terminate = unittest.mock.Mock(wraps=child.terminate)
                child.kill = unittest.mock.Mock(wraps=child.kill)
                children.append(child)
                return child
            with patch.object(batch_train.subprocess,'Popen',side_effect=start), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = batch_train.train_one(str(archive),'speaker','new',args)
            self.assertEqual('new',result['id'])
            self.assertEqual(2.5,result['best_loss'])
            self.assertEqual({1:2.5},result['epoch_losses'])
            self.assertEqual(1,result['sample_count'])
            self.assertEqual(0,children[0].poll())
            self.assertTrue(children[0].stdout.closed)
            children[0].terminate.assert_not_called()
            children[0].kill.assert_not_called()
            self.assertFalse(Path(args.datasets_dir,'speaker').exists())
            self.assertTrue(Path(args.models_dir,'new/adapter_model.safetensors').exists())
