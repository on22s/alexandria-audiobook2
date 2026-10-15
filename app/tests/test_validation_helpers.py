import copy
import os
from pathlib import Path
import tempfile
import unittest

from experiments.provenance import file_sha256
from experiments.validation_helpers import (
    MAX_RUNTIME_FILES, ensure_runtime_binding, get_alias_ledger,
    get_process_holder, get_provenance_observation,
    is_retirement_evidence_valid, start_registered_process)


class RejectAppend(list):
    def append(self, value):
        raise MemoryError('synthetic registration failure')


class RegisteredProcessTests(unittest.TestCase):
    def test_holder_exists_before_start_and_survives_success(self):
        custody, processes = [], []
        handle = object()

        def start(command, **kwargs):
            self.assertEqual(['synthetic'], command)
            self.assertEqual({'token': 3}, kwargs)
            self.assertEqual([{'process': None, 'thread': None}], custody)
            return handle

        self.assertIs(handle, start_registered_process(
            custody, processes, start, ['synthetic'], token=3))
        self.assertEqual([handle], processes)
        self.assertIs(handle, get_process_holder(custody, handle)['process'])
        self.assertEqual(1, len(custody))

    def test_initial_registration_failure_prevents_start(self):
        calls = []
        with self.assertRaises(MemoryError):
            start_registered_process(RejectAppend(), [],
                                     lambda command: calls.append(command), [])
        self.assertEqual([], calls)

    def test_unknown_constructor_failure_retains_pending_holder(self):
        custody, processes = [], []
        original = OSError('synthetic unknown constructor outcome')

        def start(command):
            raise original

        with self.assertRaises(OSError) as raised:
            start_registered_process(custody, processes, start, [])
        self.assertIs(original, raised.exception)
        self.assertEqual([{'process': None, 'thread': None}], custody)
        self.assertEqual([], processes)

    def test_secondary_registration_failure_retains_returned_handle(self):
        custody, handle = [], object()
        with self.assertRaises(MemoryError):
            start_registered_process(custody, RejectAppend(), lambda _: handle, [])
        self.assertIs(handle, get_process_holder(custody, handle)['process'])
        self.assertEqual(1, len(custody))

    def test_lookup_uses_identity_without_equality_or_mutation(self):
        class Handle:
            def __eq__(self, other):
                raise AssertionError('Equality must not identify ownership')

        first, second = Handle(), Handle()
        holders = [{'process': first}, {'process': second}]
        self.assertIs(holders[1], get_process_holder(holders, second))
        self.assertEqual(2, len(holders))

    def test_unknown_and_duplicate_handles_refuse(self):
        handle = object()
        for custody, target in (([], None), ([], handle),
                                ([{'process': handle}, {'process': handle}], handle)):
            with self.subTest(custody=custody), self.assertRaises(ValueError):
                get_process_holder(custody, target)


