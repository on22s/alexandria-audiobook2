"""Final native-review regressions: bounded reads, cancellation and late results."""
import hashlib
import json
import os
import types
from pathlib import Path
from unittest.mock import patch

import tests.test_readalong_qwen as fixtures
import tests.test_qwen_review_v2_repairs as review_helpers


class FinalUiRepairTests(fixtures._FixtureMixin):
    def _run_late_cancel(self, kind, *, reset=False):
        scenario = r"""
(async function() {
  _capabilityDeferred.resolve(null); await flush(); await flush();
  var timers=[], complete=false, gets=0, downloads=0, cancel=_deferred();
  var realTimeout=setTimeout;
  setTimeout=function(fn,delay) { if(delay>=1000) { timers.push(fn);return timers.length; } return realTimeout(fn,delay); };
  _els['readaloud-status']=_mkEl('readaloud-status');
  _els['readaloud-cancel-btn']=_mkEl('readaloud-cancel-btn');
  _els['readaloud-build-btn']=_mkEl('readaloud-build-btn');
  document.createElement=function(tag) { var el=_mkEl('',tag);el.click=function() { downloads+=1; };return el; };
  window.ensureReadalongIntegrityApproval=function() { return Promise.resolve('a'.repeat(64)); };
  API.post=function(url,body) {
    if(url.endsWith('/cancel')) {
      ok(body.claim_id==='oldclaim' && body.book_token==='oldbook','cancel must retain the exact owned identity');
      return cancel.promise;
    }
    return Promise.resolve({status:'started',claim_id:'oldclaim',book_token:'oldbook'});
  };
  API.get=function(url) {
    if(url.indexOf('/api/status/')!==0) { return new Promise(function() {}); }
    gets+=1;
    return Promise.resolve({task_claim_id:complete && KIND==='claim'?'newclaim':'oldclaim',
      readalong_book_token:complete && KIND==='book'?'newbook':'oldbook',running:!complete,
      result:complete?{status:'done'}:null});
  };
  window.buildReadaloudEpub(); await flush(); await flush();
  for(var i=0;i<1801 && timers.length;i++) { timers.shift()();await Promise.resolve();await Promise.resolve(); }
  ok(gets===1800 && timers.length===0,'must reach the actual finite production request budget');
  complete=true;window.cancelReadaloudEpub();await flush();
  if(RESET) { window.resetReadalongViews(); }
  var before=_els['readaloud-status'].textContent;
  var error=new Error('No EPUB read-aloud export is running.');error.status=400;cancel.reject(error);
  await flush();await flush();
  if(RESET) {
    ok(gets===1800,'old cancel error must not resume status after reset');
    ok(_els['readaloud-status'].textContent===before,'old cancel error must not repaint the new book');
    ok(!_els['readaloud-build-btn'].disabled,'new book must remain idle');
  } else {
    ok(gets===1801,'cancel400 must resume one bounded status GET');
    ok(downloads===(KIND==='same'?1:0),'only the exact owned book may download');
    ok(_els['readaloud-status'].textContent.indexOf(KIND==='same'?'ready':KIND==='claim'?'newer build':'book changed')>=0,'claim and book fences must run before terminal handling');
  }
  console.log('late-cancel-status-ok');
})();
""".replace('KIND', json.dumps(kind)).replace('RESET', 'true' if reset else 'false')
        review_helpers.JsHarnessTests()._run_js(scenario, 'late-cancel-status-ok')

    def test_finished_build_after_poll_budget_is_observed_after_cancel_400(self):
        self._run_late_cancel('same')

    def test_cancel_error_resume_keeps_claim_and_book_fences(self):
        for kind in ('claim', 'book'):
            with self.subTest(kind=kind):
                self._run_late_cancel(kind)

    def test_reset_fences_late_cancel_error_without_resuming_old_status(self):
        self._run_late_cancel('same', reset=True)

    def test_prebuilder_identity_hashes_receive_live_cancel_and_report_cancelled(self):
        import qwen_runtime
        import readalong_export
        from routers.editor import ReadaloudEpubRequest
        for stage in ('runtime', 'provider'):
            with self.subTest(stage=stage):
                editor, _manager = self._fixture(self._mkdtmp())
                cap = self._ready_capability(self._mkdtmp())
                cap['runtime_identity'] = cap['runtime']
                callbacks = []
                def reject(cancel_check):
                    callbacks.append(cancel_check)
                    editor.process_state['readaloud_epub']['cancel'] = True
                    if cancel_check is not None:
                        self.assertTrue(cancel_check())
                    raise ValueError('Qwen identity capture cancelled.')
                def runtime(_python, *, cancel_check=None, **kwargs):
                    if stage == 'runtime':
                        return reject(cancel_check)
                    return cap['runtime']
                def provider(_model, *, cancel_check=None, **kwargs):
                    return reject(cancel_check)
                tasks = fixtures._CaptureTasks()
                with patch.object(qwen_runtime, 'get_qwen_capability', return_value=cap), \
                        patch.object(qwen_runtime, 'get_qwen_runtime_identity', side_effect=runtime), \
                        patch.object(qwen_runtime, 'get_qwen_provider_digest', side_effect=provider), \
                        patch.object(readalong_export, 'build_epub_export', side_effect=AssertionError('cancel must precede builder')):
                    editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode='qwen'))
                    function, args, kwargs = tasks.registered[0]
                    function(*args, **kwargs)
                self.assertEqual(1, len(callbacks))
                self.assertTrue(callable(callbacks[0]), 'identity hash must receive the real task cancellation callback')
                state = editor.process_state['readaloud_epub']
                self.assertEqual('cancelled', state['result']['status'], state['result'])
                self.assertFalse(any('error:' in row.lower() for row in state['logs']))

    def test_unrelated_prebuilder_error_is_not_cancelled_by_message_alone(self):
        import qwen_runtime
        from routers.editor import ReadaloudEpubRequest
        editor, _manager = self._fixture(self._mkdtmp())
        cap = self._ready_capability(self._mkdtmp())
        cap['runtime_identity'] = cap['runtime']
        tasks = fixtures._CaptureTasks()
        with patch.object(qwen_runtime, 'get_qwen_capability', return_value=cap), \
                patch.object(qwen_runtime, 'get_qwen_runtime_identity', side_effect=ValueError('A cancelled metadata field is invalid.')):
            editor._start_readaloud_epub(tasks, ReadaloudEpubRequest(timing_mode='qwen'))
            function, args, kwargs = tasks.registered[0]
            function(*args, **kwargs)
        self.assertEqual('failed', editor.process_state['readaloud_epub']['result']['status'])

    def _source_capture_cancel(self, active):
        import project_scope_files
        import readalong_export
        from audio_generation_owner import AudioGenerationCancelled
        _editor, manager = self._fixture(self._mkdtmp())
        original = project_scope_files.capture_scope_file
        state = {'cancel': False, 'captured': False}
        def capture(root, name, **kwargs):
            if name == 'book.txt' and kwargs.get('cancel_check') is not None:
                state['captured'] = True
                if active:
                    state['cancel'] = True
                    return original(root, name, **kwargs)
                raise AudioGenerationCancelled('Controlled source-reader rejection')
            return original(root, name, **kwargs)
        with patch.object(project_scope_files, 'capture_scope_file', side_effect=capture), \
                patch.object(readalong_export, '_assemble_with_store', side_effect=AssertionError('source refusal must precede PCM assembly')):
            outcome = readalong_export.build_epub_export(manager, lambda: state['cancel'])
        self.assertTrue(state['captured'])
        self.assertFalse((Path(manager.root_dir) / 'readaloud.epub').exists())
        return outcome

    def test_actual_source_capture_cancellation_has_cancelled_terminal_contract(self):
        self.assertEqual((False, 'Export cancelled'), self._source_capture_cancel(True))

    def test_source_reader_exception_without_active_cancel_remains_failed(self):
        outcome = self._source_capture_cancel(False)
        self.assertEqual((False, 'EPUB read-aloud failed: Controlled source-reader rejection'), outcome)

    def test_download_source_identity_streams_without_materializing_source(self):
        import readalong_sidecar
        from project_scope_files import MAX_SCRIPT_BYTES
        editor, manager = self._fixture(self._mkdtmp())
        source = Path(manager.root_dir) / 'book.txt'
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        original = Path.read_bytes
        def read_bytes(path):
            if path == source:
                raise AssertionError('source identity must use bounded streaming')
            return original(path)
        with patch.object(Path, 'read_bytes', read_bytes), patch.object(
                readalong_sidecar, 'get_audio_file_sha256',
                wraps=readalong_sidecar.get_audio_file_sha256) as capture:
            identity = editor._get_current_readalong_identity()
        capture.assert_called_once_with(str(source), limit=MAX_SCRIPT_BYTES, allow_empty=True)
        self.assertEqual(expected, identity['source_sha256'])

    def test_download_source_identity_enforces_shared_source_size_bound(self):
        import project_scope_files
        from fastapi import HTTPException
        editor, _manager = self._fixture(self._mkdtmp())
        with patch.object(project_scope_files, 'MAX_SCRIPT_BYTES', 4):
            with self.assertRaises(HTTPException) as caught:
                editor._get_current_readalong_identity()
        self.assertEqual(409, caught.exception.status_code)

    def test_download_source_identity_preserves_empty_regular_source(self):
        editor, manager = self._fixture(self._mkdtmp())
        (Path(manager.root_dir) / 'book.txt').write_bytes(b'')
        identity = editor._get_current_readalong_identity()
        self.assertEqual(hashlib.sha256(b'').hexdigest(), identity['source_sha256'])

    def test_download_source_identity_preserves_readonly_backup_hardlinks(self):
        editor, manager = self._fixture(self._mkdtmp())
        source = Path(manager.root_dir) / 'book.txt'
        expected = hashlib.sha256(source.read_bytes()).hexdigest()
        backup = source.with_name('book-backup.txt')
        os.link(source, backup)
        source.chmod(0o444)
        try:
            self.assertGreater(source.stat().st_nlink, 1)
            self.assertEqual(expected, editor._get_current_readalong_identity()['source_sha256'])
        finally:
            source.chmod(0o600)

    def test_source_named_identity_change_after_real_hash_refuses_download(self):
        import audio_source_io
        from fastapi import HTTPException
        editor, manager = self._fixture(self._mkdtmp())
        source = Path(manager.root_dir) / 'book.txt'
        original_hash = audio_source_io.read_bounded_audio_hash
        original_stat = os.lstat
        state = {'hashed': False}
        def read_hash(*args, **kwargs):
            result = original_hash(*args, **kwargs)
            state['hashed'] = True
            return result
        def named_stat(path, *args, **kwargs):
            info = original_stat(path, *args, **kwargs)
            if state['hashed'] and Path(path) == source:
                return types.SimpleNamespace(
                    st_dev=info.st_dev, st_ino=info.st_ino + 1,
                    st_size=info.st_size, st_mtime_ns=info.st_mtime_ns,
                    st_ctime_ns=info.st_ctime_ns)
            return info
        with patch.object(audio_source_io, 'read_bounded_audio_hash', side_effect=read_hash), \
                patch.object(os, 'lstat', side_effect=named_stat):
            with self.assertRaises(HTTPException) as caught:
                editor._get_current_readalong_identity()
        self.assertTrue(state['hashed'], 'must hash real source before the identity changes')
        self.assertEqual(409, caught.exception.status_code)

    def test_real_named_replacement_refuses_or_windows_prevents_replacement(self):
        import audio_source_io
        import readalong_sidecar
        root = Path(self._mkdtmp())
        source, replacement = root / 'source.txt', root / 'replacement.txt'
        old_bytes, new_bytes = b'original source', b'replaced source'
        source.write_bytes(old_bytes)
        replacement.write_bytes(new_bytes)
        original_hash = audio_source_io.read_bounded_audio_hash
        state = {'replaced': False, 'denied': False}
        def read_hash(*args, **kwargs):
            result = original_hash(*args, **kwargs)
            try:
                os.replace(replacement, source)
            except PermissionError:
                self.assertEqual('nt', os.name, 'only Windows sharing denial is an accepted control')
                state['denied'] = True
            else:
                state['replaced'] = True
            return result
        result, failure = None, None
        with patch.object(audio_source_io, 'read_bounded_audio_hash', side_effect=read_hash):
            try:
                result = readalong_sidecar.get_audio_file_sha256(source)
            except ValueError as error:
                failure = error
        if state['replaced']:
            self.assertIsNotNone(failure, 'changed named file must not return the old descriptor hash')
            self.assertIn('changed', str(failure).lower())
            self.assertEqual(new_bytes, source.read_bytes())
        else:
            self.assertTrue(state['denied'])
            self.assertIsNone(failure)
            self.assertEqual((hashlib.sha256(old_bytes).hexdigest(), len(old_bytes)), result)
            self.assertEqual(old_bytes, source.read_bytes())

    def test_epub_receipt_bound_is_enforced_before_raw_read(self):
        from fastapi import HTTPException
        editor, manager = self._fixture(self._mkdtmp())
        root = Path(manager.root_dir)
        (root / 'readaloud.epub').write_bytes(b'not parsed before receipt refusal')
        receipt = root / 'readaloud.receipt.json'
        receipt.write_bytes(b'x' * (16384 + 1))
        original = Path.read_bytes
        def read_bytes(path):
            if path == receipt:
                raise AssertionError('receipt size must be refused before raw read')
            return original(path)
        with patch.object(Path, 'read_bytes', read_bytes), patch.object(
                editor, '_read_bounded_preference_bytes',
                wraps=editor._read_bounded_preference_bytes) as capture:
            with self.assertRaises(HTTPException) as caught:
                editor._validated_epub_bytes()
        capture.assert_called_once_with(str(receipt), file_description='EPUB read-aloud receipt')
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn('receipt', str(caught.exception.detail).lower())

    def _published_read_fixture(self):
        editor, manager = self._fixture(self._mkdtmp())
        root = Path(manager.root_dir)
        artifact = root / 'readaloud.epub'
        raw = fixtures._chapter_epub()
        artifact.write_bytes(raw)
        receipt = dict.fromkeys(('book_token', 'rows_digest', 'source_sha256',
            'settings_digest', 'retained_epub_sha256', 'audio_sha256'), 'f' * 64)
        receipt['epub_sha256'] = hashlib.sha256(raw).hexdigest()
        (root / 'readaloud.receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        return editor, artifact, receipt

    def test_bounded_metadata_refuses_same_size_rewrite_after_actual_read(self):
        editor, manager = self._fixture(self._mkdtmp())
        metadata = Path(manager.root_dir) / 'readalong_timing.json'
        original_bytes, replacement = b'{"v":1}', b'{"v":2}'
        metadata.write_bytes(original_bytes)
        before = metadata.stat()
        original_read = os.read
        observed = []
        def read(descriptor, count):
            block = original_read(descriptor, count)
            if block and not observed:
                observed.append(block)
                metadata.write_bytes(replacement)
                os.utime(metadata, ns=(before.st_atime_ns, before.st_mtime_ns + 2000000000))
            return block
        with patch.object(os, 'read', side_effect=read):
            with self.assertRaisesRegex(ValueError, 'changed during read'):
                editor._read_bounded_preference_bytes(metadata)
        self.assertEqual([original_bytes], observed)
        self.assertEqual(replacement, metadata.read_bytes())

    def test_bounded_metadata_refuses_named_identity_change_at_actual_eof(self):
        editor, manager = self._fixture(self._mkdtmp())
        metadata = Path(manager.root_dir) / 'readaloud.receipt.json'
        metadata.write_bytes(b'{"observed":"receipt"}')
        original_read, original_lstat = os.read, os.lstat
        state = {'eof': False}
        def read(descriptor, count):
            block = original_read(descriptor, count)
            if not block:
                state['eof'] = True
            return block
        def named(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if state['eof'] and Path(path) == metadata:
                return types.SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev,
                    st_ino=info.st_ino + 1, st_size=info.st_size,
                    st_mtime_ns=info.st_mtime_ns, st_ctime_ns=info.st_ctime_ns)
            return info
        with patch.object(os, 'read', side_effect=read), patch.object(os, 'lstat', side_effect=named):
            with self.assertRaisesRegex(ValueError, 'changed during read'):
                editor._read_bounded_preference_bytes(metadata, file_description='EPUB receipt')
        self.assertTrue(state['eof'])

    def test_bounded_metadata_preserves_missing_and_empty_raw_states(self):
        editor, manager = self._fixture(self._mkdtmp())
        metadata = Path(manager.root_dir) / 'raw-metadata.json'
        self.assertIsNone(editor._read_bounded_preference_bytes(metadata))
        metadata.write_bytes(b'')
        self.assertEqual(b'', editor._read_bounded_preference_bytes(metadata))

    def test_published_epub_hash_read_error_is_409_from_actual_shared_reader(self):
        import audio_source_io
        from fastapi import HTTPException
        editor, _artifact, _receipt = self._published_read_fixture()
        with patch.object(audio_source_io, 'read_bounded_audio_hash',
                          side_effect=OSError('controlled descriptor read failure')) as read:
            with self.assertRaises(HTTPException) as caught:
                editor._validated_epub_bytes()
        read.assert_called_once()
        self.assertEqual(409, caught.exception.status_code)
        self.assertIn('controlled descriptor read failure', caught.exception.detail)

    def test_wrong_published_hash_refuses_before_full_buffering(self):
        import audio_source_io
        from fastapi import HTTPException
        editor, artifact, receipt = self._published_read_fixture()
        receipt['epub_sha256'] = 'e' * 64
        artifact.with_name('readaloud.receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
        original_read = Path.read_bytes
        original_hash = audio_source_io.read_bounded_audio_hash
        observed = []
        def raw(path):
            if path == artifact:
                raise AssertionError('unverified published output must not use raw materialization')
            return original_read(path)
        def stream_hash(stream, limit, check):
            result = original_hash(stream, limit, check)
            observed.append((limit, result))
            return result
        with patch.object(Path, 'read_bytes', raw), patch.object(
                audio_source_io, 'read_bounded_audio_hash', side_effect=stream_hash):
            with self.assertRaises(HTTPException) as caught:
                editor._validated_epub_bytes()
        self.assertEqual(409, caught.exception.status_code)
        self.assertEqual(1, len(observed))
        self.assertEqual(artifact.stat().st_size, observed[0][0])
        self.assertIn('does not match its receipt', caught.exception.detail)

    def test_published_epub_growth_refuses_with_captured_extent(self):
        import audio_source_io
        from fastapi import HTTPException
        editor, artifact, _receipt = self._published_read_fixture()
        original_hash = audio_source_io.read_bounded_audio_hash
        old_size = artifact.stat().st_size
        observed = []
        def stream_hash(stream, limit, check):
            observed.append(limit)
            with artifact.open('ab') as writer:
                writer.write(b'growth')
            return original_hash(stream, limit, check)
        with patch.object(audio_source_io, 'read_bounded_audio_hash', side_effect=stream_hash):
            with self.assertRaises(HTTPException) as caught:
                editor._validated_epub_bytes()
        self.assertEqual([old_size], observed)
        self.assertEqual(409, caught.exception.status_code)
        self.assertGreater(artifact.stat().st_size, old_size)

    def test_default_audio_hash_refuses_empty_regular_file(self):
        import readalong_sidecar
        path = Path(self._mkdtmp()) / 'empty.mp3'
        path.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'nonempty|empty'):
            readalong_sidecar.get_audio_file_sha256(path)

    def test_audio_hash_requires_boolean_empty_policy(self):
        import readalong_sidecar
        path = Path(self._mkdtmp()) / 'empty.mp3'
        path.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'boolean'):
            readalong_sidecar.get_audio_file_sha256(path, allow_empty=1)
