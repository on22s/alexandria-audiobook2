"""A Setup-tab save must not reset settings the tab does not render."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import routers.system as system_module
from config_settings import AppConfig


def _saved_config(tmp, generation, tts=None):
    profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
    doc = {"llm": profile, "llm_mode": "local", "llm_local": profile,
           "tts": tts or {"mode": "local"}, "generation": generation}
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


class KeepUnsentFieldsTests(unittest.TestCase):
    def _save(self, tmp, payload):
        path = os.path.join(tmp, "config.json")
        with patch.object(system_module, "CONFIG_PATH", path), \
             patch.object(system_module.project_manager, "invalidate_config_cache"), \
             patch.object(system_module.project_manager, "engine", None):
            asyncio.run(system_module.save_config(AppConfig.model_validate(payload)))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_hand_set_three_pass_and_rescue_values_survive_a_save_that_omits_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            _saved_config(tmp, {"chunk_size": 3000, "three_pass_chunk_size": 9000,
                                "context_rescue_windows": [1500], "context_rescue_retries": 5},
                          tts={"mode": "local", "external_timeout_seconds": 42})
            profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
            # What an older Setup tab posts: generation with only the fields it shows.
            saved = self._save(tmp, {"llm": profile, "llm_mode": "local", "llm_local": profile,
                                     "tts": {"mode": "local"},
                                     "generation": {"chunk_size": 2500, "temperature": 0.4}})
            self.assertEqual(2500, saved["generation"]["chunk_size"], "sent fields are applied")
            self.assertEqual(0.4, saved["generation"]["temperature"])
            self.assertEqual(9000, saved["generation"]["three_pass_chunk_size"], "unsent field kept")
            self.assertEqual([1500], saved["generation"]["context_rescue_windows"])
            self.assertEqual(5, saved["generation"]["context_rescue_retries"])
            self.assertEqual(42, saved["tts"]["external_timeout_seconds"])

    def test_a_field_sent_at_its_default_value_is_applied_not_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            _saved_config(tmp, {"three_pass_chunk_size": 9000})
            profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
            saved = self._save(tmp, {"llm": profile, "llm_mode": "local", "llm_local": profile,
                                     "tts": {"mode": "local"},
                                     "generation": {"three_pass_chunk_size": 3000}})
            self.assertEqual(3000, saved["generation"]["three_pass_chunk_size"],
                             "the client sent the default on purpose; the request decides, not equality with the default")

    def test_keep_unsent_fields_ignores_unknown_saved_keys(self):
        profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
        cfg = AppConfig.model_validate({"llm": profile, "llm_mode": "local", "llm_local": profile,
                                        "tts": {"mode": "local"}, "generation": {"chunk_size": 3000}})
        merged = system_module.keep_unsent_fields(cfg, {"generation": {"not_a_field": 1, "top_k": 33}})
        self.assertEqual(33, merged.generation.top_k)
        self.assertFalse(hasattr(merged.generation, "not_a_field"))

    def test_form_save_preserves_hidden_llm_options_and_profile_identity(self):
        import copy
        from generate_script import get_generation_fingerprint, LLMGenParams
        from lmstudio_settings import get_active_llm_config
        def fingerprint(doc):
            profile = get_active_llm_config(doc)
            params = LLMGenParams('system','{chunk}',structured_output=profile['structured_output'])
            return get_generation_fingerprint('source',['source'],profile['model_name'],
                                              profile['base_url'],params,3000)
        for mode in ('local','remote'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                local = {'base_url':'http://localhost:1234/v1','model_name':'local-model',
                         'api_key':'local-key','structured_output':'off'}
                remote = {'base_url':'https://fixture.thundercompute.net/v1','model_name':'remote-model',
                          'api_key':'remote-key','structured_output':'off'}
                existing = {'llm_mode':mode,'llm_local':local,'llm_remote':remote,
                            'llm':copy.deepcopy(remote if mode == 'remote' else local),'tts':{'mode':'local'}}
                with open(os.path.join(tmp,'config.json'),'w') as handle:json.dump(existing,handle)
                payload = copy.deepcopy(existing)
                for section in ('llm','llm_local','llm_remote'):
                    payload[section].pop('structured_output');payload[section]['api_key']=system_module._REDACTED_SECRET
                payload['tts']['pause_same_speaker_ms'] = 321
                saved = self._save(tmp,payload)
                for section in ('llm','llm_local','llm_remote'):
                    self.assertEqual('off',saved[section]['structured_output'])
                    self.assertEqual(existing[section]['api_key'],saved[section]['api_key'])
                self.assertEqual(321,saved['tts']['pause_same_speaker_ms'])
                self.assertEqual(fingerprint(existing),fingerprint(saved))
                # An explicitly sent default is an edit, including after a mode switch.
                other = 'remote' if mode == 'local' else 'local'
                payload['llm_mode'] = other
                payload['llm_'+other]['structured_output'] = 'auto'
                switched = self._save(tmp,payload)
                self.assertEqual('auto',switched['llm']['structured_output'])
                self.assertEqual(other+'-key',switched['llm']['api_key'])
                self.assertEqual('off',switched['llm_'+mode]['structured_output'])

    def test_hidden_option_preservation_does_not_copy_headers_to_an_edited_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = {'base_url':'http://localhost:1234/v1','api_key':'old','model_name':'m',
                       'structured_output':'off','provider_headers':{'Authorization':'old-header'}}
            existing = {'llm_mode':'local','llm':profile,'llm_local':profile,'tts':{'mode':'local'}}
            with open(os.path.join(tmp,'config.json'),'w') as handle:json.dump(existing,handle)
            new = {'base_url':'http://localhost:4321/v1','api_key':'new','model_name':'m'}
            saved = self._save(tmp,{'llm_mode':'local','llm':new,'llm_local':new,'tts':{'mode':'local'}})
            self.assertEqual('off',saved['llm']['structured_output'])
            self.assertEqual({},saved['llm']['provider_headers'])
            self.assertEqual('new',saved['llm']['api_key'])




class AttributionPresetSaveTests(unittest.TestCase):
    """Saving Setup derives the run's prompt variant from the active preset,
    keeps a preset the client did not resend, and refuses a broken template."""

    def _save(self, tmp, payload):
        path = os.path.join(tmp, "config.json")
        with patch.object(system_module, "CONFIG_PATH", path), \
             patch.object(system_module.project_manager, "invalidate_config_cache"), \
             patch.object(system_module.project_manager, "engine", None):
            asyncio.run(system_module.save_config(AppConfig.model_validate(payload)))
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _payload(self, **extra):
        profile = {"base_url": "http://localhost:1234/v1", "api_key": "k", "model_name": "m"}
        return {"llm": profile, "llm_mode": "local", "llm_local": profile,
                "tts": {"mode": "local"}, "generation": {}, **extra}

    def test_variant_is_derived_from_the_active_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            _saved_config(tmp, {})
            saved = self._save(tmp, self._payload(
                prompts={"attribution_preset": "mine"},
                prompt_presets=[{"name": "mine", "variant": "michel2_full",
                                 "system_prompt": "S", "user_prompt": "U"}]))
            self.assertEqual("michel2_full", saved["generation"]["three_pass_attribute_prompt_variant"])
            self.assertEqual("mine", saved["prompts"]["attribution_preset"])
            # a later save that names a builtin and does not resend presets
            # keeps the user's preset on disk and derives the builtin's variant
            saved = self._save(tmp, self._payload(prompts={"attribution_preset": "michel"}))
            self.assertEqual("michel", saved["generation"]["three_pass_attribute_prompt_variant"])
            self.assertEqual(["mine"], [p["name"] for p in saved["prompt_presets"]])

    def test_a_default_template_without_placeholders_is_refused(self):
        from fastapi import HTTPException
        with tempfile.TemporaryDirectory() as tmp:
            _saved_config(tmp, {})
            with self.assertRaises(HTTPException) as ctx:
                self._save(tmp, self._payload(
                    prompts={"attribution_preset": "broken"},
                    prompt_presets=[{"name": "broken", "variant": "default",
                                     "system_prompt": "S", "user_prompt": "no placeholders"}]))
            self.assertEqual(400, ctx.exception.status_code)
            self.assertIn("{batch}", ctx.exception.detail)

    def test_builtins_and_preview(self):
        builtins = system_module._builtin_prompt_presets()
        self.assertEqual(10, len(builtins))
        self.assertTrue(all(b["builtin"] and b["system_prompt"] for b in builtins))
        preview = asyncio.run(system_module.attribution_preview(
            system_module.AttributionPreviewRequest(variant="michel2_full", context_chars=1500)))
        self.assertIn("|n|", preview["system_prompt"])
        self.assertIn("BEFORE THE PASSAGE", preview["user_message"])
        self.assertIn('|1|"We sail at dawn, Lena."|1|', preview["user_message"])
        self.assertIn("PIET (also: OLD PIET)", preview["user_message"])
        spoken_preview = asyncio.run(system_module.attribution_preview(
            system_module.AttributionPreviewRequest(variant="michel2_spoken", context_chars=1500)))
        self.assertIn('Label only the spoken quotations', spoken_preview["system_prompt"])
        self.assertNotIn('[0]', spoken_preview["user_message"])
        self.assertIn('|1|', spoken_preview["user_message"])

        edited = asyncio.run(system_module.attribution_preview(
            system_module.AttributionPreviewRequest(variant="michel2", user_prompt="MY INSTRUCTION")))
        self.assertTrue(edited["user_message"].endswith("MY INSTRUCTION"))
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            asyncio.run(system_module.attribution_preview(
                system_module.AttributionPreviewRequest(variant="default", user_prompt="no placeholders")))


if __name__ == "__main__":
    unittest.main()
