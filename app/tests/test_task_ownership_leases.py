"""Real temporary kernel leases, process death and concurrent startup admission."""
import multiprocessing
from pathlib import Path
import signal
import tempfile
import unittest

from task_ownership import acquire_task_lease, ensure_startup_recovery, TaskOwnershipBusy


def hold_lease(root, ready, release):
    lease = acquire_task_lease(root, 'audio', {'review'})
    ready.set()
    release.wait(10)
    lease.close()


def hold_recovery(root, ready, release):
    with ensure_startup_recovery(root) as allowed:
        if allowed:
            ready.set()
        release.wait(10)


class TaskOwnershipLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ctx = multiprocessing.get_context('spawn')

    def process(self, callback):
        ready, release = self.ctx.Event(), self.ctx.Event()
        process = self.ctx.Process(target=callback, args=(str(self.root), ready, release))
        process.start()
        def cleanup():
            if process.is_alive():
                release.set()
            process.join(5)
            if process.is_alive():
                process.kill()
                process.join(5)
        self.addCleanup(cleanup)
        self.assertTrue(ready.wait(5), 'child did not acquire its native lease')
        return process, release

    def test_live_owner_blocks_same_and_conflicting_tasks_and_startup_cleanup(self):
        process, release = self.process(hold_lease)
        for name, conflicts in (('audio', set()), ('review', {'audio'})):
            with self.subTest(task=name), self.assertRaises(TaskOwnershipBusy):
                acquire_task_lease(self.root, name, conflicts)
        cpu = acquire_task_lease(self.root, 'm4b_export', set())
        cpu.close()
        with ensure_startup_recovery(self.root) as allowed:
            self.assertFalse(allowed)
        release.set()
        process.join(5)
        self.assertEqual(0, process.exitcode)
        with ensure_startup_recovery(self.root) as allowed:
            self.assertTrue(allowed)
        lease = acquire_task_lease(self.root, 'review', {'audio'})
        lease.close()

    def test_hard_process_death_releases_lease_without_deleting_or_aging_marker(self):
        process, _ = self.process(hold_lease)
        path = self.root / '.task_ownership/task-audio.lock'
        inode = path.stat().st_ino
        process.kill()
        process.join(5)
        self.assertEqual(-signal.SIGKILL, process.exitcode)
        with ensure_startup_recovery(self.root) as allowed:
            self.assertTrue(allowed)
        lease = acquire_task_lease(self.root, 'audio', set())
        self.assertEqual(inode, path.stat().st_ino)
        lease.close()

    def test_startup_cleanup_holds_admission_until_its_entire_mutation_finishes(self):
        process, release = self.process(hold_recovery)
        with self.assertRaises(TaskOwnershipBusy):
            acquire_task_lease(self.root, 'audio', set())
        release.set()
        process.join(5)
        self.assertEqual(0, process.exitcode)
        lease = acquire_task_lease(self.root, 'audio', set())
        lease.close()

    def test_name_refusal_and_conflict_failure_leave_no_own_slot_held(self):
        for name in ('../audio', '', 'a/b'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                acquire_task_lease(self.root, name, set())
        owner = acquire_task_lease(self.root, 'audio', set())
        self.addCleanup(owner.close)
        with self.assertRaises(TaskOwnershipBusy):
            acquire_task_lease(self.root, 'review', {'audio'})
        lease = acquire_task_lease(self.root, 'review', set())
        lease.close()
