"""Speech verification configuration keeps active tasks and engine policy coherent."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import core
from tests import test_synthesis_window_settings_admission as windows
from tests.test_support import use_private_cpu_fixture_lock


class SpeechVerificationSettingsAdmissionTests(unittest.TestCase):
    setUp = windows.SynthesisWindowSettingsAdmissionTests.setUp

    def store(self, enabled):
        Path(self.pm.config_path).write_text(json.dumps({'tts': {'speech_verification': {'enabled': enabled}}}))
        self.pm.invalidate_config_cache()

    def save(self, policy):
        profile = dict(base_url='http://127.0.0.1:1234/v1',api_key='fixture',model_name='fixture')
        return self.client.post('/api/config',json={'llm':profile,'llm_local':profile,'llm_mode':'local',
                                                  'tts':{'speech_verification':policy}})

    @use_private_cpu_fixture_lock()
    def test_live_task_blocks_policy_change_and_actual_engine_drift_without_mutation(self):
        for before, after, active in [(False,True,None),(True,False,{'enabled':True}),(True,True,None)]:
            with self.subTest(before=before,after=after,active=active):
                self.store(before)
                engine = SimpleNamespace(_speech_verification=active,close_external=Mock())
                self.pm.engine = engine
                config=Path(self.pm.config_path).read_bytes(); rows=self.pm.get_saved_chunks()
                claim=core.reserve_background_task('audio',cpu_only=True)
                try:
                    with patch.object(self.pm,'reset_engine') as reset:
                        response=self.save({'enabled':after})
                    self.assertEqual(409,response.status_code,response.text)
                    reset.assert_not_called()
                    self.assertEqual(config,Path(self.pm.config_path).read_bytes())
                    self.assertEqual(rows,self.pm.get_saved_chunks())
                finally: core.release_gpu_task_claim('audio',claim)
                response=self.save({'enabled':after})
                self.assertEqual(200,response.status_code,response.text)
                self.assertIsNone(self.pm.engine)
                engine.close_external.assert_called_once()
                self.assertEqual(rows,self.pm.get_saved_chunks())

    def test_invalid_or_omitted_policy_never_silently_disables_saved_check(self):
        self.store(True)
        for policy in [{'enabled':1},{'enabled':'true'},{'unknown':True}]:
            with self.subTest(policy=policy):
                before=Path(self.pm.config_path).read_bytes()
                response=self.save(policy)
                self.assertEqual(422,response.status_code,response.text)
                self.assertEqual(before,Path(self.pm.config_path).read_bytes())
        profile=dict(base_url='http://127.0.0.1:1234/v1',api_key='fixture',model_name='fixture')
        response=self.client.post('/api/config',json={'llm':profile,'llm_local':profile,'llm_mode':'local',
                                                    'tts':{'language':'English'}})
        self.assertEqual(200,response.status_code,response.text)
        self.assertEqual({'enabled':True},json.loads(Path(self.pm.config_path).read_text())['tts']['speech_verification'])


if __name__ == '__main__':
    unittest.main()
