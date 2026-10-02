"""Persona completion must not add an unconditional pacing sleep."""
from contextlib import ExitStack
import io
import contextlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import generate_personas as personas

PAYLOAD = {'description': 'Warm natural voice.', 'ref_text': 'Hello there, my friend.'}


class PersonaFixedDelayTests(unittest.TestCase):
    def test_compile_success_and_failed_preview_have_no_fixed_sleep(self):
        for saved in (True, False):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as tmp:
                refs = Path(tmp) / 'refs'; refs.mkdir()
                config = {}
                saver = Mock(return_value=saved)
                with patch.object(personas, 'request_persona_with_evidence', return_value=PAYLOAD), \
                        patch.object(personas.time, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
                    result = personas._compile_persona(object(), 'fixture', object(), config, tmp,
                        str(refs), 'ANNA', {'ANNA': [PAYLOAD['ref_text']]}, '', '', preview_saver=saver)
                self.assertIs(result, saved)
                saver.assert_called_once()
                self.assertEqual(('ANNA', PAYLOAD['description'], PAYLOAD['ref_text']), saver.call_args.args[3:])
                self.assertTrue((Path(tmp) / config['ANNA']['persona_ref']).is_file())
                sleep.assert_not_called()

    def test_basic_cli_retains_preview_and_config_publication_without_fixed_sleep(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            (root / 'annotated_script.json').write_text(json.dumps([{'speaker':'ANNA','text':PAYLOAD['ref_text']}]))
            (root / 'voice_config.json').write_text('{}')
            for name, value in (('get_runtime_data_dir', tmp), ('load_app_config', {}),
                                ('get_active_llm_config', {'model_name':'fixture'}),
                                ('ensure_ideal_settings', (False, {'context_length':4096}, 'fixture')),
                                ('make_run_client', object()), ('TTSEngine', object()),
                                ('_resolve_aliases_batch', {}), ('request_persona_with_evidence', PAYLOAD)):
                stack.enter_context(patch.object(personas, name, return_value=value))
            def preview(_root, _engine, config, speaker, description, ref_text, **kwargs):
                config[speaker] = {'description': description, 'ref_text': ref_text}
                return True
            save = stack.enter_context(patch.object(personas, '_save_generated_preview', side_effect=preview))
            sleep = stack.enter_context(patch.object(personas.time, 'sleep'))
            stack.enter_context(patch('sys.argv', ['generate_personas.py']))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            personas.main()
            save.assert_called_once()
            stored = json.loads((root / 'voice_config.json').read_text())
            self.assertEqual(PAYLOAD, {k: stored['ANNA'][k] for k in PAYLOAD})
            sleep.assert_not_called()
