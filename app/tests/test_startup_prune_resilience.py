"""Startup review-session pruning must preserve unprovable sessions (#2100).

A single corrupt leftover under _sessions/ used to raise ReviewError out of
prune_sessions() during lifespan startup, before `try: yield`, taking the whole
server down. These tests drive the real startup path and the per-item pruning
behaviour: an unprovable session is preserved byte for byte and diagnosed, and
every other expired session is still pruned.
"""

import asyncio
import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app as app_module
import evaluation_reviews as er
from tests import test_review_decision_recovery as recovery_tests

DIAGNOSTIC_STAGE = 'review_session_prune_failed'


def _write_expired_session(path, **changes):
    """Write an expired, structurally valid session file at an arbitrary name."""
    document = {
        'session_id': path.stem,
        'adapter_id': 'voice_x',
        'candidate_id': 'cand1',
        'created_at': (datetime.datetime.now(datetime.timezone.utc)
                       - datetime.timedelta(hours=8)).isoformat(),
        'blind': False,
        'labels': {'A': 'production', 'B': 'candidate'},
        'audio': {},
        'fingerprint': {},
        'build': {},
        'automated': {},
    }
    document.update(changes)
    path.write_text(json.dumps(document), encoding='utf-8')
    return path


def _age_session(path, hours=8):
    """Backdate a session so the default 6h pruning budget considers it expired."""
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    document['created_at'] = old.isoformat()
    Path(path).write_text(json.dumps(document), encoding='utf-8')


def _prune_failures(record):
    return [call for call in record.call_args_list if call.args and call.args[0] == DIAGNOSTIC_STAGE]


class StartupPruneResilienceTests(unittest.TestCase):
    def _drive_lifespan(self, root, reviews):
        async def start():
            async with app_module.lifespan(app_module.app):
                pass

        with patch.object(app_module, 'DATA_DIR', root), \
             patch.object(app_module, 'RUN_HISTORY_DIR', str(Path(root) / 'history')), \
             patch.object(app_module, 'EVALUATION_REVIEWS_DIR', str(reviews)), \
             patch.object(app_module, 'reset_stuck_chunks'):
            asyncio.run(start())

    def test_malformed_journal_is_preserved_and_other_session_is_still_pruned(self):
        helper = recovery_tests.ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as directory:
            bad_session, _, bad_journal = helper._interrupt(directory, 'prepared')
            good_session, _, good_journal = helper._interrupt(directory, 'prepared')
            bad_journal.write_bytes(b'{"garbage": true}')
            preserved_journal = bad_journal.read_bytes()
            preserved_session = bad_session.read_bytes()

            with patch('review_startup.record_failure') as record:
                pruned = er.prune_sessions(directory, max_age_seconds=0)

            self.assertEqual([good_session.name], pruned)
            self.assertEqual(preserved_session, bad_session.read_bytes())
            self.assertEqual(preserved_journal, bad_journal.read_bytes())
            self.assertFalse(good_session.exists())
            self.assertFalse(good_journal.exists())
            failures = _prune_failures(record)
            self.assertEqual(1, len(failures))
            self.assertEqual('review', failures[0].kwargs['task'])
            self.assertEqual('prune_session', failures[0].kwargs['operation'])

    def test_lifespan_starts_with_corrupt_pending_decision_journal(self):
        helper = recovery_tests.ReviewDecisionRecoveryTests()
        with tempfile.TemporaryDirectory() as root:
            reviews = Path(root) / 'reviews'
            session, _, journal = helper._interrupt(str(reviews), 'prepared')
            _age_session(session)
            journal.write_bytes(b'{"garbage": true}')
            preserved = session.read_bytes()
            preserved_journal = journal.read_bytes()

            self._drive_lifespan(root, reviews)

            self.assertEqual(preserved, session.read_bytes())
            self.assertEqual(preserved_journal, journal.read_bytes())

    def test_unprovable_session_variants_are_preserved_not_fatal(self):
        helper = recovery_tests.ReviewDecisionRecoveryTests()
        for variant in ('identity_mismatch', 'invalid_adapter_id', 'insecure_filename'):
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
                reviews = Path(root) / 'reviews'
                if variant == 'insecure_filename':
                    sessions = Path(er._sessions_dir(str(reviews)))
                    sessions.mkdir(parents=True, exist_ok=True)
                    target = _write_expired_session(sessions / 'bad!name.json')
                else:
                    session, _, _ = helper._interrupt(str(reviews), 'prepared')
                    _age_session(session)
                    target = session
                    document = json.loads(target.read_text(encoding='utf-8'))
                    if variant == 'identity_mismatch':
                        # Journal stays valid; the on-disk session it must match
                        # is no longer the same object.
                        document['extra'] = 'changed after preparation'
                    else:
                        document['adapter_id'] = '../escape'
                    target.write_text(json.dumps(document), encoding='utf-8')
                preserved = target.read_bytes()

                with patch('review_startup.record_failure') as record:
                    self.assertEqual([], er.prune_sessions(str(reviews), max_age_seconds=0))
                self.assertEqual(preserved, target.read_bytes())
                self.assertEqual(1, len(_prune_failures(record)))

                # The real startup path must not abort on any of these variants.
                self._drive_lifespan(root, reviews)
                self.assertEqual(preserved, target.read_bytes())

    def test_direct_journal_check_still_refuses_invalid_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '_pending_decisions' / 'broken.json'
            path.parent.mkdir(parents=True)
            path.write_text('{"garbage": true}', encoding='utf-8')
            with self.assertRaises(er.ReviewError):
                er._get_decision_journal(str(path))


if __name__ == '__main__':
    unittest.main()
