"""Voice assignment must not merge two characters into one voice.

`generate_personas.py` is 37 KB behind a live endpoint - it decides which voice
each character gets, so its mistakes are what a listener hears - and until now
no test imported it.

THE BUG THIS WAS WRITTEN FOR. `normalize_speaker_name` stripped honorifics, so
`MR. BENNET` and `MRS. BENNET` both reduced to `bennet` and
`_resolve_to_canonical` returned whichever came first in the roster. Mr and Mrs
Bennet would have shared a voice, along with the Hilberys, Allens, Halls and
Van der Luydens - six of twenty-eight books in the PDNC set. Nothing failed;
the audiobook simply had the wrong voice.

Case folding is NOT the same hazard and must keep working: the live config has
eleven pairs like EMILIA/Emilia and NOT-SATELLA/Not-Satella that are one
character each and rely on being merged.
"""
from tests.test_support import assert_file_lock_released
import os
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generate_personas import (_resolve_to_canonical, _token_jaccard,
                               get_exact_alias_match,
                               honorifics_are_distinguishing,
                               normalize_speaker_name, pick_ref_text)


class NormalizationTest(unittest.TestCase):

    def test_persona_object_request_parses_response_and_writes_log(self):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        import generate_script as gs
        from persona_validation import validate_persona_payload

        payload = {"description": "A calm, warm narrator.",
                   "ref_text": "Once upon a time, a traveller arrived."}
        response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(payload)),
            finish_reason="stop")], usage=None)
        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=Mock(return_value=response))))
        records = []
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "persona_responses.log"
            with patch.object(gs, "get_response_log_path", return_value=str(log_path)) as log:
                result = gs.call_llm_for_object(
                    client, "test-model", "Return a persona object.", "NARRATOR",
                    gs.LLMGenParams(), label="PERSONA NARRATOR",
                    validate_object=validate_persona_payload, max_retries=0,
                    attempt_observer=records.append)
            self.assertEqual(payload, result)
            client.chat.completions.create.assert_called_once()
            log.assert_called_once_with("persona_responses.log")
            self.assertIn("PERSONA NARRATOR", log_path.read_text())
            self.assertIn(payload["description"], log_path.read_text())
            self.assertEqual("accepted", records[0]["outcome"])

    def test_wrong_top_level_reference_shape_is_reported_and_reset_by_shared_loader(self):
        import generate_personas as personas
        import utils
        for malformed in ([], "not a reference", 9, None):
            with self.subTest(value=malformed), tempfile.TemporaryDirectory() as root:
                path = Path(personas._character_ref_path(root, "ALICE"))
                path.write_text(json.dumps(malformed))
                with self.assertLogs(utils.logger, level="WARNING") as messages:
                    ref = personas._append_character_ref(root, "ALICE", 1, {
                        "features": ["a low voice"], "sample_lines": ["Hello."]})
                self.assertTrue(any("Unexpected JSON shape" in message for message in messages.output))
                self.assertEqual("ALICE", ref["name"])
                self.assertEqual(["a low voice"], ref["features"])
                self.assertEqual(1, len(ref["observations"]))
                self.assertEqual(ref, json.loads(path.read_text()))

    def test_preview_preserves_concurrent_permanent_ui_entry(self):
        import generate_personas as personas
        from utils import atomic_json_write, file_lock
        with tempfile.TemporaryDirectory() as root:
            wav = Path(root, "source.wav")
            wav.write_bytes(b"preview")
            manifest_path = Path(root, "designed_voices", "manifest.json")
            manifest_path.parent.mkdir()
            def write_ui_entry():
                with file_lock(str(manifest_path)):
                    atomic_json_write([{"id": "ui", "name": "UI voice"}], str(manifest_path))
            def render(**kwargs):
                writer = threading.Thread(target=write_ui_entry)
                writer.start()
                writer.join(2)
                self.assertFalse(writer.is_alive())
                return str(wav), None
            engine = SimpleNamespace(generate_voice_design=render)
            self.assertTrue(personas._save_generated_preview(
                root, engine, {}, "Alice", "description", "sample"))
            self.assertEqual([{"id": "ui", "name": "UI voice"}],
                             json.loads(manifest_path.read_text()))


    def test_persona_route_passes_context_lines_to_the_script(self):
        import asyncio
        from unittest.mock import patch
        from types import SimpleNamespace
        from routers import voices as voices_module
        import core
        queued = []
        def add_task(fn, task_name, claim_id, callback):
            queued.append(callback.args[0])
            # This fixture records the handoff but runs no GPU worker.
            core.release_gpu_task_claim(task_name, claim_id, pending_only=True)
        tasks = SimpleNamespace(add_task=add_task)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "script.json")
            script.write_text(json.dumps([{ "speaker": "Hero", "text": "Hello." }]))
            with patch.object(voices_module, "SCRIPT_PATH", str(script)), \
                 patch.object(core, "DATA_DIR", tmp), \
                 patch.dict(core.process_state, {name: {**state, "running": False}
                            for name, state in core.process_state.items()}), \
                 patch.object(voices_module, "project_manager", SimpleNamespace(engine=None)):
                asyncio.run(voices_module.generate_personas(
                    tasks, voices_module.GeneratePersonasRequest(context_lines=50)))
        self.assertIn("--context-lines", queued[0])
        self.assertEqual("50", queued[0][queued[0].index("--context-lines") + 1])

    def test_persona_context_size_bounds_both_sample_and_narrator_lines(self):
        from generate_personas import select_persona_context
        lines = [f"line {i}" for i in range(40)]
        narr = [f"narr {i}" for i in range(40)]
        sample, intro = select_persona_context(lines, narr, 25)
        self.assertEqual(25, len(sample.splitlines()))
        self.assertEqual(25, len(intro.splitlines()))
        sample, intro = select_persona_context(lines, [], None)
        self.assertEqual(8, len(sample.splitlines()))
        self.assertIn("No nearby narrator", intro)

    def test_case_and_punctuation_always_fold(self):
        """The live config depends on this: EMILIA and Emilia are one
        character, as are NOT-SATELLA and Not-Satella."""
        self.assertEqual(normalize_speaker_name("EMILIA"),
                         normalize_speaker_name("Emilia"))
        self.assertEqual(normalize_speaker_name("NOT-SATELLA"),
                         normalize_speaker_name("Not-Satella"))
        self.assertEqual(normalize_speaker_name("  Man 1  "),
                         normalize_speaker_name("MAN 1"))

    def test_honorific_stripping_is_optional(self):
        self.assertEqual(normalize_speaker_name("Mr. Darcy"), "darcy")
        self.assertEqual(
            normalize_speaker_name("Mr. Darcy", strip_honorifics=False),
            "mr darcy")

    def test_non_string_input_is_empty(self):
        for bad in (None, 5, [], {}):
            self.assertEqual(normalize_speaker_name(bad), "")

    def test_automatic_aliases_accept_case_but_not_relationships(self):
        names = ["NITA'S DAD", "KIT'S MOTHER", "ROSHAUN'S FATHER", "Emilia"]
        self.assertEqual("Emilia", get_exact_alias_match("EMILIA", names))
        self.assertIsNone(get_exact_alias_match("NITA", names))
        self.assertIsNone(get_exact_alias_match("KIT", names))
        self.assertIsNone(get_exact_alias_match("ROSHAUN", names))

    def test_automatic_aliases_keep_distinguishing_honorifics(self):
        self.assertIsNone(get_exact_alias_match("MR. BENNET", ["MRS. BENNET"]))


