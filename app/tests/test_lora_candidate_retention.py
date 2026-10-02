from pathlib import Path
import tempfile
import unittest

from train_lora import deduplicate_evaluation_candidates, save_evaluation_candidate


class LoraCandidateRetentionTests(unittest.TestCase):
    def test_candidate_save_is_bounded_and_does_not_mutate_records_input(self):
        class FakeModel:
            def __init__(self):
                self.saved = []

            def save_pretrained(self, path):
                Path(path).mkdir(parents=True)
                Path(path, "adapter_model.safetensors").write_bytes(b"weights")
                self.saved.append(path)

        with tempfile.TemporaryDirectory() as tmp:
            model = FakeModel()
            original = []
            records, first = save_evaluation_candidate(model, tmp, original, 2, 1, 4.8)
            records, second = save_evaluation_candidate(model, tmp, records, 2, 2, 4.5)
            records, third = save_evaluation_candidate(model, tmp, records, 2, 3, 4.2)

        self.assertEqual([], original)
        self.assertEqual(["epoch_001", "epoch_002"], [record["id"] for record in records])
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertEqual(2, len(model.saved))

    def test_final_dedup_removes_production_and_candidate_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            output.joinpath("adapter_model.safetensors").write_bytes(b"production")
            records = []
            for candidate_id, weights in (
                    ("epoch_001", b"distinct"),
                    ("epoch_002", b"production"),
                    ("epoch_003", b"distinct")):
                path = output / "candidates" / candidate_id
                path.mkdir(parents=True)
                path.joinpath("adapter_model.safetensors").write_bytes(weights)
                records.append({"id": candidate_id, "epoch": 1, "loss": 4.5,
                                "path": str(path)})

            retained, skipped, production_hash = deduplicate_evaluation_candidates(
                str(output), records)

            self.assertEqual(["epoch_001"], [record["id"] for record in retained])
            self.assertEqual(
                [("epoch_002", "production"), ("epoch_003", "epoch_001")],
                [(record["id"], record["duplicate_of"]) for record in skipped],
            )
            self.assertEqual(64, len(production_hash))
            self.assertTrue(Path(retained[0]["path"]).is_dir())
            self.assertFalse((output / "candidates" / "epoch_002").exists())
            self.assertFalse((output / "candidates" / "epoch_003").exists())


if __name__ == "__main__":
    unittest.main()


