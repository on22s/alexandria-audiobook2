"""Reference artifacts must preserve identities lost by filename sanitization."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import generate_personas as personas
from pydantic import ValidationError
from routers.voices import PersonaVoiceAuditRequest


class PersonaReferenceIdentityTests(unittest.TestCase):
    labels = ('ALICE/BOB', 'ALICE&BOB', 'A+B', 'A_B', '山田/太郎', '山田&太郎')

    def test_exact_labels_have_distinct_deterministic_bounded_safe_paths(self):
        labels = self.labels + ('БОРИС', 'मीरा', 'İ' * 200, '../../CON\\voice\0name')
        with tempfile.TemporaryDirectory() as tmp:
            paths = [Path(personas._character_ref_path(tmp, label)) for label in labels]
            self.assertEqual(len(labels), len(set(paths)))
            self.assertEqual(len(labels), len({path.name.casefold() for path in paths}))
            for label, path in zip(labels, paths):
                self.assertEqual(path, Path(personas._character_ref_path(tmp, label)))
                self.assertEqual(Path(tmp), path.parent)
                self.assertLessEqual(len(path.name.encode('utf-8')), 150)
                self.assertTrue(path.name.endswith(hashlib.sha256(label.encode('utf-8')).hexdigest() + '.json'))
                self.assertNotIn('\\', path.name)
                self.assertNotIn('\0', path.name)
                path.write_text(label, encoding='utf-8')
                self.assertEqual(label, path.read_text(encoding='utf-8'))

    def test_same_batch_and_later_append_keep_each_reference_and_compiled_sample(self):
        script = [{'speaker': label, 'text': f'Only source dialogue number {index}.'}
                  for index, label in enumerate(self.labels)]
        discovery = {row['speaker']: {'features': [f'trait {index}'],
                     'sample_lines': [row['text']],
                     'evidence': [{'entry_index': index, 'quote': row['text']}]}
                     for index, row in enumerate(script)}
        prompts = {}

        def reply(*args, **kwargs):
            if kwargs['label'].startswith('PERSONA DISCOVERY'):
                return copy.deepcopy(discovery)
            reference = json.JSONDecoder().raw_decode(
                args[3].split('Character reference:\n', 1)[1])[0]
            prompts[reference['name']] = reference
            result = {'description': 'Natural voice with ' + reference['features'][0] + '.',
                      'ref_text': reference['reference_sample']}
            kwargs['validate_object'](result)
            return result

        def preview(_root, _engine, config, speaker, description, ref_text, **kwargs):
            config[speaker].update(description=description, ref_text=ref_text)
            return True

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(personas, 'call_llm_for_object', side_effect=reply), \
             patch.object(personas, '_save_generated_preview', side_effect=preview):
            failures, config = personas.run_advanced_persona_generation(
                script, self.labels, {}, {}, object(), 'fixture', object(), tmp,
                SimpleNamespace(batch_size=len(script)))
            self.assertEqual([], failures)
            paths = [Path(tmp) / config[label]['persona_ref'] for label in self.labels]
            self.assertEqual(len(self.labels), len(set(paths)))
            for index, (label, path) in enumerate(zip(self.labels, paths)):
                ref = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(label, ref['name'])
                self.assertEqual([f'trait {index}'], ref['features'])
                self.assertEqual([script[index]['text']], ref['sample_lines'])
                self.assertEqual(ref, personas._load_character_ref(str(path.parent), label))
                self.assertEqual(label, prompts[label]['name'])
                self.assertEqual([f'trait {index}'], prompts[label]['features'])
                self.assertEqual([script[index]['text']], prompts[label]['sample_lines'])
                self.assertEqual(script[index]['text'], config[label]['ref_text'])
                self.assertIn(f'trait {index}', config[label]['description'])
                # Appending after reloading uses the same identity as compilation.
                personas._append_character_ref(str(path.parent), label, 2,
                    {'features': [f'later trait {index}']})
                updated = personas._load_character_ref(str(path.parent), label)
                self.assertEqual([f'trait {index}', f'later trait {index}'], updated['features'])
                self.assertEqual([script[index]['text']], updated['sample_lines'])

    def test_compile_rejects_dialogue_borrowed_from_colliding_label(self):
        first, second = self.labels[:2]
        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / 'refs'; refs.mkdir()
            for label, line in ((first, 'First identity speaks alone.'),
                                (second, 'Second identity speaks alone.')):
                personas._append_character_ref(str(refs), label, 1, {'sample_lines': [line]})
            preview = Mock()
            with patch.object(personas, 'call_llm_for_object', return_value={
                    'description': 'Natural voice.', 'ref_text': 'Second identity speaks alone.'}):
                with self.assertRaises(personas.PersonaContextRecoveryError):
                    personas._compile_persona(object(), 'fixture', object(), {}, tmp,
                        str(refs), first, {}, '', '', preview_saver=preview)
            preview.assert_not_called()

    def test_advanced_long_book_and_speaker_refs_fit_existing_audit_guard(self):
        cases = (('A long ASCII book title ' * 30, ['ALICE', 'A long speaker label ' * 30]),
                 ('長い日本語の本の題名' * 30, ['ミラ', 'मीरा長い名前' * 30]))

        def preview(_root, _engine, config, speaker, description, ref_text, **kwargs):
            config[speaker].update(description=description, ref_text=ref_text)
            return True

        def reply(*args, **kwargs):
            if not kwargs['label'].startswith('PERSONA COMPILE'):
                return {}
            ref = json.JSONDecoder().raw_decode(args[3].split('Character reference:\n', 1)[1])[0]
            return {'description': 'Natural voice.', 'ref_text': ref['reference_sample']}

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(personas, 'call_llm_for_object', side_effect=reply), \
             patch.object(personas, '_save_generated_preview', side_effect=preview):
            for book_id, speakers in cases:
                with self.subTest(book=book_id, speakers=speakers):
                    script = [{'speaker': speaker, 'text': f'Exact source dialogue {index}.'}
                              for index, speaker in enumerate(speakers)]
                    failures, config = personas.run_advanced_persona_generation(
                        script, speakers, {}, {}, object(), 'fixture', object(), tmp,
                        SimpleNamespace(batch_size=2), book_id=book_id)
                    self.assertEqual([], failures)
                    paths = []
                    for index, speaker in enumerate(speakers):
                        ref_path = config[speaker]['persona_ref']
                        self.assertLessEqual(len(ref_path.encode('utf-8')), 200)
                        self.assertTrue(Path(ref_path).parent.parent.name.endswith(
                            hashlib.sha256(book_id.encode('utf-8')).hexdigest()))
                        self.assertEqual(ref_path, PersonaVoiceAuditRequest(persona_ref=ref_path).persona_ref)
                        ref = json.loads((Path(tmp) / ref_path).read_text(encoding='utf-8'))
                        self.assertEqual(speaker, ref['name'])
                        self.assertEqual([script[index]['text']], ref['sample_lines'])
                        paths.append(ref_path)
                    self.assertEqual(len(speakers), len(set(paths)))
        # Keep the existing audit safety limit; shortening generated paths must
        # not be implemented by relaxing request validation.
        self.assertEqual('x' * 200, PersonaVoiceAuditRequest(persona_ref='x' * 200).persona_ref)
        with self.assertRaises(ValidationError):
            PersonaVoiceAuditRequest(persona_ref='x' * 201)

    def test_recovery_preserves_saved_identity_and_legacy_reference_contract(self):
        first, second = self.labels[:2]
        with tempfile.TemporaryDirectory() as tmp:
            refs = Path(tmp) / 'refs'; refs.mkdir()
            config = {}
            for index, label in enumerate((first, second)):
                line = f'Saved dialogue for identity {index}.'
                personas._append_character_ref(str(refs), label, 1, {'sample_lines': [line]})
                config[label] = {'description': f'Saved voice {index}.', 'ref_text': line,
                    'persona_ref': os.path.relpath(personas._character_ref_path(str(refs), label), tmp)}
            # Recovery also supports a saved path from before hashed basenames.
            legacy = refs / 'legacy.json'
            legacy.write_text(json.dumps({'name': 'LEGACY', 'sample_lines': ['Legacy source line.']}))
            config['LEGACY'] = {'description': 'Legacy voice.', 'ref_text': 'Legacy source line.',
                                'persona_ref': 'refs/legacy.json'}
            persisted = Path(tmp) / 'voice_config.json'
            persisted.write_text(json.dumps(config), encoding='utf-8')
            config = json.loads(persisted.read_text(encoding='utf-8'))
            before = {path: path.read_bytes() for path in refs.iterdir()}
            for label in (second, first, 'LEGACY'):
                preview = Mock(return_value=True)
                with patch.object(personas, '_save_generated_preview', preview), \
                     patch.object(personas, '_discover_batch_characters') as discover, \
                     patch.object(personas, '_compile_persona') as compile_voice:
                    failures, restored = personas.run_advanced_persona_generation(
                        [], [label], {}, config, object(), 'fixture', object(), tmp,
                        SimpleNamespace(batch_size=1, recovered_speaker=label))
                self.assertEqual([], failures)
                discover.assert_not_called(); compile_voice.assert_not_called()
                self.assertEqual(config, restored)
                self.assertEqual((label, config[label]['description'], config[label]['ref_text']),
                                 preview.call_args.args[3:])
                ref = json.loads((Path(tmp) / restored[label]['persona_ref']).read_text(encoding='utf-8'))
                self.assertEqual(label, ref['name'])
                self.assertEqual([restored[label]['ref_text']], ref['sample_lines'])
            self.assertEqual(before, {path: path.read_bytes() for path in refs.iterdir()})
