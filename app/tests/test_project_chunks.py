import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY, patch

from project import (EXPLICIT_SILENCE_MS, ProjectManager,
                     get_speakable_entries, group_into_chunks)
from tts import DEFAULT_PAUSE_MS


class AudioLoadingTest(unittest.TestCase):
    def test_known_extension_bypasses_format_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "line.mp3")
            open(audio_path, "wb").close()
            manager = ProjectManager(tmp)
            with patch.object(manager, "load_chunks", return_value=[
                    {"audio_path": "line.mp3"}]), \
                 patch("project.AudioSegment.from_file", return_value="audio") as load:
                result, skipped = manager._load_chunks_with_audio()

        load.assert_called_once()
        source, kwargs = load.call_args
        self.assertEqual(audio_path, source[0].name)
        self.assertEqual({"format": "mp3", "codec": "mp3"}, kwargs)
        self.assertEqual([({"audio_path": "line.mp3"}, "audio")], result)
        self.assertEqual(0, skipped)

    def test_unknown_extension_keeps_probe_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "line.custom")
            open(audio_path, "wb").close()
            manager = ProjectManager(tmp)
            with patch.object(manager, "load_chunks", return_value=[
                    {"audio_path": "line.custom"}]), \
                 patch("project.AudioSegment.from_file", return_value="audio") as load:
                manager._load_chunks_with_audio()

        load.assert_called_once_with(ANY)
        self.assertEqual(audio_path, load.call_args.args[0].name)

    def test_ambiguous_ogg_container_keeps_probe_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            audio_path = os.path.join(tmp, "line.ogg")
            open(audio_path, "wb").close()
            manager = ProjectManager(tmp)
            with patch.object(manager, "load_chunks", return_value=[
                    {"audio_path": "line.ogg"}]), \
                 patch("project.AudioSegment.from_file", return_value="audio") as load:
                manager._load_chunks_with_audio()

        load.assert_called_once_with(ANY)
        self.assertEqual(audio_path, load.call_args.args[0].name)