class RetirementEvidenceTests(unittest.TestCase):
    def receipt(self):
        return {'owned': [
            {'pid': 10, 'returncode': 0, 'native_retired': True},
            {'pid': 20, 'returncode': 0, 'native_retired': True},
            {'pid': 30, 'returncode': -15, 'native_retired': True}],
            'cleanup_dispatch_PIDs': [30, 10, 20],
            'custody_retained_PIDs': []}

    def test_workers_zero_signal_server_and_input_immutability(self):
        receipt = self.receipt()
        before = copy.deepcopy(receipt)
        self.assertTrue(is_retirement_evidence_valid(receipt, 2))
        self.assertEqual(before, receipt)

    def test_missing_partial_and_foreign_populations_refuse(self):
        for field in ('owned', 'cleanup_dispatch_PIDs', 'custody_retained_PIDs'):
            receipt = self.receipt()
            del receipt[field]
            with self.subTest(field=field):
                self.assertFalse(is_retirement_evidence_valid(receipt, 2))
        for value in ([], [10, 20], [10, 20, 99], [10, 10, 30],
                      [True, 20, 30], [10.0, 20, 30], [0, 20, 30]):
            receipt = self.receipt()
            receipt['cleanup_dispatch_PIDs'] = value
            with self.subTest(dispatch=value):
                self.assertFalse(is_retirement_evidence_valid(receipt, 2))
        receipt = self.receipt()
        receipt['owned'].pop()
        self.assertFalse(is_retirement_evidence_valid(receipt, 2))

    def test_unknown_or_unproved_owned_records_refuse(self):
        for key, value in (('pid', True), ('pid', 0), ('pid', -1), ('pid', '10'),
                           ('pid', 20), ('returncode', None), ('returncode', False),
                           ('returncode', 1), ('native_retired', False),
                           ('native_retired', 1)):
            receipt = self.receipt()
            receipt['owned'][0][key] = value
            with self.subTest(key=key, value=value):
                self.assertFalse(is_retirement_evidence_valid(receipt, 2))
        receipt = self.receipt()
        receipt['owned'][0] = None
        self.assertFalse(is_retirement_evidence_valid(receipt, 2))

    def test_unknown_server_exit_and_retained_custody_refuse(self):
        for value in (None, False, '0', 0.0):
            receipt = self.receipt()
            receipt['owned'][-1]['returncode'] = value
            self.assertFalse(is_retirement_evidence_valid(receipt, 2))
        for value in (None, False, [30]):
            receipt = self.receipt()
            receipt['custody_retained_PIDs'] = value
            self.assertFalse(is_retirement_evidence_valid(receipt, 2))

    def test_expected_worker_count_is_explicit_and_typed(self):
        for count in (True, -1, 1, 3, 2.0):
            self.assertFalse(is_retirement_evidence_valid(self.receipt(), count))

    def test_boolean_dispatch_cannot_impersonate_integer_PID_one(self):
        receipt = self.receipt()
        receipt['owned'][0]['pid'] = 1
        receipt['cleanup_dispatch_PIDs'] = [30, 1, 20]
        self.assertTrue(is_retirement_evidence_valid(receipt, 2))
        receipt['cleanup_dispatch_PIDs'] = [30, True, 20]
        self.assertEqual({30, 1, 20}, set(receipt['cleanup_dispatch_PIDs']))
        self.assertFalse(is_retirement_evidence_valid(receipt, 2))

    def test_zero_workers_still_requires_one_returned_server(self):
        receipt = {'owned': [{'pid': 1, 'returncode': -15, 'native_retired': True}],
                   'cleanup_dispatch_PIDs': [1], 'custody_retained_PIDs': []}
        self.assertTrue(is_retirement_evidence_valid(receipt, 0))
        receipt['owned'] = []
        self.assertFalse(is_retirement_evidence_valid(receipt, 0))


