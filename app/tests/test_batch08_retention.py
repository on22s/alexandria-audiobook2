import datetime
import json
from pathlib import Path
import tempfile
import unittest

import run_history as history


class CompleteRetentionTests(unittest.TestCase):
    def test_retention_visits_old_rows_beyond_listing_cap_and_preserves_protected_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.datetime.now(datetime.timezone.utc)
            for i in range(620):
                row = {'id': f'run_{i:04d}', 'status': 'completed',
                       'started_at': (now - datetime.timedelta(days=i)).isoformat()}
                if i == 619:
                    row['status'] = 'running'
                if i in (600, 610):
                    row['status'] = 'failed'
                (root / (row['id'] + '.json')).write_text(json.dumps(row))
            protected = {name: (root / name).read_bytes()
                         for name in ('run_0600.json', 'run_0619.json')}
            self.assertEqual(500, len(history.list_runs(tmp, limit=1000)))
            removed = set(history.prune_runs(tmp, max_count=5, max_age_days=30))
            self.assertIn('run_0618', removed)
            self.assertIn('run_0610', removed)
            self.assertEqual(613, len(removed))
            self.assertEqual({f'run_{i:04d}' for i in range(5)} | {'run_0600', 'run_0619'},
                             {p.stem for p in root.glob('*.json')})
            for name, raw in protected.items():
                self.assertEqual(raw, (root / name).read_bytes())
            self.assertEqual([], history.prune_runs(tmp, max_count=5, max_age_days=30))


if __name__ == '__main__':
    unittest.main()
