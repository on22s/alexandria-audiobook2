"""A character can change from a line onward (#603): the anchor in force at
a line, the config handed to the engine for that line, the CustomVoice
instruct with the anchor in front, and the endpoints that set the points."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tts
from routers import voices as voices_module


class ActiveStyleTests(unittest.TestCase):
    entry = {"type": "custom", "voice": "Ryan", "character_style": "young and eager",
             "style_timeline": [{"from_index": 300, "character_style": "thirty, worn, lower"},
                                {"from_index": 100, "character_style": "twenty, steadier"}]}

    def test_the_last_point_at_or_before_the_line_wins(self):
        self.assertEqual("young and eager", tts.active_character_style(self.entry, 0))
        self.assertEqual("young and eager", tts.active_character_style(self.entry, 99))
        self.assertEqual("twenty, steadier", tts.active_character_style(self.entry, 100))
        self.assertEqual("twenty, steadier", tts.active_character_style(self.entry, 299))
        self.assertEqual("thirty, worn, lower", tts.active_character_style(self.entry, 300))
        self.assertEqual("thirty, worn, lower", tts.active_character_style(self.entry, 5000))
        self.assertEqual("young and eager", tts.active_character_style(self.entry))

    def test_the_engine_sees_the_anchor_for_that_line_and_nothing_else_changes(self):
        config = {"HERO": self.entry, "OTHER": {"type": "custom", "character_style": "x"}}
        at_400 = tts.voice_config_for_chunk(config, "HERO", 400)
        self.assertEqual("thirty, worn, lower", at_400["HERO"]["character_style"])
        self.assertIs(config["OTHER"], at_400["OTHER"])
        self.assertEqual("young and eager", config["HERO"]["character_style"])   # the file is untouched
        self.assertIs(config, tts.voice_config_for_chunk(config, "OTHER", 400))  # no timeline -> same object

    def test_custom_voice_puts_the_anchor_before_the_lines_emotion(self):
        self.assertEqual("thirty, worn, lower Cold fury, barely contained.",
                         tts.anchored_instruct({"character_style": "thirty, worn, lower"}, "Cold fury, barely contained."))
        self.assertEqual("Cold fury.", tts.anchored_instruct({"character_style": ""}, "Cold fury."))
        self.assertEqual("thirty, worn", tts.anchored_instruct({"character_style": "thirty, worn"}, ""))
        self.assertEqual("neutral", tts.anchored_instruct({}, ""))
        self.assertEqual("old fallback", tts.anchored_instruct({"default_style": "old fallback"}, ""))


class EngineReceivesTheAnchorTests(unittest.TestCase):
    def test_the_custom_path_sends_the_anchor_in_force_at_the_line(self):
        """Through the real engine method up to the point the model would
        load: the instruct it prints (and would send) carries the anchor for
        that line's position, and the per-line emotion after it."""
        import contextlib, io
        entry = {"type": "custom", "voice": "Ryan", "character_style": "young and eager",
                 "style_timeline": [{"from_index": 300, "character_style": "thirty, worn, lower"}]}
        engine = tts.TTSEngine.__new__(tts.TTSEngine)
        seen = []
        # CI hides torch (ci_env); the method imports it before the print
        import sys, types
        fake_torch = types.ModuleType("torch")
        fake_torch.manual_seed = lambda *_: None

        def no_model():
            raise RuntimeError("stop before loading the model")
        engine._init_local_custom = no_model
        for index in (0, 300, 900):
            config = tts.voice_config_for_chunk({"HERO": entry}, "HERO", index)
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.suppress(Exception), \
                 patch.dict(sys.modules, {"torch": sys.modules.get("torch") or fake_torch}):
                engine._local_generate_custom("Hello.", "Cold fury.", "HERO", config, "/nonexistent/out.wav")
            line = next((l for l in out.getvalue().splitlines() if "generating with instruct=" in l), "")
            seen.append((index, line.split("instruct='")[1].split("'")[0] if "instruct='" in line else None))
        self.assertEqual([(0, "young and eager Cold fury."), (300, "thirty, worn, lower Cold fury."),
                          (900, "thirty, worn, lower Cold fury.")], seen)


class StyleTimelineEndpointTests(unittest.TestCase):
    def test_points_are_added_replaced_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "script.json")
            voice_path = os.path.join(tmp, "voices.json")
            Path(script_path).write_text(json.dumps([{"speaker": "Hero"}]), encoding="utf-8")
            Path(voice_path).write_text(json.dumps({"Hero": {"type": "custom", "character_style": "young"}}), encoding="utf-8")
            with patch.object(voices_module, "SCRIPT_PATH", script_path), \
                 patch.object(voices_module, "VOICE_CONFIG_PATH", voice_path):
                asyncio.run(voices_module.add_style_point("Hero", voices_module.StylePointRequest(from_index=300, character_style="thirty")))
                asyncio.run(voices_module.add_style_point("Hero", voices_module.StylePointRequest(from_index=100, character_style="twenty")))
                res = asyncio.run(voices_module.add_style_point("Hero", voices_module.StylePointRequest(from_index=300, character_style="thirty, lower")))
                self.assertEqual([(100, "twenty"), (300, "thirty, lower")],
                                 [(p["from_index"], p["character_style"]) for p in res["style_timeline"]])
                res = asyncio.run(voices_module.remove_style_point("Hero", 100))
                self.assertEqual([300], [p["from_index"] for p in res["style_timeline"]])
                saved = json.loads(Path(voice_path).read_text(encoding="utf-8"))["Hero"]
                self.assertEqual("young", saved["character_style"])
                self.assertEqual(1, len(saved["style_timeline"]))
                # the save endpoint's model accepts the field, so a Voices-tab save keeps it
                self.assertEqual([{"from_index": 3, "character_style": "x"}],
                                 voices_module.VoiceConfigItem(style_timeline=[{"from_index": 3, "character_style": "x"}]).style_timeline)
