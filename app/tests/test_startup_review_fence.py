"""Review-session pruning must run inside the startup ownership fence (#2099).

Blind-review sessions carry no task lease, so age is the only abandonment
signal. A second worker that cannot own recovery must therefore never prune a
live (long-open) session: pruning happens only while the admission fence is
held and only when this process is allowed to recover.
"""

import asyncio
import contextlib
import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import evaluation_reviews as er
from tests import test_review_decision_recovery as recovery_tests


def _clock(now):
    real_datetime = datetime.datetime
    class FixedNow(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)
    return patch.object(er.datetime, 'datetime', FixedNow)


class StartupReviewFenceTests(unittest.TestCase):
    def _drive_lifespan(self, root, reviews, fence):
        async def start():
            async with app_module.lifespan(app_module.app):
                pass

        with patch.object(app_module, 'DATA_DIR', root), \
             patch.object(app_module, 'RUN_HISTORY_DIR', str(Path(root) / 'history')), \
             patch.object(app_module, 'EVALUATION_REVIEWS_DIR', str(reviews)), \
             patch.object(app_module, 'ensure_startup_recovery', fence), \
             patch.object(app_module, 'reset_stuck_chunks'):
            asyncio.run(start())

    def test_other_worker_owns_recovery_preserves_old_session(self):
        helper = recovery_tests.ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as root:
            reviews = Path(root) / 'reviews'
            session, _, _ = helper._interrupt(str(reviews), 'prepared')
            preserved = Path(session).read_bytes()
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)

            @contextlib.contextmanager
            def denied(_data_dir, timeout=10):
                yield False

            with _clock(future), \
                 patch.object(app_module.evaluation_reviews, 'prune_sessions') as prune:
                self._drive_lifespan(root, reviews, denied)

            prune.assert_not_called()
            self.assertEqual(preserved, Path(session).read_bytes())

    def test_pruning_runs_only_while_fence_is_held(self):
        helper = recovery_tests.ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as root:
            reviews = Path(root) / 'reviews'
            session, _, _ = helper._interrupt(str(reviews), 'prepared')
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)

            observed = {'held': False, 'called': False}
            real_prune = er.prune_sessions

            @contextlib.contextmanager
            def held_fence(_data_dir, timeout=10):
                observed['held'] = True
                try:
                    yield True
                finally:
                    observed['held'] = False

            def watched_prune(*args, **kwargs):
                observed['called'] = True
                self.assertTrue(observed['held'], 'pruning ran outside the ownership fence')
                return real_prune(*args, **kwargs)

            with _clock(future), \
                 patch.object(app_module.evaluation_reviews, 'prune_sessions', watched_prune):
                self._drive_lifespan(root, reviews, held_fence)

            self.assertTrue(observed['called'])
            self.assertFalse(session.exists())

    def test_corrupt_session_is_preserved_while_valid_one_is_pruned_in_fence(self):
        helper = recovery_tests.ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as root:
            reviews = Path(root) / 'reviews'
            bad, _, bad_journal = helper._interrupt(str(reviews), 'prepared')
            good, _, _ = helper._interrupt(str(reviews), 'prepared')
            bad_journal.write_bytes(b'{"garbage": true}')
            preserved = Path(bad).read_bytes()
            future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=7)

            @contextlib.contextmanager
            def allowed(_data_dir, timeout=10):
                yield True

            with _clock(future):
                self._drive_lifespan(root, reviews, allowed)

            self.assertEqual(preserved, Path(bad).read_bytes())
            self.assertFalse(good.exists())


if __name__ == '__main__':
    unittest.main()
