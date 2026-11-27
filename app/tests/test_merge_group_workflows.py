"""Queue checks must run on the queue tree without a pull-request payload."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]


class MergeGroupWorkflowTests(unittest.TestCase):
    def test_every_pr_workflow_also_runs_for_merge_groups(self):
        checked = []
        for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
            text = path.read_text(encoding='utf-8')
            if re.search(r'^  pull_request:', text, re.MULTILINE):
                with self.subTest(workflow=path.name):
                    self.assertRegex(text, r'(?m)^  merge_group:\s*$')
                checked.append(path.name)
        self.assertEqual(10, len(checked))
        manual = (ROOT / '.github/workflows/windows-export-staging-evidence.yml').read_text(encoding='utf-8')
        self.assertRegex(manual, r'(?m)^  workflow_dispatch:')
        self.assertNotRegex(manual, r'(?m)^  (pull_request|merge_group):')

    def test_queue_checkouts_use_the_event_ref_and_hints_use_its_base(self):
        for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
            text = path.read_text(encoding='utf-8')
            if not re.search(r'^  pull_request:', text, re.MULTILINE):
                continue
            with self.subTest(workflow=path.name):
                self.assertNotRegex(text, r'(?m)^\s+ref:')
                references = [line.strip() for line in text.splitlines()
                              if 'github.event.pull_request.' in line]
                self.assertTrue(all('|| github.event.merge_group.base_sha' in line
                                    for line in references), references)
        workflow = (ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8')
        self.assertIn("github.event_name == 'merge_group'", workflow)

    def test_matrix_checks_have_unambiguous_names(self):
        names = []
        for path in sorted((ROOT / '.github/workflows').glob('*.yml')):
            text = path.read_text(encoding='utf-8')
            if 'matrix.os' not in text:
                continue
            job_names = re.findall(r'^    name: (.+)$', text, re.MULTILINE)
            self.assertEqual(1, len(job_names), path.name)
            names.extend(job_names)
        self.assertEqual(6, len(names))
        self.assertEqual(len(names), len(set(names)))