class SpeakableEntryTests(unittest.TestCase):
    def test_sentence_endings_inside_closing_marks_and_unicode_merge(self):
        for texts in [('"Okay."', '"Continue."'), ('“Okay!”', '“Continue?”'),
                      ('はい。', '続けて。'), ('好的！', '继续？'),
                      ('「はい。」', '『続けて。』'), ('(Okay.)', '[Continue.]')]:
            with self.subTest(texts=texts):
                chunks = group_into_chunks([
                    {'speaker': 'ALICE', 'text': text, 'instruct': 'neutral'}
                    for text in texts])
                self.assertEqual(1, len(chunks))
                self.assertEqual(' '.join(texts), chunks[0]['text'])

    def test_structural_fragments_and_explicit_pauses_keep_boundaries(self):
        for texts in [('Chapter 1', 'The Awakening'), ('"The Awakening"', 'Chapter 2')]:
            with self.subTest(texts=texts):
                chunks = group_into_chunks([
                    {'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}
                    for text in texts])
                self.assertEqual(2, len(chunks))
        chunks = group_into_chunks([
            {'speaker': 'ALICE', 'text': '“Okay.”', 'instruct': 'neutral', 'pause_after': 500},
            {'speaker': 'ALICE', 'text': '“Continue.”', 'instruct': 'neutral'}])
        self.assertEqual(2, len(chunks))
        self.assertEqual(500, chunks[0]['pause_after'])

    def test_nonverbal_dialogue_becomes_pause_without_mutating_input(self):
        entries = [
            {"speaker": "A", "text": "Wait here.", "instruct": "quiet"},
            {"speaker": "A", "text": "――――", "instruct": "silent"},
            {"speaker": "B", "text": "I understand.", "instruct": "calm"},
        ]
        prepared = get_speakable_entries(entries)
        self.assertEqual(["Wait here.", "I understand."],
                         [entry["text"] for entry in prepared])
        self.assertEqual(DEFAULT_PAUSE_MS, prepared[0]["pause_after"])
        self.assertNotIn("pause_after", entries[0])

    def test_block_glyphs_and_leading_marks_are_not_sent_to_tts(self):
        entries = [
            {"speaker": "A", "text": "…", "instruct": "silent"},
            {"speaker": "A", "text": "■■●■", "instruct": "noise"},
        ]
        self.assertEqual([], group_into_chunks(entries))

    def test_spoken_words_with_punctuation_remain_speakable(self):
        entries = [{"speaker": "A", "text": "No—wait!", "instruct": "urgent"}]
        self.assertEqual("No—wait!", group_into_chunks(entries)[0]["text"])


class UnspeakablePassthroughTest(unittest.TestCase):
    """Scene-break glyphs embedded in prose reached TTS as text. is_nonverbal_text
    only fires on entries with no alphanumerics at all, so a break inside a
    paragraph passed every gate: 57 of mushoku16's 76 shipped into the script
    while the run reported one failure."""

    def test_embedded_scene_break_becomes_a_pause(self):
        from project import get_speakable_entries
        entries = [{"speaker": "NARRATOR", "instruct": "",
                    "text": "I wonder if she hates mice\n\n\u25a0\n\nIt seems that a cat"}]
        out = get_speakable_entries(entries)
        self.assertEqual(len(out), 2)
        self.assertNotIn("\u25a0", out[0]["text"])
        self.assertNotIn("\u25a0", out[1]["text"])
        self.assertGreaterEqual(out[0].get("pause_after", 0), 1000)

    def test_leading_scene_break_makes_no_orphan_pause(self):
        from project import get_speakable_entries
        entries = [{"speaker": "NARRATOR", "instruct": "",
                    "text": "\u25a0\n\nThe day began."}]
        out = get_speakable_entries(entries)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "The day began.")

    def test_symbol_is_verbalized(self):
        from project import get_speakable_entries
        entries = [{"speaker": "NARRATOR", "instruct": "",
                    "text": "the value is \u221e."}]
        out = get_speakable_entries(entries)
        self.assertEqual(out[0]["text"], "the value is infinity.")

    def test_elongation_moves_into_instruct(self):
        from project import get_speakable_entries
        from verbalization import ELONGATION_HINT
        entries = [{"speaker": "ERIS", "instruct": "Cheerful.", "text": "Yaaay~"}]
        out = get_speakable_entries(entries)
        self.assertEqual(out[0]["text"], "Yaaay")
        self.assertIn("Cheerful.", out[0]["instruct"])
        self.assertIn(ELONGATION_HINT, out[0]["instruct"])

    def test_clean_entry_is_unchanged(self):
        from project import get_speakable_entries
        entries = [{"speaker": "ERIS", "instruct": "Flat.", "text": "Nothing odd."}]
        out = get_speakable_entries(entries)
        self.assertEqual(out[0]["text"], "Nothing odd.")
        self.assertEqual(out[0]["instruct"], "Flat.")

    def test_caller_data_is_not_mutated(self):
        from project import get_speakable_entries
        entries = [{"speaker": "NARRATOR", "instruct": "", "text": "a\n\n\u25a0\n\nb"}]
        get_speakable_entries(entries)
        self.assertIn("\u25a0", entries[0]["text"])


if __name__ == "__main__":
    unittest.main()


