"""Native kernel leases preserve task identity while allowing CPU/GPU overlap."""
from pathlib import Path
import multiprocessing
import tempfile
import unittest
from task_ownership import acquire_task_lease, ensure_startup_recovery, TaskOwnershipBusy


def hold_cpu_lease(root, ready, release):
    handle = acquire_task_lease(root, 'voicelab', set(), cpu_only=True)
    try:
        ready.set()
        release.wait(10)
    finally:
        handle.close()


class CpuTaskOwnershipTests(unittest.TestCase):
    def test_other_process_cpu_owner_allows_gpu_but_fences_same_task_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            context = multiprocessing.get_context('spawn')
            ready, release = context.Event(), context.Event()
            child = context.Process(target=hold_cpu_lease, args=(tmp, ready, release))
            child.start()
            try:
                self.assertTrue(ready.wait(5))
                gpu = acquire_task_lease(tmp, 'audio', {'voicelab'})
                gpu.close()
                for cpu_only in (True, False):
                    with self.subTest(cpu_only=cpu_only), self.assertRaises(TaskOwnershipBusy):
                        acquire_task_lease(tmp, 'voicelab', set(), cpu_only=cpu_only)
                with ensure_startup_recovery(tmp) as allowed:
                    self.assertFalse(allowed)
            finally:
                release.set()
                child.join(5)
                if child.is_alive():
                    child.kill()
                    child.join(5)
            self.assertEqual(0, child.exitcode)
            with ensure_startup_recovery(tmp) as allowed:
                self.assertTrue(allowed)

    def test_cpu_and_unrelated_gpu_claims_overlap_in_both_orders(self):
        for first in ('cpu','gpu'):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as tmp:
                handles=[]
                try:
                    def cpu(): return acquire_task_lease(tmp,'voicelab',{'audio'},cpu_only=True)
                    def gpu(): return acquire_task_lease(tmp,'audio',{'voicelab'})
                    handles.append(cpu() if first=='cpu' else gpu())
                    handles.append(gpu() if first=='cpu' else cpu())
                    with ensure_startup_recovery(tmp) as allowed:
                        self.assertFalse(allowed)
                    self.assertTrue((Path(tmp)/'.task_ownership/task-cpu__voicelab.lock').is_file())
                finally:
                    for handle in handles:handle.close()
                with ensure_startup_recovery(tmp) as allowed:
                    self.assertTrue(allowed)

    def test_same_task_exclusion_covers_cpu_gpu_and_same_mode(self):
        for first in (True,False):
            with self.subTest(first_cpu=first), tempfile.TemporaryDirectory() as tmp:
                owner=acquire_task_lease(tmp,'voicelab',set(),cpu_only=first)
                try:
                    for second in (True,False):
                        with self.subTest(second_cpu=second), self.assertRaises(TaskOwnershipBusy):
                            acquire_task_lease(tmp,'voicelab',set(),cpu_only=second)
                finally:owner.close()
                next_owner=acquire_task_lease(tmp,'voicelab',set(),cpu_only=not first)
                next_owner.close()

    def test_gpu_conflicts_remain_and_internal_cpu_slot_names_cannot_be_claimed_as_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner=acquire_task_lease(tmp,'voicelab',set())
            try:
                with self.assertRaises(TaskOwnershipBusy):
                    acquire_task_lease(tmp,'audio',{'voicelab'})
            finally:owner.close()
            for name in ('cpu__voicelab','../voicelab',''):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    acquire_task_lease(tmp,name,set(),cpu_only=True)