class RosterAmbiguityTest(unittest.TestCase):

    def test_a_married_couple_is_detected(self):
        self.assertTrue(honorifics_are_distinguishing(
            ["MR. BENNET", "MRS. BENNET"]))

    def test_an_ordinary_roster_is_not(self):
        self.assertFalse(honorifics_are_distinguishing(
            ["EMILIA", "SUBARU", "MR. DARCY"]))

    def test_empty_and_none_are_safe(self):
        self.assertFalse(honorifics_are_distinguishing([]))
        self.assertFalse(honorifics_are_distinguishing(None))


class ResolutionTest(unittest.TestCase):
    """The behaviour that reaches a listener."""

    COUPLE = ["MR DARCY", "MRS DARCY"]

    def test_a_couple_resolves_to_two_distinct_voices(self):
        """THE BUG. Before the fix both returned MR DARCY."""
        self.assertEqual(_resolve_to_canonical("Mr. Darcy", self.COUPLE),
                         "MR DARCY")
        self.assertEqual(_resolve_to_canonical("Mrs. Darcy", self.COUPLE),
                         "MRS DARCY")

    def test_a_third_honorific_is_refused_rather_than_guessed(self):
        """Miss Darcy is not in this roster. Returning one of the two would be
        a confident wrong answer; None lets the caller decide."""
        self.assertIsNone(_resolve_to_canonical("Miss Darcy", self.COUPLE))

    def test_loose_matching_survives_where_it_is_safe(self):
        """Most books have no couple, and stripping the honorific there is
        useful. Turning it off globally would cost every one of them."""
        for raw in ("Mr. Darcy", "Darcy", "Miss Darcy"):
            self.assertEqual(_resolve_to_canonical(raw, ["DARCY"]), "DARCY")

    def test_case_variants_still_resolve(self):
        self.assertEqual(_resolve_to_canonical("Emilia", ["EMILIA"]), "EMILIA")
        self.assertEqual(
            _resolve_to_canonical("NOT-SATELLA", ["Not-Satella"]),
            "Not-Satella")

    def test_a_bare_surname_still_resolves_on_an_ambiguous_roster(self):
        """'Darcy' alone is genuinely ambiguous; resolving it to one of them is
        the existing behaviour and not what this fix is about. Asserted so a
        future change to it is deliberate."""
        self.assertIn(_resolve_to_canonical("Darcy", self.COUPLE), self.COUPLE)

    def test_unrelated_names_do_not_match(self):
        self.assertIsNone(_resolve_to_canonical("Subaru", ["EMILIA", "FELT"]))

    def test_short_names_do_not_match_longer_ones(self):
        """'al' must not resolve to 'allan' - initials would collide with
        everyone."""
        self.assertIsNone(_resolve_to_canonical("al", ["ALLAN", "JONATHAN"]))

    def test_empty_input_is_none(self):
        self.assertIsNone(_resolve_to_canonical("", ["EMILIA"]))
        self.assertIsNone(_resolve_to_canonical(None, ["EMILIA"]))


class JaccardTest(unittest.TestCase):

    def test_it_takes_the_same_honorific_decision_as_its_caller(self):
        """The leak that survived the first fix. With honorifics stripped,
        'Miss Darcy' and 'MR DARCY' both reduce to {darcy} and score 1.0, so
        step 3 undid step 1."""
        self.assertEqual(_token_jaccard("Miss Darcy", "MR DARCY"), 1.0)
        self.assertLess(
            _token_jaccard("Miss Darcy", "MR DARCY", strip_honorifics=False),
            0.4)

    def test_empty_names_score_zero(self):
        self.assertEqual(_token_jaccard("", "EMILIA"), 0.0)
        self.assertEqual(_token_jaccard("EMILIA", ""), 0.0)


class RefTextTest(unittest.TestCase):

    def test_it_picks_something_from_real_lines(self):
        lines = ["Short.", "A rather longer line of dialogue to read aloud.",
                 "Mid length line here."]
        self.assertIn(pick_ref_text(lines), lines + [""])

    def test_no_lines_does_not_crash(self):
        self.assertIsInstance(pick_ref_text([]), str)


class RealRosterShapeTest(unittest.TestCase):
    """Rosters shaped like the ones this app actually loads.

    This was a test against whatever voice_config.json happened to be on disk,
    asserting only isinstance(..., bool). It skipped in CI - where no config is
    committed, and the release verifier counts a skip as a failure - and when
    it did run it could not fail, since every return value is a bool. These
    cases state the invariant instead.
    """

    LIVE_SHAPE = ["EMILIA", "Emilia", "NOT-SATELLA", "Not-Satella", "SUBARU",
                  "FELT", "Narrator"]

    def test_a_case_variant_roster_is_not_treated_as_ambiguous(self):
        """The live book's collisions are all case variants, which SHOULD
        merge. Stripping honorifics is safe there."""
        self.assertFalse(honorifics_are_distinguishing(self.LIVE_SHAPE))

    def test_one_couple_anywhere_in_a_large_roster_is_enough(self):
        """The check must not be diluted by roster size - a single couple
        among many singletons is exactly the production case."""
        self.assertTrue(honorifics_are_distinguishing(
            self.LIVE_SHAPE + ["MR. HALL", "MRS. HALL"]))

    def test_a_roster_loaded_from_a_config_shaped_dict(self):
        """voice_config.json nests under 'characters'; the roster passed in is
        its keys. Asserted so the call site's shape stays covered."""
        raw = {"characters": {n: {"voice": "x"} for n in self.LIVE_SHAPE}}
        names = [n for n, v in raw["characters"].items() if isinstance(v, dict)]
        self.assertEqual(len(names), len(self.LIVE_SHAPE))
        self.assertFalse(honorifics_are_distinguishing(names))


if __name__ == "__main__":
    unittest.main()


