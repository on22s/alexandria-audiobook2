import json
import tempfile
import unittest
import wave
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import generate_personas as personas


class PersonaBatchTests(unittest.TestCase):
    def test_unique_extension_never_exceeds_limit_or_mutates_input(self):
        for count in (119, 120, 130):
            with self.subTest(count=count):
                values = [str(i) for i in range(count)]
                before = values[:]
                result = personas._unique_extend(values, ['new', 'NEW', 'next'], limit=120)
                self.assertEqual(120, len(result))
                self.assertEqual(before, values)
        self.assertEqual([], personas._unique_extend(['old'], ['new'], limit=0))

    def run_cli(self, tmp, flags, voices, script, engine):
        root = Path(tmp)
        (root / 'annotated_script.json').write_text(json.dumps(script))
        (root / 'voice_config.json').write_text(json.dumps(voices))
        with ExitStack() as stack:
            for name, value in (('get_runtime_data_dir', tmp), ('load_app_config', {}),
                                ('get_active_llm_config', {'model_name':'fixture'}),
                                ('ensure_ideal_settings', (False, {'context_length':4096}, 'fixture')),
                                ('make_run_client', object()), ('TTSEngine', engine)):
                stack.enter_context(patch.object(personas, name, return_value=value))
            stack.enter_context(patch('sys.argv', ['generate_personas.py'] + flags))
            stack.enter_context(patch.object(personas.time, 'sleep'))
            personas.main()
        return json.loads((root / 'voice_config.json').read_text())

    def test_advanced_alias_check_publishes_requested_alias_without_compile(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(personas, '_resolve_aliases_batch', return_value={'BETTY':'ROOT'}) as aliases, \
             patch.object(personas, '_compile_persona') as compile_voice, \
             patch.object(personas, '_discover_batch_characters', return_value=[]):
            saved = self.run_cli(tmp, ['--advanced', '--alias-check', '--speakers', 'BETTY'],
                {'ROOT':{'voice':'Ryan'}, 'BETTY':{'seed':0}},
                [{'speaker':'BETTY','text':'Hello there.'}], object())
            aliases.assert_called_once()
            compile_voice.assert_not_called()
            self.assertEqual('ROOT', saved['BETTY']['alias_of'])
            self.assertEqual(0, saved['BETTY']['seed'])

    def test_recovery_uses_requested_age_version_and_preserves_other_versions(self):
        import copy
        young = {'description': 'Synthetic young adult voice.', 'ref_text': 'A young greeting.', 'age_group': 'young'}
        old = {'description': 'Synthetic elderly voice.', 'ref_text': 'An older greeting.', 'age_group': 'old'}
        voices = {'ALICE': {**young, 'active_version': 'young', 'versions': {'young': young, 'old': old}}}
        for advanced in (False, True):
            for age in ('young', 'old'):
                with self.subTest(advanced=advanced, age=age), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp); source = root / 'source.wav'
                    with wave.open(str(source), 'wb') as stream:
                        stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed')); stream.writeframes(b'\0\0' * 2400)
                    engine = unittest.mock.Mock(); engine.generate_voice_design.return_value = (str(source), None)
                    before = copy.deepcopy(voices)
                    with patch.object(personas, '_compile_persona') as compile_voice, \
                         patch.object(personas, '_discover_batch_characters') as discover, \
                         patch.object(personas, 'request_persona_with_evidence') as request:
                        saved = self.run_cli(tmp, (['--advanced'] if advanced else []) + ['--recovered-speaker', 'ALICE', '--age-group', age], copy.deepcopy(voices), [{'speaker': 'ALICE', 'text': 'Synthetic line.'}], engine)
                    chosen = young if age == 'young' else old
                    engine.generate_voice_design.assert_called_once_with(description=chosen['description'], sample_text=chosen['ref_text'])
                    compile_voice.assert_not_called(); discover.assert_not_called(); request.assert_not_called()
                    self.assertEqual(chosen['description'], saved['ALICE']['versions'][age]['description'])
                    self.assertEqual(chosen['ref_text'], saved['ALICE']['versions'][age]['ref_text'])
                    self.assertEqual(age, saved['ALICE']['active_version'])
                    self.assertEqual(before['ALICE']['versions']['old' if age == 'young' else 'young'], saved['ALICE']['versions']['old' if age == 'young' else 'young'])
                    self.assertEqual(before, voices)
                    self.assertEqual(source.read_bytes(), (root / saved['ALICE']['ref_audio']).read_bytes())

    def test_recovery_refuses_missing_requested_age_without_overwriting_saved_voice(self):
        for advanced in (False, True):
            with self.subTest(advanced=advanced), tempfile.TemporaryDirectory() as tmp:
                voices = {'ALICE': {'description': 'Saved young voice.', 'ref_text': 'Saved greeting.', 'active_version': 'young'}}
                engine = unittest.mock.Mock()
                with patch.object(personas, '_discover_batch_characters') as discover:
                    with self.assertRaisesRegex(RuntimeError, 'ALICE'):
                        self.run_cli(tmp, (['--advanced'] if advanced else []) + ['--recovered-speaker', 'ALICE', '--age-group', 'old'], voices, [{'speaker': 'ALICE', 'text': 'Synthetic line.'}], engine)
                engine.generate_voice_design.assert_not_called(); discover.assert_not_called()
                self.assertEqual(voices, json.loads((Path(tmp) / 'voice_config.json').read_text()))

    def test_advanced_recovery_renders_saved_persona_without_discovery_or_compile(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'source.wav')
            with wave.open(str(source), 'wb') as wav:
                wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0' * 2400)
            engine = unittest.mock.Mock()
            engine.generate_voice_design.return_value = (str(source), None)
            with patch.object(personas, '_compile_persona') as compile_voice, \
                 patch.object(personas, '_discover_batch_characters') as discover:
                saved = self.run_cli(tmp, ['--advanced', '--recovered-speaker', 'ALICE'],
                    {'ALICE':{'description':'Saved warm voice.', 'ref_text':'Saved spoken line.', 'persona_ref':'legacy/ref.json'}},
                    [{'speaker':'ALICE','text':'New spoken line.'}], engine)
            compile_voice.assert_not_called();discover.assert_not_called()
            engine.generate_voice_design.assert_called_once_with(description='Saved warm voice.', sample_text='Saved spoken line.')
            self.assertEqual('legacy/ref.json', saved['ALICE']['persona_ref'])
            self.assertEqual(source.read_bytes(), (Path(tmp) / saved['ALICE']['ref_audio']).read_bytes())

    def test_advanced_slices_are_produced_only_as_discovery_consumes_them(self):
        observed = []
        class Script(list):
            def __getitem__(self, key):
                if isinstance(key, slice):
                    observed.append(('slice', key.start))
                return super().__getitem__(key)
        script = Script([{'speaker':'ALICE', 'text':'Hello'}] * 3)
        def discover(*args, **kwargs):
            observed.append(('discovery', kwargs['batch_start']))
            return []
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(personas, '_discover_batch_characters', side_effect=discover), \
             patch.object(personas, '_compile_persona'):
            personas.run_advanced_persona_generation(script, ['ALICE'], {}, {}, object(), 'fixture',
                object(), tmp, SimpleNamespace(batch_size=1))
        self.assertEqual([('slice',0), ('discovery',0), ('slice',1), ('discovery',1),
                          ('slice',2), ('discovery',2)], observed)

    def test_partial_standard_and_advanced_runs_save_success_then_report_failure(self):
        for advanced in (False, True):
            for failure in ('preview', 'exception'):
                with self.subTest(advanced=advanced, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source = root / 'source.wav'
                    with wave.open(str(source), 'wb') as wav:
                        wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0' * 2400)
                    count = [0]
                    def render(**kwargs):
                        count[0] += 1
                        if count[0] == 1:
                            raise OSError('fixture preview unavailable')
                        return str(source), None
                    engine = SimpleNamespace(generate_voice_design=render)
                    calls = [0]
                    def compile_reply(*args, **kwargs):
                        calls[0] += 1
                        if failure == 'exception' and calls[0] == 1:
                            raise OSError('fixture speaker request failed')
                        return {'description':'Warm natural voice.', 'ref_text':'Hello there, my friend.'}
                    if failure == 'exception':
                        # Standard currently catches this at speaker scope. Advanced
                        # intentionally falls back after provider failure, so inject
                        # an actual compile function failure instead.
                        count[0] = 1
                    original_compile = personas._compile_persona
                    def compile_voice(*args, **kwargs):
                        if failure == 'exception' and args[6] == 'ALICE':
                            raise OSError('fixture speaker compile failed')
                        return original_compile(*args, **kwargs)
                    with patch.object(personas, '_discover_batch_characters', return_value=[]), \
                         patch.object(personas, 'call_llm_for_object', side_effect=compile_reply), \
                         patch.object(personas, '_compile_persona', side_effect=compile_voice):
                        with self.assertRaisesRegex(RuntimeError, 'ALICE'):
                            self.run_cli(tmp, ['--advanced'] if advanced else [], {},
                                [{'speaker':'ALICE','text':'Hello there.'}, {'speaker':'BOB','text':'Hello again.'}], engine)
                    saved = json.loads((root / 'voice_config.json').read_text())
                    self.assertEqual('clone', saved['BOB']['type'])
                    self.assertEqual(source.read_bytes(), (root / saved['BOB']['ref_audio']).read_bytes())

    def test_malicious_legacy_manifest_is_not_consulted_or_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            victim = root / 'voice_config.json';victim.write_text('{"keep":17}')
            manifest = root / 'designed_voices/manifest.json';manifest.parent.mkdir()
            manifest.write_text(json.dumps([{'name':'ALICE','id':'../../voice_config','filename':'../../voice_config.json'}]))
            before = (victim.read_bytes(), manifest.read_bytes())
            source = root / 'source.wav'
            with wave.open(str(source), 'wb') as wav:
                wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0' * 2400)
            config = {}
            engine = SimpleNamespace(generate_voice_design=lambda **kw: (str(source), None))
            self.assertTrue(personas._save_generated_preview(tmp, engine, config, 'ALICE', 'Warm voice.', 'Hello there.'))
            self.assertEqual(before, (victim.read_bytes(), manifest.read_bytes()))
            self.assertEqual(source.read_bytes(), (root / config['ALICE']['ref_audio']).read_bytes())
