"""Native worker fidelity and pre-write cap refusal on disposable CPU tones."""
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from audio_encoder_identity import get_ffmpeg_runtime_identity
from audio_mastering_filter import get_mastering_filter
from json_metadata import get_metadata_json_bytes
from media_process import MediaProcessError, run_owned_media_check, get_media_process_environment
from pcm_mux_profile import get_pcm_mux_profile
from pcm_mux_worker_request import get_pcm_mux_worker_request, MAX_REQUEST_BYTES
from pcm_worker_contract import PCMInputShape, get_pcm_encode_timeout
from project_scope_files import capture_scope_file


def request(root, directory, *, frames=48001, rate=24000, width=2, channels=1,
            fmt='mp3', limit=1024**2):
    raw=root/'input.pcm'
    facts,_=capture_scope_file(root,raw.name)
    return dict(schema_version=1,format=fmt,frames=frames,frame_rate=rate,
        sample_width=width,channels=channels,output_rate=rate,output_channels=channels,
        raw_name=raw.name,raw=dict(version=facts['file_version'],sha256=facts['sha256']),
        encoder=get_ffmpeg_runtime_identity().model_dump(),output_limit=limit,
        mux_profile=get_pcm_mux_profile(),mastering=None,measured=None)


def run(arguments):
    return subprocess.run(arguments,check=True,capture_output=True,timeout=30,
        env=get_media_process_environment())