class PersonaInternationalInputTests(unittest.TestCase):
    def test_non_ascii_speakers_attach_to_their_own_reference_files(self):
        import generate_personas as personas
        cases = (('José', 'JOSÉ'), ('Анна', 'АННА'), ('小明', '小明'),
                 ('मीरा', 'मीरा'), ('Jose\u0301', 'JOSÉ'))
        for raw, canonical in cases:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as tmp:
                roster = ['JOSE', 'БОРИС', '小红', 'किरण', canonical]
                self.assertEqual(canonical, personas.get_exact_alias_match(raw, roster))
                self.assertEqual(canonical, personas._resolve_to_canonical(raw, roster))
                personas._write_batch_character_refs(tmp, [{'name': raw, 'features': ['Warm voice']}], roster, 1)
                artifact = Path(personas._character_ref_path(tmp, canonical))
                self.assertEqual(canonical, json.loads(artifact.read_text())['name'])
                self.assertEqual(['Warm voice'], json.loads(artifact.read_text())['features'])
                self.assertNotEqual(personas.normalize_speaker_name(canonical), '')
        self.assertNotEqual(personas.normalize_speaker_name('JOSÉ'), personas.normalize_speaker_name('JOSE'))
        self.assertEqual('annamarie 2', personas.normalize_speaker_name('Anna_Marie- 2'))

    def test_nonstring_discovery_lists_do_not_become_saved_aliases_or_observations(self):
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as tmp:
            mixed = [None, {'wrong': 'value'}, ['nested'], 7, True, '  Annie  ', '']
            data = {k: list(mixed) for k in ('aliases', 'features', 'personality', 'voice_clues', 'relationships', 'sample_lines')}
            before = json.loads(json.dumps(data))
            result = personas._append_character_ref(tmp, 'ANNA', 1, data)
            saved = json.loads(Path(personas._character_ref_path(tmp, 'ANNA')).read_text())
            self.assertEqual(result, saved)
            for field in data:
                self.assertEqual(['Annie'], saved[field])
                if field != 'aliases':
                    self.assertEqual(['Annie'], saved['observations'][0][field])
            self.assertEqual(before, data)
            self.assertEqual(['Annie'], personas._as_list(' Annie '))
            for invalid in (None, 7, True, {}):
                self.assertEqual([], personas._as_list(invalid))


class PersonaWindowsReferenceTests(unittest.TestCase):
    def test_preview_and_compiled_reference_use_portable_windows_paths(self):
        import ntpath
        import numpy as np
        import soundfile as sf
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / 'generated.wav'
            sf.write(wav, np.sin(np.arange(24000) * 0.05).astype('float32') * 0.1, 24000)
            engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(wav), None))
            config = {}
            with patch.object(personas.os.path, 'relpath', side_effect=ntpath.relpath):
                self.assertTrue(personas._save_generated_preview(tmp, engine, config, 'ANNA', 'Warm voice.', 'Hello there.'))
            with self.subTest(stage='preview'):
                self.assertIn('designed_voices/persona/', config['ANNA']['ref_audio'])
                self.assertNotIn('\\', config['ANNA']['ref_audio'])
            audio, sr = sf.read(Path(tmp) / config['ANNA']['ref_audio'].replace('\\', '/'))
            self.assertEqual(24000, sr)
            self.assertEqual(24000, len(audio))
            ref_dir = Path(tmp) / 'refs'
            ref_dir.mkdir()
            payload = {'description': 'Warm natural voice.', 'ref_text': 'Hello there, my friend.'}
            with patch.object(personas.os.path, 'relpath', side_effect=ntpath.relpath), \
                 patch.object(personas, 'call_llm_for_object', return_value=payload), \
                 patch.object(personas.time, 'sleep'):
                personas._compile_persona(object(), 'mock', engine, config, tmp, str(ref_dir), 'ANNA',
                                          {'ANNA': ['Hello there, my friend.']}, '', '')
            with self.subTest(stage='compiled'):
                self.assertEqual('refs/anna.json', config['ANNA']['persona_ref'])
            self.assertTrue((Path(tmp) / config['ANNA']['persona_ref'].replace('\\', '/')).is_file())
            self.assertNotIn('\\', config['ANNA']['ref_audio'])


class PersonaManifestShapeTests(unittest.TestCase):
    def test_malformed_permanent_manifest_is_untouched_by_persona_previews(self):
        import numpy as np
        import soundfile as sf
        import generate_personas as personas
        import core
        import utils
        other = {'id': 'existing', 'name': 'BOB', 'filename': 'existing.wav'}
        cases = ({'bad': 'object'}, None, 'manifest', 9, [None, 'bad', other, 42], [])
        for document in cases:
            with self.subTest(document=document), tempfile.TemporaryDirectory() as tmp:
                wav = Path(tmp) / 'source.wav'
                sf.write(wav, np.sin(np.arange(24000) * 0.05).astype('float32') * 0.1, 24000)
                dest = Path(tmp) / 'designed_voices'
                dest.mkdir()
                manifest = dest / 'manifest.json'
                manifest.write_text(json.dumps(document))
                previous = dest / 'existing.wav'
                previous.write_bytes(wav.read_bytes())
                existing_audio = previous.read_bytes()
                engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(wav), None))
                config = {}
                with patch.object(core.logger, 'warning') as row_warning, patch.object(utils.logger, 'warning') as shape_warning:
                    self.assertTrue(personas._save_generated_preview(tmp, engine, config, 'ANN', 'Warm voice.', 'Hello there.'))
                self.assertEqual(document, json.loads(manifest.read_text()))
                self.assertEqual(existing_audio, previous.read_bytes())
                self.assertFalse(row_warning.called)
                self.assertFalse(shape_warning.called)
                audio, sr = sf.read(Path(tmp) / config['ANN']['ref_audio'])
                self.assertEqual(24000, len(audio))
                self.assertEqual(24000, sr)
                self.assertTrue((Path(tmp) / config['ANN']['ref_audio']).is_file())