class ReviewFlagTest(unittest.TestCase):
    """Unmapped glyphs and pictographic kana are reported, never guessed at."""

    def test_pictographic_kana_is_flagged_and_left_alone(self):
        from project import split_on_unspeakable
        entry = {"speaker": "NARRATOR", "instruct": "",
                 "text": "Eris pouts, her mouth へ."}
        parts, review = split_on_unspeakable(entry, 1000)
        self.assertIn("へ", parts[0]["text"])
        self.assertIn("へ", review)

    def test_repeated_unmapped_glyph_is_counted_each_time(self):
        # text.index() would have found only the first occurrence.
        from project import split_on_unspeakable
        entry = {"speaker": "NARRATOR", "instruct": "", "text": "a ⌘ b ⌘ c"}
        _parts, review = split_on_unspeakable(entry, 1000)
        self.assertEqual(review.count("⌘"), 2)

    def test_kana_in_real_japanese_is_not_flagged(self):
        from project import split_on_unspeakable
        entry = {"speaker": "NARRATOR", "instruct": "", "text": "こんにちは"}
        _parts, review = split_on_unspeakable(entry, 1000)
        self.assertEqual(review, [])


class BracketBoundaryTest(unittest.TestCase):
    """A bracketed span is a delivery change, not a scene break: it must not
    pick up the scene-break pause that separates sections."""

    def test_bracket_split_carries_no_pause(self):
        from project import split_on_unspeakable
        entry = {"speaker": "NARRATOR", "instruct": "",
                 "text": "He readied himself. <I saw a person.> I shuddered."}
        parts, _ = split_on_unspeakable(entry, 1000)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(not p.get("pause_after") for p in parts))

    def test_bracketed_part_gains_the_set_apart_hint(self):
        from project import split_on_unspeakable
        from verbalization import SET_APART_HINT
        entry = {"speaker": "NARRATOR", "instruct": "Tense.",
                 "text": "He readied himself. <I saw a person.> I shuddered."}
        parts, _ = split_on_unspeakable(entry, 1000)
        self.assertIn(SET_APART_HINT, parts[1]["instruct"])
        self.assertIn("Tense.", parts[1]["instruct"])
        self.assertNotIn(SET_APART_HINT, parts[0]["instruct"])

    def test_scene_break_still_pauses_when_brackets_present(self):
        from project import split_on_unspeakable
        entry = {"speaker": "NARRATOR", "instruct": "",
                 "text": "First part. <A vision.>\n\n■\n\nSecond part."}
        parts, _ = split_on_unspeakable(entry, 1000)
        paused = [p for p in parts if p.get("pause_after")]
        self.assertEqual(len(paused), 1)
        self.assertEqual(paused[0]["text"], "A vision.")


