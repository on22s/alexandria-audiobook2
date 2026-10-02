"""Selected launcher model reaches the app's actual active-profile resolver."""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from lmstudio_settings import get_active_llm_config

ROOT=Path(__file__).resolve().parents[2]


class StartLlmModelProfileTests(unittest.TestCase):
    def descriptor(self):
        path=os.environ.get('START_LLM_BASELINE',str(ROOT/'start_llm.js'))
        result=subprocess.run(['node','-e','process.stdout.write(JSON.stringify(require(process.argv[1])))',path],
            capture_output=True,text=True,check=True)
        return json.loads(result.stdout)

    def test_selected_models_replace_stale_local_profile_and_mirror(self):
        descriptor=self.descriptor();steps=descriptor['run']
        shell=next(step for step in steps if step['method']=='shell.run')
        save=next(step for step in steps if step['method']=='json.set')
        self.assertGreater(steps.index(save),steps.index(shell))
        self.assertEqual(shell['when'],save['when'])
        self.assertIn('--model={{args.model}}',shell['params']['message']['_'])
        self.assertTrue(shell['params']['on'][0]['done'])
        for mode in ('local','remote'):
            for model in ('Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf','Qwen2.5-14B-Instruct-Q6_K.gguf','models/another model.gguf'):
                with self.subTest(mode=mode,model=model):
                    config={'llm_mode':mode,'llm':{'model_name':'old','base_url':'old-url'},
                            'llm_local':{'model_name':'old-local','base_url':'old-local-url','temperature':.7},
                            'llm_remote':{'model_name':'cloud','base_url':'https://remote/v1'},'other':{'retain':1}}
                    remote=copy.deepcopy(config['llm_remote'])
                    updates=save['params']['app/config.json']
                    for key,value in updates.items():
                        value={'{{args.model}}':model,'{{local.llm_url}}':'http://127.0.0.1:51842/v1'}.get(value,value)
                        parts=key.split('.');target=config
                        for part in parts[:-1]:target=target.setdefault(part,{})
                        target[parts[-1]]=value
                    active=get_active_llm_config(config)
                    self.assertEqual(model,active['model_name'])
                    self.assertEqual('http://127.0.0.1:51842/v1',active['base_url'])
                    self.assertEqual(model,config['llm']['model_name'])
                    self.assertEqual('local',config['llm_mode'])
                    self.assertEqual(remote,config['llm_remote'])
                    self.assertEqual(.7,config['llm_local']['temperature'])
                    self.assertEqual({'retain':1},config['other'])

    def test_prerequisite_failure_returns_before_server_and_config_write(self):
        steps=self.descriptor()['run']
        failure=next(step for step in steps if step['method']=='script.return')
        self.assertLess(steps.index(failure),next(i for i,s in enumerate(steps) if s['method']=='shell.run'))
        self.assertIn("!exists(args.model)",failure['when'])
        self.assertIn("!exists('app/env')",failure['when'])
        self.assertEqual(1,sum(s['method']=='json.set' for s in steps))
