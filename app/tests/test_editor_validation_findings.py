"""Actual route and storage behavior adjudicate conditional editor findings."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from project import ProjectManager
from routers import editor


class EditorValidationFindingTests(unittest.TestCase):
    def test_negative_pause_request_cannot_persist_negative_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager=ProjectManager(tmp)
            manager.save_chunks([{'text':'line','speaker':'ALICE','status':'done','audio_path':'voicelines/fixture.wav'}])
            manager.load_chunks()
            before=json.loads(Path(manager.chunks_path).read_text())[0]
            app=FastAPI();app.include_router(editor.router)
            results=[]
            async def probe():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                    for requested,expected in [(-5,0),(0,0),(12,12),(None,None)]:
                        response=await client.post('/api/chunks/0',json={'pause_after':requested})
                        self.assertEqual(200,response.status_code,response.text)
                        stored=json.loads(Path(manager.chunks_path).read_text())[0]
                        self.assertEqual(expected,stored.get('pause_after'))
                        self.assertEqual(stored,response.json())
                        for field in ('uid','text','speaker','status','audio_path'):
                            self.assertEqual(before[field],stored[field])
                        results.append({'requested':requested,'status':response.status_code,'stored_pause':stored.get('pause_after')})
            with patch.object(editor,'project_manager',manager):
                asyncio.run(probe())
            self.pause_results=results

    def test_empty_persisted_row_gets_uid_and_success_missing_index_alone_is_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager=ProjectManager(tmp)
            manager.save_chunks([{}])
            app=FastAPI();app.include_router(editor.router)
            results=[]
            async def probe():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
                    response=await client.post('/api/chunks/0',json={})
                    self.assertEqual(200,response.status_code,response.text)
                    stored=json.loads(Path(manager.chunks_path).read_text())[0]
                    self.assertEqual(stored,response.json())
                    self.assertTrue(stored['uid'])
                    self.assertEqual({'uid'},set(stored))
                    before=Path(manager.chunks_path).read_bytes()
                    results.append({'index':0,'status':response.status_code,'stored_keys':sorted(stored)})
                    for index in (-1,1):
                        missing=await client.post(f'/api/chunks/{index}',json={})
                        self.assertEqual(404,missing.status_code)
                        self.assertEqual(before,Path(manager.chunks_path).read_bytes())
                        results.append({'index':index,'status':missing.status_code})
            with patch.object(editor,'project_manager',manager):
                asyncio.run(probe())
            self.empty_row_results=results
