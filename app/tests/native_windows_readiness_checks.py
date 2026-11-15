"""Native CPU portability regressions; every selected test must run without skips.

This is separate from full release verification and real-symlink qualification.
Run from app with the application CPU test dependencies installed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback
import unittest

SUITES = (
    'tests.test_native_windows_readiness_runner.NativePathSpellingTests',
    'tests.test_promotion_publication_transaction',
    'tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_hard_exit_before_commit_restores_original_after_commit_keeps_new_on_fresh_cli',
    'tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_interrupted_recovery_resumes_after_each_native_restore_and_terminal_cleanup',
    'tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_corrupt_original_and_unowned_journal_paths_refuse_without_mutation',
    'tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_shared_family_admission_refuses_pending_cli_or_legacy_swap',
    'tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_hard_exit_recovers_both_artifacts_before_commit_and_keeps_both_after_commit',
    'tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_manifest_and_adapter_recovery_remain_repeatable_after_recovery_hard_exit',
    'tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_changed_live_manifest_or_corrupt_snapshot_refuses_recovery_before_adapter_moves',
    'tests.test_dataset_delta_queue_http',
    'tests.test_cors_auth_order',
    'tests.test_fidelity_seed_completion',
    'tests.test_tts_vram_cli',
    'tests.test_compare_terminal_text',
    'tests.test_run_history',
    'tests.test_voice_dataset_merge',
    'tests.test_reference_provenance',
    'tests.test_pitch_profile_matrix',
    'tests.test_goals_evidence_paths',
    'tests.test_morning_chain.MorningChainTests.test_chain_preserves_cwd_env_nonzero_and_completed_process',
    'tests.test_morning_chain.MorningChainTests.test_git_replays_decoded_stderr_on_success_and_before_nonzero_error',
    'tests.test_benchmark_payload_transport.BenchmarkPayloadTransportTests.test_all_local_dispatchers_decode_large_payload_in_native_workers',
    'tests.test_gpu_pause_status_batch.PauseStatusBatchTests.test_nvidia_memory_query_uses_visible_selector_and_failure_does_not_report_number',
    'tests.test_adapter_id_aliases.AdapterIdAliasTests.test_historical_static_urls_redirect_to_exact_nested_bytes_and_keep_native_head',
    'tests.test_adapter_id_aliases.AdapterIdAliasTests.test_old_id_recovers_its_canonical_interrupted_checkpoint_before_snapshot',
    'tests.test_adapter_id_aliases.AdapterIdAliasTests.test_public_naming_rejects_id_exchange_that_would_retarget_saved_voices',
)


SYMLINK_SUITES = (
    'tests.test_adapter_checkpoint_transaction.AdapterCheckpointTransactionTests.test_failed_or_incomplete_staging_preserves_original_bundle_bytes',
    'tests.test_adapter_checkpoint_transaction.CheckpointGenerationFamilyAdmissionTests.test_pending_generation_fences_other_training_http_cli_and_naming_without_mutation',
    'tests.test_adapter_checkpoint_transaction.CheckpointManifestTransactionTests.test_broken_own_generation_journal_is_not_treated_as_absent',
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json-report', type=Path)
    parser.add_argument('--suite-group', choices=('portable', 'symlink'), default='portable')
    args = parser.parse_args(argv)
    if args.json_report:
        args.json_report = args.json_report.resolve()
    original_cwd, original_path = Path.cwd(), sys.path[:]
    suites = SYMLINK_SUITES if args.suite_group == 'symlink' else SUITES
    report = {'checkout_sha': None, 'suite_group': args.suite_group,
              'expected_tests': 0, 'tests_run': 0, 'failures': 0, 'errors': 0,
              'skips': 0, 'success': False, 'suites': suites}
    status = 1
    try:
        if sys.platform != 'win32':
            report['setup_error'] = 'Native Windows readiness regressions require Windows; nothing was verified.'
            print(report['setup_error'], file=sys.stderr)
            status = 2
        else:
            app = Path(__file__).resolve().parents[1]
            os.chdir(app)
            # Child interpreters spawned by transaction fixtures inherit both roots.
            sys.path[:0] = [str(app), str(app.parent)]
            report['checkout_sha'] = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=app, text=True).strip()
            suite = unittest.defaultTestLoader.loadTestsFromNames(suites)
            expected = report['expected_tests'] = suite.countTestCases()
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            report.update(tests_run=result.testsRun, failures=len(result.failures),
                          errors=len(result.errors), skips=len(result.skipped))
            report['success'] = bool(expected > 0 and result.testsRun == expected
                                     and result.wasSuccessful() and not result.skipped)
            status = 0 if report['success'] else 1
    except Exception as error:
        report.update(errors=1, setup_error=f'{type(error).__name__}: {error}')
        traceback.print_exc()
    finally:
        os.chdir(original_cwd)
        sys.path[:] = original_path
        if args.json_report:
            try:
                args.json_report.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
            except OSError as error:
                report.update(success=False, report_error=f'{type(error).__name__}: {error}')
                print('Could not retain native Windows readiness report: '+report['report_error'], file=sys.stderr)
                if status == 0:
                    status = 1
        print('NATIVE_WINDOWS_READINESS='+json.dumps(report))
    return status


if __name__ == '__main__':
    raise SystemExit(main())