class PersonaPersistenceExitTests(unittest.TestCase):
    def test_actual_cli_exits_nonzero_when_cast_save_fails_in_both_modes(self):
        import subprocess
        worker = r"""
import sys
from unittest.mock import patch
import generate_personas as personas
root, advanced, fail = sys.argv[1:]
sys.argv = ['generate_personas.py'] + (['--advanced'] if advanced == 'yes' else [])
original_write = personas._atomic_json_write

def write(data, path):
    assert path == root + '/voice_config.json'
    if fail == 'yes':
        raise PermissionError('injected cast write failure')
    original_write(data, path)

def preview(_root, _engine, config, speaker, description, ref_text, **kwargs):
    config[speaker] = {'description':description,'ref_text':ref_text,'type':'design'}
    return True

def advanced_run(**kwargs):
    kwargs['voice_config']['ALICE'] = {'description':'Warm natural voice.',
                                     'ref_text':'Hello there, my friend.','type':'design'}
    return [], kwargs['voice_config']

with patch.object(personas, 'get_runtime_data_dir', return_value=root), \
     patch.object(personas, 'load_app_config', return_value={}), \
     patch.object(personas, 'get_active_llm_config', return_value={'model_name':'fixture','base_url':'http://unused.invalid'}), \
     patch.object(personas, 'ensure_ideal_settings', return_value=(False,{'context_length':4096},'fixture self-heal disabled')), \
     patch.object(personas, 'make_run_client', return_value=object()), \
     patch.object(personas, 'llm_timeout_seconds', return_value=30), \
     patch.object(personas, 'TTSEngine'), \
     patch.object(personas, 'call_llm_for_object', return_value={'description':'Warm natural voice.','ref_text':'Hello there, my friend.'}), \
     patch.object(personas, '_save_generated_preview', side_effect=preview), \
     patch.object(personas, 'run_advanced_persona_generation', side_effect=advanced_run), \
     patch.object(personas.time, 'sleep'), \
     patch.object(personas, '_atomic_json_write', side_effect=write):
    personas.main()
"""
        for advanced in ('no', 'yes'):
            for fail in ('yes', 'no'):
                with self.subTest(advanced=advanced, fail=fail), tempfile.TemporaryDirectory() as root:
                    Path(root, 'annotated_script.json').write_text(json.dumps([
                        {'speaker':'ALICE','text':'Hello there, my friend.'}]))
                    original = b'{"BOB":{"voice":"Ryan","seed":0}}'
                    cast = Path(root, 'voice_config.json')
                    cast.write_bytes(original)
                    result = subprocess.run([sys.executable, '-c', worker, root, advanced, fail],
                                            capture_output=True, text=True, timeout=30)
                    if fail == 'yes':
                        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                        self.assertIn('Failed to save voice_config.json: injected cast write failure', result.stdout)
                        self.assertIn('PermissionError: injected cast write failure', result.stderr)
                        self.assertNotIn('Updated voice_config saved', result.stdout)
                        self.assertEqual(original, cast.read_bytes())
                    else:
                        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                        saved = json.loads(cast.read_text())
                        self.assertEqual({'voice':'Ryan','seed':0}, saved['BOB'])
                        self.assertEqual('Warm natural voice.', saved['ALICE']['description'])
                        self.assertEqual('Hello there, my friend.', saved['ALICE']['ref_text'])
                        self.assertIn('Updated voice_config saved', result.stdout)


class PersonaPreviewPublicationTests(unittest.TestCase):
    def test_previous_assets_survive_even_when_permanent_manifest_is_unwritable(self):
        from contextlib import redirect_stdout
        import io
        import array
        import wave
        import generate_personas as personas
        from utils import atomic_json_write
        for failure in (None, OSError("disk full"), PermissionError("read only")):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                dest = root / "designed_voices"
                dest.mkdir()
                source = root / "rendered.wav"
                def write_wav(path, amplitude):
                    with wave.open(str(path), "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(24000)
                        audio.writeframes(array.array('h', [amplitude] * 1200).tobytes())
                write_wav(source, 2048)
                old_audio = dest / "old_preview.wav"
                write_wav(old_audio, 1024)
                old_meta = dest / "old_meta.json"
                old_meta.write_text('{"description":"prior"}')
                other_audio = dest / "other.wav"
                write_wav(other_audio, 4096)
                manifest = dest / "manifest.json"
                other = {"id": "other", "name": "OTHER", "filename": "other.wav"}
                manifest.write_text(json.dumps([
                    {"id": "old", "name": "ALICE", "filename": "old_preview.wav"}, other]))
                originals = {p: p.read_bytes() for p in (old_audio, old_meta, manifest, source, other_audio)}
                writes = []
                def publish(data, path):
                    if Path(path) == manifest:
                        self.assertEqual(originals[old_audio], old_audio.read_bytes())
                        self.assertEqual(originals[old_meta], old_meta.read_bytes())
                        self.assertTrue(Path(str(manifest) + ".lock").exists())
                        writes.append("manifest")
                        if failure is not None:
                            raise failure
                    atomic_json_write(data, path)
                engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(source), None))
                config = {"ALICE": {"type": "clone", "ref_audio": "designed_voices/old_preview.wav",
                                     "ref_text": "Prior line.", "keep": "voice option"}}
                output = io.StringIO()
                with patch.object(personas, "_atomic_json_write", side_effect=publish), redirect_stdout(output):
                    self.assertTrue(personas._save_generated_preview(tmp, engine, config, "ALICE", "Warm voice.", "Current line."))
                self.assertEqual([], writes)
                new_audio = root / config["ALICE"]["ref_audio"]
                self.assertEqual(originals[source], new_audio.read_bytes())
                with wave.open(str(new_audio), "rb") as audio:
                    self.assertEqual(24000, audio.getframerate())
                    self.assertEqual(1200, audio.getnframes())
                    self.assertEqual(array.array('h', [2048] * 1200).tobytes(), audio.readframes(1200))
                self.assertEqual("voice option", config["ALICE"]["keep"])
                self.assertEqual("Current line.", config["ALICE"]["ref_text"])
                for p in (source, other_audio):
                    self.assertEqual(originals[p], p.read_bytes())
                for p in (old_audio, old_meta, manifest):
                    self.assertEqual(originals[p], p.read_bytes())
                metadata = new_audio.parent / "meta.json"
                saved_meta = json.loads(metadata.read_text())
                self.assertEqual("ALICE", saved_meta["speaker"])
                self.assertEqual(config["ALICE"]["ref_audio"], saved_meta["preview"])



class PersonaProtectedGroupTests(unittest.TestCase):
    def test_groups_never_resolve_to_a_component_or_a_different_group(self):
        cases = [('A AND B', ['A']), ('TWINS', ['TWIN']), ('CROWD', ['CROW']),
                 ('ALICE', ['ALICE AND BETTY']), ('A/B', ['AB']),
                 ('RAM AND REM', ['RAM AND EMILIA'])]
        for raw, roster in cases:
            with self.subTest(raw=raw, roster=roster):
                before = list(roster)
                self.assertIsNone(_resolve_to_canonical(raw, roster))
                self.assertEqual(before, roster)
        self.assertIsNone(get_exact_alias_match('A/B', ['AB']))

    def test_character_reference_publication_preserves_group_and_individual_evidence(self):
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as tmp:
            alice = personas._append_character_ref(tmp, 'ALICE', 1,
                    {'sample_lines':['Alice alone.'], 'features':['individual']})
            alice_path = Path(personas._character_ref_path(tmp, 'ALICE'))
            original = alice_path.read_bytes()
            # Without an established group label, group evidence must not be
            # silently written into the individual reference.
            personas._write_batch_character_refs(tmp,
                    [{'name':'ALICE AND BETTY', 'sample_lines':['Together!'],
                      'features':['two voices']}], ['ALICE'], 2)
            self.assertEqual(original, alice_path.read_bytes())
            self.assertEqual([alice_path], list(Path(tmp).glob('*.json')))
            # The same group's case and whitespace spelling is accepted.
            personas._write_batch_character_refs(tmp,
                    [{'name':'alice   and betty', 'sample_lines':['Together!'],
                      'features':['two voices']}], ['ALICE','ALICE AND BETTY'], 3)
            group_path = Path(personas._character_ref_path(tmp, 'ALICE AND BETTY'))
            group = json.loads(group_path.read_text())
            self.assertEqual('ALICE AND BETTY', group['name'])
            self.assertEqual(['Together!'], group['sample_lines'])
            self.assertEqual(['two voices'], group['features'])
            self.assertEqual([3], [observation['batch'] for observation in group['observations']])
            self.assertEqual(original, alice_path.read_bytes())
            self.assertEqual(alice['sample_lines'], json.loads(alice_path.read_text())['sample_lines'])

    def test_review_and_nickname_share_group_policy_and_preserve_same_group_spelling(self):
        import find_nicknames
        import review_script
        self.assertTrue(review_script.is_speaker_merge_allowed('alice   and betty', 'ALICE AND BETTY'))
        self.assertFalse(review_script.is_speaker_merge_allowed('A/B', 'AB'))
        self.assertFalse(review_script.is_speaker_merge_allowed('ALICE AND BETTY', 'ALICE AND CHARLIE'))
        actual, _ = find_nicknames._parse_alias_response(
            json.dumps({'aliases':{'ALICE AND BETTY':'ALICE'}}), ['ALICE','ALICE AND BETTY'])
        self.assertEqual({}, actual)
        self.assertEqual('ALICE AND BETTY', _resolve_to_canonical('alice   and betty', ['ALICE AND BETTY']))


