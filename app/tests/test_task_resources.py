"""Native resource leases fence both admission orders without global I/O locks."""
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from task_ownership import (TaskOwnershipBusy, acquire_task_lease, acquire_task_slot,
                            ensure_startup_recovery, ensure_task_ownership_directory,
                            ensure_task_resources_available, get_task_resource_slot)


def try_matching_and_disjoint(root, first, second, result):
    try:
        lease = acquire_task_lease(root, 'dataset_builder', set(), resources=[first])
    except TaskOwnershipBusy:
        result.put('matching refused')
    else:
        lease.close()
        result.put('matching incorrectly admitted')
    with acquire_task_lease(root, 'dataset_builder', set(), resources=[second]):
        result.put('disjoint admitted')


def hold_resource_task(root, path, ready):
    import time
    with acquire_task_lease(root, 'dataset_builder', set(), resources=[path]):
        ready.set()
        time.sleep(10)


class TaskResourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first, self.second = self.root / 'a', self.root / 'b'

    def test_mutation_blocks_matching_foreign_admission_but_releases_global_barrier(self):
        ctx = multiprocessing.get_context('spawn')
        result = ctx.Queue()
        with ensure_task_resources_available(self.root, ['dataset_builder'], [self.first]):
            child = ctx.Process(target=try_matching_and_disjoint,
                                args=(str(self.root), str(self.first), str(self.second), result))
            child.start()
            try:
                self.assertEqual('matching refused', result.get(timeout=5))
                self.assertEqual('disjoint admitted', result.get(timeout=5))
                child.join(5)
                self.assertEqual(0, child.exitcode)
            finally:
                if child.is_alive():
                    child.kill()
                    child.join(5)
                result.close()
            with ensure_startup_recovery(self.root) as allowed:
                self.assertFalse(allowed, 'startup must respect in-flight resource mutation')
        with acquire_task_lease(self.root, 'dataset_builder', set(), resources=[self.first]):
            pass

    def test_normal_and_cpu_tasks_reserve_only_their_declared_scope(self):
        for cpu_only in (False, True):
            with self.subTest(cpu_only=cpu_only), acquire_task_lease(
                    self.root, 'dataset_builder', set(), cpu_only=cpu_only, resources=[self.first]):
                with self.assertRaises(TaskOwnershipBusy), ensure_task_resources_available(
                        self.root, ['dataset_builder'], [self.first]):
                    self.fail('matching mutation entered')
                with ensure_task_resources_available(self.root, ['dataset_builder'], [self.second]):
                    pass

    def test_legacy_unknown_and_malformed_live_scope_refuse_mutation(self):
        directory = ensure_task_ownership_directory(self.root)
        with acquire_task_slot(directory, 'dataset_builder') as lease:
            lease.seek(0, 2)
            if not lease.tell():
                lease.write(b'\0')
            for data in (b'', b'{}', b'{"resources":"bad"}', b'{"resources":["relative"]}', b'\xff'):
                lease.truncate(1)
                lease.write(data)
                lease.flush()
                with self.subTest(data=data), self.assertRaises(TaskOwnershipBusy), \
                     ensure_task_resources_available(self.root, ['dataset_builder'], [self.second]):
                    self.fail('unknown live task scope authorized mutation')

    def test_dead_owner_metadata_is_ignored_and_marker_inode_survives(self):
        ctx = multiprocessing.get_context('spawn')
        ready = ctx.Event()
        child = ctx.Process(target=hold_resource_task,
                            args=(str(self.root), str(self.first), ready))
        child.start()
        try:
            self.assertTrue(ready.wait(5))
            path = self.root / '.task_ownership/task-dataset_builder.lock'
            inode = path.stat().st_ino
            with self.assertRaises(TaskOwnershipBusy), ensure_task_resources_available(
                    self.root, ['dataset_builder'], [self.first]):
                self.fail('live owner was ignored')
            child.kill()
            child.join(5)
            with ensure_task_resources_available(self.root, ['dataset_builder'], [self.first]):
                pass
            self.assertEqual(inode, path.stat().st_ino)
        finally:
            if child.is_alive():
                child.kill()
                child.join(5)

    def test_partial_multi_resource_reservation_rolls_back_its_earlier_slots(self):
        with ensure_task_resources_available(self.root, [], [self.second]):
            with self.assertRaises(TaskOwnershipBusy), ensure_task_resources_available(
                    self.root, [], [self.first, self.second]):
                self.fail('conflicting second resource entered')
            with ensure_task_resources_available(self.root, [], [self.first]):
                pass

    def test_metadata_failure_releases_task_and_canonical_alias_matches(self):
        from unittest.mock import patch
        alias = self.root / 'nested/../a'
        self.assertEqual(get_task_resource_slot(self.first), get_task_resource_slot(alias))
        with patch('task_ownership._save_task_resources', side_effect=OSError('fixture scope write failed')):
            with self.assertRaisesRegex(OSError, 'fixture scope write failed'):
                acquire_task_lease(self.root, 'dataset_builder', set(), resources=[self.first])
        with acquire_task_lease(self.root, 'dataset_builder', set(), resources=[alias]) as lease:
            path = Path(lease.name)
            lease.seek(0)
            raw = lease.read()
            self.assertEqual(b'\0', raw[:1])
            self.assertEqual([os.path.normcase(os.path.realpath(self.first))],
                             json.loads(raw[1:])['resources'])
            with self.assertRaises(TaskOwnershipBusy), ensure_task_resources_available(
                    self.root, ['dataset_builder'], [self.first]):
                self.fail('canonical scope alias was bypassed')

    def test_body_timeout_is_not_misreported_as_ownership_and_releases_mutation(self):
        error = TimeoutError('fixture analysis timeout')
        with self.assertRaises(TimeoutError) as caught, ensure_task_resources_available(
                self.root, ['dataset_builder'], [self.first]):
            raise error
        self.assertIs(error, caught.exception)
        with acquire_task_lease(self.root, 'dataset_builder', set(), resources=[self.first]):
            pass

    def test_large_valid_scope_still_allows_an_unrelated_saved_name(self):
        # Large batches must not silently become an unknown, globally busy scope.
        resources = [os.path.normcase(str(self.first))] + [
            os.path.normcase(str(self.root / ('book_' + 'x' * 180 + str(index))))
            for index in range(6000)]
        with acquire_task_lease(self.root, 'batch_review', set(), resources=[self.first]) as lease:
            lease.truncate(1)
            lease.write(json.dumps({'resources': resources}).encode('utf-8'))
            lease.flush()
            self.assertGreater(os.fstat(lease.fileno()).st_size, 1024 * 1024)
            with ensure_task_resources_available(self.root, ['batch_review'], [self.second]):
                pass
            with self.assertRaises(TaskOwnershipBusy), ensure_task_resources_available(
                    self.root, ['batch_review'], [self.first]):
                self.fail('large selected scope lost its matching reservation')

    def test_serialized_mutation_waits_without_holding_global_task_admission(self):
        from unittest.mock import patch
        retrying, proceed, finished = threading.Event(), threading.Event(), threading.Event()
        errors = []
        sleep = time.sleep
        def wait(delay):
            retrying.set()
            if not proceed.wait(3):
                raise AssertionError('mutation waiter was not released')
            sleep(delay)
        def mutate():
            try:
                with ensure_task_resources_available(self.root, ['dataset_builder'],
                                                       [self.first], mutation_timeout=3):
                    finished.set()
            except BaseException as error:
                errors.append(error)
        worker = threading.Thread(target=mutate)
        with patch('task_ownership.time.sleep', side_effect=wait):
            try:
                with ensure_task_resources_available(self.root, [], [self.first]):
                    worker.start()
                    self.assertTrue(retrying.wait(2))
                    self.assertFalse(finished.is_set())
                    with acquire_task_lease(self.root, 'dataset_builder', set(), resources=[self.second]):
                        pass
            finally:
                proceed.set()
                if worker.ident is not None:
                    worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(finished.is_set())
