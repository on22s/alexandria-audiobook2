"""Native source files through batch start/background sizing; no generation subprocess."""
import asyncio
import builtins
import copy
from contextlib import ExitStack, nullcontext
import importlib.util
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi import BackgroundTasks
from tests.test_batch_script_concurrency import OwnedScriptTestCase
from routers import script
import core

if os.environ.get('BATCH_PREPARED_SOURCE'):
    spec = importlib.util.spec_from_file_location('saved_batch_router', os.environ['BATCH_PREPARED_SOURCE'])
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)


class BatchPreparedSourcesTests(OwnedScriptTestCase):
    def setUp(self):
        super().setUp()
        context = patch.object(script,'process_state',core.process_state)
        context.start();self.addCleanup(context.stop)

    def test_actual_start_and_background_preflight_read_each_source_once(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); uploads = root/'uploads'; uploads.mkdir(); scripts = root/'scripts'; scripts.mkdir()
            sources = [uploads/'first.txt', uploads/'second.txt']
            for path in sources:
                path.write_text('Narration. "A spoken line." More narration.', encoding='utf-8')
            request = script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename=p.name) for p in sources])
            before = request.model_dump(); reads = []; actual_open = builtins.open
            def observed(file, *args, **kwargs):
                if str(file) in [str(p) for p in sources]:
                    reads.append(str(file))
                return actual_open(file,*args,**kwargs)
            stack.enter_context(patch.object(script,'UPLOADS_DIR',str(uploads)))
            stack.enter_context(patch.object(script,'SCRIPTS_DIR',str(scripts)))
            stack.enter_context(patch.object(script,'ensure_book_state',side_effect=lambda *args:nullcontext()))
            stack.enter_context(patch.object(script,'load_app_config',return_value={}))
            stack.enter_context(patch.object(script,'get_planned_ideal_settings',return_value={'parallel':2,'context_length':32768}))
            stack.enter_context(patch.object(script,'ensure_ideal_settings',return_value=(True,{},'fixture')))
            stack.enter_context(patch.object(script,'_init_task_log',return_value=str(root/'task.log')))
            worker = stack.enter_context(patch.object(script,'_run_batch_script_job'))
            stack.enter_context(patch.object(builtins,'open',side_effect=observed))
            async def run():
                background = BackgroundTasks()
                result = await script.generate_script_batch_start(request,background)
                self.assertEqual('started',result['status'])
                await background()
            asyncio.run(run())
            self.assertCountEqual([str(p) for p in sources],reads)
            self.assertEqual(2,worker.call_count)
            self.assertEqual(before,request.model_dump())
            for call in worker.call_args_list:
                job = call.args[0]
                self.assertEqual(sources[job['index']].read_text(),job['prepared_source']['text'])

    def test_changed_source_revalidates_narrator_even_if_size_and_mtime_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'book.txt';path.write_text('Alexis Alexis Alexis')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt',first_person_narrator='ALEXIS')])
            with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}):
                jobs=script.get_prepared_batch_script_jobs(request)
            before=copy.deepcopy(jobs);old=path.stat()
            path.write_text('Nobody Nobody Nobody');os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
            with self.assertRaisesRegex(ValueError,'at least three times'):
                script.get_validated_batch_script_source(jobs[0])
            self.assertEqual(before,jobs)

    def test_source_replacement_and_removal_cannot_reuse_cached_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt';path.write_text('First source')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}):
                jobs=script.get_prepared_batch_script_jobs(request)
            replacement=Path(tmp)/'replacement';replacement.write_text('Other source');os.replace(replacement,path)
            self.assertEqual(('Other source',[]),script.get_validated_batch_script_source(jobs[0]))
            path.unlink()
            with self.assertRaises(FileNotFoundError):script.get_validated_batch_script_source(jobs[0])

    def test_source_changed_during_preparation_is_refused_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt';path.write_text('Original source')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            actual=script._read_and_validate_batch_script_source
            def changed(job):
                result=actual(job)
                path.write_text('Changed source!')
                return result
            from fastapi import HTTPException
            with patch.object(script,'UPLOADS_DIR',tmp), \
                 patch.object(script,'load_app_config',return_value={}), \
                 patch.object(script,'_read_and_validate_batch_script_source',side_effect=changed), \
                 patch.object(script,'three_pass_refusal') as refusal:
                with self.assertRaises(HTTPException) as error:
                    script.get_prepared_batch_script_jobs(request)
                self.assertEqual(400,error.exception.status_code)
                self.assertIn('Source changed while preparing',error.exception.detail)
                refusal.assert_not_called()
