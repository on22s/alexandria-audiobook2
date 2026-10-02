from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
import run_stage6_listening as runner


class Stage6RunnerTests(unittest.TestCase):
    def test_gpu_wrapper_delegates_to_shared_approved_runner(self):
        with patch.object(runner, "run_gpu_job") as run:
            runner.run_gpu("stage", 30, "experiment.py", ["--flag"])
        run.assert_called_once_with(
            runner.REPO, runner.APP, runner.PYTHON, "stage", 30,
            "experiment.py", ["--flag"], "stage6_listening.log")


class Stage6ShapeTests(unittest.TestCase):
    def test_real_checkpoint_identity_audio_and_nested_shapes(self):
        import copy
        import json
        import tempfile
        from contextlib import ExitStack
        from tests.test_blinded_listening import provenance, write_wav
        with tempfile.TemporaryDirectory(dir=runner.REPO) as directory, ExitStack() as stack:
            root = Path(directory)
            for name in ("CHUNKS", "VOICE_CONFIG", "CONFIG", "ALIASES", "LORA_MANIFEST",
                         "SCENE", "INSTRUCTION", "CASTING", "INSTRUCTION_DIR", "CASTING_DIR"):
                path = root / name
                if not name.endswith("DIR"):
                    path.write_text("{}")
                stack.enter_context(patch.object(runner, name, str(path)))
            stack.enter_context(patch.object(runner, "get_reproducible_harness_source", return_value="fixture"))
            wav = root / "source.wav"
            write_wav(wav)
            audio = runner._relative(str(wav))
            def document(script, args, inputs, **fields):
                prov = provenance(script)
                prov.update(args=args, input_sha256=runner.input_sha256(inputs))
                return dict(status="complete", provenance=prov, **fields)
            rel = runner._relative
            scene = document("scene_aware_casting.py", {
                "script": rel(runner.CHUNKS), "voice_config": rel(runner.VOICE_CONFIG),
                "manifest": rel(runner.LORA_MANIFEST), "window": 20,
                "aliases": rel(runner.ALIASES), "out": rel(runner.SCENE)},
                (runner.CHUNKS, runner.VOICE_CONFIG, runner.LORA_MANIFEST, runner.ALIASES),
                scene_aware={"assignment": {}})
            Path(runner.SCENE).write_text(json.dumps(scene))
            instruction = document("instruct_listening.py", {
                "script": rel(runner.CHUNKS), "voice_config": rel(runner.VOICE_CONFIG),
                "config": rel(runner.CONFIG), "speaker": "", "lines": 4,
                "seed": 1234, "out_dir": rel(runner.INSTRUCTION_DIR), "out": rel(runner.INSTRUCTION)},
                (runner.CHUNKS, runner.VOICE_CONFIG, runner.CONFIG), all_arms_rendered=True,
                comparisons=[{"arm_files": dict.fromkeys(("none", "per_char", "per_line"), audio)} for _ in range(4)])
            casting = document("casting_ab_audio.py", {
                "script": rel(runner.CHUNKS), "voice_config": rel(runner.VOICE_CONFIG),
                "config": rel(runner.CONFIG), "casting": rel(runner.SCENE),
                "characters": ["FELT", "REINHARD"], "size": 14,
                "out_dir": rel(runner.CASTING_DIR), "seed": 1234},
                (runner.CHUNKS, runner.VOICE_CONFIG, runner.CONFIG, runner.SCENE, runner.ALIASES, runner.LORA_MANIFEST),
                published=True, lines=14, arms={name: {"lines": list(range(14)), "path": audio} for name in ("current", "scene_aware")})
            # Casting is validated before mutating its hashed scene input.
            groups = [
                (runner.validate_casting, runner.CASTING, casting, [
                    (lambda d: dict(d, arms=[]), "wrong arms"),
                    (lambda d: dict(d, arms=dict(d["arms"], current=[])), "arm current"),
                    (lambda d: dict(d, arms=dict(d["arms"], current={"lines": {}, "path": audio})), "line coverage"),
                    (lambda d: dict(d, arms=dict(d["arms"], current={"lines": list(range(14)), "path": []})), "path must")]),
                (runner.validate_instruction, runner.INSTRUCTION, instruction, [
                    (lambda d: dict(d, comparisons={}), "four comparisons"),
                    (lambda d: dict(d, comparisons=[[]] + d["comparisons"][1:]), "comparison 0"),
                    (lambda d: dict(d, comparisons=[{"arm_files": []}] + d["comparisons"][1:]), "wrong arms"),
                    (lambda d: dict(d, comparisons=[{"arm_files": dict.fromkeys(("none", "per_char", "per_line"), [])}] + d["comparisons"][1:]), "path must")]),
                (runner.validate_scene, runner.SCENE, scene, [
                    (lambda d: dict(d, scene_aware=[]), "scene_aware"),
                    (lambda d: dict(d, scene_aware={"assignment": []}), "assignment")]),
            ]
            for validate, path, valid, cases in groups:
                Path(path).write_text(json.dumps(valid))
                self.assertEqual(valid, validate(path))
                for mutate, message in cases:
                    with self.subTest(validator=validate.__name__, message=message):
                        Path(path).write_text(json.dumps(mutate(copy.deepcopy(valid))))
                        before = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
                        with self.assertRaisesRegex(RuntimeError, message):
                            validate(path)
                        self.assertEqual(before, {p: p.read_bytes() for p in before})


if __name__ == "__main__":
    unittest.main()
