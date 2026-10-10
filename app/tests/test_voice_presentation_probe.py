"""Known accepted/rejected cases calibrate the voice-presentation instrument."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import tempfile
import contextlib
import io
from experiments.voice_presentation_probe import get_probe_verdict


class VoicePresentationProbeTests(unittest.TestCase):
    def test_rejects_identity_only_wrong_speaker_and_invented_evidence(self):
        cases=json.loads((Path(__file__).parent.parent/'fixtures/voice_presentation_733.json').read_text())
        for case in cases:
            with self.subTest(case=case['id']):
                if case['presentation']=='unknown':
                    self.assertTrue(get_probe_verdict(case, {'voice_presentation':'unknown','evidence':''}))
                    self.assertFalse(get_probe_verdict(case, {'voice_presentation':'masculine','evidence':case['text']}))
                else:
                    phrase='masculine voice' if case['presentation']=='masculine' else 'womanish voice'
                    self.assertTrue(get_probe_verdict(case, {'voice_presentation':case['presentation'],'evidence':phrase}))
                    self.assertFalse(get_probe_verdict(case, {'voice_presentation':case['presentation'],'evidence':'invented vocal evidence'}))
                    self.assertFalse(get_probe_verdict(case, {'voice_presentation':'unknown','evidence':''}))
                self.assertFalse(get_probe_verdict(case, None))
        self.assertFalse(get_probe_verdict(cases[0], {'voice_presentation':'masculine','evidence':'It is so unfeminine,'}))

    def test_real_probe_publishes_every_verified_response(self):
        from experiments.voice_presentation_probe import main
        cases=json.loads((Path(__file__).parent.parent/'fixtures/voice_presentation_733.json').read_text())
        answers=[]
        for case in cases*2:
            value=case['presentation']
            evidence='' if value=='unknown' else ('masculine voice' if value=='masculine' else 'womanish voice')
            answers.append(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({'voice_presentation':value,'evidence':evidence})),finish_reason='stop')]))
        from unittest.mock import Mock
        client=Mock();client.models.list.return_value=SimpleNamespace(data=[SimpleNamespace(id='fixture')])
        client.chat.completions.create.side_effect=answers
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'results.json'
            with patch('sys.argv',['probe','--out',str(out)]), patch('openai.OpenAI',return_value=client), patch('experiments.gpu_guard.require_free_gpu'), contextlib.redirect_stdout(io.StringIO()):
                main()
            artifact=json.loads(out.read_text())
            self.assertEqual(12,len(artifact['results']))
            self.assertTrue(all(row['passed'] for row in artifact['results']))
            self.assertEqual('fixture',artifact['model'])

    def test_child_probe_publishes_four_real_waveform_pairs_without_catalog_writes(self):
        from experiments.child_voice_probe import main
        import numpy as np
        import soundfile as sf
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);config=root/'config.json';config.write_text('{}')
            out=root/'candidates';engine=Mock();index=iter(range(4))
            def design(*args,**kwargs):
                p=root/f'reference_{next(index)}.wav';sf.write(p,np.sin(np.arange(2400)*0.1)*.2,24000);return str(p),24000
            def render(*args):
                sf.write(args[-1],np.sin(np.arange(2400)*0.1)*.2,24000)
            engine.generate_voice_design.side_effect=design
            with patch('sys.argv',['probe','--config',str(config),'--out',str(out)]), patch('tts.TTSEngine',return_value=engine), patch('experiments.generation.render',side_effect=render), patch('experiments.gpu_guard.require_free_gpu'), patch.dict('os.environ',{},clear=False), contextlib.redirect_stdout(io.StringIO()):
                main()
            artifact=json.loads((out/'candidates.json').read_text())
            self.assertEqual(4,len(artifact['candidates']))
            for row in artifact['candidates']:
                self.assertEqual('pending',row['listening_verdict'])
                self.assertEqual(2,len(row['clips']))
                self.assertTrue(all(Path(clip['path']).is_file() and clip['sha256'] for clip in row['clips']))
            self.assertFalse((root/'voice_library.json').exists())