class ScenBreakPauseSurvivesGroupingTest(unittest.TestCase):
    """A scene break's silence must survive chunk grouping.

    split_on_unspeakable turns an inline scene break into two parts and puts
    pause_after on the first, but group_into_chunks merged them whenever
    speaker and instruct matched. The pause then applied to the end of the
    combined text, so the silence the break exists to produce was played after
    both sentences instead of between them - inaudible as a scene break.

    Unit tests covered the split and the grouping separately; nothing covered
    them composed, which is how this survived.
    """

    def test_an_inline_scene_break_still_separates_two_chunks(self):
        entries = [{"speaker": "NARRATOR", "instruct": "Calm.",
                    "text": "First sentence. ■ Second sentence."}]
        chunks = group_into_chunks(entries)
        self.assertEqual(2, len(chunks))
        self.assertEqual("First sentence.", chunks[0]["text"])
        self.assertEqual("Second sentence.", chunks[1]["text"])
        self.assertEqual(EXPLICIT_SILENCE_MS, chunks[0]["pause_after"])
        self.assertNotIn("pause_after", chunks[1])

    def test_explicit_zero_pause_survives_persisted_chunks_and_timeline(self):
        import copy
        import json
        from pydub import AudioSegment
        from tts import compute_timeline

        entries = [{"speaker": "NARRATOR", "instruct": "Calm.",
                    "text": "One.", "pause_after": 0},
                   {"speaker": "NARRATOR", "instruct": "Calm.",
                    "text": "Two.", "pause_after": 350},
                   {"speaker": "NARRATOR", "instruct": "Calm.", "text": "Three."}]
        original = copy.deepcopy(entries)
        with tempfile.TemporaryDirectory() as root:
            script_path = os.path.join(root, "annotated_script.json")
            with open(script_path, "w", encoding="utf-8") as target:
                json.dump(entries, target)
            manager = ProjectManager(root)
            chunks = manager.load_chunks()
            self.assertEqual(["One.", "Two.", "Three."],
                             [chunk["text"] for chunk in chunks])
            self.assertEqual([0, 350, None],
                             [chunk.get("pause_after") for chunk in chunks])
            with open(manager.chunks_path, encoding="utf-8") as source:
                self.assertEqual(chunks, json.load(source))
            segment = AudioSegment(data=b"\x00\x20" * 1600,
                                   sample_width=2, frame_rate=16000, channels=1)
            timeline = compute_timeline([(chunk, segment) for chunk in chunks],
                                        same_speaker_pause_ms=900)
            self.assertEqual([0, 100, 550], [start for _, _, start in timeline])
        self.assertEqual(original, entries)

    def test_entries_without_a_pause_still_merge(self):
        # The fix must not stop ordinary same-speaker merging.
        entries = [{"speaker": "NARRATOR", "instruct": "Calm.", "text": "One."},
                   {"speaker": "NARRATOR", "instruct": "Calm.", "text": "Two."}]
        chunks = group_into_chunks(entries)
        self.assertEqual(1, len(chunks))
        self.assertEqual("One. Two.", chunks[0]["text"])

    def test_a_pause_bearing_chunk_does_not_absorb_the_next_entry(self):
        entries = [{"speaker": "NARRATOR", "instruct": "Calm.",
                    "text": "Before. ■ After."},
                   {"speaker": "NARRATOR", "instruct": "Calm.",
                    "text": "Third."}]
        chunks = group_into_chunks(entries)
        self.assertEqual("Before.", chunks[0]["text"])
        self.assertEqual(EXPLICIT_SILENCE_MS, chunks[0]["pause_after"])
        # "After." carries no pause, so it may still merge with "Third."
        self.assertEqual("After. Third.", chunks[1]["text"])

    def test_several_scene_breaks_each_keep_their_silence(self):
        entries = [{"speaker": "NARRATOR", "instruct": "Calm.",
                    "text": "A. ■ B. ■ C."}]
        chunks = group_into_chunks(entries)
        self.assertEqual(["A.", "B.", "C."], [c["text"] for c in chunks])
        self.assertEqual([EXPLICIT_SILENCE_MS, EXPLICIT_SILENCE_MS],
                         [c["pause_after"] for c in chunks[:2]])


class M4BExportArtifactTests(unittest.TestCase):
    def test_staged_export_has_aac_chapters_and_failed_reexport_preserves_it(self):
        import json
        from pathlib import Path
        import subprocess
        from m4b_encode import start_owned_subprocess
        from pydub import AudioSegment
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            for number in (1, 2):
                AudioSegment.silent(duration=300, frame_rate=24000).export(
                    Path(tmp, f"line{number}.wav"), format="wav")
            manager.save_chunks([
                {"id": 0, "uid": "one", "speaker": "A", "text": "Opening.",
                 "audio_path": "line1.wav", "pause_after": 0},
                {"id": 1, "uid": "two", "speaker": "A", "text": "Ending.",
                 "audio_path": "line2.wav", "pause_after": 0}])
            ok, message = manager.merge_m4b(per_chunk_chapters=True,
                                            metadata={"title": "Example Book", "author": "Example Author"})
            self.assertTrue(ok, message)
            target = Path(tmp, "audiobook.m4b")
            original = target.read_bytes()
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams",
                                    "-show_chapters", "-show_format", "-of", "json", str(target)],
                                   capture_output=True, text=True, check=True, timeout=20)
            artifact = json.loads(probe.stdout)
            self.assertEqual(["aac"], [s["codec_name"] for s in artifact["streams"] if s["codec_type"] == "audio"])
            self.assertEqual(2, len(artifact["chapters"]))
            self.assertEqual("Example Book", artifact["format"]["tags"]["title"])
            self.assertEqual("Example Author", artifact["format"]["tags"]["artist"])
            self.assertAlmostEqual(0.6, float(artifact["format"]["duration"]), delta=0.1)
            self.assertGreater(len(AudioSegment.from_file(str(target))), 500)
            def failed_encoder(command, **kwargs):
                broken = list(command)
                broken[broken.index("-c:a") + 1] = "invalid-test-codec"
                return start_owned_subprocess(broken, **kwargs)
            with patch("m4b_encode.start_owned_subprocess", side_effect=failed_encoder):
                ok, message = manager.merge_m4b(per_chunk_chapters=True)
            self.assertFalse(ok)
            self.assertIn("FFmpeg failed", message)
            self.assertEqual(original, target.read_bytes())
            self.assertEqual([], list(Path(tmp).glob("*.pending.*")))
            self.assertFalse(Path(tmp, "temp_m4b_meta.txt").exists())
            self.assertFalse(Path(tmp, "temp_m4b_combined.wav").exists())