@unittest.skipUnless(os.name == 'posix', 'Static binding contract requires POSIX paths')
class StaticRuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.target = self.root / 'runtime.bin'
        self.target.write_bytes(b'synthetic runtime')
        self.alias = self.root / 'runtime-link'
        self.alias.symlink_to(self.target.name)
        self.runtime = {str(self.alias): file_sha256(self.target)}
        self.files = dict(self.runtime, **{str(self.target): file_sha256(self.target)})
        self.ledger = get_alias_ledger(self.runtime, str(self.alias))

    def test_literal_alias_and_target_are_hashed_once_without_mutation(self):
        before = copy.deepcopy((self.runtime, self.files, self.ledger))
        calls = []

        def read(path):
            calls.append(path)
            return file_sha256(path)

        result = get_provenance_observation(self.runtime, self.files, self.ledger, read)
        self.assertTrue(result['runtime_unchanged'])
        self.assertEqual(self.files, result['observed_sha256'])
        self.assertCountEqual(self.files, calls)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(before, (self.runtime, self.files, self.ledger))

    def test_physical_target_digest_mismatch_is_not_hidden_by_alias_digest(self):
        def read(path):
            return '0' * 64 if path == str(self.target) else file_sha256(path)

        for runtime_only in (False, True):
            result = get_provenance_observation(
                self.runtime, self.files, self.ledger, read, runtime_only=runtime_only)
            self.assertFalse(result['runtime_unchanged'])

    def test_alias_digest_mismatch_is_not_hidden_by_target_digest(self):
        def read(path):
            return '0' * 64 if path == str(self.alias) else file_sha256(path)

        result = get_provenance_observation(self.runtime, self.files, self.ledger, read)
        self.assertFalse(result['runtime_unchanged'])

    def test_changed_content_is_reported_not_relearned(self):
        self.target.write_bytes(b'changed runtime')
        result = get_provenance_observation(self.runtime, self.files, self.ledger)
        self.assertFalse(result['runtime_unchanged'])

    def test_same_content_replacement_refuses_identity_change(self):
        replacement = self.root / 'replacement'
        replacement.write_bytes(self.target.read_bytes())
        replacement.replace(self.target)
        with self.assertRaises(ValueError):
            get_provenance_observation(self.runtime, self.files, self.ledger)

    def test_same_content_alias_retarget_refuses(self):
        other = self.root / 'other'
        other.write_bytes(self.target.read_bytes())
        self.alias.unlink()
        self.alias.symlink_to(other.name)
        with self.assertRaises(ValueError):
            get_provenance_observation(self.runtime, self.files, self.ledger)

    def test_mid_hash_identity_change_refuses(self):
        replaced = False

        def read(path):
            nonlocal replaced
            digest = file_sha256(path)
            if not replaced:
                replaced = True
                replacement = self.root / 'replacement'
                replacement.write_bytes(self.target.read_bytes())
                replacement.replace(self.target)
            return digest

        with self.assertRaises(ValueError):
            get_provenance_observation(self.runtime, self.files, self.ledger, read)

    def test_missing_literal_alias_or_target_refuses_before_hashing(self):
        for path in self.files:
            files = dict(self.files)
            del files[path]
            with self.assertRaises(ValueError):
                get_provenance_observation(
                    self.runtime, files, self.ledger,
                    lambda _: self.fail('Invalid binding reached hashing'))

    def test_undeclared_link_and_alias_chain_refuse(self):
        with self.assertRaises(ValueError):
            get_provenance_observation(self.runtime, self.files,
                                       {'schema': 1, 'aliases': []})
        chain = self.root / 'chain'
        chain.symlink_to(self.alias.name)
        with self.assertRaises(ValueError):
            get_alias_ledger({str(chain): file_sha256(self.target)}, str(chain))

    def test_malformed_ledger_digest_and_scope_refuse(self):
        for mutate in (lambda d: d.update(schema=True),
                       lambda d: d.update(extra=1),
                       lambda d: d['aliases'][0].update(sha256='bad'),
                       lambda d: d['aliases'][0]['target_identity'].update(inode=True)):
            ledger = copy.deepcopy(self.ledger)
            mutate(ledger)
            with self.assertRaises(ValueError):
                ensure_runtime_binding(self.runtime, self.files, ledger)
        with self.assertRaises(ValueError):
            get_provenance_observation(self.runtime, self.files, self.ledger,
                                       runtime_only=1)
        with self.assertRaises(ValueError):
            get_provenance_observation(self.runtime, self.files, self.ledger,
                                       lambda _: 'invalid')

    def test_original_runtime_and_alias_count_limits_remain(self):
        runtime = {str(self.root / ('file' + str(i))): 'a' * 64
                   for i in range(MAX_RUNTIME_FILES + 1)}
        with self.assertRaises(ValueError):
            ensure_runtime_binding(runtime, runtime, {'schema': 1, 'aliases': []})
        ledger = copy.deepcopy(self.ledger)
        ledger['aliases'].append(copy.deepcopy(ledger['aliases'][0]))
        with self.assertRaises(ValueError):
            ensure_runtime_binding(self.runtime, self.files, ledger)

    def test_regular_file_and_runtime_only_source_scope(self):
        runtime = {str(self.target): file_sha256(self.target)}
        source = self.root / 'source.txt'
        source.write_text('synthetic source', encoding='utf-8')
        files = dict(runtime, **{str(source): file_sha256(source)})
        ledger = {'schema': 1, 'aliases': []}
        full = get_provenance_observation(runtime, files, ledger)
        limited = get_provenance_observation(runtime, files, ledger, runtime_only=True)
        self.assertEqual(files, full['observed_sha256'])
        self.assertEqual(runtime, limited['observed_sha256'])
        self.assertTrue(full['runtime_unchanged'])
        source.write_text('changed source', encoding='utf-8')
        changed = get_provenance_observation(runtime, files, ledger)
        self.assertTrue(changed['runtime_unchanged'])
        self.assertNotEqual(files, changed['observed_sha256'])


if __name__ == '__main__':
    unittest.main()
