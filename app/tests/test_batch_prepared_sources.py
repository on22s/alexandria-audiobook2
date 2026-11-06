"""Native source files through batch start/background sizing; no generation subprocess."""
import asyncio
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
        # One FULL text/decode capture per source (include_bytes=True
        # through the confined seam); digest-only revalidations
        # (include_bytes=False) are the separate cache checks and are
        # counted separately — this is not claimed as one physical read.
        import script_selection_binding
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); uploads = root/'uploads'; uploads.mkdir(); scripts = root/'scripts'; scripts.mkdir()
            sources = [uploads/'first.txt', uploads/'second.txt']
            for path in sources:
                path.write_text('Narration. "A spoken line." More narration.', encoding='utf-8')
            request = script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename=p.name) for p in sources])
            before = request.model_dump()
            full_captures = []; digest_captures = []
            actual_capture = script_selection_binding._capture_raw_source
            def observed_capture(path, **kwargs):
                bucket = full_captures if kwargs.get('include_bytes') else digest_captures
                bucket.append(str(path))
                return actual_capture(path, **kwargs)
            stack.enter_context(patch.object(script,'UPLOADS_DIR',str(uploads)))
            stack.enter_context(patch.object(script,'SCRIPTS_DIR',str(scripts)))
            stack.enter_context(patch.object(script,'ensure_book_state',side_effect=lambda *args:nullcontext()))
            stack.enter_context(patch.object(script,'load_app_config',return_value={}))
            stack.enter_context(patch('lmstudio_settings.get_planned_ideal_settings', return_value={'parallel':2,'context_length':32768}))
            stack.enter_context(patch.object(script,'ensure_ideal_settings',return_value=(True,{},'fixture')))
            stack.enter_context(patch.object(script,'_init_task_log',return_value=str(root/'task.log')))
            worker = stack.enter_context(patch.object(script,'_run_batch_script_job'))
            stack.enter_context(patch.object(script_selection_binding,'_capture_raw_source',side_effect=observed_capture))
            async def run():
                background = BackgroundTasks()
                result = await script.generate_script_batch_start(request,background)
                self.assertEqual('started',result['status'])
                await background()
            asyncio.run(run())
            self.assertCountEqual([str(p) for p in sources], full_captures)
            self.assertTrue(all(str(p) in digest_captures for p in sources),
                            'each source must also be digest-revalidated')
            self.assertEqual(2,worker.call_count)
            self.assertEqual(before,request.model_dump())
            for call in worker.call_args_list:
                job = call.args[0]
                self.assertEqual(sources[job['index']].read_text(),job['prepared_source']['text'])

    def test_busy_batch_refuses_before_source_preparation(self):
        from fastapi import HTTPException
        request = script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
        background = BackgroundTasks()
        core.process_state['batch_script']['running'] = True
        with patch.object(script, 'get_prepared_batch_script_jobs') as prepare:
            with self.assertRaises(HTTPException) as error:
                asyncio.run(script.generate_script_batch_start(request, background))
        self.assertEqual(400, error.exception.status_code)
        self.assertIn('already running', error.exception.detail)
        prepare.assert_not_called()
        self.assertEqual([], background.tasks)

    def test_atomic_claim_rechecks_busy_state_after_preparation(self):
        from fastapi import HTTPException
        request = script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
        background = BackgroundTasks()
        def became_busy(request):
            core.process_state['batch_script']['running'] = True
            return []
        with patch.object(script, 'get_prepared_batch_script_jobs', side_effect=became_busy):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(script.generate_script_batch_start(request, background))
        self.assertEqual(400, error.exception.status_code)
        self.assertIn('already running', error.exception.detail)
        self.assertEqual([], background.tasks)
        self.assertEqual({}, core._task_claims)

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

    def test_cache_hit_reuses_decoded_text_without_rereading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt';path.write_text('Narration. "A spoken line." More narration.', encoding='utf-8')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}):
                jobs=script.get_prepared_batch_script_jobs(request)
                with patch.object(script,'_capture_and_validate_batch_script_source',side_effect=AssertionError('cache hit must not re-read')) as reread:
                    self.assertEqual((jobs[0]['prepared_source']['text'],jobs[0]['prepared_source']['normalizations']),
                                     script.get_validated_batch_script_source(jobs[0]))
                    reread.assert_not_called()

    def test_hardlinked_and_missing_sources_are_refused_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt';path.write_text('Narration. "A spoken line." More narration.', encoding='utf-8')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}):
                jobs=script.get_prepared_batch_script_jobs(request)
                os.link(path, Path(tmp)/'alias.txt')
                with self.assertRaises(ValueError):
                    script.get_validated_batch_script_source(jobs[0])
                path.unlink();(Path(tmp)/'alias.txt').unlink()
                with self.assertRaises(FileNotFoundError):
                    script.get_validated_batch_script_source(jobs[0])

    def test_source_changed_during_preparation_is_refused_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt';path.write_text('Original source')
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            actual=script._capture_and_validate_batch_script_source
            def changed(job):
                result=actual(job)
                path.write_text('Changed source!')
                return result
            from fastapi import HTTPException
            with patch.object(script,'UPLOADS_DIR',tmp), \
                 patch.object(script,'load_app_config',return_value={}), \
                 patch.object(script,'_capture_and_validate_batch_script_source',side_effect=changed), \
                 patch.object(script,'three_pass_refusal') as refusal:
                with self.assertRaises(HTTPException) as error:
                    script.get_prepared_batch_script_jobs(request)
                self.assertEqual(400,error.exception.status_code)
                self.assertIn('Source changed while preparing',error.exception.detail)
                refusal.assert_not_called()

    def test_cp1252_and_utf8_decode_like_generation_preparation(self):
        import hashlib
        cases = [
            ('Alexis Alexis Alexis. Café — “quiet.”', 'utf-8'),
            ('Alexis Alexis Alexis. Café — “quiet.”', 'cp1252'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt'
            for text, encoding in cases:
                with self.subTest(encoding=encoding):
                    raw=text.encode(encoding); path.write_bytes(raw)
                    request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
                    with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}):
                        jobs=script.get_prepared_batch_script_jobs(request)
                    # Same decoded text/policy as actual get_prepared_source.
                    from three_pass_generate import get_prepared_source
                    prepared, _ = get_prepared_source(str(path))
                    self.assertEqual(prepared, jobs[0]['prepared_source']['text'])
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), jobs[0]['prepared_source']['identity'][0])
                    self.assertEqual(raw, path.read_bytes())

    def test_crlf_cr_newlines_normalize_like_textio_under_raw_identity(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt'
            raw=b'Alexis Alexis Alexis.\r\nSecond line.\rThird line.\nFourth.'
            path.write_bytes(raw)
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}):
                jobs=script.get_prepared_batch_script_jobs(request)
            expected='Alexis Alexis Alexis.\nSecond line.\nThird line.\nFourth.'
            self.assertEqual(expected,jobs[0]['prepared_source']['text'])
            self.assertEqual(expected,script.get_validated_batch_script_source(jobs[0])[0])
            # Identity is the original raw-byte SHA, source unchanged.
            self.assertEqual(hashlib.sha256(raw).hexdigest(),jobs[0]['prepared_source']['identity'][0])
            self.assertEqual(raw,path.read_bytes())

    def test_aba_revert_during_preparation_never_binds_changed_text_under_original_identity(self):
        # Controlled A/B/A (root probe shape): captured B text must not
        # be stored under A's identity when the file reverts to A.
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'book.txt';original='Original source';other='Changed source!'
            self.assertEqual(len(original),len(other))
            path.write_text(original,encoding='utf-8');before=path.stat()
            actual=script._capture_and_validate_batch_script_source
            def changed_then_reverted(job):
                path.write_text(other,encoding='utf-8')
                try:
                    return actual(job)
                finally:
                    path.write_text(original,encoding='utf-8')
                    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            from fastapi import HTTPException
            request=script.BatchScriptRequest(tasks=[script.BatchScriptTask(filename='book.txt')])
            with patch.object(script,'UPLOADS_DIR',tmp),patch.object(script,'load_app_config',return_value={}),patch.object(script,'_capture_and_validate_batch_script_source',side_effect=changed_then_reverted):
                with self.assertRaises(HTTPException) as error:
                    script.get_prepared_batch_script_jobs(request)
            self.assertEqual(400,error.exception.status_code)
            self.assertIn('Source changed while preparing',error.exception.detail)
            self.assertEqual(original,path.read_text())
