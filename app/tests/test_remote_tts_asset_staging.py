"""Execute remote staging commands locally against isolated filesystem attacks."""
import copy
import hashlib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import benchmark_runner as runner


class RemoteTTSAssetStagingTests(unittest.TestCase):
    def test_native_mktemp_is_private_unique_and_ignores_a_precreated_fixed_directory_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);remote=root/'remote';remote.mkdir();victim=root/'victim';victim.mkdir()
            (remote/'alexandria-tts-benchmark-assets').symlink_to(victim,target_is_directory=True)
            source=root/'ref.wav';source.write_bytes(b'known reference');digest=hashlib.sha256(source.read_bytes()).hexdigest()
            target=victim/f'{digest}.wav';target.write_bytes(b'unrelated existing data')
            payload={'fixtures':[{'voice_type':'clone','ref_audio':'ref.wav','ref_audio_sha256':digest} for _ in range(2)]};before=copy.deepcopy(payload)
            real_run=subprocess.run;transfers=[]
            def local_transport(argv,**kwargs):
                if argv[0]=='ssh':
                    command=' '.join(argv[2:])
                    command=command.replace('/tmp/',str(remote)+'/')
                    result=real_run(['bash','-c',command],**kwargs)
                    result.stdout='Decorative SSH banner\n'+result.stdout.replace(str(remote)+'/','/tmp/')
                    return result
                self.assertEqual('scp',argv[0]);destination=argv[2].split(':',1)[1]
                native=remote/destination.removeprefix('/tmp/')
                shutil.copy2(argv[1],native);transfers.append(native)
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            with patch.object(runner.subprocess,'run',side_effect=local_transport):
                first=runner._stage_remote_tts_assets(payload,str(root),'fixture-only')
                second=runner._stage_remote_tts_assets(payload,str(root),'fixture-only')
            self.assertEqual(b'unrelated existing data',target.read_bytes())
            self.assertEqual(before,payload);self.assertEqual(2,len(transfers))
            self.assertNotEqual(first['fixtures'][0]['ref_audio'],second['fixtures'][0]['ref_audio'])
            for destination in transfers:
                self.assertEqual(source.read_bytes(),destination.read_bytes())
                self.assertFalse(destination.parent.is_symlink())
                self.assertEqual(os.getuid(),destination.parent.stat().st_uid)
                self.assertEqual(0o700,stat.S_IMODE(destination.parent.stat().st_mode))

    def test_bad_or_missing_remote_directory_output_refuses_before_any_scp(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'ref.wav';source.write_bytes(b'audio')
            payload={'fixtures':[{'voice_type':'clone','ref_audio':'ref.wav','ref_audio_sha256':hashlib.sha256(b'audio').hexdigest()}]}
            for stdout in ('','banner only\n','/tmp/alexandria-tts-benchmark-assets\n','/tmp/alexandria-tts-benchmark-assets.abcdefghij/../escape\n','/tmp/alexandria-tts-benchmark-assets.abcdefghij; touch bad\n'):
                with self.subTest(stdout=stdout),patch.object(runner.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=stdout,stderr='')) as run:
                    with self.assertRaisesRegex(RuntimeError,'remote asset directory'):
                        runner._stage_remote_tts_assets(payload,tmp,'fixture-only')
                    self.assertEqual(1,run.call_count)

    def test_creation_failure_and_no_asset_payload_do_not_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'ref.wav';source.write_bytes(b'audio')
            payload={'fixtures':[{'voice_type':'clone','ref_audio':'ref.wav',
                'ref_audio_sha256':hashlib.sha256(b'audio').hexdigest()}]}
            with patch.object(runner.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='fixture permission denied')) as run:
                self.assertEqual({'fixtures':[{'voice_type':'custom'}]},runner._stage_remote_tts_assets({'fixtures':[{'voice_type':'custom'}]},tmp,'fixture-only'))
                run.assert_not_called()
                with self.assertRaisesRegex(RuntimeError,'fixture permission denied'):
                    runner._stage_remote_tts_assets(payload,tmp,'fixture-only')
                self.assertEqual(1,run.call_count)
