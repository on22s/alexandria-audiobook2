"""Rejected automatic aliases must still receive generated voices through main."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

import generate_personas as personas


class RejectedPersonaAliasTests(unittest.TestCase):
    def test_actual_basic_and_advanced_main_save_previews_for_rejected_aliases(self):
        for advanced in (False, True):
            for labels in (("ANNA", "Anna"), ("ANNA", "Anna!")):
                with self.subTest(advanced=advanced, labels=labels), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    script = [{"speaker": name, "text": "Hello there, my friend."} for name in labels]
                    (root / "annotated_script.json").write_text(json.dumps(script))
                    (root / "voice_config.json").write_text(json.dumps(dict.fromkeys(labels, {})))
                    previews = []
                    class Engine:
                        def generate_voice_design(self, description, sample_text):
                            path = root / f"preview_{len(previews)}.wav"
                            with wave.open(str(path), "wb") as stream:
                                stream.setparams((1, 2, 16000, 0, "NONE", "NONE"))
                                stream.writeframes(b"\0\0" * 160)
                            previews.append(path)
                            return str(path), None
                    discovery = {name: {"evidence": [{"entry_index": i, "quote": row["text"]}],
                                        "sample_lines": [row["text"]]}
                                 for i, (name, row) in enumerate(zip(labels, script))}
                    with contextlib.ExitStack() as stack:
                        for name, value in (
                            ("get_runtime_data_dir", tmp), ("load_app_config", {}),
                            ("get_active_llm_config", {"model_name": "fixture"}),
                            ("ensure_ideal_settings", (False, {"context_length": 4096}, "fixture")),
                            ("make_run_client", object()), ("TTSEngine", Engine()),
                            ("call_llm_for_object", discovery),
                            ("request_persona_with_evidence", {"description": "Warm natural voice.",
                                                              "ref_text": "Hello there, my friend."})):
                            stack.enter_context(patch.object(personas, name, return_value=value))
                        stack.enter_context(patch("sys.argv", ["generate_personas.py", "--new-only"]
                                                  + (["--advanced"] if advanced else [])))
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        personas.main()
                    saved = json.loads((root / "voice_config.json").read_text())
                    self.assertEqual(len(labels), len(previews))
                    for name in labels:
                        self.assertTrue(personas.voice_is_set(saved[name]), saved)
                        self.assertTrue((root / saved[name]["ref_audio"]).is_file())
                    self.assertEqual(script, json.loads((root / "annotated_script.json").read_text()))
