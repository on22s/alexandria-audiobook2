"""Digest-bound, descriptor-pinned download and mutation refusal."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from sound_design_download import open_sound_delivery


class SoundDeliveryDownloadTests(unittest.TestCase):
    def test_verified_blocks_close_and_changed_digest_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);raw=b'own synthetic output bytes'*5000
            path=root/'mixed_audiobook.mp3';path.write_bytes(raw)
            digest=hashlib.sha256(raw).hexdigest()
            stream,size,suffix=open_sound_delivery(root,'mp3',digest)
            try:
                self.assertEqual(len(raw),size);self.assertEqual('.mp3',suffix)
                self.assertEqual(raw,b''.join(iter(lambda:stream.read(65536),b'')))
            finally:stream.close()
            self.assertTrue(stream._stream.closed)
            with self.assertRaises(ValueError):open_sound_delivery(root,'mp3','0'*64)
            with self.assertRaises(ValueError):open_sound_delivery(root,'../source',digest)
            self.assertEqual(raw,path.read_bytes())

    def test_in_place_mutation_aborts_and_closes_pinned_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);raw=b'A'*131072;path=root/'mixed_audiobook.wav';path.write_bytes(raw)
            stream,_,_=open_sound_delivery(root,'wav',hashlib.sha256(raw).hexdigest())
            self.assertEqual(raw[:65536],stream.read(65536))
            path.write_bytes(b'B'*len(raw))
            with self.assertRaisesRegex(ValueError,'changed'):stream.read(65536)
            self.assertTrue(stream._stream.closed)

    def test_replaced_path_cannot_substitute_other_download_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);raw=b'approved bytes';path=root/'mixed_audiobook.m4b';path.write_bytes(raw)
            stream,_,_=open_sound_delivery(root,'m4b',hashlib.sha256(raw).hexdigest())
            replacement=root/'replacement';replacement.write_bytes(b'different bytes');replacement.replace(path)
            try:
                # Hosts may update the unlinked inode's change time. Either
                # refuse that mutation or finish the already pinned old bytes.
                try:self.assertEqual(raw,stream.read(65536))
                except ValueError:pass
            finally:stream.close()
            self.assertEqual(b'different bytes',path.read_bytes())

    def test_response_disconnect_and_cancellation_close_actual_pinned_descriptor(self):
        import asyncio
        from audio_stream_response import AudioStreamResponse
        async def exercise(root, fault):
            raw=b'own stream fixture'*10000;path=root/'mixed_audiobook.mp3';path.write_bytes(raw)
            stream,size,suffix=open_sound_delivery(root,'mp3',hashlib.sha256(raw).hexdigest())
            response=AudioStreamResponse(stream,size,suffix)
            entered=asyncio.Event()
            async def send(message):
                if message['type']=='http.response.body' and message.get('body'):
                    entered.set()
                    if fault=='disconnect':raise OSError('Synthetic browser disconnect')
                    await asyncio.Event().wait()
            async def receive():raise AssertionError('Unexpected legacy ASGI polling')
            task=asyncio.create_task(response({'type':'http','method':'GET','headers':[],
                'asgi':{'spec_version':'2.4'}},receive,send))
            try:
                await asyncio.wait_for(entered.wait(),3)
                if fault=='cancel':task.cancel()
                result=await asyncio.gather(task,return_exceptions=True)
                self.assertIsInstance(result[0],BaseException)
                self.assertTrue(stream._stream.closed)
            finally:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                stream.close()
        with tempfile.TemporaryDirectory() as tmp:
            for fault in ('disconnect','cancel'):
                with self.subTest(fault=fault):asyncio.run(exercise(Path(tmp),fault))