class PersonaAliasGraphCliTests(unittest.TestCase):
    def test_actual_cli_validates_alias_graph_and_preserves_latest_human_cast(self):
        import subprocess
        import soundfile as sf
        worker=r"""
import copy,json,pathlib,sys
from unittest.mock import patch
import numpy as np,soundfile as sf
import generate_personas as personas
from utils import file_lock,atomic_json_write
root=pathlib.Path(sys.argv[1]);case=json.loads((root/'case.json').read_text())
sys.argv=['generate_personas.py']+(['--advanced'] if case.get('advanced') else ['--alias-check'])
def aliases(*args,**kwargs):
 if 'late' in case:
  with file_lock(str(root/'voice_config.json')):atomic_json_write(case['late'],str(root/'voice_config.json'))
 return case['proposals']
def preview(_root,_engine,config,speaker,description,ref_text,**kwargs):
 with (root/'previews.log').open('a') as f:f.write(speaker+'\n')
 sf.write(root/(speaker.replace(' ','_')+'.wav'),np.full(4000,0.1,dtype='float32'),16000)
 value=dict(config.get(speaker,{}));value.update(description=description,ref_text=ref_text,type='design')
 config[speaker]=value;return True
def advanced_run(**kwargs):
 for speaker in kwargs['selected_speakers']:
  preview(str(root),None,kwargs['voice_config'],speaker,'Warm natural voice.','Hello there, my friend.')
 return [], kwargs['voice_config']
with patch.object(personas,'run_advanced_persona_generation',side_effect=advanced_run),patch.object(personas,'get_runtime_data_dir',return_value=str(root)),patch.object(personas,'load_app_config',return_value={}),patch.object(personas,'get_active_llm_config',return_value={'model_name':'fixture','base_url':'http://unused.invalid'}),patch.object(personas,'ensure_ideal_settings',return_value=(False,{'context_length':4096},'fixture')),patch.object(personas,'make_run_client',return_value=object()),patch.object(personas,'llm_timeout_seconds',return_value=30),patch.object(personas,'TTSEngine'),patch.object(personas,'_resolve_aliases_batch',side_effect=aliases),patch.object(personas,'call_llm_for_object',return_value={'description':'Warm natural voice.','ref_text':'Hello there, my friend.'}),patch.object(personas,'_save_generated_preview',side_effect=preview),patch.object(personas.time,'sleep'):
 personas.main()
"""
        common={'ROOT':{'voice':'Ryan','seed':0},'OTHER':{'keep':17}}
        cases=[
            {'name':'advanced_human_alias','advanced':True,'initial':dict(common,A={'alias_of':'ROOT','seed':0,'keep':'human'}),'speakers':['A'],'proposals':{},'aliases':{'A':'ROOT'},'previews':[]},
            {'name':'advanced_mixed','advanced':True,'initial':dict(common,A={'alias_of':'ROOT','seed':0,'keep':'human'}),'speakers':['A','C'],'proposals':{},'aliases':{'A':'ROOT'},'previews':['C']},
            {'name':'chain','initial':dict(common,B={'alias_of':'ROOT','keep':'b'}),'speakers':['A'],'proposals':{'A':'B'},'aliases':{'A':'ROOT'},'previews':[]},
            {'name':'cycle','initial':common,'speakers':['A','B'],'proposals':{'A':'B','B':'A'},'aliases':{},'previews':['A','B']},
            {'name':'unknown','initial':common,'speakers':['A'],'proposals':{'A':'INVENTED'},'aliases':{},'previews':['A']},
            {'name':'near_miss','initial':common,'speakers':['A'],'proposals':{'A':'ROOTX'},'aliases':{},'previews':['A']},
            {'name':'group','initial':dict(common,A={'voice':'Ryan','keep':'individual'}),'speakers':['A AND B'],'proposals':{'A AND B':'A'},'aliases':{},'previews':['A AND B']},
            {'name':'existing_human','initial':dict(common,A={'alias_of':'ROOT','seed':0,'keep':'human'}),'speakers':['A'],'proposals':{'A':'OTHER'},'aliases':{'A':'ROOT'},'previews':[]},
            {'name':'bad_initial','initial':dict(common,A={'alias_of':'B'},B={'alias_of':'A'}),'speakers':['A'],'proposals':{'A':'ROOT'},'failed':True},
            {'name':'late_human','initial':dict(common,A={'seed':3,'keep':'initial'}),'late':dict(common,A={'alias_of':'HUMAN_ROOT','seed':0,'keep':'human'},HUMAN_ROOT={'voice':'Aiden'},LATE={'keep':'new'}),'speakers':['A'],'proposals':{'A':'ROOT'},'aliases':{'A':'HUMAN_ROOT'},'previews':[]},
            {'name':'late_removal','initial':dict(common,A={'alias_of':'ROOT','seed':3}),'late':dict(common,A={'seed':0,'keep':'unalias'}),'speakers':['A'],'proposals':{'A':'ROOT'},'aliases':{},'previews':[]},
            {'name':'late_chain_root','initial':dict(common,B={'alias_of':'ROOT'}),'late':dict(common,B={'alias_of':'HUMAN_ROOT'},HUMAN_ROOT={'voice':'Aiden'}),'speakers':['A'],'proposals':{'A':'B'},'aliases':{'A':'HUMAN_ROOT'},'previews':[]},
            {'name':'late_other_field','initial':dict(common,A={}), 'late':dict(common,A={'keep':'human'}),'speakers':['A'],'proposals':{'A':'ROOT'},'aliases':{},'previews':[]},
            {'name':'bad_late','initial':common,'late':dict(common,A={'alias_of':'B'},B={'alias_of':'A'}),'speakers':['A'],'proposals':{'A':'ROOT'},'failed':True},
        ]
        for case in cases:
            with self.subTest(case=case['name']),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);(root/'case.json').write_text(json.dumps(case))
                script=root/'annotated_script.json';script.write_text(json.dumps([{'speaker':name,'text':'Hello there, my friend.'} for name in case['speakers']]))
                before=script.read_bytes();cast=root/'voice_config.json';cast.write_text(json.dumps(case['initial']))
                result=subprocess.run([sys.executable,'-c',worker,tmp],capture_output=True,text=True,timeout=15)
                output=result.stdout+result.stderr
                current=case.get('late',case['initial'])
                if case.get('failed'):
                    self.assertNotEqual(0,result.returncode,output)
                    self.assertIn('cycle',output)
                    self.assertEqual(current,json.loads(cast.read_text()))
                    self.assertNotIn('Updated voice_config saved',result.stdout)
                else:
                    self.assertEqual(0,result.returncode,output)
                    saved=json.loads(cast.read_text())
                    for key,value in current.items():self.assertEqual(value,saved[key],(key,output))
                    expected=case['aliases']
                    for name in case['speakers']:
                        self.assertEqual(expected.get(name),saved.get(name,{}).get('alias_of'))
                    previews=(root/'previews.log').read_text().splitlines() if (root/'previews.log').exists() else []
                    self.assertEqual(case['previews'],previews,output)
                    for name in previews:
                        decoded,rate=sf.read(root/(name.replace(' ','_')+'.wav'))
                        self.assertEqual(16000,rate);self.assertEqual(4000,len(decoded))
                        self.assertAlmostEqual(0.1,float(decoded.mean()),places=3)
                self.assertEqual(before,script.read_bytes())
                assert_file_lock_released(str(cast))


