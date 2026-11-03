"""Casting research workflow (#1532): stars/circles/collections, batch marks, activity.

Every test runs on a temporary casting_marks.json; the route tests use the real
ASGI router with a mocked catalogue, so validation, locking and one atomic write
are exercised end to end.
"""
from contextlib import ExitStack
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

import casting_catalog as cc


class CastingMarkSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'casting_marks.json')

    def test_legacy_marks_fill_canonical_defaults_without_mutating_them(self):
        cc.save_casting_mark(self.path, 'persona/a/b', {'status': 'callback', 'notes': 'keep'})
        stored = cc.get_casting_marks(self.path)['persona/a/b']
        self.assertEqual({'status': 'callback', 'notes': 'keep'}, stored)
        full = cc.normalize_casting_mark(stored)
        self.assertEqual({'status': 'callback', 'priority': 'none', 'notes': 'keep', 'tags': [],
                          'stars': [], 'circles': [], 'collections': []}, full)
        full['tags'].append('leak')
        self.assertEqual([], cc.MARK_DEFAULTS['tags'])
        self.assertEqual({'status': 'callback', 'notes': 'keep'}, stored)

    def test_stars_circles_and_collections_are_validated_and_deduplicated(self):
        self.assertEqual({'stars': ['red', 'amber']},
                         cc.get_validated_mark_patch({'stars': ['red', 'amber', 'red']}))
        self.assertEqual({'circles': ['purple']}, cc.get_validated_mark_patch({'circles': ['purple', 'purple']}))
        self.assertEqual({'collections': ['leads', 'villain']},
                         cc.get_validated_mark_patch({'collections': [' leads ', 'leads', 'villain']}))
        for patch, field in (({'stars': ['pink']}, 'stars'), ({'stars': ['red'] * 6}, 'stars'),
                             ({'stars': 'red'}, 'stars'), ({'circles': ['Red']}, 'circles'),
                             ({'collections': ['']}, 'collections'), ({'collections': ['x' * 41]}, 'collections'),
                             ({'collections': [str(i) for i in range(21)]}, 'collections'),
                             ({'collections': 'leads'}, 'collections'), ({'colour': 'red'}, 'status, priority')):
            with self.subTest(patch=str(patch)[:40]), self.assertRaisesRegex(ValueError, field):
                cc.get_validated_mark_patch(patch)

    def test_marks_are_independent_and_arrays_clear_selectively(self):
        cc.save_casting_mark(self.path, 'persona/a/b', {'collections': ['leads'], 'stars': ['red'], 'tags': ['warm']})
        cc.save_casting_mark(self.path, 'persona/a/c', {'collections': ['villain']})
        cc.save_casting_mark(self.path, 'persona/a/b', {'stars': [], 'tags': []})
        marks = cc.get_casting_marks(self.path)
        self.assertEqual({'collections': ['leads'], 'stars': [], 'tags': []}, marks['persona/a/b'])
        self.assertEqual({'collections': ['villain']}, marks['persona/a/c'])


