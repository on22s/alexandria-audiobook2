"""Verify export metrics against real CPU-generated Audacity artifacts."""
import builtins
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import export_benchmark


class ExportBenchmarkHashTests(unittest.TestCase):
    def test_large_real_audacity_artifact_uses_bounded_reads_and_exact_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "sample.wav")
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(32000)
                audio.writeframes(os.urandom(4 * 1024 * 1024))
            source_bytes = source.read_bytes()
            payload = {"stage": "audacity_export", "source_root": tmp, "fixture": {
                "chunks": [{"id": 0, "speaker": "NARRATOR", "text": "Benchmark line.",
                            "audio_path": "sample.wav", "status": "done"}],
                "audio_sha256": {"sample.wav": hashlib.sha256(source_bytes).hexdigest()},
                "per_chunk_chapters": True}}
            expected = {}
            sizes = []
            original_export = export_benchmark.ProjectManager.export_audacity
            original_open = builtins.open

            def capture_export(manager):
                result = original_export(manager)
                artifact = Path(manager.root_dir, "audacity_export.zip")
                raw = artifact.read_bytes()
                expected.update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
                self.assertGreater(len(raw), 4 * 1024 * 1024)
                return result

            class BoundedReader:
                def __init__(self, stream):
                    self.stream = stream

                def read(self, size=-1):
                    if size <= 0:
                        raise AssertionError("Artifact hash must not read the whole file into one bytes object")
                    sizes.append(size)
                    return self.stream.read(size)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.stream.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self.stream, name)

            def open_file(path, mode="r", *args, **kwargs):
                stream = original_open(path, mode, *args, **kwargs)
                if mode == "rb" and str(path).endswith("audacity_export.zip"):
                    return BoundedReader(stream)
                return stream

            with patch.object(export_benchmark.ProjectManager, "export_audacity", capture_export), \
                 patch("builtins.open", side_effect=open_file):
                result = export_benchmark.execute_payload(payload)
            self.assertEqual("passed", result["status"])
            self.assertEqual(expected["size"], result["artifact_bytes"])
            self.assertEqual(expected["sha256"], result["artifact_sha256"])
            self.assertEqual(1, result["label_count"])
            self.assertIn("project.lof", result["members"])
            self.assertIn("narrator.wav", result["members"])
            self.assertGreater(len(sizes), 1)
            self.assertLess(max(sizes), expected["size"])
            self.assertEqual(source_bytes, source.read_bytes())



class ExportBenchmarkSourceHashTests(unittest.TestCase):
    def make_payload(self, root):
        from benchmark_fixtures import build_export_manifest
        chunks=[]
        for index in range(2):
            path=Path(root,f'sample-{index}.wav')
            with wave.open(str(path),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000)
                audio.writeframes(bytes([index+1,0])*1600)
            chunks.append({'id':index,'speaker':'NARRATOR','text':f'Line {index}',
                           'audio_path':path.name,'status':'done'})
        fixture=build_export_manifest('audacity_export',[{'chunks':chunks}],str(root))['fixtures'][0]
        return {'stage':'audacity_export','source_root':str(root),'fixture':fixture}

    def test_changed_or_unverified_selected_audio_refuses_before_export(self):
        import copy
        for mode in ('after-preflight','during-copy','missing-hash'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                payload=self.make_payload(tmp)
                source=Path(tmp,'sample-1.wav')
                original_copy=export_benchmark.shutil.copy2
                if mode=='after-preflight':
                    source.write_bytes(b'changed after manifest built')
                elif mode=='missing-hash':
                    payload['fixture']['audio_sha256'].pop(source.name)
                snapshot=copy.deepcopy(payload)
                before={p.name:p.read_bytes() for p in Path(tmp).iterdir()}
                def copy_source(src,dst,*args,**kwargs):
                    if mode=='during-copy' and Path(src)==source:
                        source.write_bytes(b'changed between validation and copying')
                    return original_copy(src,dst,*args,**kwargs)
                with patch.object(export_benchmark.shutil,'copy2',side_effect=copy_source), \
                     patch.object(export_benchmark,'ProjectManager') as manager:
                    manager.side_effect=AssertionError('Export must not start on unverified input')
                    with self.assertRaisesRegex(ValueError,'export audio (hash changed|is unverified)'):
                        export_benchmark.execute_payload(payload)
                manager.assert_not_called()
                self.assertEqual(snapshot,payload)
                self.assertEqual(set(before),{p.name for p in Path(tmp).iterdir()})
                self.assertEqual(before['sample-0.wav'],Path(tmp,'sample-0.wav').read_bytes())
                if mode!='during-copy':
                    self.assertEqual(before['sample-1.wav'],source.read_bytes())

    def test_verified_copy_remains_valid_when_original_changes_after_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload=self.make_payload(tmp)
            original_copy=export_benchmark.shutil.copy2
            original_export=export_benchmark.ProjectManager.export_audacity
            seen=[]
            def copy_source(src,dst,*args,**kwargs):
                result=original_copy(src,dst,*args,**kwargs)
                Path(src).write_bytes(b'new source revision after copied snapshot')
                return result
            def export(manager):
                for index,chunk in enumerate(payload['fixture']['chunks']):
                    copied=Path(manager.root_dir,'voicelines',f'sample-{index}.wav')
                    self.assertEqual(payload['fixture']['audio_sha256'][chunk['audio_path']],
                                     hashlib.sha256(copied.read_bytes()).hexdigest())
                    seen.append(chunk['audio_path'])
                return original_export(manager)
            with patch.object(export_benchmark.shutil,'copy2',side_effect=copy_source), \
                 patch.object(export_benchmark.ProjectManager,'export_audacity',export):
                result=export_benchmark.execute_payload(payload)
            self.assertEqual('passed',result['status'])
            self.assertEqual(2,result['label_count'])
            self.assertEqual(['sample-0.wav','sample-1.wav'],seen)