class ScriptShapeRegenerationTests(unittest.TestCase):
    def test_repeated_corrupt_loads_preserve_every_prior_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            chunks = Path(tmp) / 'chunks.json'
            expected = []
            for value in (b'["first rejected row"]', b'["second rejected row"]', b'{broken third row'):
                chunks.write_bytes(value)
                expected.append(value)
                self.assertEqual([], manager.load_chunks())
                self.assertFalse(chunks.exists())
                backups = [path.read_bytes() for path in Path(tmp).glob('chunks.json.corrupt*')]
                self.assertCountEqual(expected, backups)
            self.assertEqual(expected[0], Path(tmp, 'chunks.json.corrupt').read_bytes())

    def test_failed_corrupt_backup_preserves_original_and_refuses_regeneration(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            chunks = Path(tmp, "chunks.json")
            original = b'{broken generation progress'
            chunks.write_bytes(original)
            script = Path(tmp, "annotated_script.json")
            script.write_text(json.dumps([{"speaker": "Narrator", "text": "Recoverable source."}]))
            source = script.read_bytes()
            previous_backup = Path(tmp, "chunks.json.corrupt")
            previous_backup.write_bytes(b'older saved progress')
            replace = os.replace

            def refuse_backup(src, dst):
                if os.fspath(src) == str(chunks) and os.fspath(dst).startswith(str(previous_backup)):
                    raise PermissionError("backup destination denied")
                return replace(src, dst)

            with patch("project.os.replace", side_effect=refuse_backup), \
                    patch("project.group_into_chunks", wraps=group_into_chunks) as regenerate:
                with self.assertRaisesRegex(OSError, "Cannot preserve corrupted chunks.*refusing regeneration"):
                    manager.load_chunks()
                regenerate.assert_not_called()
            self.assertEqual(original, chunks.read_bytes())
            self.assertEqual(source, script.read_bytes())
            self.assertEqual(b'older saved progress', previous_backup.read_bytes())

    def test_malformed_json_shapes_warn_without_replacing_source_or_writing_chunks(self):
        for value in ({}, {"speaker": "A", "text": "wrong outer shape"},
                      None, True, 42, "text", [None], [False], [1], ["text"],
                      [[]], [{"speaker": "A", "text": "valid first"}, None]):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp, "annotated_script.json")
                raw = json.dumps(value).encode()
                script.write_bytes(raw)
                manager = ProjectManager(tmp)
                with self.assertLogs("project", level="WARNING") as logs:
                    self.assertEqual([], manager.load_chunks())
                    self.assertEqual((False, "No audio segments found"), manager.merge_audio())
                self.assertTrue(all("corrupted" in line for line in logs.output))
                self.assertTrue(any("array of objects" in line for line in logs.output))
                self.assertEqual(raw, script.read_bytes())
                self.assertFalse(Path(tmp, "chunks.json").exists())
                self.assertFalse(Path(tmp, "cloned_audiobook.mp3").exists())

    def test_corrupt_chunk_backup_survives_invalid_source_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            chunks = Path(tmp, "chunks.json")
            chunks.write_bytes(b"{broken progress")
            script = Path(tmp, "annotated_script.json")
            script.write_text('{"entries":[]}')
            with self.assertLogs("project", level="WARNING"):
                self.assertEqual([], ProjectManager(tmp).load_chunks())
            self.assertEqual(b"{broken progress", Path(tmp, "chunks.json.corrupt").read_bytes())
            self.assertFalse(chunks.exists())
            self.assertEqual('{"entries":[]}', script.read_text())

    def test_valid_empty_and_nonempty_arrays_keep_regeneration_and_uid_persistence(self):
        for entries in ([], [{"speaker": "ALICE", "text": "Wait here.", "instruct": "quiet"}]):
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp, "annotated_script.json")
                original = json.dumps(entries).encode()
                script.write_bytes(original)
                manager = ProjectManager(tmp)
                chunks = manager.load_chunks()
                self.assertEqual(len(entries), len(chunks))
                saved = Path(tmp, "chunks.json").read_bytes()
                self.assertEqual(chunks, manager.load_chunks())
                self.assertEqual(saved, Path(tmp, "chunks.json").read_bytes())
                self.assertEqual(original, script.read_bytes())
                if entries:
                    self.assertEqual("Wait here.", chunks[0]["text"])
                    self.assertEqual("ALICE", chunks[0]["speaker"])
                    self.assertEqual("quiet", chunks[0]["instruct"])
                    self.assertTrue(chunks[0]["uid"])
                    self.assertEqual("pending", chunks[0]["status"])
                    self.assertIsNone(chunks[0]["audio_path"])


