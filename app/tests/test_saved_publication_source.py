from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import core
from epub_publication import get_epub_publication_candidate
from publication_source import (get_upload_publication_update, apply_publication_source_update,
    get_publication_source_capture, get_publication_source_document, get_publication_source_path)
from routers import scripts_library as routes
from tests import test_saved_book_publication as fixtures
from tests.test_upload_publication_source import epub, TEXT
from tests.test_m4b_cover_admission import image_bytes
from book_state_transaction import apply_book_input_selection, get_book_operation_token
from script_selection_binding import BINDING_NAME, capture_generation_selection, publish_generated_script


class SavedPublicationSourceTests(unittest.TestCase):
    def test_review_voice_mapping_requires_the_matching_voice_config(self):
        from script_selection_binding import (publish_controlled_script_edit,
                                               publish_saved_script_edit)
        with self.fixture() as (root, _client):
            with self.assertRaisesRegex(ValueError, 'Voice remap requires'):
                publish_controlled_script_edit(str(root), b'[]', b'[]',
                                               voice_mapping={'Alias': 'Canonical'})
            saved = root / 'scripts/book.json'
            with self.assertRaisesRegex(ValueError, 'Voice remap requires'):
                publish_saved_script_edit(str(root), str(saved.parent), str(saved),
                                          saved.read_bytes(), saved.read_bytes(),
                                          voice_mapping={'Alias': 'Canonical'})

    def test_review_voice_mapping_refuses_a_disappeared_voice_config(self):
        from review_script import _remapped_voice_config
        from script_selection_binding import (publish_controlled_script_edit,
                                               publish_saved_script_edit)
        with self.fixture() as (root, _client):
            active = root / 'annotated_script.json'
            script = b'[{"speaker":"Alias","text":"Line."}]'
            active.write_bytes(script)
            active_voice = root / 'voice_config.json'
            active_voice.unlink(missing_ok=True)
            with self.assertRaisesRegex(ValueError, 'disappeared'):
                publish_controlled_script_edit(
                    str(root), script, script, voice_config_path=str(active_voice),
                    voice_mapping={'Alias':'Canonical'},
                    remap_voice_config=_remapped_voice_config)
            saved = root / 'scripts/book.json'
            saved.write_bytes(script)
            saved_voice = root / 'scripts/book.voice_config.json'
            saved_voice.unlink(missing_ok=True)
            with patch('publication_source.get_saved_publication_source_bytes', return_value=None), \
                 self.assertRaisesRegex(ValueError, 'disappeared'):
                publish_saved_script_edit(
                    str(root), str(saved.parent), str(saved), saved.read_bytes(),
                    saved.read_bytes(), voice_config_path=str(saved_voice),
                    voice_mapping={'Alias':'Canonical'},
                    remap_voice_config=_remapped_voice_config)

    @contextmanager
    def fixture(self):
        with fixtures.SavedBookPublicationTests().fixture() as (root, api), ExitStack() as stack:
            source = root / 'uploads/source.txt'; source.write_bytes(TEXT)
            candidate = get_epub_publication_candidate(epub(), filename='origin.epub',
                                                       text_sha256=hashlib.sha256(TEXT).hexdigest())
            apply_publication_source_update(get_upload_publication_update(
                source, candidate, 48 * 1024**2, source_limit=512 * 1024**2))
            state = {'active_book_id': 'new-book', 'book_generation': 'generation-a', 'input_file_path': str(source)}
            (root / 'state.json').write_text(json.dumps(state))
            for module in (routes, core):
                for key, name in (('AUDIOBOOK_PATH', 'book.mp3'), ('M4B_PATH', 'book.m4b'), ('CHUNKS_PATH', 'chunks.json')):
                    stack.enter_context(patch.object(module, key, str(root / name)))
            with TestClient(api, raise_server_exceptions=False) as client:
                yield root, client

    def test_review_voice_remap_is_atomic_with_bound_script_and_preserves_current_settings(self):
        import book_state_transaction as books
        from review_script import _remapped_voice_config
        from script_selection_binding import publish_controlled_script_edit
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text())
            state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = b'[{"speaker":"Alias","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], original, expected_run_id='f' * 32)
            edited = b'[{"speaker":"Canonical","text":"Shareable source paragraph."}]'
            voice = root / 'voice_config.json'
            voice.write_text(json.dumps({'Alias': {'voice': 'a'}, 'Canonical': {'speed': 1.2},
                                         'other': {'voice': 'recent UI edit'}}))
            old_voice, old_binding = voice.read_bytes(), (root / BINDING_NAME).read_bytes()
            move = books._move
            failed = False
            def fail_voice_once(src, dest, **kwargs):
                nonlocal failed
                if not failed and Path(dest) == voice and 'new-' in str(src):
                    failed = True
                    raise OSError('injected voice publication failure')
                return move(src, dest, **kwargs)
            args = dict(voice_config_path=str(voice), voice_mapping={'Alias': 'Canonical'},
                        remap_voice_config=_remapped_voice_config)
            with patch.object(books, '_move', side_effect=fail_voice_once):
                with self.assertRaisesRegex(OSError, 'injected'):
                    publish_controlled_script_edit(str(root), original, edited, **args)
            self.assertTrue(failed)
            self.assertEqual(original, (root / 'annotated_script.json').read_bytes())
            self.assertEqual(old_binding, (root / BINDING_NAME).read_bytes())
            self.assertEqual(old_voice, voice.read_bytes())
            self.assertEqual(1, publish_controlled_script_edit(str(root), original, edited, **args))
            self.assertEqual({'Canonical': {'voice': 'a', 'speed': 1.2},
                              'other': {'voice': 'recent UI edit'}}, json.loads(voice.read_text()))
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'reviewed'}).status_code)

    def test_saved_review_commits_binding_voice_and_aliases_together(self):
        import book_state_transaction as books
        from review_script import _remapped_voice_config
        from script_selection_binding import publish_saved_script_edit
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            selection = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text())
            state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = b'[{"speaker":"Alias","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), selection['book_token'], str(source), selection['source_sha256'],
                                     original, expected_run_id='f'*32)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'review'}).status_code)
            saved = root/'scripts/review.json'; binding = root/'scripts/review.selection_binding.json'
            voice = root/'scripts/review.voice_config.json'; voice.write_text('{"Alias":{"voice":"a"}}')
            registry = root/'character_aliases.json'; registry.write_text('{}')
            edited = b'[{"speaker":"Canonical","text":"Shareable source paragraph."}]'
            stage = {'path':str(registry), 'before':registry.read_bytes(), 'after':b'{"Alias":"Canonical"}'}
            publication = Path(get_publication_source_path(str(saved)))
            before = {p:p.read_bytes() for p in (saved, binding, voice, registry, publication)}
            kwargs = {'voice_config_path':str(voice), 'voice_mapping':{'Alias':'Canonical'},
                      'remap_voice_config':_remapped_voice_config, 'registry_staging':stage}
            move = books._move; failed = False
            def fail_voice(src, dst, **kwargs):
                nonlocal failed
                if not failed and Path(dst) == voice and 'new-' in str(src):
                    failed = True
                    raise OSError('injected saved review failure')
                return move(src, dst, **kwargs)
            with patch.object(books, '_move', side_effect=fail_voice):
                with self.assertRaisesRegex(OSError, 'injected'):
                    publish_saved_script_edit(str(root), str(root/'scripts'), str(saved), original, edited, **kwargs)
            self.assertTrue(failed)
            self.assertEqual(before, {p:p.read_bytes() for p in before})
            self.assertEqual(1, publish_saved_script_edit(str(root), str(root/'scripts'), str(saved), original, edited, **kwargs))
            self.assertEqual({'Canonical':{'voice':'a'}}, json.loads(voice.read_text()))
            self.assertEqual({'Alias':'Canonical'}, json.loads(registry.read_text()))
            self.assertEqual('current', get_publication_source_capture(saved)['binding_status'])
            self.assertEqual(200, client.post('/api/scripts/load', json={'name':'review'}).status_code)
            self.assertEqual(edited, (root/'annotated_script.json').read_bytes())
            after = {p:p.read_bytes() for p in (saved, binding, voice, registry)}
            with self.assertRaisesRegex(ValueError, 'currently loaded'):
                publish_saved_script_edit(str(root), str(root/'scripts'), str(saved), edited, original)
            self.assertEqual(after, {p:p.read_bytes() for p in after})

    def test_saved_review_refuses_changed_script_and_changed_alias_registry(self):
        from script_selection_binding import publish_saved_script_edit
        with self.fixture() as (root, client):
            saved = root/'scripts/book.json'; old = saved.read_bytes(); new = b'[{"text":"new"}]'
            Path(get_publication_source_path(str(saved))).unlink()
            registry = root/'character_aliases.json'; registry.write_text('{}')
            stage = {'path':str(registry), 'before':b'{}', 'after':b'{"Alias":"Canonical"}'}
            registry.write_text('{"Other":"Name"}')
            with self.assertRaisesRegex(ValueError, 'Alias registry changed'):
                publish_saved_script_edit(str(root), str(root/'scripts'), str(saved), old, new, registry_staging=stage)
            self.assertEqual(old, saved.read_bytes())
            self.assertEqual('{"Other":"Name"}', registry.read_text())
            saved.write_text('[{"text":"newer edit"}]')
            with self.assertRaisesRegex(ValueError, 'changed during review'):
                publish_saved_script_edit(str(root), str(root/'scripts'), str(saved), old, new)
            self.assertEqual('[{"text":"newer edit"}]', saved.read_text())

    def test_staged_alias_registry_cannot_replace_book_state(self):
        from script_selection_binding import publish_saved_script_edit
        with self.fixture() as (root, client):
            saved = root/'scripts/book.json'; original=saved.read_bytes(); state=root/'state.json'
            state_before=state.read_bytes()
            stage={'path':str(state), 'before':state_before, 'after':b'{"ALICE":"BETTY"}'}
            with self.assertRaisesRegex(ValueError, 'collides'):
                publish_saved_script_edit(str(root),str(root/'scripts'),str(saved),original,original,registry_staging=stage)
            self.assertEqual(state_before,state.read_bytes());self.assertEqual(original,saved.read_bytes())

    def test_dedupe_stages_new_alias_without_writing_before_publication(self):
        import review_script as review
        with self.fixture() as (root, client):
            registry = root/'character_aliases.json'; registry.write_bytes(b'{}')
            entries = [{'speaker':'ALICE','text':'One line.'}, {'speaker':'BETTY','text':'Another line.'}]
            stage = {}
            with patch.object(review, 'call_llm_for_object', return_value={'ALICE':'BETTY'}):
                mapping, count, changes = review.dedupe_speakers(
                    object(), 'fixture', entries, registry_path=str(registry), registry_staging=stage)
            self.assertEqual({'ALICE':'BETTY'}, mapping)
            self.assertEqual(1, count)
            self.assertEqual([(0, 'speaker', 'BETTY')], changes)
            self.assertEqual(b'{}', registry.read_bytes())
            self.assertEqual(b'{}', stage['before'])
            self.assertEqual({'ALICE':'BETTY'}, json.loads(stage['after']))

    def test_saved_review_cli_keeps_bound_book_loadable(self):
        import review_script as review
        from contextlib import redirect_stdout
        from io import StringIO
        with self.fixture() as (root, client), ExitStack() as stack:
            source = root/'uploads/source.txt'; apply_book_input_selection(str(root), str(source), 'selected')
            selected = capture_generation_selection(str(root), str(source))
            state = json.loads((root/'state.json').read_text()); state['script_generation_run_id']='f'*32
            state['script_generation_base_sha256']=hashlib.sha256((root/'annotated_script.json').read_bytes()).hexdigest()
            (root/'state.json').write_text(json.dumps(state))
            original = b'[{"speaker":"Alias","text":"Shareable source paragraph.","instruct":"Neutral."}]'
            publish_generated_script(str(root), selected['book_token'], str(source), selected['source_sha256'],
                                     original, expected_run_id='f'*32)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'review-cli'}).status_code)
            saved = root/'scripts/review-cli.json'; voice = root/'scripts/review-cli.voice_config.json'
            voice.write_text('{"Alias":{"voice":"a"}}')
            registry = root/'character_aliases.json'; registry.write_text('{"Alias":"Canonical"}')
            patches = {'get_runtime_data_dir':str(root),
                       'load_app_config':{'generation':{'review_batch_size':1}, 'llm_mode':'remote'},
                       'get_active_llm_config':{}, 'ensure_ideal_settings':(True, {}, 'fixture'),
                       'get_current_status':{'loaded':False}, 'make_run_client':object(),
                       'get_cached_or_benchmarked_concurrency':1}
            for name, value in patches.items():
                stack.enter_context(patch.object(review, name, return_value=value))
            stack.enter_context(patch.object(review, 'review_batch', side_effect=lambda _c,_m,batch,*a,**k:batch))
            stack.enter_context(patch.object(review.sys, 'argv', ['review', '--input',str(saved), '--output',str(saved),
                '--dedupe-speakers', '--alias-registry',str(registry), '--remap-voice-config',str(voice)]))
            stack.enter_context(redirect_stdout(StringIO()))
            review.main()
            self.assertEqual('Canonical', json.loads(saved.read_text())[0]['speaker'])
            self.assertEqual({'Canonical':{'voice':'a'}}, json.loads(voice.read_text()))
            self.assertEqual('current', get_publication_source_capture(saved)['binding_status'])
            self.assertEqual(200, client.post('/api/scripts/load', json={'name':'review-cli'}).status_code)

    def test_saved_review_process_death_fences_readers_until_parent_recovery(self):
        import book_state_transaction as books
        worker = '''import os,sys
from pathlib import Path
import book_state_transaction as books
from script_selection_binding import publish_saved_script_edit
root=Path(sys.argv[1]);saved=root/'scripts/book.json';old=saved.read_bytes();move=books._move
def die(src,dst, **kwargs):
 result=move(src,dst, **kwargs)
 if Path(dst)==saved: os._exit(77)
 return result
books._move=die
publish_saved_script_edit(str(root),str(root/'scripts'),str(saved),old,b'[{"speaker":"Old","text":"Edited book."}]')
'''
        with self.fixture() as (root, client):
            saved=root/'scripts/book.json'; Path(get_publication_source_path(str(saved))).unlink()
            old=saved.read_bytes()
            result=subprocess.run([sys.executable,'-c',worker,str(root)],capture_output=True,timeout=15,env=os.environ.copy())
            self.assertEqual(77,result.returncode,result.stderr.decode())
            self.assertTrue((root/books.JOURNAL).exists())
            self.assertEqual(409,client.get('/api/scripts').status_code)
            with books.ensure_book_state(str(root)):
                pass
            self.assertEqual(old,saved.read_bytes())
            self.assertFalse((root/books.JOURNAL).exists())
            self.assertEqual(200,client.get('/api/scripts').status_code)

    def test_review_cli_rejects_aliases_of_guarded_book_destinations(self):
        import review_script as review
        with self.fixture() as (root, client):
            alias = root/'scripts-alias'; active_alias = root/'active-alias.json'
            try:
                alias.symlink_to(root/'scripts', target_is_directory=True)
                active_alias.symlink_to(root/'annotated_script.json')
            except (OSError, NotImplementedError):
                self.skipTest('Symbolic links unavailable on this host')
            for path in (alias/'book.json', active_alias):
                with self.subTest(path=path), patch.object(review, 'get_runtime_data_dir', return_value=str(root)), \
                        patch.object(review.sys, 'argv', ['review', '--input',str(path), '--output',str(path)]), \
                        patch.object(review, 'load_app_config') as load:
                    with self.assertRaisesRegex(ValueError, 'direct path'):
                        review.main()
                    load.assert_not_called()

    def test_review_cli_rejects_reserved_active_metadata_outputs_before_model_work(self):
        import review_script as review
        with self.fixture() as (root, client):
            source = root/'scripts/book.json'
            for name in ('state.json', 'voice_config.json', 'chunks.json',
                         'annotated_script.selection_binding.json',
                         'annotated_script.json.threepass_checkpoint.json',
                         'annotated_script.json.threepass_manifest.json',
                         'state.json.lock', 'voice_config.json.lock',
                         'annotated_script.json.lock'):
                destination = root/name
                if not destination.exists():
                    destination.write_bytes(b'preserve')
                before = destination.read_bytes()
                with self.subTest(name=name), \
                        patch.object(review, 'get_runtime_data_dir', return_value=str(root)), \
                        patch.object(review.sys, 'argv', [
                            'review', '--input', str(source), '--output', str(destination)]), \
                        patch.object(review, 'load_app_config') as load:
                    with self.assertRaisesRegex(ValueError, 'reserved active-book metadata'):
                        review.main()
                    load.assert_not_called()
                self.assertEqual(before, destination.read_bytes())

    def test_batch_review_companions_cannot_replace_active_metadata(self):
        import review_script as review
        with self.fixture() as (root, _client):
            external = root / 'reports' / 'external.json'
            external.parent.mkdir(exist_ok=True)
            external.write_text('[{"speaker":"NARRATOR","text":"Line."}]')
            for option, destination in (
                    ('--alias-registry', root / 'state.json'),
                    ('--remap-voice-config', root / 'voice_config.json'),
                    ('--alias-registry', root / 'state.json.lock')):
                before = destination.read_bytes() if destination.exists() else None
                with self.subTest(option=option, destination=destination), \
                        patch.object(review, 'get_runtime_data_dir', return_value=str(root)), \
                        patch.object(review.sys, 'argv', [
                            'review', '--input', str(external), '--output', str(external),
                            '--dedupe-speakers', option, str(destination)]), \
                        patch.object(review, 'load_app_config') as load:
                    with self.assertRaisesRegex(ValueError, 'reserved active-book metadata'):
                        review.main()
                    load.assert_not_called()
                if before is not None:
                    self.assertEqual(before, destination.read_bytes())

            active = root / 'annotated_script.json'
            active.write_text('[{"speaker":"Alias","text":"Line."}]')
            voice = root / 'voice_config.json'
            voice.write_text('{"Alias":{"voice":"a"}}')
            with patch.object(review, 'get_runtime_data_dir', return_value=str(root)), \
                    patch.object(review.sys, 'argv', [
                        'review', '--input', str(active), '--output', str(active),
                        '--dedupe-speakers', '--remap-voice-config', str(voice)]), \
                    patch.object(review, 'load_app_config',
                                 side_effect=RuntimeError('reached config after path validation')):
                with self.assertRaisesRegex(RuntimeError, 'reached config'):
                    review.main()

    def test_review_cli_rejects_symlink_alias_into_transaction_workspace(self):
        import review_script as review
        with self.fixture() as (root, client):
            source = root/'scripts/book.json'
            workspace = root/'.book-switch-private'; workspace.mkdir()
            alias = root/'output-alias'
            try:
                alias.symlink_to(workspace, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest('Symbolic links unavailable on this host')
            destination = alias/'reviewed.json'
            with patch.object(review, 'get_runtime_data_dir', return_value=str(root)), \
                    patch.object(review.sys, 'argv', [
                        'review', '--input', str(source), '--output', str(destination)]), \
                    patch.object(review, 'load_app_config') as load:
                with self.assertRaisesRegex(ValueError, 'reserved active-book metadata'):
                    review.main()
                load.assert_not_called()
            self.assertFalse((workspace/'reviewed.json').exists())

    def test_review_cli_rejects_unsupported_custom_companions_before_model_work(self):
        import review_script as review
        with self.fixture() as (root, client):
            saved = root/'scripts/book.json'; before = saved.read_bytes()
            for option in ('--alias-registry', '--remap-voice-config'):
                with self.subTest(option=option), patch.object(review, 'get_runtime_data_dir', return_value=str(root)), \
                        patch.object(review.sys, 'argv', ['review','--input',str(saved),'--output',str(saved),
                            '--dedupe-speakers',option,str(root/'scripts'/'custom.json')]), \
                        patch.object(review, 'load_app_config') as load:
                    with self.assertRaisesRegex(ValueError, 'must belong'):
                        review.main()
                    load.assert_not_called(); self.assertEqual(before, saved.read_bytes())

    def test_save_load_list_and_delete_keep_exact_origin_and_script_binding(self):
        with self.fixture() as (root, client):
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            saved = root / 'scripts/book.json'
            captured = get_publication_source_capture(saved)
            self.assertEqual('current', captured['binding_status'])
            self.assertEqual({'kind': 'script', 'sha256': hashlib.sha256(saved.read_bytes()).hexdigest()},
                             captured['document']['binding'])
            self.assertEqual('origin.epub', captured['document']['candidates'][0]['origin']['filename'])
            self.assertEqual(['book'], [row['name'] for row in client.get('/api/scripts').json()])
            self.assertEqual(400, client.post('/api/scripts/save', json={'name': 'bad.json.publication'}).status_code)
            response = client.post('/api/scripts/load', json={'name': 'book'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(str(saved), json.loads((root / 'state.json').read_bytes())['input_file_path'])
            self.assertEqual('current', get_publication_source_capture(saved)['binding_status'])
            # Deleting the selected source would strand the active binding on
            # a missing file. Switch its source before deleting the saved copy.
            self.assertEqual(409, client.delete('/api/scripts/book').status_code)
            state = json.loads((root / 'state.json').read_bytes())
            state['input_file_path'] = str(root / 'uploads/source.txt')
            (root / 'state.json').write_text(json.dumps(state))
            self.assertEqual(200, client.delete('/api/scripts/book').status_code)
            self.assertEqual({}, fixtures.SavedBookPublicationTests().artifacts(root))

    def test_reselected_source_cannot_certify_previous_script(self):
        with self.fixture() as (root, client):
            previous = (root / 'annotated_script.json').read_bytes()
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'newly-selected-book')
            token = get_book_operation_token(str(root))
            response = client.post('/api/scripts/save', json={
                'name': 'wrong-source', 'expected_book_token': token})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(previous, (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / 'scripts/wrong-source.json').exists())

    def test_save_rejects_oversized_state_before_unbounded_read(self):
        with self.fixture() as (root, client):
            script = (root / 'annotated_script.json').read_bytes()
            with (root / 'state.json').open('wb') as output:
                output.truncate(4 * 1024**2 + 1)
            read_bytes = Path.read_bytes
            def checked_read(path):
                if path == root / 'state.json':
                    raise AssertionError('unbounded state read')
                return read_bytes(path)
            with patch.object(Path, 'read_bytes', checked_read):
                response = client.post('/api/scripts/save', json={
                    'name': 'oversized-state', 'expected_book_token': 'a' * 64})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(script, (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / 'scripts/oversized-state.json').exists())

    def test_save_rejects_oversized_script_before_unbounded_read(self):
        with self.fixture() as (root, client):
            script = root / 'annotated_script.json'
            with script.open('wb') as output:
                output.truncate(512 * 1024**2 + 1)
            read_bytes = Path.read_bytes
            def checked_read(path):
                if path == script:
                    raise AssertionError('unbounded script read')
                return read_bytes(path)
            with patch.object(Path, 'read_bytes', checked_read):
                response = client.post('/api/scripts/save', json={'name': 'oversized-script'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(512 * 1024**2 + 1, script.stat().st_size)
            self.assertFalse((root / 'scripts/oversized-script.json').exists())

    def test_loading_saved_book_removes_old_active_binding_and_pending_selection(self):
        with self.fixture() as (root, client):
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'known'}).status_code)
            binding = root / BINDING_NAME
            binding.write_text('{"version":2}')
            state = json.loads((root / 'state.json').read_text())
            state['script_selection_pending'] = True
            (root / 'state.json').write_text(json.dumps(state))
            response = client.post('/api/scripts/load', json={'name':'known'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertFalse(binding.exists())
            self.assertNotIn('script_selection_pending', json.loads((root / 'state.json').read_text()))
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'copy'}).status_code)

    def test_changed_selected_source_cannot_rebind_generated_script(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            script = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], script, expected_run_id='f' * 32)
            source.write_text('Different source after generation.')
            response = client.post('/api/scripts/save', json={'name':'wrong-source',
                'expected_book_token': captured['book_token']})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(script, (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / 'scripts/wrong-source.json').exists())

    def test_missing_required_binding_refuses_save_without_losing_script(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            script = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], script, expected_run_id='f' * 32)
            (root / BINDING_NAME).unlink()
            response = client.post('/api/scripts/save', json={'name':'missing-binding',
                'expected_book_token': captured['book_token']})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(script, (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / 'scripts/missing-binding.json').exists())

    def test_bound_script_save_load_round_trip_and_tamper_refusal(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            script = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], script, expected_run_id='f' * 32)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'bound'}).status_code)
            saved_binding = root / 'scripts/bound.selection_binding.json'
            self.assertTrue(saved_binding.exists())
            self.assertEqual({'bound', 'book'}, {row['name'] for row in client.get('/api/scripts').json()})
            self.assertEqual(200, client.post('/api/scripts/load', json={'name':'bound'}).status_code)
            self.assertTrue(json.loads((root / 'state.json').read_text())['script_binding_required'])
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'bound-copy'}).status_code)
            (root / 'scripts/bound.json').write_text('[{"text":"tampered"}]')
            active_before = (root / 'annotated_script.json').read_bytes()
            response = client.post('/api/scripts/load', json={'name':'bound'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(active_before, (root / 'annotated_script.json').read_bytes())

    def test_bound_saved_content_repair_keeps_book_loadable(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            script = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph.","instruct":" Calm. "}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], script, expected_run_id='f' * 32)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'repairable'}).status_code)
            response = client.post('/api/scripts/repairable/repair/content/apply', json={
                'expected_sha256':hashlib.sha256(script).hexdigest(),
                'direction_changes':[{'entry_number':1,'expected_instruct':' Calm. ','new_instruct':'Calm.'}]})
            self.assertEqual(200, response.status_code, response.text)
            response = client.post('/api/scripts/load', json={'name':'repairable'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('Calm.', json.loads((root / 'annotated_script.json').read_text())[0]['instruct'])

    def test_edit_loaded_book_save_same_name_remains_saveable(self):
        from script_selection_binding import publish_controlled_script_edit
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], original, expected_run_id='f' * 32)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'bound'}).status_code)
            saved_binding_path = root / 'scripts/bound.selection_binding.json'
            original_binding = json.loads(saved_binding_path.read_bytes())
            self.assertEqual(200, client.post('/api/scripts/load', json={'name':'bound'}).status_code)
            current = (root / 'annotated_script.json').read_bytes()
            if os.path.exists(str(root / 'scripts/bound.json').swapcase()):
                # An existing selected path can have a different spelling on a
                # case-insensitive volume while naming the same file.
                from script_selection_binding import make_script_selection_binding
                from book_state_transaction import get_book_operation_token
                state_path = root / 'state.json'
                state = json.loads(state_path.read_text())
                state['input_file_path'] = state['input_file_path'].swapcase()
                self.assertTrue(os.path.samefile(state['input_file_path'], root / 'scripts/bound.json'))
                state_path.write_text(json.dumps(state))
                token = get_book_operation_token(str(root), state=state)
                binding = make_script_selection_binding(token, hashlib.sha256(current).hexdigest(),
                                                        current, source_path=state['input_file_path'])
                (root / BINDING_NAME).write_text(json.dumps(binding))
            edited = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph.","instruct":"Calm."}]'
            publish_controlled_script_edit(str(root), current, edited)
            import book_state_transaction as transaction
            old_saved = (root / 'scripts/bound.json').read_bytes()
            old_binding = (root / BINDING_NAME).read_bytes()
            move = transaction._move; failed = False
            def fail_binding_once(src, dest, **kwargs):
                nonlocal failed
                if not failed and Path(dest) == root / BINDING_NAME and 'new-' in str(src):
                    failed = True
                    raise OSError('injected binding publication failure')
                return move(src, dest, **kwargs)
            with patch.object(transaction, '_move', side_effect=fail_binding_once):
                self.assertEqual(500, client.post('/api/scripts/save', json={'name':'bound'}).status_code)
            self.assertTrue(failed)
            self.assertEqual(old_saved, (root / 'scripts/bound.json').read_bytes())
            self.assertEqual(old_binding, (root / BINDING_NAME).read_bytes())
            saved_binding_bytes = saved_binding_path.read_bytes()
            saved_binding_path.write_bytes(b'{}')
            response = client.post('/api/scripts/save', json={'name':'bound'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(old_saved, (root / 'scripts/bound.json').read_bytes())
            self.assertEqual(old_binding, (root / BINDING_NAME).read_bytes())
            self.assertEqual(b'{}', saved_binding_path.read_bytes())
            saved_binding_path.write_bytes(saved_binding_bytes)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'bound'}).status_code)
            saved_binding = json.loads(saved_binding_path.read_bytes())
            for key in ('book_token', 'source_path_sha256', 'source_sha256'):
                self.assertEqual(original_binding[key], saved_binding[key])
            self.assertEqual(hashlib.sha256(edited).hexdigest(), saved_binding['published_script_sha256'])
            alias = root / 'scripts/alias.json'
            os.link(root / 'scripts/bound.json', alias)
            try:
                current_binding = (root / BINDING_NAME).read_bytes()
                response = client.post('/api/scripts/save', json={'name':'alias'})
                self.assertEqual(409, response.status_code, response.text)
                # Isolate the later alias fence from the earlier provenance
                # hard-link rejection, which independently protects this case.
                with patch.object(routes, 'get_saved_publication_source_bytes', return_value=None):
                    response = client.post('/api/scripts/save', json={'name':'alias'})
                self.assertEqual(409, response.status_code, response.text)
                self.assertIn('ambiguous path alias', response.json()['detail'])
                self.assertEqual(edited, alias.read_bytes())
                self.assertEqual(edited, (root / 'scripts/bound.json').read_bytes())
                self.assertEqual(current_binding, (root / BINDING_NAME).read_bytes())
            finally:
                alias.unlink()
            response = client.post('/api/scripts/save', json={'name':'bound-copy'})
            self.assertEqual(200, response.status_code, response.text)

    def test_process_death_in_cross_directory_save_is_fenced_and_recoverable(self):
        import book_state_transaction as books
        from script_selection_binding import publish_controlled_script_edit
        worker = '''import os,sys
from pathlib import Path
import core,book_state_transaction as books
from routers import scripts_library as routes
root=Path(sys.argv[1])
for module in (routes,core):
 module.DATA_DIR=str(root);module.SCRIPTS_DIR=str(root/'scripts')
 module.SCRIPT_PATH=str(root/'annotated_script.json');module.VOICE_CONFIG_PATH=str(root/'voice_config.json')
 module.CHUNKS_PATH=str(root/'chunks.json');module.AUDIOBOOK_PATH=str(root/'book.mp3');module.M4B_PATH=str(root/'book.m4b')
move=books._move;count=0
def die(source,destination, **kwargs):
 global count
 result=move(source,destination, **kwargs);count+=1
 if count==3:os._exit(77)
 return result
books._move=die
routes._save_script_sync(routes.ScriptSaveRequest(name='bound'))
'''
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], original, expected_run_id='f' * 32)
            self.assertEqual(200, client.post('/api/scripts/save', json={'name':'bound'}).status_code)
            self.assertEqual(200, client.post('/api/scripts/load', json={'name':'bound'}).status_code)
            current = (root / 'annotated_script.json').read_bytes()
            edited = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph.","instruct":"Calm."}]'
            publish_controlled_script_edit(str(root), current, edited)
            before = fixtures.SavedBookPublicationTests().artifacts(root)
            binding_before = (root / BINDING_NAME).read_bytes()
            result = subprocess.run([sys.executable, '-c', worker, str(root)], capture_output=True, text=True, timeout=30)
            self.assertEqual(77, result.returncode, result.stderr)
            self.assertTrue((root / books.JOURNAL).exists())
            self.assertEqual(409, client.get('/api/scripts').status_code)
            with books.ensure_book_state(str(root)):
                pass
            self.assertEqual(200, client.get('/api/scripts').status_code)
            self.assertEqual(before, fixtures.SavedBookPublicationTests().artifacts(root))
            self.assertEqual(binding_before, (root / BINDING_NAME).read_bytes())
            self.assertEqual(edited, (root / 'annotated_script.json').read_bytes())

    def test_untracked_script_replacement_cannot_inherit_source_provenance(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            script = b'[{"speaker":"NARRATOR","text":"Shareable source paragraph."}]'
            publish_generated_script(str(root), captured['book_token'], str(source),
                                     captured['source_sha256'], script, expected_run_id='f' * 32)
            unrelated = b'[{"speaker":"NARRATOR","text":"Unrelated story."}]'
            (root / 'annotated_script.json').write_bytes(unrelated)
            # A user-writable receipt with matching hashes is not authority to
            # attach the selected source to unrelated replacement text.
            from completed_review_receipt import save_completed_review_receipt, get_review_entries_fingerprint
            path = str(root / 'annotated_script.json')
            save_completed_review_receipt(path, path,
                get_review_entries_fingerprint(json.loads(script)),
                get_review_entries_fingerprint(json.loads(unrelated)), 'forged')
            response = client.post('/api/scripts/save', json={'name':'untracked',
                'expected_book_token': captured['book_token']})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(unrelated, (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / 'scripts/untracked.json').exists())

    def test_completed_review_edit_can_be_saved_when_receipt_matches(self):
        from script_selection_binding import publish_controlled_script_edit
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = [{'speaker':'NARRATOR','text':'Shareable source paragraph.'}]
            publish_generated_script(str(root), captured['book_token'], str(source),
                captured['source_sha256'], json.dumps(original).encode(), expected_run_id='f' * 32)
            reviewed = [{'speaker':'NARRATOR','text':'Shareable source paragraph.','instruct':'Calm.'}]
            script_path = root / 'annotated_script.json'
            publish_controlled_script_edit(str(root), script_path.read_bytes(), json.dumps(reviewed).encode())
            response = client.post('/api/scripts/save', json={'name':'reviewed',
                'expected_book_token':captured['book_token']})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(reviewed, json.loads((root / 'scripts/reviewed.json').read_text()))

    def test_delivery_retry_edit_remains_saveable(self):
        from delivery_review import ensure_delivery_review_snapshot, apply_delivery_retry
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = [{'speaker':'NARRATOR','text':'Shareable source paragraph.','instruct':'Neutral.',
                         'instruct_unchecked':True}]
            publish_generated_script(str(root), captured['book_token'], str(source),
                captured['source_sha256'], json.dumps(original).encode(), expected_run_id='f' * 32)
            snapshot = ensure_delivery_review_snapshot(str(root))
            after = [{'speaker':'NARRATOR','text':'Shareable source paragraph.','instruct':'Calm.'}]
            apply_delivery_retry(str(root), snapshot, after)
            response = client.post('/api/scripts/save', json={'name':'delivery-reviewed',
                'expected_book_token':captured['book_token']})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(after, json.loads((root / 'scripts/delivery-reviewed.json').read_text()))

    def test_cancelled_delivery_retry_preserves_script_and_binding(self):
        from delivery_review import ensure_delivery_review_snapshot, apply_delivery_retry
        with self.fixture() as (root, _):
            source = root / 'uploads/source.txt'
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            original = [{'speaker':'NARRATOR','text':'Shareable source paragraph.','instruct':'Neutral.',
                         'instruct_unchecked':True}]
            publish_generated_script(str(root), captured['book_token'], str(source),
                captured['source_sha256'], json.dumps(original).encode(), expected_run_id='f' * 32)
            snapshot = ensure_delivery_review_snapshot(str(root))
            script_before = (root / 'annotated_script.json').read_bytes()
            binding_before = (root / BINDING_NAME).read_bytes()
            def cancel(): raise RuntimeError('cancelled fixture')
            with self.assertRaisesRegex(RuntimeError, 'cancelled fixture'):
                apply_delivery_retry(str(root), snapshot, [{'speaker':'NARRATOR',
                    'text':'Shareable source paragraph.','instruct':'Calm.'}], cancel_check=cancel)
            self.assertEqual(script_before, (root / 'annotated_script.json').read_bytes())
            self.assertEqual(binding_before, (root / BINDING_NAME).read_bytes())

    def test_explicit_legacy_checkpoint_replay_can_publish_and_save(self):
        import three_pass_generate as tp
        from generate_script import LLMGenParams
        from tests.test_three_pass_checkpoint_recovery import delivery_client
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'; text = source.read_text()
            apply_book_input_selection(str(root), str(source), 'selected')
            captured = capture_generation_selection(str(root), str(source))
            state = json.loads((root / 'state.json').read_text()); state['script_generation_run_id'] = 'f' * 32
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            output = str(root / 'annotated_script.json')
            params = LLMGenParams(max_tokens=500, segmentation='llm')
            fingerprint = tp.three_pass_fingerprint(text, 'fixture', 3000, params)
            segmented = [{'type':'NARRATOR', 'text':text}]
            named = [{'speaker':'NARRATOR', 'text':text, 'type':'NARRATOR'}]
            annotated = [{**named[0], 'instruct':'Neutral.'}]
            checkpoint = {'fingerprint':fingerprint, 'stage':'done', 'chunks_done':1,
                'segmented':segmented, 'named':named, 'annotated':annotated,
                'resolutions':['clean'], 'elapsed_s':{'segment':1.5}, 'diagnostic_failures':[]}
            Path(tp.three_pass_checkpoint_path(output)).write_text(json.dumps(checkpoint))
            with patch.object(tp, 'segment_chunk') as segment, patch.object(tp, 'instruct_batch') as instruct:
                result = tp.run_three_pass(delivery_client(), 'fixture', text, params, 3000,
                    output_path=output, require_matching_checkpoint=True, defer_final_publication=True)
            segment.assert_not_called(); instruct.assert_not_called()
            self.assertEqual('publishing', json.loads(Path(tp.three_pass_manifest_path(output)).read_text())['status'])
            publish_generated_script(str(root), captured['book_token'], str(source), captured['source_sha256'],
                json.dumps(result, ensure_ascii=False, indent=2).encode(), expected_run_id='f' * 32,
                complete_manifest=True)
            response = client.post('/api/scripts/save', json={'name':'legacy-replayed',
                'expected_book_token':captured['book_token']})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(result, json.loads((root / 'scripts/legacy-replayed.json').read_text()))
            tp._refuse_checkpoint_replay_over_edited_publication(output)
            from book_state_transaction import ensure_book_state
            from script_selection_binding import publish_controlled_script_edit
            original = Path(output).read_bytes()
            edited_entries = json.loads(original)
            edited_entries[0]['instruct'] = 'Reviewed instruction.'
            edited = json.dumps(edited_entries).encode('utf-8')
            with ensure_book_state(root):
                publish_controlled_script_edit(str(root), original, edited)
            checkpoint_bytes = Path(tp.three_pass_checkpoint_path(output)).read_bytes()
            manifest_bytes = Path(tp.three_pass_manifest_path(output)).read_bytes()
            with patch.object(tp, 'segment_chunk') as segment, patch.object(tp, 'instruct_batch') as instruct:
                with self.assertRaisesRegex(RuntimeError, 'changed after this checkpoint'):
                    tp.run_three_pass(delivery_client(), 'fixture', text, params, 3000,
                        output_path=output, require_matching_checkpoint=True, defer_final_publication=True)
            segment.assert_not_called(); instruct.assert_not_called()
            self.assertEqual(edited, Path(output).read_bytes())
            self.assertEqual(checkpoint_bytes, Path(tp.three_pass_checkpoint_path(output)).read_bytes())
            self.assertEqual(manifest_bytes, Path(tp.three_pass_manifest_path(output)).read_bytes())

    def test_corrupt_state_does_not_bypass_selection_guard(self):
        with self.fixture() as (root, client):
            script = (root / 'annotated_script.json').read_bytes()
            (root / 'state.json').write_text('{bad')
            response = client.post('/api/scripts/save', json={'name':'corrupt-state'})
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(script, (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / 'scripts/corrupt-state.json').exists())

    def test_stale_input_hints_are_preserved_without_certifying_the_new_script(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            original = Path(get_publication_source_path(source)).read_bytes()
            source.write_bytes(b'Changed source words.')
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            saved = root / 'scripts/book.json'
            self.assertEqual(original, Path(get_publication_source_path(saved)).read_bytes())
            self.assertEqual('stale', get_publication_source_capture(saved)['binding_status'])

    def test_corrupt_hint_blocks_save_or_load_before_book_files_change(self):
        for corrupt in (b'not JSON', b'[' * 2000 + b']' * 2000):
            with self.subTest(size=len(corrupt)), self.fixture() as (root, client):
                before = fixtures.SavedBookPublicationTests().artifacts(root)
                source = root / 'uploads/source.txt'
                companion = Path(get_publication_source_path(source)); original = companion.read_bytes()
                companion.write_bytes(corrupt)
                self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                self.assertEqual(before, fixtures.SavedBookPublicationTests().artifacts(root))
                companion.write_bytes(original)
                self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                Path(get_publication_source_path(root / 'scripts/book.json')).write_bytes(corrupt)
                state = (root / 'state.json').read_bytes(); active = (root / 'annotated_script.json').read_bytes()
                self.assertEqual(409, client.post('/api/scripts/load', json={'name': 'book'}).status_code)
                self.assertEqual(state, (root / 'state.json').read_bytes())
                self.assertEqual(active, (root / 'annotated_script.json').read_bytes())

    def test_provenance_publication_failure_rolls_back_entire_saved_family(self):
        import source_upload_publication
        with self.fixture() as (root, client):
            before = fixtures.SavedBookPublicationTests().artifacts(root)
            move = source_upload_publication.move_source_upload_exclusively; failed = False
            def refuse(source, destination, *args, **kwargs):
                nonlocal failed
                if (not failed and Path(destination).name == Path(get_publication_source_path(root / 'scripts/book.json')).name
                        and Path(source).name.startswith('new-')):
                    failed = True
                    raise OSError('fixture source provenance publication failure')
                return move(source, destination, *args, **kwargs)
            with patch.object(source_upload_publication, 'move_source_upload_exclusively', side_effect=refuse):
                self.assertEqual(500, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            self.assertTrue(failed)
            self.assertEqual(before, fixtures.SavedBookPublicationTests().artifacts(root))

    def test_speaker_repair_keeps_provenance_but_marks_its_script_binding_stale(self):
        with self.fixture() as (root, client):
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            saved = root / 'scripts/book.json'; provenance = Path(get_publication_source_path(saved))
            before = provenance.read_bytes()
            response = client.post('/api/scripts/book/repair/speakers/apply', json={
                'expected_sha256': hashlib.sha256(saved.read_bytes()).hexdigest(),
                'selections': [{'entry_number': 1, 'expected_speaker': 'Alice', 'new_speaker': 'Bob'}]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, provenance.read_bytes())
            self.assertEqual('stale', get_publication_source_capture(saved)['binding_status'])

    def test_reader_rejects_forged_identity_invalid_shapes_and_cover_hashes(self):
        with self.fixture() as (root, _):
            original = json.loads(Path(get_publication_source_path(root / 'uploads/source.txt')).read_bytes())
            invalid = []
            doc = copy.deepcopy(original); doc['candidates'][0]['id'] = '0' * 64; invalid.append(doc)
            doc = copy.deepcopy(original); doc['binding']['sha256'] = '0' * 64; invalid.append(doc)
            doc = copy.deepcopy(original); doc['candidates'][0]['fields']['title'] = ['x'] * 33; invalid.append(doc)
            doc = copy.deepcopy(original); doc['candidates'][0]['cover_status'] = 'available'; invalid.append(doc)
            doc = copy.deepcopy(original); doc['candidates'] *= 2; invalid.append(doc)
            cover_doc = copy.deepcopy(original)
            cover_doc['candidates'] = [get_epub_publication_candidate(epub(cover=image_bytes('PNG')),
                filename='cover.epub', text_sha256=hashlib.sha256(TEXT).hexdigest())]
            self.assertEqual('available', get_publication_source_document(
                json.dumps(cover_doc).encode())['candidates'][0]['cover_status'])
            doc = copy.deepcopy(cover_doc); doc['candidates'][0]['cover']['sha256'] = '0' * 64; invalid.append(doc)
            doc = copy.deepcopy(cover_doc); doc['candidates'][0]['cover']['data_base64'] = 'invalid'; invalid.append(doc)
            for doc in invalid:
                with self.subTest(document=invalid.index(doc)), self.assertRaises(ValueError):
                    get_publication_source_document(json.dumps(doc).encode())