class TrainingReferenceTranscriptTests(unittest.TestCase):
    def test_finalized_production_and_candidate_metadata_support_real_inference_dispatch(self):
        import itertools
        import json
        import os
        import sys
        from types import ModuleType, SimpleNamespace
        from unittest.mock import Mock, patch
        import numpy as np
        import soundfile as sf
        import train_lora
        import tts

        for content, ref_index in itertools.product(
                (None, "", " \n\t", "  Explicit reference words. \n"), (0, 1, None)):
            with self.subTest(content=content, ref_index=ref_index), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dataset = root / "dataset"
                output = root / "adapter"
                candidate = output / "candidates" / "epoch_001"
                dataset.mkdir()
                candidate.mkdir(parents=True)
                reference = dataset / "ref.wav"
                first_pcm = (0.2 * np.sin(np.arange(4800) * 0.08)).astype(np.float32)
                samples = []
                for index, data in enumerate((first_pcm, -first_pcm)):
                    audio = dataset / ("sample_" + str(index) + ".wav")
                    sf.write(audio, data, 24000)
                    samples.append({"text": ("Known sample transcript." if index == 0 else "Later reference transcript."),
                                    "audio_path": str(audio)})
                pcm = first_pcm if ref_index == 0 else (-first_pcm if ref_index == 1 else 2 * first_pcm)
                sf.write(reference, pcm, 24000)
                from tests.test_support import write_test_adapter
                write_test_adapter(output,value=1)
                write_test_adapter(candidate,value=2)
                if content is not None:
                    (dataset / "ref_text.txt").write_text(content, encoding="utf-8")
                before = {p: p.read_bytes() for p in dataset.iterdir()}
                args = SimpleNamespace(data_dir=str(dataset), output_dir=str(output), model_name="base",
                    epochs=3, lr=1e-6, lora_r=8, lora_alpha=16, gradient_accumulation_steps=1,
                    batch_size=1, language="English")
                ref_text = train_lora.get_training_reference_text(str(dataset),str(reference),samples)
                metadata = train_lora.get_training_checkpoint_metadata(args,samples,str(reference),ref_text,
                    2,4.4,4.4,12.0,0)
                train_lora.save_training_checkpoint(None,str(output),str(reference),ref_text,metadata,
                    [{"id":"epoch_001","epoch":1,"loss":4.5,"path":str(candidate)}],2,2,4.4,finalize=True)
                expected = (content.strip() if content and content.strip()
                            else samples[1 if ref_index == 1 else 0]["text"])
                for adapter in (output, candidate):
                    metadata = json.loads((adapter / "training_meta.json").read_text())
                    engine = tts.TTSEngine.__new__(tts.TTSEngine)
                    engine._mode = "local"
                    engine._max_new_tokens = 100
                    engine._lora_prompt_cache = {}
                    prompt = object()
                    model = SimpleNamespace(create_voice_clone_prompt=Mock(return_value=prompt),
                        generate_voice_clone=Mock(return_value=([pcm.copy()], 24000)))
                    engine._init_local_lora = Mock(return_value=model)
                    fake_torch = ModuleType("torch")
                    fake_torch.manual_seed = Mock()
                    rendered = root / (adapter.name + "_render.wav")
                    with patch.dict(sys.modules, {"torch": fake_torch}):
                        ok = engine.generate_lora_voice("A known test line.", "",
                            {"adapter_path": str(adapter), "seed": 5}, str(rendered))
                    self.assertTrue(ok, metadata)
                    self.assertEqual(2, metadata["epochs"])
                    self.assertEqual(3, metadata["requested_epochs"])
                    self.assertEqual(expected, metadata["ref_sample_text"])
                    self.assertEqual(expected, model.create_voice_clone_prompt.call_args.kwargs["ref_text"])
                    self.assertEqual(train_lora.get_checkpoint_sha256(str(adapter)), metadata["checkpoint_sha256"])
                    decoded, rate = sf.read(rendered)
                    self.assertEqual(24000, rate)
                    np.testing.assert_allclose(pcm, decoded, atol=1e-4)
                    self.assertEqual(before[reference], (adapter / "ref_sample.wav").read_bytes())
                for path, data in before.items():
                    self.assertEqual(data, path.read_bytes())


class TrainingCandidatePublicationSafetyTests(unittest.TestCase):
    def test_changed_candidate_or_traversal_is_rejected_before_pruning_or_publishing(self):
        import json
        import numpy as np
        import soundfile as sf
        import train_lora
        from tests.test_support import write_test_adapter
        from adapter_checkpoint_transaction import validate_adapter_checkpoint_generation
        for kind in ('changed','traversal'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);output=root/'adapter';reference=root/'reference.wav'
                sf.write(reference,np.full(2400,.1,dtype=np.float32),24000)
                write_test_adapter(output,value=1)
                train_lora.save_training_checkpoint(None,str(output),str(reference),'reference words',{},[],2,1,4.5)
                candidate=output/'candidates'/'epoch_001';write_test_adapter(candidate,value=2)
                record={'id':'epoch_001','epoch':1,'loss':4.5,'path':str(candidate),
                        'sha256':train_lora.get_checkpoint_sha256(str(output))}
                if kind=='traversal':record['id']='../../outside'
                before={str(path.relative_to(output)):path.read_bytes() for path in output.rglob('*') if path.is_file()}
                with self.assertRaisesRegex(ValueError,'changed|identity'):
                    train_lora.save_training_checkpoint(None,str(output),str(reference),'reference words',{},[record],2,1,4.5,finalize=True)
                self.assertEqual(before,{str(path.relative_to(output)):path.read_bytes() for path in output.rglob('*') if path.is_file()})
                self.assertEqual('reference words',validate_adapter_checkpoint_generation(output)['ref_sample_text'])
