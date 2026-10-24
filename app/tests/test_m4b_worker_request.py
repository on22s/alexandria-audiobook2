"""Unsafe worker requests must fail before media IO or process creation."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from m4b_mux_profile import get_m4b_mux_profile
from m4b_worker_request import get_m4b_worker_request, MAX_REQUEST_BYTES, MAX_PCM_BYTES


def request():
    return dict(encoder=dict(binary=dict(path='/unexecuted-fixture',version=[0,0,0,0,0],sha256='c'*64),version_sha256='d'*64),schema_version=1,raw_name='input.pcm',
        raw=dict(version=[1,2,96000,4,5],sha256='a'*64),
        metadata=dict(version=[1,3,42,4,5],sha256='b'*64),
        frames=24000,frame_rate=24000,sample_width=2,channels=2,
        output_rate=24000,output_channels=2,output_limit=1024,
        mux_profile=get_m4b_mux_profile())


def parse(value):
    return get_m4b_worker_request(json.dumps(value).encode())


class M4BWorkerRequestTests(unittest.TestCase):
    def test_valid_geometry_and_finite_mastering(self):
        row=request()
        self.assertEqual(24000,parse(row).frames)
        row.update(mastering={},measured=dict(input_i=-24,input_tp=-10,input_lra=0,
            input_thresh=-34,target_offset=6))
        self.assertEqual(-24,parse(row).measured['input_i'])

    def test_actual_duration_boundary_refuses_before_worker_capture(self):
        from m4b_worker_request import M4BInputShape
        import m4b_mux_worker
        for rate in (1, 24000):
            row=request()
            row.update(frame_rate=rate,frames=rate*86400,sample_width=1,channels=1)
            from audio_encoding_layout import get_audio_encoding_layout
            row['output_rate']=get_audio_encoding_layout('m4b',rate,1)['frame_rate']
            row['output_channels']=1
            row['raw']['version'][2]=row['frames']
            self.assertEqual(row['frames'],parse(row).frames)
            shape={key:row[key] for key in ('raw_name','frames','frame_rate',
                'sample_width','channels','output_rate','output_channels')}
            self.assertEqual(row['frames'],M4BInputShape.model_validate(shape).frames)
            for frames in (row['frames']+1, max(row['frames']+1,96000)):
                bad=copy.deepcopy(row);bad['frames']=frames;bad['raw']['version'][2]=frames
                with self.subTest(rate=rate,frames=frames),tempfile.TemporaryDirectory() as tmp:
                    directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=tmp)).resolve()
                    (directory/'request.json').write_text(json.dumps(bad))
                    with self.assertRaises(ValueError):
                        M4BInputShape.model_validate({**shape,'frames':frames})
                    with patch.object(m4b_mux_worker,'_capture') as capture, \
                            patch.object(m4b_mux_worker.subprocess,'Popen') as spawn:
                        with self.assertRaisesRegex(ValueError,'PCM shape'):
                            m4b_mux_worker.run_m4b_mux_worker(directory)
                        capture.assert_not_called();spawn.assert_not_called()

    def test_ambiguous_json_size_nonfinite_and_nonobject_refuse(self):
        for raw in (b'',b' '*(MAX_REQUEST_BYTES+1),b'[]',b'null',
                b'{"schema_version":1,"schema_version":1}',b'{"x":NaN}',
                b'{"x":Infinity}',b'{"x":-Infinity}',b'\xff','{}'):
            with self.subTest(raw=repr(raw)[:80]),self.assertRaises((ValueError,TypeError)):
                get_m4b_worker_request(raw)

    def test_paths_coercions_limits_and_unknown_fields_refuse(self):
        cases=[('raw_name','../input.pcm'),('raw_name','/tmp/input.pcm'),
            ('raw_name','sub/input.pcm'),('raw_name','a'*129),
            ('schema_version',True),('frames',True),('frames','24000'),
            ('frames',0),('frame_rate',192001),('sample_width',3),('channels',3),
            ('output_limit',0),('output_limit',MAX_PCM_BYTES+1),
            ('output_rate',True),('output_rate',192000),('output_channels',1),('extra','ignored'),('mux_profile',{})]
        for key,value in cases:
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):
                parse({**request(),key:value})

    def test_identity_geometry_cover_pairs_and_profile_refuse(self):
        for field,key,value in [('raw','version',[1,2,95999,4,5]),
                ('raw','version',[True,2,96000,4,5]),('raw','version',[1,2,96000,4,5,6]),
                ('raw','sha256','Z'*64),('metadata','version',[1,3,8*1024**2+1,4,5]),
                ('raw','version',[1,2,MAX_PCM_BYTES+1,4,5]),('mux_profile','muxer','other')]:
            row=request();row[field][key]=value
            with self.subTest(field=field,key=key,value=value),self.assertRaises(ValueError):parse(row)
        for change in (dict(cover_name='cover.jpg'),dict(cover=copy.deepcopy(request()['metadata'])),
                dict(cover_name='input.pcm',cover=copy.deepcopy(request()['metadata'])),
                dict(cover_name='cover.jpg',cover=dict(version=[1,2,0,4,5],sha256='a'*64)),
                dict(cover_name='cover.jpg',cover=dict(version=[1,2,2**64-1,4,5],sha256='a'*64))):
            with self.subTest(change=change),self.assertRaises(ValueError):parse({**request(),**change})

    def test_mastering_observations_require_real_finite_numbers_and_exact_keys(self):
        measured=dict(input_i=-24,input_tp=-10,input_lra=0,input_thresh=-34,target_offset=6)
        for field in ('mastering','measured'):
            for value in (True,'-18',float('nan'),float('inf'),10**1000):
                row=request();row.update(mastering={},measured=dict(measured))
                row[field]['loudness' if field=='mastering' else 'input_i']=value
                with self.subTest(field=field,value=value),self.assertRaises(ValueError):parse(row)
        for observations in ({},{**measured,'unknown':0},{'input_i':-24}):
            with self.subTest(observations=observations),self.assertRaises(ValueError):
                parse({**request(),'mastering':{},'measured':observations})
        with self.assertRaises(ValueError):parse({**request(),'measured':measured})
        with self.assertRaises(ValueError):parse({**request(),'mastering':{}})

    def test_invalid_request_stops_before_source_capture_and_process_creation(self):
        import m4b_mux_worker
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=tmp)).resolve()
            (directory/'request.json').write_bytes(b'{"raw_name":"../escape"}')
            with patch.object(m4b_mux_worker,'_capture') as capture, \
                    patch.object(m4b_mux_worker.subprocess,'Popen') as spawn:
                with self.assertRaises(ValueError):m4b_mux_worker.run_m4b_mux_worker(directory)
                capture.assert_not_called();spawn.assert_not_called()

    def test_overlapping_chapters_refuse_before_native_encoder(self):
        import m4b_mux_worker
        from project_scope_files import capture_scope_file
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'input.pcm').write_bytes(b'\0'*96000)
            directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=root)).resolve()
            (directory/'chapters.txt').write_bytes(
                b';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=800\ntitle=One\n'
                b'[CHAPTER]\nTIMEBASE=1/1000\nSTART=500\nEND=1000\ntitle=Two\n')
            row=request()
            from audio_encoder_identity import get_ffmpeg_runtime_identity
            row['encoder']=get_ffmpeg_runtime_identity().model_dump()
            for field,base,name in [('raw',root,'input.pcm'),('metadata',directory,'chapters.txt')]:
                facts,_=capture_scope_file(base,name)
                row[field]=dict(version=facts['file_version'],sha256=facts['sha256'])
            (directory/'request.json').write_text(json.dumps(row))
            with patch.object(m4b_mux_worker.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(ValueError,'chapter clock'):
                    m4b_mux_worker.run_m4b_mux_worker(directory)
                spawn.assert_not_called()
            self.assertFalse((directory/'output.m4b').exists())

    def test_untrusted_encoder_path_refuses_before_media_capture_or_process_creation(self):
        import m4b_mux_worker
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tempfile.mkdtemp(prefix='.pcm-encode-',dir=tmp)).resolve()
            (directory/'request.json').write_text(json.dumps(request()))
            with patch.object(m4b_mux_worker,'_capture') as capture, \
                    patch.object(m4b_mux_worker.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(ValueError,'approved identity'):
                    m4b_mux_worker.run_m4b_mux_worker(directory)
                capture.assert_not_called();spawn.assert_not_called()