class PersonaCastMergeTests(unittest.TestCase):
    def test_generated_fields_merge_only_where_human_has_not_changed_them(self):
        import copy
        from generate_personas import save_generated_voice_config
        initial={'A':{'description':'old','ref_text':'old text','seed':0,'obsolete':1},'REMOVED':{'keep':1}}
        generated={'A':{'description':'generated','ref_text':'generated text','seed':0,'added':[1]},'REMOVED':{'keep':2},'NEW':{'description':'new'}}
        latest={'A':{'description':'human','ref_text':'old text','seed':0,'obsolete':1,'keep':[7]},'HUMAN':{'keep':2}}
        originals=copy.deepcopy((initial,generated,latest))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'voice_config.json');path.write_text(json.dumps(latest))
            result=save_generated_voice_config(str(path),generated,initial,['A','NEW'],{})
            self.assertEqual({'A':{'description':'human','ref_text':'generated text','seed':0,'added':[1],'keep':[7]},'HUMAN':{'keep':2},'NEW':{'description':'new'}},json.loads(path.read_text()))
            self.assertEqual(json.loads(path.read_text()),result)
            result['A']['added'].append(2)
            self.assertEqual(originals,(initial,generated,latest))
            assert_file_lock_released(str(path))

    def test_noop_preserves_bytes_and_save_holds_lock(self):
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,'voice_config.json');raw=b'{ "A" : {"seed":0} }';path.write_bytes(raw)
            initial=json.loads(raw)
            with patch.object(personas,'_atomic_json_write',side_effect=AssertionError('no-op must not write')):
                personas.save_generated_voice_config(str(path),initial,initial,['A'],{})
            self.assertEqual(raw,path.read_bytes())
            def failed_write(value,target):
                self.assertTrue(Path(str(path)+'.lock').exists())
                raise PermissionError('fixture write failure')
            with patch.object(personas,'_atomic_json_write',side_effect=failed_write):
                with self.assertRaises(PermissionError):
                    personas.save_generated_voice_config(str(path),{'A':{'seed':0,'description':'new'}},initial,['A'],{})
            self.assertEqual(raw,path.read_bytes());assert_file_lock_released(str(path))


class EmptyPersonaSelectionTests(unittest.TestCase):
    def test_empty_selection_returns_before_runtime_side_effects(self):
        import contextlib
        import io
        import json
        import tempfile
        import generate_personas as personas
        from unittest.mock import Mock

        entries = [{"speaker": "NARRATOR", "text": "A door opened."},
                   {"speaker": "ALICE", "text": "Who is there?"}]
        configured = {speaker: {"type": "custom", "voice": "Ryan", "description": "A completed calm persona."}
                      for speaker in ("NARRATOR", "ALICE")}
        cases = ((entries, {}, ["--speakers", "MISSING"], "No speakers to process."),
                 (entries, {}, ["--advanced", "--speakers", "MISSING"], "No speakers to process."),
                 ([], {}, [], "No speakers to process."),
                 (entries, configured, ["--new-only"], "No speakers to process."),
                 (entries, {"ALICE": {"alias_of": "NARRATOR"}},
                  ["--advanced", "--speakers", "ALICE"], "No unique speakers to process"))
        for script, voices, arguments, message in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "annotated_script.json").write_text(json.dumps(script))
                (root / "voice_config.json").write_text(json.dumps(voices))
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                output = io.StringIO()
                heal, client, engine, save = (Mock() for _ in range(4))
                heal.side_effect = AssertionError("empty selection reached LM Studio self-healing")
                client.side_effect = AssertionError("empty selection created provider")
                engine.side_effect = AssertionError("empty selection constructed TTS engine")
                with patch.object(sys, "argv", ["generate_personas.py", *arguments]), \
                     patch.object(personas, "get_runtime_data_dir", return_value=tmp), \
                     patch.object(personas, "load_app_config", return_value={}), \
                     patch.object(personas, "ensure_ideal_settings", heal), \
                     patch.object(personas, "make_run_client", client), \
                     patch.object(personas, "TTSEngine", engine), \
                     patch.object(personas, "save_generated_voice_config", save), \
                     contextlib.redirect_stdout(output):
                    self.assertIsNone(personas.main())
                self.assertIn(message, output.getvalue())
                heal.assert_not_called()
                client.assert_not_called()
                engine.assert_not_called()
                save.assert_not_called()
                self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()
                                          if p.name not in (".active_book_transaction.json.lock", "voice_config.json.lock")})


