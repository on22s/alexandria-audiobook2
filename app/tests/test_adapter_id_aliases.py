"""Persisted adapter IDs resolve through a single validated manifest map."""
import copy
import unittest
from voice_manifest import get_adapter_id_alias_map,get_resolved_adapter_id


class AdapterIdAliasTests(unittest.TestCase):
    def test_canonical_metadata_preserves_historical_spelling_under_case_normalization(self):
        from unittest.mock import patch
        from voice_manifest import get_resolved_adapter_manifest_rows_locked
        rows=[{'id':'RAW_A','previous_ids':['PREVIOUS'],'keep':1}];before=copy.deepcopy(rows)
        with patch('voice_manifest.os.path.normcase',side_effect=str.lower),patch('voice_manifest.get_adapter_id_map_locked',return_value={'raw_a':'Voice','previous':'Voice'}):
            normalized=get_resolved_adapter_manifest_rows_locked('unused',rows)
        self.assertEqual('Voice',normalized[0]['id'])
        self.assertEqual(['PREVIOUS','raw_a'],normalized[0]['previous_ids'])
        self.assertEqual(before,rows)

    def test_batch_resume_finds_renamed_voice_through_stale_manifest_without_rewriting_it(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject,_files
        from tests.test_voicelab_pipeline_scripts import batch_train
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            default=models/'manifest.json';custom=root/'custom.json';custom.write_bytes(default.read_bytes())
            args=SimpleNamespace(manifest=str(custom),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),str(custom)):
                self.assertEqual(0,subject._run_naming(args))
            expected=get_resolved_adapter_path(str(models/'raw_a'));before=_files(models);original=copy.deepcopy(rows)
            self.assertEqual(expected,batch_train.adapter_exists(str(models),'raw_a',rows))
            self.assertEqual(original,rows);self.assertEqual(before,_files(models))

    def test_gated_promotion_and_later_rollback_follow_alias_destinations_not_source_names(self):
        import json
        import tempfile
        from pathlib import Path
        import promote_adapters as promotion
        from adapter_naming_transaction import lock_adapter_naming,apply_adapter_naming_locked
        from tests.test_promotion_publication_transaction import PromotionPublicationTransactionTests
        from tests.test_adapter_naming_transaction import _files
        fixture=PromotionPublicationTransactionTests()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);configured=fixture.fixture(root);models,source,gates,backups=configured
            manifest=models/'manifest.json';custom=root/'custom.json';custom.write_bytes(manifest.read_bytes())
            old=json.loads(manifest.read_bytes());updated=copy.deepcopy(old);updated[0]['id']='named_a'
            before=_files(models/'a');gate_bytes=_files(gates);source_bytes=_files(source)
            original_samples=json.loads((models/'a'/'training_meta.json').read_bytes())['num_samples']
            with lock_adapter_naming(str(models),str(custom)):
                apply_adapter_naming_locked(str(models),str(custom),updated,[('a','named_a')])
            custom_bytes=custom.read_bytes()
            with fixture.configured(configured):
                self.assertEqual(0,promotion.promote(['a'],'stamp',False))
                self.assertEqual((source/'a'/'adapter'/'adapter_model.safetensors').read_bytes(),(models/'named_a'/'adapter_model.safetensors').read_bytes())
                self.assertEqual(before,_files(backups/'stamp'/'named_a'))
                rows=json.loads(manifest.read_bytes());self.assertEqual('named_a',rows[0]['id']);self.assertEqual(.8,rows[0]['gate_ecapa']);self.assertEqual(17,rows[0]['sample_count'])
                self.assertEqual(.8,promotion.shipped_scores()['a'])
                receipt=backups/'stamp.json';receipt_bytes=receipt.read_bytes();record=json.loads(receipt_bytes)['adapters'][0]
                self.assertEqual('named_a',record['adapter']);self.assertEqual('a',record['evidence_adapter']);self.assertEqual(.4,record['shipped_ecapa'])
                renamed=copy.deepcopy(rows);renamed[0]['id']='final_a'
                with lock_adapter_naming(str(models),str(manifest)):
                    apply_adapter_naming_locked(str(models),str(manifest),renamed,[('named_a','final_a')])
                try:
                    rollback_result=promotion.rollback('stamp')
                except ValueError as error:
                    self.fail(f'Historical rollback destination was refused: {error}')
                self.assertEqual(0,rollback_result)
                self.assertEqual(before,_files(models/'final_a'))
                restored=json.loads(manifest.read_bytes());self.assertEqual('final_a',restored[0]['id']);self.assertEqual(.4,restored[0]['gate_ecapa']);self.assertEqual(original_samples,restored[0]['sample_count'])
                self.assertEqual(.4,promotion.shipped_scores()['a']);self.assertEqual(receipt_bytes,receipt.read_bytes())
                fixture.assert_no_journal(models)
            self.assertFalse((models/'a').exists());self.assertFalse((models/'named_a').exists())
            self.assertEqual(old[1:],restored[1:]);self.assertEqual(custom_bytes,custom.read_bytes())
            self.assertEqual(gate_bytes,_files(gates));self.assertEqual(source_bytes,_files(source))

    def test_promotion_rejects_multiple_names_for_one_current_voice_before_backup(self):
        import json
        import tempfile
        from pathlib import Path
        import promote_adapters as promotion
        from adapter_naming_transaction import lock_adapter_naming,apply_adapter_naming_locked
        from tests.test_promotion_publication_transaction import PromotionPublicationTransactionTests
        from tests.test_adapter_naming_transaction import _files
        fixture=PromotionPublicationTransactionTests()
        with tempfile.TemporaryDirectory() as tmp:
            configured=fixture.fixture(Path(tmp));models,source,gates,backups=configured
            manifest=models/'manifest.json';rows=json.loads(manifest.read_bytes());rows[0]['id']='named_a'
            with lock_adapter_naming(str(models),str(manifest)):
                apply_adapter_naming_locked(str(models),str(manifest),rows,[('a','named_a')])
            before=_files(models)
            with fixture.configured(configured),self.assertRaisesRegex(ValueError,'duplicate publication'):
                promotion.promote(['a','named_a'],'stamp',False)
            self.assertEqual(before,_files(models));self.assertFalse(backups.exists())

    def test_existing_batch_output_cannot_resurrect_historical_alias_before_extraction(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from types import SimpleNamespace
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject,_files
        from tests.test_voicelab_pipeline_scripts import batch_train
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            naming=SimpleNamespace(manifest=str(models/'manifest.json'),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),naming.manifest):
                self.assertEqual(0,subject._run_naming(naming))
            old=models/'raw_a';old.mkdir();(old/'keep.bin').write_bytes(b'preserve existing output')
            before=_files(models)
            args=SimpleNamespace(models_dir=str(models),manifest=str(models/'manifest.json'),datasets_dir=str(root/'datasets'))
            with patch.object(batch_train,'extract_zip',side_effect=RuntimeError('fixture would stop before child launch')) as extract:
                self.assertIsNone(batch_train.train_one(str(root/'input.zip'),'dataset','raw_a',args))
            self.assertEqual(0,extract.call_count);self.assertFalse((root/'datasets').exists())
            self.assertEqual(before,_files(models))

    def test_subsequent_naming_from_stale_default_manifest_preserves_all_history(self):
        import json
        import sys
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from types import SimpleNamespace
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            default=models/'manifest.json';custom=root/'custom.json';custom.write_bytes(default.read_bytes())
            args=SimpleNamespace(manifest=str(custom),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),str(custom)):
                self.assertEqual(0,subject._run_naming(args))
            previous=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            custom_bytes=custom.read_bytes()
            stale=json.loads(default.read_bytes());self.assertEqual('raw_a',stale[0]['id'])
            stale[0]['voice_profile']='Gravelly bass in his 50s; best for mystery.'
            default.write_text(json.dumps(stale))
            with patch.object(sys,'argv',['name_voices.py','--models-dir',str(models),'--manifest',str(default),'--overwrite','--apply']):
                self.assertEqual(0,subject.main())
            final=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            self.assertNotEqual(previous,final);self.assertTrue((models/final).is_dir())
            self.assertEqual(str(models/final),get_resolved_adapter_path(str(models/previous)))
            saved=json.loads(default.read_bytes())
            self.assertEqual(final,saved[0]['id']);self.assertEqual({'raw_a',previous},set(saved[0]['previous_ids']))
            self.assertEqual(rows[0]['extra'],saved[0]['extra']);self.assertEqual(rows[2],saved[2])
            self.assertEqual(custom_bytes,custom.read_bytes());self.assertFalse((models/previous).exists());self.assertFalse((models/'raw_a').exists())

    def test_cached_suggestion_applies_historical_id_and_keeps_input_and_other_members(self):
        import json
        import tempfile
        from pathlib import Path
        from contextlib import ExitStack
        from unittest.mock import patch
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import core
        from routers import voices
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            args=SimpleNamespace(manifest=str(models/'manifest.json'),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),args.manifest):
                self.assertEqual(0,subject._run_naming(args))
            current=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            config=root/'voices.json';config.write_text(json.dumps({'Other':{'type':'custom','voice':'Ryan'}}))
            library=root/'library.json'
            other={'name':'Other','config':{'type':'lora','adapter_id':'raw_a'},'line_count':2}
            library.write_text(json.dumps({'shared':{},'casts':{'series':{'members':{'other':other}}}}))
            for module in (core,voices):
                for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('VOICE_CONFIG_PATH',str(config)),('VOICE_LIBRARY_PATH',str(library))):
                    stack.enter_context(patch.object(module,name,value))
            stack.enter_context(patch.object(voices,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(voices,'get_active_book_id',return_value='book'))
            stack.enter_context(patch.object(voices,'_script_line_counts',return_value={'Hero':5}))
            suggestion={'adapter_id':'raw_a','book_id':'book','character_style':'calm','reason':'original cached choice'}
            before=copy.deepcopy(suggestion)
            app=FastAPI();app.include_router(voices.router)
            with TestClient(app) as client:
                response=client.post('/api/suggest_voices/apply',json={'character':'Hero','suggestion':suggestion,'cast':'series'})
                self.assertEqual(200,response.status_code,response.text)
            result=response.json();self.assertEqual(['Hero'],result['applied'])
            self.assertEqual(2,result['adapter_usage'][current]['character_count'])
            self.assertEqual(7,result['adapter_usage'][current]['total_lines'])
            saved=json.loads(config.read_bytes())
            self.assertEqual(current,saved['Hero']['adapter_id']);self.assertEqual('lora_models/'+current,saved['Hero']['adapter_path'])
            self.assertEqual('calm',saved['Hero']['character_style']);self.assertEqual('original cached choice',saved['Hero']['persona_voice_audit']['suggestion_reason'])
            self.assertEqual({'type':'custom','voice':'Ryan'},saved['Other'])
            self.assertEqual(other,json.loads(library.read_bytes())['casts']['series']['members']['other'])
            self.assertEqual(before,suggestion)

    def test_suggestions_keep_existing_alias_assignment_and_count_fixed_aliases_in_one_snapshot(self):
        import json
        import tempfile
        from pathlib import Path
        from contextlib import ExitStack
        from unittest.mock import patch
        from types import SimpleNamespace
        from routers import voices
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            args=SimpleNamespace(manifest=str(models/'manifest.json'),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),args.manifest):
                self.assertEqual(0,subject._run_naming(args))
            current=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            script=root/'script.json';script.write_text(json.dumps([{'speaker':'Hero','text':'known line'},{'speaker':'Fixed','text':'another line'}]))
            config=root/'voices.json';config.write_text(json.dumps({'Fixed':{'type':'lora','adapter_id':'raw_a'}}));config_bytes=config.read_bytes()
            lib={'shared':{},'favorites':['raw_a'],'casts':{'series':{'members':{'hero':{'name':'Hero','config':{'adapter_id':'raw_a'},'line_count':3}}}}}
            before=copy.deepcopy(lib)
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('SCRIPT_PATH',str(script)),('VOICE_CONFIG_PATH',str(config))):
                stack.enter_context(patch.object(voices,name,value))
            stack.enter_context(patch.object(voices,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(voices,'_load_voice_library',return_value=lib))
            stack.enter_context(patch.object(voices,'get_active_book_id',return_value='book'))
            stack.enter_context(patch.object(voices,'_script_line_counts',return_value={'Hero':1,'Fixed':1}))
            stack.enter_context(patch.object(voices,'_make_llm_client',side_effect=RuntimeError('CPU fixture uses heuristic fallback')))
            import voice_manifest
            identity_reads=stack.enter_context(patch('voice_manifest.get_resolved_adapter_id_mapping',wraps=voice_manifest.get_resolved_adapter_id_mapping))
            result=voices._suggest_voices_impl(voices.SuggestVoicesRequest(cast='series',only_unset=True))
            self.assertEqual(current,result['suggestions']['Hero']['adapter_id'])
            self.assertEqual(2,result['suggestions']['Hero']['reuse_count_before'])
            self.assertEqual(2,result['suggestions']['Hero']['reuse_count_after'])
            self.assertEqual([current],result['favorites'])
            self.assertEqual(2,result['adapter_usage'][current]['character_count'])
            self.assertEqual(4,result['adapter_usage'][current]['total_lines'])
            self.assertEqual(before,lib);self.assertEqual(config_bytes,config.read_bytes())
            self.assertEqual(0,identity_reads.call_count,'A second identity snapshot was read')

    def test_cast_usage_combines_historical_and_current_ids_without_rewriting_library(self):
        import json
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        import core
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject
        from types import SimpleNamespace
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            args=SimpleNamespace(manifest=str(models/'manifest.json'),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),args.manifest):
                self.assertEqual(0,subject._run_naming(args))
            current=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            lib={'shared':{'narrator':{'name':'Narrator','config':{'adapter_id':'raw_a'},'line_count':3}},
                 'casts':{'series':{'members':{'hero':{'name':'Hero','config':{'adapter_id':current},'assignments':{'book':{'line_count':7}}},
                                             'other':{'name':'Other','config':{'adapter_id':'builtin'},'line_count':2}}}}}
            path=root/'library.json';path.write_text(json.dumps(lib));original=path.read_bytes();before=copy.deepcopy(lib)
            with patch.object(core,'LORA_MODELS_DIR',str(models)):
                usage=core.get_cast_adapter_usage(lib,'series')
                self.assertEqual({'character_count':2,'total_lines':10,'characters':['Narrator','Hero']},usage[current])
                self.assertNotIn('raw_a',usage)
                self.assertEqual(2,usage['builtin']['total_lines'])
            self.assertEqual(before,lib);self.assertEqual(original,path.read_bytes())

    def test_repeated_renames_resolve_every_previous_id_without_mutating_input(self):
        manifest=[{'id':'warm_baritone','previous_ids':['raw_20260930','earlier_name'],'extra':{'keep':1}},
                  {'id':'untouched','name':'Existing'}]
        before=copy.deepcopy(manifest)
        for old in ('raw_20260930','earlier_name','warm_baritone'):
            self.assertEqual('warm_baritone',get_resolved_adapter_id(old,manifest))
        self.assertEqual('untouched',get_resolved_adapter_id('untouched',manifest))
        self.assertEqual('unknown',get_resolved_adapter_id('unknown',manifest))
        self.assertEqual(before,manifest)

    def test_colliding_aliases_and_canonical_ids_refuse_instead_of_retargeting(self):
        for rows in ([{'id':'new','previous_ids':['old']},{'id':'old'}],
                     [{'id':'a','previous_ids':['old']},{'id':'b','previous_ids':['old']}],
                     [{'id':'a','previous_ids':['a']}],
                     [{'id':'a','previous_ids':['old','old']}]):
            with self.subTest(rows=rows),self.assertRaisesRegex(ValueError,'duplicate'):
                get_adapter_id_alias_map(rows)

    def test_malformed_or_traversing_aliases_refuse(self):
        for aliases in (None,'old',[None],['../outside'],['nested/path'],['nested\\path'],['.'],['..']):
            with self.subTest(aliases=aliases),self.assertRaises(ValueError):
                get_adapter_id_alias_map([{'id':'new','previous_ids':aliases}])
        self.assertEqual({'legacy':'legacy'},get_adapter_id_alias_map([{'id':'legacy'}]))

    def test_native_cli_publishes_and_reserves_alias_history_without_mutating_configs(self):
        import json
        from pathlib import Path
        import tempfile
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            rows[0]['previous_ids']=['warm_baritone_30s_m_fantasy']
            manifest=models/'manifest.json';manifest.write_text(json.dumps(rows))
            config=root/'voice_config.json';config.write_text(json.dumps({'ALICE':{'adapter_path':'lora_models/raw_a'}}))
            before=config.read_bytes()
            self.assertEqual(0,_invoke(models,'--apply'))
            named=json.loads(manifest.read_text())
            first=next(row for row in named if row.get('dataset_id')=='raw_a')
            self.assertNotEqual('warm_baritone_30s_m_fantasy',first['id'])
            self.assertEqual(['warm_baritone_30s_m_fantasy','raw_a'],first['previous_ids'])
            self.assertTrue((models/first['id']).is_dir())
            self.assertFalse((models/'raw_a').exists())
            first['voice_profile']='Silky soprano in her 20s; best for anime.'
            manifest.write_text(json.dumps(named))
            self.assertEqual(0,_invoke(models,'--apply','--overwrite'))
            again=json.loads(manifest.read_text())
            second=next(row for row in again if row.get('dataset_id')=='raw_a')
            self.assertEqual([*first['previous_ids'],first['id']],second['previous_ids'])
            for old in second['previous_ids']:
                self.assertEqual(second['id'],get_resolved_adapter_id(old,again))
            self.assertEqual(before,config.read_bytes())

    def test_public_naming_rejects_id_exchange_that_would_retarget_saved_voices(self):
        import json
        from pathlib import Path
        import tempfile
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_files
        from adapter_naming_transaction import apply_adapter_naming_locked,lock_adapter_naming
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp));before=_files(models)
            swapped=copy.deepcopy(rows)
            swapped[0]['id']='raw_b';swapped[1]['id']='raw_a'
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                with self.assertRaisesRegex(ValueError,'duplicate'):
                    apply_adapter_naming_locked(str(models),str(models/'manifest.json'),swapped,[('raw_a','raw_b'),('raw_b','raw_a')])
            self.assertEqual({**before,'manifest.json.lock':b''},_files(models))

    def test_native_cli_old_absolute_and_relative_config_paths_resolve_same_bundle(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        import tts
        from voice_manifest import get_resolved_adapter_path
        from adapter_publication import get_adapter_bundle_sha256
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            models.rename(root/'lora_models');models=root/'lora_models'
            old=models/'raw_a';digest=get_adapter_bundle_sha256(str(old))
            self.assertEqual(0,_invoke(models,'--apply'))
            resolved=get_resolved_adapter_path(str(old))
            self.assertNotEqual(str(old),resolved)
            self.assertEqual(digest,get_adapter_bundle_sha256(resolved))
            with patch.object(tts,'_get_runtime_data_dir',return_value=str(root)):
                relative=tts._resolve_asset_path('lora_models/raw_a')
                self.assertEqual(resolved,get_resolved_adapter_path(relative))
            self.assertFalse(old.exists())

    def test_custom_manifest_rename_publishes_root_alias_registry_for_runtime(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject
        from voice_manifest import get_resolved_adapter_path,get_adapter_alias_registry
        from adapter_naming_transaction import lock_adapter_naming
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            custom=root/'custom.json';(models/'manifest.json').rename(custom)
            args=SimpleNamespace(manifest=str(custom),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),str(custom)):
                self.assertEqual(0,subject._run_naming(args))
            named=json.loads(custom.read_text())
            renamed=next(row for row in named if row.get('dataset_id')=='raw_a')['id']
            self.assertEqual(str(models/renamed),get_resolved_adapter_path(str(models/'raw_a')))
            self.assertEqual(renamed,get_adapter_alias_registry(str(models))['raw_a'])
            self.assertFalse((models/'manifest.json').exists())

    def test_snapshot_resolves_previously_resolved_path_after_another_native_rename(self):
        import json
        from pathlib import Path
        import tempfile
        from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot,get_adapter_generation_sha256
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp))
            original=models/'raw_a';digest=get_adapter_generation_sha256(original)
            self.assertEqual(0,_invoke(models,'--apply'))
            stale=get_resolved_adapter_path(str(original))
            rows=json.loads((models/'manifest.json').read_text())
            next(row for row in rows if row['dataset_id']=='raw_a')['voice_profile']='Silky soprano in her 20s; best for anime.'
            (models/'manifest.json').write_text(json.dumps(rows))
            self.assertEqual(0,_invoke(models,'--apply','--overwrite'))
            self.assertFalse(Path(stale).exists())
            for persisted in (str(original),stale):
                with ensure_adapter_generation_snapshot(persisted) as (snapshot,generation):
                    self.assertEqual(digest,generation)
                    self.assertEqual(digest,get_adapter_generation_sha256(snapshot))

    def test_custom_first_publication_journal_fences_resolution_without_manifest_or_registry(self):
        from pathlib import Path
        import tempfile
        from adapter_checkpoint_transaction import ensure_adapter_checkpoint
        from adapter_publication import NAMING_PUBLICATION_JOURNAL
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/NAMING_PUBLICATION_JOURNAL).write_text('{}')
            for resolve in (lambda:get_resolved_adapter_path(str(root/'old')),
                            lambda:ensure_adapter_checkpoint(root/'old').__enter__()):
                with self.assertRaisesRegex(ValueError,'recovery.*required|recovery required'):
                    resolve()
            self.assertEqual('{}',(root/NAMING_PUBLICATION_JOURNAL).read_text())

    def test_alias_target_symlink_remains_rejected_by_snapshot_admission(self):
        import json
        from pathlib import Path
        import tempfile
        from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot
        from voice_manifest import ADAPTER_ID_ALIASES_FILE
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models=root/'models';models.mkdir();outside=root/'outside';outside.mkdir()
            (models/'new').symlink_to(outside,target_is_directory=True)
            (models/ADAPTER_ID_ALIASES_FILE).write_text(json.dumps({'version':1,'aliases':{'old':'new'}}))
            with self.assertRaisesRegex(ValueError,'Unsafe'):
                with ensure_adapter_generation_snapshot(models/'old'):
                    self.fail('symlink was admitted')

    def test_old_id_recovers_its_canonical_interrupted_checkpoint_before_snapshot(self):
        from pathlib import Path
        import tempfile
        from adapter_checkpoint_transaction import ensure_adapter_generation_snapshot,get_adapter_generation_sha256
        from tests.test_adapter_checkpoint_transaction import AdapterCheckpointTransactionTests,_crash_save
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp));old=models/'raw_a'
            digest=get_adapter_generation_sha256(old)
            self.assertEqual(0,_invoke(models,'--apply'))
            current=Path(get_resolved_adapter_path(str(old)))
            self.assertEqual(79,AdapterCheckpointTransactionTests().child(_crash_save,current,'replacement'))
            with ensure_adapter_generation_snapshot(old) as (snapshot,generation):
                self.assertEqual(digest,generation)
                self.assertEqual(digest,get_adapter_generation_sha256(snapshot))
            self.assertEqual(digest,get_adapter_generation_sha256(current))

    def test_waiting_snapshot_reads_rename_published_under_the_same_root_lock(self):
        import contextlib
        from pathlib import Path
        import tempfile
        import threading
        from unittest.mock import patch
        import adapter_checkpoint_transaction as checkpoint
        from adapter_naming_transaction import apply_adapter_naming_locked,lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp));old=models/'raw_a'
            digest=checkpoint.get_adapter_generation_sha256(old)
            waiting=threading.Event();results=[];errors=[]
            original_lock=checkpoint.file_lock
            @contextlib.contextmanager
            def observed_lock(*args,**kwargs):
                waiting.set()
                with original_lock(*args,**kwargs):
                    yield
            def capture():
                try:
                    with checkpoint.ensure_adapter_generation_snapshot(old) as (snapshot,generation):
                        results.append((generation,checkpoint.get_adapter_generation_sha256(snapshot)))
                except BaseException as error:
                    errors.append(error)
            renamed=copy.deepcopy(rows);renamed[0]['id']='new_current'
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                with patch.object(checkpoint,'file_lock',observed_lock):
                    reader=threading.Thread(target=capture);reader.start()
                    self.assertTrue(waiting.wait(5),'snapshot did not reach root admission')
                    self.assertEqual([],results)
                    apply_adapter_naming_locked(str(models),str(models/'manifest.json'),renamed,[('raw_a','new_current')])
            reader.join(10)
            self.assertFalse(reader.is_alive(),'snapshot reader did not finish')
            self.assertEqual([],errors)
            self.assertEqual([(digest,digest)],results)
            self.assertFalse(old.exists())

    def test_http_test_and_preview_publish_to_current_id_when_native_rename_happens_during_generation(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from urllib.parse import unquote
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import numpy as np
        import soundfile as sf
        from routers import lora
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        for route in ('test','preview'):
            with self.subTest(route=route),tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
                root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
                self.assertEqual(0,_invoke(models,'--apply'))
                stages=[]
                def generate(output_path,**kwargs):
                    stages.append(Path(output_path))
                    self.assertEqual(models,Path(output_path).parent.parent)
                    rows=json.loads((models/'manifest.json').read_text())
                    next(row for row in rows if row.get('dataset_id')=='raw_a')['voice_profile']='Silky soprano in her 20s; best for anime.'
                    (models/'manifest.json').write_text(json.dumps(rows))
                    self.assertEqual(0,_invoke(models,'--apply','--overwrite'))
                    sf.write(output_path,np.full(2400,.2),24000)
                    return True
                for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('project_manager',SimpleNamespace(get_engine=lambda:SimpleNamespace(generate_voice=generate)))):
                    stack.enter_context(patch.object(lora,name,value))
                stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
                check=stack.enter_context(patch.object(lora,'check_global_gpu_lock'))
                claim=stack.enter_context(patch.object(lora,'claim_gpu_task',return_value='fixture-claim'))
                release=stack.enter_context(patch.object(lora,'release_gpu_task_claim'))
                app=FastAPI();app.include_router(lora.router)
                with TestClient(app) as client:
                    response=(client.post('/api/lora/test',json={'adapter_id':'raw_a','text':'known line'}) if route=='test'
                              else client.post('/api/lora/preview/raw_a'))
                    self.assertEqual(200,response.status_code,response.text)
                    url=response.json()['audio_url'];relative=unquote(url.removeprefix('/lora_models/'))
                    published=models/relative
                    self.assertTrue(published.is_file(),url)
                    pcm,rate=sf.read(published);self.assertEqual(24000,rate)
                    np.testing.assert_allclose(pcm,.2,atol=4e-5)
                    self.assertFalse(stages[0].exists())
                    check.assert_called_once_with('lora_test');claim.assert_called_once_with('lora_test')
                    release.assert_called_once_with('lora_test','fixture-claim')
                    if route=='preview':
                        cached=client.post('/api/lora/preview/raw_a')
                        self.assertEqual(200,cached.status_code,cached.text)
                        self.assertEqual('cached',cached.json()['status']);self.assertEqual(url,cached.json()['audio_url'])
                        self.assertEqual(1,len(stages));claim.assert_called_once()

    def test_clone_prompt_reads_saved_old_adapter_reference_without_changing_config(self):
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        import numpy as np
        import soundfile as sf
        import tts
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from tests.test_lora_generation_cache import _write
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp));_write(models/'raw_a')
            config={'ANN':{'ref_audio':str(models/'raw_a'/'ref_sample.wav'),'ref_text':'reference generation 1'}}
            before=copy.deepcopy(config);reference=(models/'raw_a'/'ref_sample.wav').read_bytes()
            self.assertEqual(0,_invoke(models,'--apply'))
            captured=[]
            def prompt(**kwargs):
                captured.append(kwargs);return object()
            engine=tts.TTSEngine({'tts':{'mode':'local'}})
            with patch.object(engine,'_init_local_clone',return_value=SimpleNamespace(create_voice_clone_prompt=prompt)):
                first=engine._get_clone_prompt('ANN',config)
                self.assertIs(first,engine._get_clone_prompt('ANN',config))
            self.assertEqual(before,config);self.assertEqual(1,len(captured))
            audio,rate=captured[0]['ref_audio'];self.assertEqual(24000,rate)
            np.testing.assert_allclose(audio,.05,atol=4e-5)
            self.assertEqual('reference generation 1',captured[0]['ref_text'])
            from voice_manifest import get_adapter_asset_snapshot
            _path,snapshot=get_adapter_asset_snapshot(config['ANN']['ref_audio'])
            self.assertEqual(reference,snapshot)

    def test_management_promote_rollback_and_backup_deletion_resolve_native_renamed_id(self):
        import json
        from pathlib import Path
        import tempfile
        from routers import lora
        from tests.test_lora_candidate_promotion import LoraCandidatePromotionTests,_promotion_fixture
        from tests.test_adapter_naming_transaction import _invoke
        from voice_manifest import get_resolved_adapter_path
        for final in ('rollback','delete-backup'):
            with self.subTest(final=final),tempfile.TemporaryDirectory() as tmp:
                models=Path(tmp)/'models'
                adapter,candidate,manifest,rows=_promotion_fixture(LoraCandidatePromotionTests(),models)
                before=(adapter/'adapter_model.safetensors').read_bytes()
                promoted=(candidate/'adapter_model.safetensors').read_bytes()
                rows[0]['voice_profile']='Warm baritone in his 30s; best for fantasy.'
                manifest.write_text(json.dumps(rows));self.assertEqual(0,_invoke(models,'--apply'))
                current=Path(get_resolved_adapter_path(str(adapter)))
                result=lora._promote_lora_candidate('voice',str(models),str(manifest))
                self.assertEqual('promoted',result['status'])
                self.assertEqual(promoted,(current/'adapter_model.safetensors').read_bytes())
                backup=current/'promotion_backups'/result['backup_id'];self.assertTrue(backup.is_dir())
                if final=='rollback':
                    self.assertEqual('rolled_back',lora._rollback_lora_promotion('voice',str(models),str(manifest))['status'])
                    self.assertEqual(before,(current/'adapter_model.safetensors').read_bytes())
                else:
                    result=lora._delete_rollback_backup('voice',str(models),str(manifest))
                    self.assertEqual(current.name,result['adapter_id'])
                    self.assertEqual(promoted,(current/'adapter_model.safetensors').read_bytes())
                self.assertFalse(backup.exists())

    def test_native_renamed_comparison_and_deletion_reach_the_original_bundle(self):
        import json
        from pathlib import Path
        import tempfile
        from routers import lora
        from adapter_naming_transaction import lock_adapter_naming,apply_adapter_naming_locked
        from tests.test_adapter_deletion_transaction import AdapterDeletionTransactionTests,tree_bytes
        from tests.test_lora_candidate_promotion import LoraCandidatePromotionTests
        with tempfile.TemporaryDirectory() as tmp:
            models,manifest=LoraCandidatePromotionTests()._write_comparison_fixture(tmp)
            rows=json.loads(manifest.read_text());rows[0]['id']='new_voice'
            with lock_adapter_naming(str(models),str(manifest)):
                apply_adapter_naming_locked(str(models),str(manifest),rows,[('voice','new_voice')])
            result=lora._get_lora_candidate_comparison('voice',str(models),str(manifest))
            self.assertEqual('new_voice',result['adapter_id'])
            self.assertIn('/new_voice/',json.dumps(result))
        case=AdapterDeletionTransactionTests();case.setUp()
        try:
            rows=json.loads(case.manifest.read_text());rows[0]['id']='new_voice'
            with lock_adapter_naming(str(case.root),str(case.manifest)):
                apply_adapter_naming_locked(str(case.root),str(case.manifest),rows,[('voice','new_voice')])
            response=case.invoke_route();self.assertEqual('new_voice',response['adapter_id'])
            self.assertFalse((case.root/'new_voice').exists())
            self.assertEqual(case.before_other,tree_bytes(case.other))
            self.assertEqual(['other'],[row['id'] for row in json.loads(case.manifest.read_text())])
        finally:
            case.doCleanups()

    def test_existing_blind_session_streams_same_nested_probe_after_native_rename(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import lora
        from adapter_naming_transaction import apply_adapter_naming_locked,lock_adapter_naming
        from tests.test_lora_review_integration import _build_evaluated_adapter
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models=root/'models';models.mkdir();reviews=root/'reviews'
            adapter,candidate,manifest=_build_evaluated_adapter(str(models))
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',manifest),('EVALUATION_REVIEWS_DIR',str(reviews))):
                stack.enter_context(patch.object(lora,name,value))
            session=lora._open_review_session('voice')
            payload=json.dumps(session);self.assertNotIn('/candidates/',payload);self.assertNotIn(str(models),payload)
            import evaluation_reviews
            paths={label:evaluation_reviews.get_session_audio_path(str(reviews),session['session_id'],label,'probe_0',adapter_id='voice') for label in ('A','B')}
            expected={label:Path(path).read_bytes() for label,path in paths.items()}
            rows=json.loads(Path(manifest).read_text());rows[0]['id']='new_voice'
            with lock_adapter_naming(str(models),manifest):
                apply_adapter_naming_locked(str(models),manifest,rows,[('voice','new_voice')])
            self.assertFalse(Path(adapter).exists())
            app=FastAPI();app.include_router(lora.router);snapshots=[]
            native=lora._serve_review_audio
            def capture(*args):
                response=native(*args);snapshots.append(Path(response.path));return response
            with patch.object(lora,'_serve_review_audio',side_effect=capture),TestClient(app) as client:
                for label in ('A','B'):
                    response=client.get(session['pairs'][0][label]['audio_url'])
                    self.assertEqual(200,response.status_code,response.text)
                    self.assertEqual(expected[label],response.content)
                    self.assertEqual('audio/wav',response.headers['content-type'])
                    self.assertFalse(snapshots[-1].exists(),'snapshot leaked after streaming')
                result=client.post('/api/lora/models/voice/review/session/'+session['session_id'],json={'choice':'A','rating':4})
                self.assertEqual(200,result.status_code,result.text)
                listed=client.get('/api/lora/models/voice/reviews')
                self.assertEqual(1,len(listed.json()['reviews']))

    def test_review_audio_snapshot_is_cleaned_when_response_send_fails(self):
        import asyncio
        from pathlib import Path
        import tempfile
        from routers import lora
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'probe.wav';path.write_bytes(b'owned probe bytes')
            response=lora._ReviewAudioSnapshotResponse(str(path),media_type='audio/wav')
            async def send(message):
                raise RuntimeError('client disconnected')
            async def receive():
                return {'type':'http.disconnect'}
            with self.assertRaisesRegex(RuntimeError,'disconnected'):
                asyncio.run(response({'type':'http','method':'GET','headers':[]},receive,send))
            self.assertFalse(path.exists())

    def test_historical_static_urls_redirect_to_exact_nested_bytes_and_keep_native_head(self):
        from pathlib import Path
        import tempfile
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from adapter_static import AdapterStaticFiles
        import app as app_module
        mount=next(route for route in app_module.app.routes if getattr(route,'name',None)=='lora_models')
        self.assertIsInstance(mount.app,AdapterStaticFiles)
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp))
            filename='probe #100%?.wav';source=models/'raw_a'/'keep'/filename
            source.write_bytes(b'exact nested probe bytes');expected=source.read_bytes()
            self.assertEqual(0,_invoke(models,'--apply'))
            app=FastAPI();app.mount('/lora_models',AdapterStaticFiles(directory=str(models)))
            with TestClient(app) as client:
                url='/lora_models/raw_a/keep/probe%20%23100%25%3F.wav?test=one%20two'
                redirect=client.get(url,follow_redirects=False)
                self.assertEqual(307,redirect.status_code,redirect.text)
                location=redirect.headers['location'];self.assertIn('probe%20%23100%25%3F.wav',location)
                self.assertTrue(location.endswith('?test=one%20two'))
                response=client.get(url);self.assertEqual(200,response.status_code,response.text)
                self.assertEqual(expected,response.content)
                head=client.head(url);self.assertEqual(200,head.status_code)
                self.assertEqual(b'',head.content);self.assertEqual(str(len(expected)),head.headers['content-length'])
                for invalid in ('/lora_models/raw_a/keep/missing.wav','/lora_models/raw_a/../../outside','/lora_models/%2E%2E/outside'):
                    self.assertEqual(404,client.get(invalid).status_code,invalid)
                from adapter_publication import NAMING_PUBLICATION_JOURNAL
                journal=models/NAMING_PUBLICATION_JOURNAL;journal.write_text('{}')
                self.assertEqual(409,client.get(url).status_code)
                self.assertEqual('{}',journal.read_text())

    def test_current_and_historical_review_routes_share_bounded_history_summary_and_cleanup(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import evaluation_reviews as reviews_module
        from routers import lora
        from adapter_naming_transaction import apply_adapter_naming_locked,lock_adapter_naming
        from tests.test_lora_review_integration import _build_evaluated_adapter
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models=root/'models';models.mkdir();reviews=root/'reviews'
            adapter,candidate,manifest=_build_evaluated_adapter(str(models))
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',manifest),('EVALUATION_REVIEWS_DIR',str(reviews))):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(lora,'_load_voice_library',return_value={}))
            comparison,production,candidate_result=lora._load_candidate_comparison_full('voice',str(models),manifest)
            fingerprint=reviews_module.evidence_fingerprint(production,candidate_result)
            def record(identity,choice):
                session=reviews_module.create_session(str(reviews),identity,'cand1',fingerprint,comparison['probe_pairs'],{},blind=False)
                return reviews_module.submit(str(reviews),identity,session['session_id'],choice,fingerprint)['review_id']
            original=[record('voice','A') for _ in range(30)]
            unrelated=record('other','tie');other_before=(reviews/'other.json').read_bytes()
            rows=json.loads(Path(manifest).read_text());rows[0]['id']='new_voice'
            with lock_adapter_naming(str(models),manifest):
                apply_adapter_naming_locked(str(models),manifest,rows,[('voice','new_voice')])
            current=[record('new_voice','tie') for _ in range(30)]
            expected=list(reversed([*original,*current]))[:reviews_module.MAX_REVIEWS]
            app=FastAPI();app.include_router(lora.router)
            with TestClient(app) as client:
                for identity in ('voice','new_voice'):
                    response=client.get('/api/lora/models/'+identity+'/reviews')
                    self.assertEqual(200,response.status_code,response.text)
                    self.assertEqual(expected,[row['id'] for row in response.json()['reviews']])
                models_response=client.get('/api/lora/models');self.assertEqual(200,models_response.status_code,models_response.text)
                summary=models_response.json()[0]['review_summary']
                self.assertEqual(50,summary['count']);self.assertEqual(30,summary['tie']);self.assertEqual(20,summary['preferred_production'])
                before_bytes=sum(path.stat().st_size for path in (reviews/'voice.json',reviews/'new_voice.json'))
                cleanup=client.post('/api/lora/models/new_voice/reviews/cleanup')
                self.assertEqual(200,cleanup.status_code,cleanup.text)
                self.assertEqual({'removed_count':60,'freed_bytes':before_bytes},cleanup.json())
                self.assertEqual(other_before,(reviews/'other.json').read_bytes())
                self.assertEqual([unrelated],[row['id'] for row in reviews_module.list_reviews(str(reviews),'other')])
                for identity in ('voice','new_voice'):
                    self.assertEqual([],client.get('/api/lora/models/'+identity+'/reviews').json()['reviews'])
                self.assertEqual(400,client.get('/api/lora/models/bad%5Cname/reviews').status_code)
                (models/'adapter_id_aliases.json').write_text('{broken')
                self.assertEqual(409,client.get('/api/lora/models/new_voice/reviews').status_code)

    def test_deleted_alias_target_cannot_be_reused_by_another_voice(self):
        import json
        from pathlib import Path
        import tempfile
        from adapter_naming_transaction import apply_adapter_deletion_locked,apply_adapter_naming_locked,lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke,_files
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp))
            self.assertEqual(0,_invoke(models,'--apply'))
            named=json.loads((models/'manifest.json').read_text())
            retired=next(row['id'] for row in named if row.get('dataset_id')=='raw_a')
            other=next(row['id'] for row in named if row.get('dataset_id')=='raw_b')
            remaining=[row for row in named if row['id']!=retired]
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                apply_adapter_deletion_locked(str(models),str(models/'manifest.json'),remaining,retired)
            self.assertFalse((models/retired).exists());before=_files(models)
            reused=copy.deepcopy(remaining);next(row for row in reused if row['id']==other)['id']=retired
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                with self.assertRaisesRegex(ValueError,'historical'):
                    apply_adapter_naming_locked(str(models),str(models/'manifest.json'),reused,[(other,retired)])
            self.assertEqual(before,_files(models))
            remaining[0]['voice_profile']='Warm baritone in his 30s; best for fantasy.'
            (models/'manifest.json').write_text(json.dumps(remaining))
            self.assertEqual(0,_invoke(models,'--apply','--overwrite'))
            new=next(row['id'] for row in json.loads((models/'manifest.json').read_text()) if row.get('dataset_id')=='raw_b')
            self.assertNotEqual(retired,new)
            self.assertTrue((models/new).is_dir())
            self.assertEqual(str(models/retired),get_resolved_adapter_path(str(models/'raw_a')))
            self.assertFalse((models/retired).exists(),'old saved voice was retargeted to a different bundle')

    def test_saved_favorites_follow_native_rename_and_toggle_through_either_id(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import core
        from routers import lora,voice_library
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from voice_manifest import get_resolved_adapter_path,get_resolved_adapter_ids
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            library=root/'library.json';library.write_text(json.dumps({'shared':{},'casts':{},'favorites':['raw_a','builtin_keep','untouched']}))
            original=library.read_bytes();self.assertEqual(0,_invoke(models,'--apply'))
            current=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            for module in (lora,voice_library):
                stack.enter_context(patch.object(module,'LORA_MODELS_DIR',str(models)))
            stack.enter_context(patch.object(lora,'LORA_MODELS_MANIFEST',str(models/'manifest.json')))
            stack.enter_context(patch.object(lora,'EVALUATION_REVIEWS_DIR',str(root/'reviews')))
            for module in (core,voice_library):
                stack.enter_context(patch.object(module,'VOICE_LIBRARY_PATH',str(library)))
            stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
            self.assertEqual({current,'builtin_keep','untouched'},get_resolved_adapter_ids(str(models),['raw_a','builtin_keep','untouched']))
            self.assertEqual(original,library.read_bytes(),'reading favorites rewrote user data')
            app=FastAPI();app.include_router(lora.router);app.include_router(voice_library.router)
            with TestClient(app) as client:
                response=client.get('/api/lora/models');self.assertEqual(200,response.status_code,response.text)
                chosen=next(row for row in response.json() if row['id']==current)
                self.assertTrue(chosen['favorite']);self.assertEqual(original,library.read_bytes())
                for identity,expected in ((current,False),('raw_a',True),('raw_a',False),(current,True)):
                    response=client.post('/api/voice_library/favorites/'+identity)
                    self.assertEqual(200,response.status_code,response.text)
                    self.assertEqual(expected,response.json()['favorite'])
                    stored=json.loads(library.read_text())['favorites']
                    self.assertEqual({'builtin_keep','untouched',*([current] if expected else [])},set(stored))
                    self.assertNotIn('raw_a',stored)

    def test_training_refuses_old_and_deleted_identity_before_device_or_checkpoint_work(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        import train_lora
        from voice_manifest import get_resolved_adapter_path,validate_adapter_training_output
        from adapter_naming_transaction import apply_adapter_deletion_locked,lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke,_files
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp));old=models/'raw_a'
            self.assertEqual(0,_invoke(models,'--apply'))
            current=Path(get_resolved_adapter_path(str(old)));before=_files(models)
            validate_adapter_training_output(current)
            with patch.object(train_lora,'resolve_device') as device,patch.object(train_lora,'save_adapter_checkpoint') as save:
                with self.assertRaisesRegex(ValueError,'historical'):
                    train_lora.train(SimpleNamespace(output_dir=str(old)))
                with self.assertRaisesRegex(ValueError,'historical'):
                    train_lora.save_training_checkpoint(None,str(old),'unused','unused',{},[],0,1,4.5)
                device.assert_not_called();save.assert_not_called()
            self.assertEqual(before,_files(models))
            remaining=[row for row in json.loads((models/'manifest.json').read_text()) if row['id']!=current.name]
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                apply_adapter_deletion_locked(str(models),str(models/'manifest.json'),remaining,current.name)
            before=_files(models)
            with self.assertRaisesRegex(ValueError,'historical'):
                validate_adapter_training_output(current)
            self.assertEqual(before,_files(models));self.assertFalse(current.exists())
            validate_adapter_training_output(models/'genuinely_new')

    def test_checkpoint_save_uses_admitted_canonical_directory_for_historical_id(self):
        from pathlib import Path
        import tempfile
        from adapter_checkpoint_transaction import save_adapter_checkpoint,validate_adapter_checkpoint_generation
        from voice_manifest import get_resolved_adapter_path
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke
        from tests.test_lora_generation_cache import _write
        with tempfile.TemporaryDirectory() as tmp:
            models,rows=NamingTransactionTests().fixture(Path(tmp));old=models/'raw_a';_write(old)
            auxiliary=(old/'keep'/'probe.txt').read_bytes()
            self.assertEqual(0,_invoke(models,'--apply'))
            current=Path(get_resolved_adapter_path(str(old)))
            save_adapter_checkpoint(old,lambda stage:_write(stage,2))
            self.assertFalse(old.exists(),'checkpoint publication resurrected the historical directory')
            self.assertEqual('reference generation 2',validate_adapter_checkpoint_generation(current)['ref_sample_text'])
            self.assertEqual(auxiliary,(current/'keep'/'probe.txt').read_bytes())

    def test_ui_training_refuses_historical_and_deleted_registration_ids_before_scheduling(self):
        import asyncio
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import BackgroundTasks,HTTPException
        from routers import lora
        from adapter_naming_transaction import apply_adapter_deletion_locked,lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke,_files
        from voice_manifest import get_resolved_adapter_path
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root);datasets=root/'datasets';(datasets/'book').mkdir(parents=True)
            self.assertEqual(0,_invoke(models,'--apply'));current=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            remaining=[row for row in json.loads((models/'manifest.json').read_text()) if row['id']!=current]
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                apply_adapter_deletion_locked(str(models),str(models/'manifest.json'),remaining,current)
            sentinel=object();manager=SimpleNamespace(engine=sentinel)
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('LORA_DATASETS_DIR',str(datasets)),('project_manager',manager)):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'check_global_gpu_lock'))
            scheduled=stack.enter_context(patch.object(lora,'schedule_claimed_background_task'))
            worker=stack.enter_context(patch.object(lora,'run_process'))
            before=_files(models)
            for identity in ('raw_a',current,'stable'):
                with patch.object(lora,'get_unique_id',return_value=identity),self.assertRaises(HTTPException) as rejected:
                    asyncio.run(lora.lora_start_training(lora.LoraTrainingRequest(name='New',dataset_id='book'),BackgroundTasks()))
                self.assertEqual(409,rejected.exception.status_code)
                self.assertEqual(before,_files(models));self.assertIs(sentinel,manager.engine)
            scheduled.assert_not_called();worker.assert_not_called()

    def test_ui_reserves_new_directory_against_naming_then_registers_worker_result(self):
        import asyncio
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import BackgroundTasks
        from routers import lora
        from adapter_naming_transaction import apply_adapter_naming_locked,lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root);datasets=root/'datasets';(datasets/'book').mkdir(parents=True)
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('LORA_DATASETS_DIR',str(datasets)),('project_manager',SimpleNamespace(engine=None))):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'check_global_gpu_lock'));stack.enter_context(patch.object(lora,'get_unique_id',return_value='reserved_new'))
            callbacks=[]
            stack.enter_context(patch.object(lora,'schedule_claimed_background_task',side_effect=lambda background,task,callback:callbacks.append(callback)))
            state=copy.deepcopy(lora.process_state);state['lora_training']['cancel']=False
            stack.enter_context(patch.object(lora,'process_state',state))
            def worker(command,task):
                self.assertTrue((models/'reserved_new').is_dir())
                (models/'reserved_new'/'training_meta.json').write_text(json.dumps({'epochs':1,'num_samples':2}))
                return 0
            stack.enter_context(patch.object(lora,'run_process',side_effect=worker))
            response=asyncio.run(lora.lora_start_training(lora.LoraTrainingRequest(name='New',dataset_id='book'),BackgroundTasks()))
            self.assertEqual('reserved_new',response['adapter_id']);self.assertTrue((models/'reserved_new').is_dir())
            renamed=copy.deepcopy(rows);renamed[2]['id']='reserved_new'
            with lock_adapter_naming(str(models),str(models/'manifest.json')):
                with self.assertRaisesRegex(ValueError,'occupied'):
                    apply_adapter_naming_locked(str(models),str(models/'manifest.json'),renamed,[('stable','reserved_new')])
            self.assertTrue((models/'stable').is_dir());self.assertEqual(rows,json.loads((models/'manifest.json').read_text()))
            callbacks[0]()
            saved=json.loads((models/'manifest.json').read_text());self.assertEqual(1,sum(row['id']=='reserved_new' for row in saved))
            self.assertEqual('book',next(row['dataset_id'] for row in saved if row['id']=='reserved_new'))

    def test_batch_historical_id_refuses_before_extraction_or_child_start(self):
        import importlib.util
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from tests.test_adapter_naming_transaction import NamingTransactionTests,_invoke,_files
        module_path=Path(__file__).resolve().parents[2]/'tools'/'voice_lab'/'batch_train_lora.py'
        spec=importlib.util.spec_from_file_location('registration_batch_fixture',module_path);batch=importlib.util.module_from_spec(spec);spec.loader.exec_module(batch)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            self.assertEqual(0,_invoke(models,'--apply'));before=_files(models)
            args=SimpleNamespace(models_dir=str(models),manifest=str(models/'manifest.json'),datasets_dir=str(root/'datasets'))
            with patch.object(batch,'extract_zip') as extract,patch.object(batch.subprocess,'Popen') as child:
                self.assertIsNone(batch.train_one(str(root/'unused.zip'),'book','raw_a',args))
                extract.assert_not_called();child.assert_not_called()
            self.assertEqual(before,_files(models));self.assertFalse((root/'datasets').exists())

    def test_custom_first_registry_adoption_preserves_unrelated_default_manifest_history(self):
        import json
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject,_files
        from voice_manifest import get_resolved_adapter_path
        for conflicting in (False,True):
            with self.subTest(conflicting=conflicting),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
                (models/'raw_a').rename(models/'legacy_current')
                rows[0]['id']='legacy_current';rows[0]['previous_ids']=['ancient']
                manifest=models/'manifest.json';manifest.write_text(json.dumps(rows));original=manifest.read_bytes()
                self.assertEqual(str(models/'legacy_current'),get_resolved_adapter_path(str(models/'ancient')))
                custom=root/'custom.json';selected=copy.deepcopy(rows[1])
                if conflicting:selected['previous_ids']=['ancient']
                custom.write_text(json.dumps([selected]));before=_files(models)
                args=SimpleNamespace(manifest=str(custom),models_dir=str(models),verify=False,overwrite=False,apply=True)
                with lock_adapter_naming(str(models),str(custom)):
                    if conflicting:
                        with self.assertRaisesRegex(ValueError,'Conflicting historical'):
                            subject._run_naming(args)
                    else:
                        self.assertEqual(0,subject._run_naming(args))
                self.assertEqual(original,manifest.read_bytes())
                self.assertEqual(str(models/'legacy_current'),get_resolved_adapter_path(str(models/'ancient')))
                if conflicting:self.assertEqual(before,_files(models))
                else:self.assertNotEqual(str(models/'raw_b'),get_resolved_adapter_path(str(models/'raw_b')))

    def test_default_runtime_metadata_and_cpu_test_render_survive_custom_manifest_rename(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from urllib.parse import unquote
        import numpy as np
        import soundfile as sf
        import tts
        from routers import lora,voices
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests,subject
        from tests.test_lora_generation_cache import _write,LoraGenerationCacheTests
        from voice_manifest import get_resolved_adapter_path,get_resolved_adapter_manifest_rows_locked
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root);_write(models/'raw_a')
            default=models/'manifest.json';original=default.read_bytes();custom=root/'custom.json';custom.write_bytes(original)
            args=SimpleNamespace(manifest=str(custom),models_dir=str(models),verify=False,overwrite=False,apply=True)
            with lock_adapter_naming(str(models),str(custom)):
                self.assertEqual(0,subject._run_naming(args))
            current=Path(get_resolved_adapter_path(str(models/'raw_a'))).name
            before=copy.deepcopy(rows)
            with lock_adapter_naming(str(models),str(default)):
                normalized=get_resolved_adapter_manifest_rows_locked(str(models),rows)
            self.assertEqual(before,rows);self.assertEqual(current,normalized[0]['id'])
            self.assertEqual(original,default.read_bytes());self.assertNotEqual('raw_a',current)
            for module in (lora,voices):
                stack.enter_context(patch.object(module,'LORA_MODELS_DIR',str(models)))
                stack.enter_context(patch.object(module,'LORA_MODELS_MANIFEST',str(default)))
                stack.enter_context(patch.object(module,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(lora,'EVALUATION_REVIEWS_DIR',str(root/'reviews')))
            stack.enter_context(patch.object(lora,'_load_voice_library',return_value={}))
            engine=tts.TTSEngine({'tts':{'mode':'local'}});loads=[];prompts=[]
            stack.enter_context(LoraGenerationCacheTests().providers(engine,loads,prompts))
            stack.enter_context(patch.object(lora,'project_manager',SimpleNamespace(get_engine=lambda:engine)))
            for name in ('check_global_gpu_lock','claim_gpu_task','release_gpu_task_claim'):
                stack.enter_context(patch.object(lora,name))
            app=FastAPI();app.include_router(lora.router)
            with TestClient(app) as client:
                result=client.post('/api/lora/test',json={'adapter_id':'raw_a','text':'known line'})
                self.assertEqual(200,result.status_code,result.text)
                output=models/unquote(result.json()['audio_url'].removeprefix('/lora_models/'))
                audio,rate=sf.read(output);self.assertEqual(24000,rate)
                np.testing.assert_allclose(audio,.0425,atol=4e-5)
                self.assertEqual('reference generation 1',prompts[0][0]);self.assertEqual(1,len(loads))
                listed=client.get('/api/lora/models');self.assertEqual(200,listed.status_code,listed.text)
                self.assertEqual(current,listed.json()[0]['id'])
                candidates=voices._build_lora_candidates();self.assertEqual(current,candidates[0]['adapter_id'])
            self.assertEqual(original,default.read_bytes());self.assertFalse((models/'raw_a').exists())

    def test_model_status_remains_readable_during_pending_checkpoint_recovery(self):
        import json
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import lora
        from adapter_checkpoint_transaction import get_adapter_checkpoint_journal
        from tests.test_adapter_naming_transaction import NamingTransactionTests
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            marker=get_adapter_checkpoint_journal(models/'raw_a');marker.write_text(json.dumps({'adapter':'raw_a'}));original=marker.read_bytes()
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('EVALUATION_REVIEWS_DIR',str(root/'reviews'))):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(lora,'_load_voice_library',return_value={}))
            app=FastAPI();app.include_router(lora.router)
            with TestClient(app) as client:
                result=client.get('/api/lora/models');self.assertEqual(200,result.status_code,result.text)
                model=next(row for row in result.json() if row['id']=='raw_a')
                self.assertEqual({'status':'recovery_required','operation':'checkpoint_generation'},model['checkpoint_swap'])
                self.assertEqual(200,client.get('/api/lora/models/raw_a/reviews').status_code)
            self.assertEqual(original,marker.read_bytes(),'status reader attempted recovery or consumed its evidence')

    def test_model_list_lock_wait_keeps_event_loop_responsive(self):
        import asyncio
        from contextlib import ExitStack
        from pathlib import Path
        import tempfile
        import threading
        from unittest.mock import patch
        from routers import lora
        from adapter_naming_transaction import lock_adapter_naming
        from tests.test_adapter_naming_transaction import NamingTransactionTests
        with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
            root=Path(tmp);models,rows=NamingTransactionTests().fixture(root)
            for name,value in (('LORA_MODELS_DIR',str(models)),('LORA_MODELS_MANIFEST',str(models/'manifest.json')),('EVALUATION_REVIEWS_DIR',str(root/'reviews'))):
                stack.enter_context(patch.object(lora,name,value))
            stack.enter_context(patch.object(lora,'_load_builtin_lora_manifest',return_value=[]))
            stack.enter_context(patch.object(lora,'_load_voice_library',return_value={}))
            waiting=threading.Event();native=lora.get_adapter_manifest_rows
            def admitted(*args):
                waiting.set();return native(*args)
            stack.enter_context(patch.object(lora,'get_adapter_manifest_rows',side_effect=admitted))
            async def exercise():
                with lock_adapter_naming(str(models),str(models/'manifest.json')):
                    task=asyncio.create_task(lora.lora_list_models())
                    self.assertTrue(await asyncio.to_thread(waiting.wait,2))
                    ticks=[]
                    async def heartbeat():
                        for _ in range(3):
                            await asyncio.sleep(.01);ticks.append(True)
                    await asyncio.wait_for(heartbeat(),1)
                    self.assertEqual(3,len(ticks));self.assertFalse(task.done())
                result=await asyncio.wait_for(task,3)
                self.assertEqual(['raw_a','raw_b','stable'],[row['id'] for row in result])
            asyncio.run(exercise())