class CastingBatchTests(unittest.TestCase):
    VOICES = ['persona/bk/a', 'persona/bk/b', 'persona/bk/c']

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import casting
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'casting_marks.json')
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        catalog = {'voices': [{'id': voice_id, 'source': 'persona', 'book_id': 'bk', 'name': voice_id,
                               'description': '', 'ref_text': '', 'preview_url': None, 'audio_present': False,
                               'tags': []} for voice_id in self.VOICES], 'skipped': [], 'truncated': False}
        self.stack.enter_context(patch.object(casting, 'CASTING_MARKS_PATH', self.path))
        self.stack.enter_context(patch.object(casting, 'get_casting_catalog', lambda _dir: catalog))
        app = FastAPI()
        app.include_router(casting.router)
        self.client = self.stack.enter_context(TestClient(app))

    def read(self):
        return json.loads(Path(self.path).read_text(encoding='utf-8')) if os.path.exists(self.path) else None

    def write(self, data):
        Path(self.path).write_text(json.dumps(data), encoding='utf-8')

    def post(self, changes, action='mark'):
        return self.client.post('/api/casting/marks/batch', json={'changes': changes, 'action': action})

    def test_batch_returns_full_marks_and_preserves_everything_else(self):
        self.write({'persona/bk/a': {'status': 'callback', 'notes': 'keep'},
                    'persona/gone/z': {'notes': 'untouched'}})
        response = self.post([{'voice_id': 'persona/bk/a', 'patch': {'status': 'reject', 'collections': ['leads']},
                               'expected': {'status': 'callback', 'collections': []}}], action='reject selected')
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual({'status': 'reject', 'priority': 'none', 'notes': 'keep', 'tags': [], 'stars': [],
                          'circles': [], 'collections': ['leads']}, body['marks']['persona/bk/a'])
        self.assertEqual(1, len(body['activity']))
        self.assertEqual({'id', 'at', 'action', 'count'}, set(body['activity'][0]))
        self.assertEqual(('reject selected', 1), (body['activity'][0]['action'], body['activity'][0]['count']))
        stored = self.read()
        self.assertEqual('keep', stored['persona/bk/a']['notes'])
        self.assertEqual({'notes': 'untouched'}, stored['persona/gone/z'])

    def test_batch_clears_one_array_without_touching_the_rest(self):
        cc.save_casting_mark(self.path, 'persona/bk/a',
                             {'status': 'shortlist', 'tags': ['warm'], 'stars': ['red'], 'circles': ['blue'],
                              'collections': [' leads ']})
        response = self.post([{'voice_id': 'persona/bk/a', 'patch': {'stars': []}, 'expected': {'stars': ['red']}}])
        self.assertEqual(200, response.status_code, response.text)
        full = response.json()['marks']['persona/bk/a']
        self.assertEqual([], full['stars'])
        self.assertEqual(['blue'], full['circles'])
        self.assertEqual(['warm'], full['tags'])
        self.assertEqual('shortlist', full['status'])
        cleared = self.post([{'voice_id': 'persona/bk/a', 'patch': {'collections': []},
                              'expected': {'collections': [' leads ']}}])
        self.assertEqual(200, cleared.status_code, cleared.text)
        self.assertEqual([], cleared.json()['marks']['persona/bk/a']['collections'])

    def test_batch_is_all_or_none_for_invalid_missing_stale_and_duplicate_changes(self):
        self.write({'persona/bk/a': {'status': 'callback'}})
        before = Path(self.path).read_bytes()
        ok = [{'voice_id': 'persona/bk/a', 'patch': {'status': 'reject'}, 'expected': {'status': 'callback'}}]
        cases = [
            (ok + [{'voice_id': 'persona/bk/b', 'patch': {'priority': 'urgent'}, 'expected': {'priority': 'none'}}], 400),
            (ok + [{'voice_id': 'persona/bk/nobody', 'patch': {'status': 'reject'},
                    'expected': {'status': 'unreviewed'}}], 404),
            ([{'voice_id': 'persona/bk/a', 'patch': {'status': 'reject'}, 'expected': {'status': 'reject'}}], 409),
            ([{'voice_id': 'persona/bk/a', 'patch': {'status': 'reject'},
               'expected': {'status': 'callback', 'priority': 'none'}}], 400),
            (ok + [{'voice_id': 'persona/bk/a', 'patch': {'notes': 'x'}, 'expected': {'notes': ''}}], 400),
            ([], 422),
        ]
        for changes, code in cases:
            with self.subTest(code=code, changes=str(changes)[:60]):
                response = self.post(changes)
                self.assertEqual(code, response.status_code, response.text)
                self.assertEqual(before, Path(self.path).read_bytes())
        self.assertNotIn('__studio__', json.loads(before))

    def test_invalid_action_writes_nothing(self):
        self.write({'persona/bk/a': {'status': 'callback'}})
        before = Path(self.path).read_bytes()
        change = {'voice_id': 'persona/bk/a', 'patch': {'status': 'reject'}, 'expected': {'status': 'callback'}}
        for action, code in (('   ', 400), ('x' * 101, 422), ('', 422)):
            with self.subTest(action=action[:6]):
                response = self.post([change], action=action)
                self.assertEqual(code, response.status_code, response.text)
                self.assertEqual(before, Path(self.path).read_bytes())

    def test_activity_keeps_only_the_latest_hundred_events(self):
        events = [{'id': f'e{index}', 'at': '2026-01-01T00:00:00.000+00:00', 'action': 'seed', 'count': 1}
                  for index in range(100)]
        self.write({'__studio__': {'events': events}})
        response = self.post([{'voice_id': 'persona/bk/a', 'patch': {'status': 'shortlist'},
                               'expected': {'status': 'unreviewed'}}], action='shortlist one')
        self.assertEqual(200, response.status_code, response.text)
        activity = response.json()['activity']
        self.assertEqual(100, len(activity))
        self.assertEqual(('e1', 'shortlist one'), (activity[0]['id'], activity[-1]['action']))
        self.assertEqual(activity, cc.get_casting_activity(self.path))
        self.assertEqual([], response.json()['marks']['persona/bk/a']['tags'])

    def test_library_returns_activity_and_never_metadata_as_a_mark(self):
        self.write({'__studio__': {'events': [{'id': 'e1', 'at': 't', 'action': 'stack', 'count': 2}]},
                    'persona/bk/a': {'status': 'shortlist'}, 'persona/gone/z': {'status': 'reject'}})
        body = self.client.get('/api/casting/voices').json()
        self.assertEqual(self.VOICES, [voice['id'] for voice in body['voices']])
        self.assertEqual({'persona/bk/a': {'status': 'shortlist'}}, body['marks'])
        self.assertEqual([{'id': 'e1', 'at': 't', 'action': 'stack', 'count': 2}], body['activity'])

    def test_concurrent_nonconflicting_writers_lose_nothing(self):
        self.write({})
        errors = []

        def run(call):
            try:
                call()
            except Exception as error:
                errors.append(error)

        def batch(voice_id):
            cc.save_casting_mark_batch(self.path, [{'voice_id': voice_id, 'patch': {'status': 'shortlist'},
                                                    'expected': {'status': 'unreviewed'}}],
                                       'shortlist', set(self.VOICES))

        threads = [threading.Thread(target=run, args=(lambda voice_id=voice_id: batch(voice_id),))
                   for voice_id in self.VOICES[:2]]
        threads += [threading.Thread(target=run, args=(lambda voice_id=voice_id: cc.save_casting_mark(
            self.path, voice_id, {'notes': 'note'}),)) for voice_id in ('persona/bk/c', 'persona/bk/x')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        marks = cc.get_casting_marks(self.path)
        self.assertEqual({'persona/bk/a', 'persona/bk/b', 'persona/bk/c', 'persona/bk/x'}, set(marks))
        self.assertEqual(2, len(cc.get_casting_activity(self.path)))

    def test_malformed_marks_or_metadata_fail_loudly_and_are_not_overwritten(self):
        change = {'voice_id': 'persona/bk/a', 'patch': {'status': 'reject'}, 'expected': {'status': 'unreviewed'}}
        for data in ({'__studio__': []}, {'__studio__': {'events': 'nope'}},
                     {'persona/bk/a': 'not a mark'}, ['x']):
            with self.subTest(data=str(data)[:40]):
                self.write(data)
                before = Path(self.path).read_bytes()
                with self.assertRaises(ValueError):
                    cc.get_casting_activity(self.path)
                self.assertEqual(500, self.post([change]).status_code)
                self.assertEqual(before, Path(self.path).read_bytes())
        Path(self.path).write_text('{not json', encoding='utf-8')
        before = Path(self.path).read_bytes()
        with self.assertRaises(ValueError):
            cc.get_casting_marks(self.path)
        self.assertEqual(before, Path(self.path).read_bytes())


if __name__ == '__main__':
    unittest.main()