class PersonaFallbackSourceIndexTests(unittest.TestCase):
    def test_actual_advanced_batches_persist_exact_global_source_evidence(self):
        import copy
        from unittest.mock import Mock
        import generate_personas as personas
        script = [{"speaker": "ALICE", "text": f"Unique source line {index}"}
                  for index in range(403)]
        before = copy.deepcopy(script)
        voice_config = {}
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "state.json").write_text(json.dumps({"active_book_id": "index_fixture"}))
            with patch.object(personas, "call_llm_for_object", side_effect=[{}, RuntimeError("API fixture"), {}]), \
                 patch.object(personas, "_compile_persona") as compile_voice:
                personas.run_advanced_persona_generation(script, ["ALICE"], {}, voice_config,
                    None, "fixture", Mock(), root, SimpleNamespace(batch_size=200))
            ref = json.loads(Path(personas._character_ref_path(
                compile_voice.call_args.args[5], "ALICE")).read_text())
            expected = [[0, 1, 2], [200, 201, 202], [400, 401, 402]]
            observations = ref["observations"]
            self.assertEqual(expected, [[item["entry_index"] for item in obs["evidence"]] for obs in observations])
            for observation in observations:
                self.assertLessEqual(len(observation["evidence"]), 3)
                self.assertLessEqual(len(observation["sample_lines"]), 3)
                for evidence in observation["evidence"]:
                    self.assertEqual(script[evidence["entry_index"]]["text"][:240], evidence["quote"])
            self.assertEqual(1, compile_voice.call_count)
        self.assertEqual(before, script)
        self.assertEqual({}, voice_config)

    def test_zero_origin_direct_call_preserves_limits_and_exact_quote_prefix(self):
        import copy
        import generate_personas as personas
        entries = [{"speaker": "ALICE", "text": "日本語 café " + str(index) + "x" * 400}
                   for index in range(5)]
        before = copy.deepcopy(entries)
        with patch.object(personas, "call_llm_for_object", return_value={}):
            characters = personas._discover_batch_characters(None, "fixture", "source", entries, 1)
        self.assertEqual([0, 1, 2], [e["entry_index"] for e in characters[0]["evidence"]])
        self.assertEqual(3, len(characters[0]["sample_lines"]))
        for item in characters[0]["evidence"]:
            self.assertEqual(entries[item["entry_index"]]["text"][:240], item["quote"])
            self.assertEqual(240, len(item["quote"]))
        self.assertEqual(before, entries)

    def test_benchmark_batches_use_the_same_source_indices_and_read_real_refs(self):
        import copy
        import benchmark_runner as benchmarks
        import generate_personas as personas
        entries = [{"speaker": "ALICE", "text": f"Benchmark source {index}"} for index in range(5)]
        fixture = {"id": "source_indices", "entries": entries, "speakers": ["ALICE"], "batch_size": 2}
        fixture["sha256"] = benchmarks._hash_entries({key: fixture[key] for key in ("entries", "speakers", "batch_size")})
        before = copy.deepcopy(fixture)
        captured = []
        def compile_fixture(client, model, engine, config, root, refs, speaker, samples, *args, **kwargs):
            reference = personas._load_character_ref(refs, speaker)
            captured.extend(reference["observations"])
            # Exercise the benchmark's real preview capture callback; no synthesis.
            kwargs["preview_saver"](root, engine, config, speaker, "fixture voice", "fixture sample")
        with patch.object(personas, "call_llm_for_object", side_effect=[{}, RuntimeError("API fixture"), {}]), \
             patch.object(personas, "_compile_persona", side_effect=compile_fixture):
            result = benchmarks._run_persona_case(fixture, None, "fixture", None)
        self.assertEqual([[0, 1], [2, 3], [4]], [[e["entry_index"] for e in obs["evidence"]] for obs in captured])
        for observation in captured:
            for evidence in observation["evidence"]:
                self.assertEqual(entries[evidence["entry_index"]]["text"], evidence["quote"])
        self.assertEqual("passed", result["status"])
        self.assertEqual(3, result["discovery_calls"])
        self.assertEqual(1, result["compile_calls"])
        self.assertEqual({"passed": True, "speaker_coverage": 1.0, "evidence_coverage": 1.0}, result["quality"])
        self.assertEqual(before, fixture)


class PersonaPreviewIsolationTests(unittest.TestCase):
    def test_same_speaker_keeps_permanent_and_legacy_assets_across_books(self):
        import array
        import wave
        import generate_personas as personas
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from fastapi.testclient import TestClient
        from routers import voice_design
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "designed_voices"
            dest.mkdir()
            source = root / "source.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(array.array('h', [1234] * 1200).tobytes())
            permanent = dest / "saved.wav"
            permanent.write_bytes(source.read_bytes())
            legacy = dest / "legacy.wav"
            legacy.write_bytes(source.read_bytes())
            manifest = dest / "manifest.json"
            rows = [{"id": "saved", "name": "ALICE", "filename": "saved.wav"},
                    {"id": "legacy", "name": "ALICE", "filename": "legacy.wav"}]
            manifest.write_text(json.dumps(rows))
            originals = {p: p.read_bytes() for p in (manifest, permanent, legacy)}
            engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(source), None))
            configs = []
            for book in ("Book A", "Book B", "Book A"):
                (root / "state.json").write_text(json.dumps({"active_book_id": book}))
                config = {"ALICE": {"ref_audio": "designed_voices/legacy.wav", "keep": 17}}
                self.assertTrue(personas._save_generated_preview(tmp, engine, config, "ALICE", "voice", "sample"))
                configs.append(config)
                self.assertEqual(17, config["ALICE"]["keep"])
                for path, expected in originals.items():
                    self.assertEqual(expected, path.read_bytes())
            refs = [c["ALICE"]["ref_audio"] for c in configs]
            self.assertEqual(3, len(set(refs)))
            self.assertEqual(Path(refs[0]).parent.parent, Path(refs[2]).parent.parent)
            self.assertNotEqual(Path(refs[0]).parent.parent, Path(refs[1]).parent.parent)
            app = FastAPI()
            app.include_router(voice_design.router)
            app.mount("/designed_voices", StaticFiles(directory=dest), name="designed_voices")
            with patch.object(voice_design, "DESIGNED_VOICES_MANIFEST", str(manifest)), \
                 patch.object(voice_design, "DESIGNED_VOICES_DIR", str(dest)), TestClient(app) as client:
                self.assertEqual(rows, client.get("/api/voice_design/list").json())
                for ref in refs:
                    result = client.get("/" + ref)
                    self.assertEqual(200, result.status_code)
                    self.assertEqual(source.read_bytes(), result.content)
                self.assertEqual(200, client.delete("/api/voice_design/saved").status_code)
                for ref in refs:
                    self.assertEqual(source.read_bytes(), (root / ref).read_bytes())
                self.assertEqual(originals[legacy], legacy.read_bytes())

    def test_copy_and_metadata_failures_preserve_existing_assignment_and_assets(self):
        import copy
        import generate_personas as personas
        from utils import atomic_json_write
        for phase in ("copy", "metadata"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "source.wav"
                source.write_bytes(b"source bytes")
                dest = root / "designed_voices"
                dest.mkdir()
                old = dest / "old.wav"
                old.write_bytes(b"old bytes")
                manifest = dest / "manifest.json"
                manifest.write_text('[{"id":"old","name":"ALICE","filename":"old.wav"}]')
                originals = {p: p.read_bytes() for p in (old, manifest, source)}
                config = {"ALICE": {"type":"clone", "ref_audio":"designed_voices/old.wav", "ref_text":"old", "keep":13}}
                before = copy.deepcopy(config)
                engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(source), None))
                def write(data, path):
                    if phase == "metadata":
                        raise OSError("fixture metadata disk full")
                    atomic_json_write(data, path)
                with patch.object(personas, "_atomic_json_write", side_effect=write):
                    if phase == "copy":
                        with patch.object(personas.shutil, "copy2", side_effect=OSError("fixture copy disk full")):
                            result = personas._save_generated_preview(tmp, engine, config, "ALICE", "voice", "sample")
                    else:
                        result = personas._save_generated_preview(tmp, engine, config, "ALICE", "voice", "sample")
                self.assertFalse(result)
                self.assertEqual(before, config)
                for path, expected in originals.items():
                    self.assertEqual(expected, path.read_bytes())
                self.assertEqual([], list(dest.rglob("preview.wav")))


