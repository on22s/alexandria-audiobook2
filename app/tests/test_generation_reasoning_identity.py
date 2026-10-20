"""Checkpoint identities must describe the reasoning options sent on the wire."""

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import generate_script as single
import llm_provider
import three_pass_generate as three


class GenerationReasoningIdentityTests(unittest.TestCase):
    def test_actual_single_pass_cli_publishes_distinct_effort_fingerprints(self):
        from tests import test_generation_model_binding as fixture
        identities = []
        for effort in ('none','high'):
            config = fixture.config()
            config['llm_local']['reasoning_effort'] = effort
            with tempfile.TemporaryDirectory() as directory, \
                 patch.object(fixture,'config',return_value=config):
                _,record = fixture.GenerationModelBindingTests().run_cli(single,Path(directory),False)
                identities.append(record['fingerprint']['settings_sha256'])
        self.assertNotEqual(identities[0],identities[1])

    def test_profile_effort_changes_wire_and_both_checkpoint_identities(self):
        fingerprints = []
        for effort in ('none','high'):
            profile = {'base_url':'http://localhost:1234/v1','api_key':'fixture',
                       'model_name':'fixture','reasoning_effort':effort}
            config = {'llm':profile,'generation':{}}
            original = copy.deepcopy(config)
            params = three.get_three_pass_run_params(config,{})
            wire = []
            sdk = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=lambda **kwargs:wire.append(kwargs))))
            with patch.object(llm_provider,'OpenAI',return_value=sdk):
                client = llm_provider.make_llm_client(profile,10)
                client.chat.completions.create(model='fixture',messages=[],
                                               extra_body=single.build_extra_body(params))
            self.assertEqual(effort,wire[0]['extra_body']['reasoning_effort'])
            fingerprints.append((three.three_pass_fingerprint('source','fixture',3000,params),
                single.get_generation_fingerprint('source',['source'],'fixture',profile['base_url'],params,3000)))
            self.assertEqual(original,config)
        self.assertNotEqual(fingerprints[0][0],fingerprints[1][0])
        self.assertNotEqual(fingerprints[0][1],fingerprints[1][1])
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory,'book.json'))
            three._save_three_pass_checkpoint(path,fingerprints[0][0],'segment',[],0,[],[])
            self.assertIsNotNone(three._load_three_pass_checkpoint(path,fingerprints[0][0]))
            self.assertIsNone(three._load_three_pass_checkpoint(path,fingerprints[1][0]))
            single.save_generation_checkpoint(path,fingerprints[0][1],[{
                'source_sha256':fingerprints[0][1]['chunk_sha256'][0],
                'entries':[{'speaker':'NARRATOR','text':'source','instruct':''}],
                'quality':{'passed':True}}])
            self.assertEqual([],single.load_generation_checkpoint(path,fingerprints[1][1]))

    def test_custom_body_precedence_and_explicit_cli_override_are_preserved(self):
        identities = []
        for effort in ('none','high'):
            config = {'llm':{'base_url':'http://localhost:1234/v1','model_name':'fixture',
                              'reasoning_effort':effort,
                              'provider_extra_body':{'reasoning_effort':'low'}},'generation':{}}
            params = three.get_three_pass_run_params(config,{})
            self.assertEqual('low',single.build_extra_body(params)['reasoning_effort'])
            identities.append(three.three_pass_fingerprint('source','fixture',3000,params))
            explicit = three.get_three_pass_run_params(config,{},reasoning_effort='none')
            self.assertEqual('none',single.build_extra_body(explicit)['reasoning_effort'])
        self.assertEqual(identities[0],identities[1])


if __name__ == '__main__':unittest.main()