class ChunkUpdateNullTests(unittest.TestCase):
    def _setup(self, root):
        manager = ProjectManager(str(root))
        chunk = {'uid': 'stable', 'text': 'Existing line', 'speaker': 'Hero',
                 'instruct': 'Calm', 'status': 'done', 'audio_path': 'audio.wav',
                 'pause_after': 200, 'extra': {'keep': True}}
        path = root / 'chunks.json'
        path.write_text(json.dumps([chunk]))
        (root / 'audio.wav').write_bytes(b'preserved prior audio')
        return manager, path, chunk

    def test_null_render_fields_reject_entire_request_before_any_mutation(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        api = FastAPI()
        api.include_router(editor.router)
        for field in ('text', 'speaker', 'instruct'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                root = Path(tmp)
                manager, path, _chunk = self._setup(root)
                before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
                with patch.object(editor, 'project_manager', manager), \
                     patch.object(editor, 'check_global_gpu_lock') as guard:
                    response = client.post('/api/chunks/0', json={field: None, 'pause_after': 999})
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(field, response.json()['detail'][0]['loc'][-1])
                guard.assert_not_called()
                self.assertEqual(before, {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()})

    def test_omitted_fields_and_null_pause_preserve_existing_render(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        api = FastAPI()
        api.include_router(editor.router)
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            root = Path(tmp)
            manager, path, original = self._setup(root)
            with patch.object(editor, 'project_manager', manager), \
                 patch.object(editor, 'check_global_gpu_lock'):
                unchanged = client.post('/api/chunks/0', json={})
                self.assertEqual(200, unchanged.status_code)
                self.assertEqual(original, unchanged.json())
                response = client.post('/api/chunks/0', json={'pause_after': None})
            self.assertEqual(200, response.status_code)
            expected = {k: v for k, v in original.items() if k != 'pause_after'}
            self.assertEqual(expected, response.json())
            self.assertEqual([expected], json.loads(path.read_bytes()))
            self.assertEqual(b'preserved prior audio', (root / 'audio.wav').read_bytes())

    def test_empty_and_unicode_strings_remain_editable_with_render_invalidated(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        api = FastAPI()
        api.include_router(editor.router)
        for field, value in (('text', ''), ('speaker', ''), ('instruct', ''),
                             ('text', 'こんにちは — café')):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
                root = Path(tmp)
                manager, path, original = self._setup(root)
                with patch.object(editor, 'project_manager', manager), \
                     patch.object(editor, 'check_global_gpu_lock'):
                    response = client.post('/api/chunks/0', json={field: value})
                self.assertEqual(200, response.status_code, response.text)
                expected = {**original, field: value, 'status': 'pending', 'audio_path': None}
                self.assertEqual(expected, response.json())
                self.assertEqual([expected], json.loads(path.read_bytes()))
                self.assertEqual(b'preserved prior audio', (root / 'audio.wav').read_bytes())


class BatchWorkerConfigTests(unittest.TestCase):
    def test_actual_config_loader_normalizes_worker_count_before_both_routes_dispatch(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import editor
        from config_settings import load_app_config_result
        api = FastAPI()
        api.include_router(editor.router)
        cases = ((2, 2), ('2', 2), (3.0, 3), (2.5, None), ('bad', None),
                 (0, None), (-1, None), (None, None), ([], None), ({}, None))
        with tempfile.TemporaryDirectory() as tmp, TestClient(api) as client:
            path = Path(tmp) / 'config.json'
            for endpoint, method, default in (
                    ('/api/generate_batch', 'generate_chunks_parallel', 2),
                    ('/api/generate_batch_fast', 'generate_chunks_batch', 4)):
                for stored, validated in cases:
                    with self.subTest(endpoint=endpoint, stored=stored):
                        path.write_text(json.dumps({'tts': {'parallel_workers': stored},
                                                   'extra': {'preserve': 1}}))
                        before = path.read_bytes()
                        config = load_app_config_result(str(path))
                        if validated is None:
                            self.assertNotIn('parallel_workers', config.data['tts'])
                            self.assertIn('tts.parallel_workers', {w.field for w in config.warnings})
                        else:
                            self.assertIs(type(config.data['tts']['parallel_workers']), int)
                            self.assertEqual(validated, config.data['tts']['parallel_workers'])
                        generate = Mock(return_value={'completed': [0], 'failed': [], 'cancelled': 0})
                        manager = SimpleNamespace(load_chunks=lambda: [{'uid': 'fixture'}], **{method: generate})
                        state = {'audio': {'running': False}}
                        with patch.object(editor, 'CONFIG_PATH', str(path)), \
                             patch.object(editor, 'project_manager', manager), \
                             patch.object(editor, 'process_state', state), \
                             patch.object(editor, 'check_global_gpu_lock'), \
                             patch.object(editor, 'schedule_claimed_background_task',
                                          side_effect=lambda background, name, callback: background.add_task(callback)) as schedule:
                            response = client.post(endpoint, json={'indices': [0]})
                        schedule.assert_called_once()
                        self.assertEqual(200, response.status_code, response.text)
                        generate.assert_called_once()
                        position = 1 if method == 'generate_chunks_parallel' else 2
                        actual = generate.call_args.args[position]
                        self.assertIs(type(actual), int)
                        self.assertEqual(default if validated is None else validated, actual)
                        self.assertFalse(state['audio']['running'])
                        self.assertNotIn('Batch generation error', '\n'.join(state['audio']['logs']))
                        self.assertEqual(before, path.read_bytes())


class InvalidExportAudioPathTests(unittest.TestCase):
    def _fixture(self, parent):
        import wave
        root = Path(parent, "book")
        root.mkdir()
        for path in (Path(parent, "private.wav"), root / "a.wav", root / "b.wav"):
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(b"\x01\x00" * 4800)
        (root / "corrupt.wav").write_bytes(b"not audio")
        paths = ["a.wav", "../private.wav", str(Path(parent, "private.wav")),
                 ["a.wav"], {"path": "a.wav"}, "missing.wav", "corrupt.wav", "b.wav"]
        chunks = [{"index": i, "speaker": "NARRATOR", "text": "A long narration line " * 5,
                   "audio_path": path} for i, path in enumerate(paths)]
        manager = ProjectManager(str(root))
        manager.save_chunks(chunks)
        manager.load_chunks()  # Complete existing UID backfill before the byte snapshot.
        before = {p: p.read_bytes() for p in Path(parent).rglob("*") if p.is_file()}
        return manager, before

    def test_invalid_persisted_paths_are_skipped_without_decoding_outside_audio(self):
        from project import _load_audio_segment
        with tempfile.TemporaryDirectory() as parent:
            manager, before = self._fixture(parent)
            with patch("project._load_audio_segment", wraps=_load_audio_segment) as decode:
                pairs, skipped = manager._load_chunks_with_audio()
            self.assertEqual([0, 7], [chunk["index"] for chunk, _ in pairs])
            self.assertEqual([200, 200], [len(audio) for _, audio in pairs])
            self.assertEqual(6, skipped)
            self.assertEqual(["a.wav", "corrupt.wav", "b.wav"],
                             [Path(call.args[0]).name for call in decode.call_args_list])
            self.assertTrue(all(Path(call.args[0]).parent == Path(manager.root_dir)
                                for call in decode.call_args_list))
            for path, data in before.items():
                self.assertEqual(data, path.read_bytes(), str(path))

    def test_partial_merge_publishes_decodable_audio_and_reports_every_skip(self):
        from pydub import AudioSegment
        with tempfile.TemporaryDirectory() as parent:
            manager, before = self._fixture(parent)
            ok, message = manager.merge_audio()
            self.assertTrue(ok, message)
            self.assertIn("6 chunk(s) skipped", message)
            merged = AudioSegment.from_file(str(Path(manager.root_dir, "cloned_audiobook.mp3")))
            self.assertGreaterEqual(len(merged), 400)
            ok, message = manager.export_chapters(fmt="wav")
            self.assertTrue(ok, message)
            self.assertIn("6 chunk(s) skipped", message)
            manifest = json.loads(Path(manager.root_dir, "chapter_exports", "manifest.json").read_text())
            self.assertEqual(2, len(manifest["chapters"]))
            ok, message = manager.export_chapters(fmt="wav", changed_only=True)
            self.assertTrue(ok, message)
            self.assertIn("6 chunk(s) skipped", message)
            for row in manifest["chapters"]:
                chapter = Path(manager.root_dir, "chapter_exports", row["file"])
                self.assertEqual(200, len(AudioSegment.from_file(str(chapter))))
            for path, data in before.items():
                self.assertEqual(data, path.read_bytes(), str(path))

    def test_all_invalid_and_cancellation_keep_the_existing_export(self):
        from project import ExportCancelled
        with tempfile.TemporaryDirectory() as parent:
            manager, before = self._fixture(parent)
            output = Path(manager.root_dir, "cloned_audiobook.mp3")
            output.write_bytes(b"existing export")
            chunks = manager.load_chunks()[1:-1]
            with patch.object(manager, "load_chunks", return_value=chunks):
                self.assertEqual(([], 6), manager._load_chunks_with_audio())
                self.assertEqual((False, "No audio segments found"), manager.merge_audio())
            with patch("project._load_audio_segment") as decode:
                with self.assertRaises(ExportCancelled):
                    manager._load_chunks_with_audio(cancel_check=lambda: True)
                self.assertEqual((False, "Merge cancelled"),
                                 manager.merge_audio(cancel_check=lambda: True))
                decode.assert_not_called()
            self.assertEqual(b"existing export", output.read_bytes())
            for path, data in before.items():
                self.assertEqual(data, path.read_bytes(), str(path))