class PersonaPreviewRuntimeRootTests(unittest.TestCase):
    def test_standard_cli_writes_runtime_assets_that_tts_can_resolve(self):
        import array
        import wave
        import generate_personas as personas
        import tts
        from contextlib import ExitStack
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "annotated_script.json").write_text(json.dumps([
                {"speaker":"ALICE", "text":"Hello there, my friend."}]))
            (root / "state.json").write_text('{"active_book_id":"runtime book"}')
            source = root / "rendered.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(array.array('h', [2000] * 600).tobytes())
            engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(source), None))
            with ExitStack() as stack:
                for name, value in (
                    ("get_runtime_data_dir", tmp), ("load_app_config", {}),
                    ("get_active_llm_config", {"model_name":"fixture", "base_url":"http://unused.invalid"}),
                    ("ensure_ideal_settings", (False, {"context_length":4096}, "fixture")),
                    ("make_run_client", object()), ("llm_timeout_seconds", 30),
                    ("TTSEngine", engine), ("call_llm_for_object", {
                        "description":"Warm natural voice.", "ref_text":"Hello there, my friend."})):
                    stack.enter_context(patch.object(personas, name, return_value=value))
                stack.enter_context(patch.object(personas.time, "sleep"))
                stack.enter_context(patch.object(sys, "argv", ["generate_personas.py"]))
                personas.main()
            saved = json.loads((root / "voice_config.json").read_text())
            ref = saved["ALICE"]["ref_audio"]
            with patch.object(tts, "_get_runtime_data_dir", return_value=tmp):
                path = Path(tts._resolve_asset_path(ref))
            self.assertEqual(source.read_bytes(), path.read_bytes())
            self.assertEqual("runtime book", json.loads((path.parent / "meta.json").read_text())["book_id"])
            self.assertFalse((root / "designed_voices/manifest.json").exists())


class PersonaReferenceGenerationOwnershipTests(unittest.TestCase):
    def test_failed_new_run_does_not_delete_a_saved_reference(self):
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state.json").write_text('{"active_book_id":"shared book"}')
            saved = root / "persona_refs/shared_book/alice.json"
            saved.parent.mkdir(parents=True)
            saved.write_text('{"name":"ALICE","sample_lines":["saved evidence"]}')
            before = saved.read_bytes()
            with patch.object(personas, "_discover_batch_characters", side_effect=OSError("fixture stopped discovery")):
                with self.assertRaisesRegex(OSError, "stopped discovery"):
                    personas.run_advanced_persona_generation(
                        [{"speaker":"ALICE","text":"new evidence"}], ["ALICE"], {}, {},
                        None, "fixture", None, tmp, SimpleNamespace(batch_size=1))
            self.assertTrue(saved.is_file(), "saved persona reference was deleted before discovery")
            self.assertEqual(before, saved.read_bytes())


    def test_parallel_runs_and_failed_restart_preserve_legacy_and_saved_generations(self):
        import array
        import wave
        from concurrent.futures import ThreadPoolExecutor
        import generate_personas as personas
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "state.json").write_text('{"active_book_id":"shared book"}')
            legacy = root / "persona_refs/shared_book/alice.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text('{"name":"ALICE","sample_lines":["legacy saved book"]}')
            legacy_bytes = legacy.read_bytes()
            source = root / "source.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(24000)
                audio.writeframes(array.array('h', [1000] * 1200).tobytes())
            engine = SimpleNamespace(generate_voice_design=lambda **kwargs: (str(source), None))
            barrier = threading.Barrier(2)
            discover = personas._discover_batch_characters
            def interleaved_discovery(*args, **kwargs):
                barrier.wait(5)
                return discover(*args, **kwargs)
            def generate(text):
                _, config = personas.run_advanced_persona_generation(
                    [{"speaker":"ALICE", "text":text}], ["ALICE"], {"ALICE":[text]},
                    {}, None, "fixture", engine, tmp, SimpleNamespace(batch_size=1))
                return config
            def source_backed_reply(*args, **kwargs):
                if not kwargs.get("label", "").startswith("PERSONA COMPILE"):
                    return {}
                reference = json.loads(args[3].split("Character reference:\n", 1)[1])
                return {"description": "Warm voice.", "ref_text": reference["reference_sample"]}
            with patch.object(personas, "_discover_batch_characters", side_effect=interleaved_discovery), \
                 patch.object(personas, "call_llm_for_object", side_effect=source_backed_reply), \
                 patch.object(personas.time, "sleep"), ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(generate, text) for text in ("First job evidence.", "Second job evidence.")]
                configs = [future.result(timeout=10) for future in futures]
            self.assertEqual(legacy_bytes, legacy.read_bytes())
            refs = [root / config["ALICE"]["persona_ref"] for config in configs]
            self.assertNotEqual(refs[0].parent, refs[1].parent)
            for ref, text in zip(refs, ("First job evidence.", "Second job evidence.")):
                doc = json.loads(ref.read_text())
                self.assertEqual([text], doc["sample_lines"])
                self.assertEqual(text, doc["observations"][0]["evidence"][0]["quote"])
            originals = {ref:ref.read_bytes() for ref in refs}
            with patch.object(personas, "_discover_batch_characters", side_effect=OSError("fixture interrupted discovery")):
                with self.assertRaisesRegex(OSError, "interrupted discovery"):
                    generate("Third job never completed.")
            self.assertEqual(legacy_bytes, legacy.read_bytes())
            for ref, expected in originals.items():
                self.assertEqual(expected, ref.read_bytes())
            for config in configs:
                self.assertEqual(source.read_bytes(), (root / config["ALICE"]["ref_audio"]).read_bytes())