class PCMMuxWorkerTests(unittest.TestCase):
    def test_request_refuses_untrusted_shape_and_settings_before_capture_or_spawn(self):
        import pcm_mux_worker
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'input.pcm').write_bytes(b'\0'*96002)
            directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=root)).resolve()
            row=request(root,directory)
            for change in (dict(frames=True),dict(frames='48001'),dict(frame_rate=0),
                    dict(frame_rate=1,frames=96000),dict(raw_name='../escape'),
                    dict(format='m4b'),dict(output_rate=192000),dict(output_limit=True),dict(mux_profile={}),
                    dict(unknown=1),dict(mastering={}),dict(measured={'input_i':-24}),
                    dict(mastering={'loudness':True},measured={}),dict(bitrate='192k -y')):
                with self.subTest(change=change):
                    (directory/'request.json').write_text(json.dumps({**row,**change}))
                    original=pcm_mux_worker.capture_scope_file
                    def only_request(base,name,**kwargs):
                        if name != 'request.json':raise AssertionError('PCM read before validation')
                        return original(base,name,**kwargs)
                    with patch.object(pcm_mux_worker,'capture_scope_file',side_effect=only_request), \
                            patch.object(pcm_mux_worker,'require_ffmpeg_binary_identity') as binary, \
                            patch.object(pcm_mux_worker.subprocess,'Popen') as spawn:
                        with self.assertRaises(ValueError):pcm_mux_worker.run_pcm_mux_worker(directory)
                        binary.assert_not_called();spawn.assert_not_called()
            for raw in (b'{"format":"mp3","format":"wav"}',b'[]',b'{"x":NaN}',
                        b' '*(MAX_REQUEST_BYTES+1),b'\xff'):
                with self.subTest(raw=raw[:40]),self.assertRaises(ValueError):
                    get_pcm_mux_worker_request(raw)
            shape={key:row[key] for key in PCMInputShape.model_fields}
            self.assertEqual(600,get_pcm_encode_timeout(shape))
            self.assertEqual(172830,get_pcm_encode_timeout({**shape,'frame_rate':1,'frames':86400}))

    def test_native_packets_full_pcm_and_gapless_metadata_match_for_raw_and_mastered(self):
        import av
        import io
        from bounded_media_io import ForwardOnlyMediaInput
        from audio_mastering import get_loudnorm_record

        # Model the observed Windows pipe: advertising seekability does not
        # make a demuxer's later seek valid. Both native transports must work
        # with only forward reads, while lifetime stays with the worker.
        class MisleadingPipe(io.BytesIO):
            def seekable(self):return True
            def seek(self,*args):raise OSError(22,'Invalid argument')
            def tell(self):return 0
        for transport in ('nut','mp4'):
            args=['ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=24000:cl=mono',
                '-t','0.2','-c:a','libmp3lame' if transport=='nut' else 'aac']
            args += (['-syncpoints','none','-strict','experimental'] if transport=='nut' else
                     ['-movflags','frag_keyframe+delay_moov+default_base_moof'])
            raw=run([*args,'-f',transport,'pipe:1']).stdout
            with self.subTest(transport=transport):
                with self.assertRaises(OSError):
                    with av.open(MisleadingPipe(raw),format=transport) as container:
                        list(container.demux(audio=0))
                pipe=MisleadingPipe(raw);forward=ForwardOnlyMediaInput(pipe)
                for name in ('seek','tell','close','fileno','write'):
                    self.assertFalse(hasattr(forward,name))
                with av.open(forward,format=transport) as container:
                    self.assertGreater(sum(packet.size for packet in container.demux(audio=0)),0)
                self.assertFalse(pipe.closed)
                pipe.close()
        for rate,frames,width,channels in ((24000,72001,4,2),(44100,132301,2,1),(48000,144000,1,2),
                                          (44100,1,2,1)):
            for fmt in ('mp3','wav'):
                for mastering in (None,{}) if frames>1 else (None,):
                    with self.subTest(rate=rate,frames=frames,width=width,channels=channels,
                            fmt=fmt,mastering=mastering),tempfile.TemporaryDirectory() as tmp:
                        root=Path(tmp).resolve();raw=root/'input.pcm'
                        amplitudes={1:12,2:3500,4:229376000}
                        raw.write_bytes(b''.join(struct.pack({1:'b',2:'<h',4:'<i'}[width],
                            round(amplitudes[width]*math.sin(2*math.pi*(440+c*220)*i/rate)))
                            for i in range(frames) for c in range(channels)))
                        directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=root)).resolve()
                        row=request(root,directory,frames=frames,rate=rate,width=width,channels=channels,fmt=fmt)
                        base=[row['encoder']['binary']['path'],'-nostdin','-hide_banner','-nostats','-xerror',
                            '-threads','1','-filter_threads','1']
                        args=[*base,'-f',{1:'s8',2:'s16le',4:'s32le'}[width],'-ar',str(rate),
                            '-ac',str(channels),'-i',str(raw),'-map','0:a:0']
                        if mastering is not None:
                            before=get_loudnorm_record(run([*args,'-af',get_mastering_filter(mastering),
                                '-f','null','-']).stderr.decode())
                            row.update(mastering=mastering,measured={key:float(before[key]) for key in (
                                'input_i','input_tp','input_lra','input_thresh','target_offset')})
                            args+=['-af',get_mastering_filter(mastering,row['measured'])]
                        codec='libmp3lame' if fmt=='mp3' else {1:'pcm_u8',2:'pcm_s16le',4:'pcm_s32le'}[width]
                        args+=['-ar',str(rate),'-ac',str(channels),'-c:a',codec]
                        args+=['-b:a','192k','-id3v2_version','4'] if fmt=='mp3' else ['-rf64','auto']
                        reference=root/('reference.'+fmt);run([*args,'-f',fmt,str(reference)])
                        (directory/'request.json').write_bytes(get_metadata_json_bytes(row,MAX_REQUEST_BYTES,'PCM request'))
                        tail=[]
                        run_owned_media_check([sys.executable,str(Path(__file__).resolve().parents[1]/
                            'pcm_mux_worker.py'),str(directory)],lambda:None,get_pcm_encode_timeout(
                                {key:row[key] for key in PCMInputShape.model_fields}),consume_line=tail.append)
                        actual=directory/('output.'+fmt)
                        def pcm(path,seek=None):
                            return run([*base,*(['-ss',format(seek,'.9f')] if seek is not None else []),
                                '-i',str(path),*(['-t','0.05'] if seek is not None else []),'-f','s32le','-']).stdout
                        self.assertEqual(pcm(reference),pcm(actual))
                        if frames>1:
                            for seek in (.01,frames/rate/2,frames/rate-.06):
                                self.assertEqual(pcm(reference,seek),pcm(actual,seek))
                        def packets(path):
                            with av.open(str(path)) as container:
                                return [hashlib.sha256(bytes(p)).hexdigest() for p in container.demux(audio=0) if p.size]
                        self.assertEqual(packets(reference),packets(actual))
                        if fmt=='wav' and width==1 and mastering is None:
                            import wave
                            with wave.open(str(actual),'rb') as decoded:
                                self.assertEqual(bytes((value+128)%256 for value in raw.read_bytes()),decoded.readframes(frames))
                        if fmt=='mp3':
                            def gapless(path):
                                data=json.loads(run(['ffprobe','-v','error','-show_packets','-select_streams','a',
                                    '-show_entries','packet=side_data_list','-of','json',str(path)]).stdout)
                                return [row['side_data_list'] for row in data['packets'] if 'side_data_list' in row]
                            self.assertEqual(gapless(reference),gapless(actual))
                            self.assertTrue(b'Info' in actual.read_bytes()[:2048] or b'Xing' in actual.read_bytes()[:2048])
                        if mastering is not None:
                            self.assertIn(get_loudnorm_record('\n'.join(tail))['normalization_type'],('linear','dynamic'))
                        self.assertLessEqual(actual.stat().st_size,row['output_limit'])
                        self.assertEqual({'request.json','output.'+fmt},{p.name for p in directory.iterdir()})

    def test_output_guard_refuses_before_allowance_is_crossed(self):
        for fmt in ('mp3','wav'):
            with self.subTest(format=fmt),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve();raw=root/'input.pcm';raw.write_bytes(b'\0'*96002)
                directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=root)).resolve()
                limit=1024 if fmt=='mp3' else 96002+44
                row=request(root,directory,fmt=fmt,limit=limit)
                (directory/'request.json').write_text(json.dumps(row))
                with self.assertRaises(MediaProcessError):
                    run_owned_media_check([sys.executable,str(Path(__file__).resolve().parents[1]/
                        'pcm_mux_worker.py'),str(directory)],lambda:None,30,output_limit=65536)
                self.assertLessEqual((directory/('output.'+fmt)).stat().st_size,limit)
                self.assertEqual(b'\0'*96002,raw.read_bytes())

    def test_diagnostic_reader_failure_stops_real_native_child_without_hanging(self):
        import pcm_mux_worker
        import time
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'input.pcm').write_bytes(b'\0'*96002)
            directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=root)).resolve()
            row=request(root,directory);(directory/'request.json').write_text(json.dumps(row))
            spawn=pcm_mux_worker.subprocess.Popen;children=[]
            class BrokenReader:
                def __init__(self,stream):self.stream=stream
                def read(self,size):raise OSError('injected diagnostic read failure')
                def close(self):self.stream.close()
            def fail_reader(*args,**kwargs):
                process=spawn(*args,**kwargs);children.append(process)
                process.stderr=BrokenReader(process.stderr)
                return process
            started=time.monotonic()
            with patch.object(pcm_mux_worker.subprocess,'Popen',side_effect=fail_reader):
                with self.assertRaises(Exception):pcm_mux_worker.run_pcm_mux_worker(directory)
            self.assertLess(time.monotonic()-started,5)
            self.assertEqual(1,len(children));self.assertIsNotNone(children[0].poll())
            self.assertNotEqual(0,children[0].returncode)
            self.assertTrue(children[0].stdout.closed)
