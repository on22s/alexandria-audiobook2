"""More focused regressions for the resume/report follow-up on #2222."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import core
import review_script as review
from completed_review_receipt import get_review_entries_fingerprint
from tests.test_review_partial_retention import SOURCE, IncompletePublicationTests

INCOMPLETE = ['Review incomplete: 1 -> 1 entries', 'Total changes: 0']


class MarkerAndBoundaryTests(unittest.TestCase):
    def run_parent(self, lines):
        state = {'running': False, 'logs': [], 'status': '', 'return_code': None}
        with tempfile.TemporaryDirectory() as tmp:
            def stream(command, cwd, actual_state, **kwargs):
                actual_state['logs'].extend(lines)
                return 0, ''
            with patch.dict(core.process_state, {'review': state}), \
                 patch.object(core, 'REPORTS_DIR', str(Path(tmp, 'reports'))), \
                 patch.object(core, '_init_task_log', return_value=None), \
                 patch.object(core, '_stream_subprocess_to_logs', side_effect=stream), \
                 patch.object(core, '_llm_summarize_report', side_effect=AssertionError('no model')):
                core.run_process(['synthetic-review'], 'review')
        return state

    def test_explicit_complete_marker_beats_historical_counters(self):
        stats = core._extract_review_stats([
            'Review complete: 2 -> 2 entries', 'Batches failed: 1'])
        self.assertTrue(stats['review_complete'])
        self.assertFalse(core._review_is_incomplete(stats))
        state = self.run_parent(['Review complete: 2 -> 2 entries', 'Batches failed: 1'])
        self.assertEqual('done', state['status'])
        notes = '\n'.join(state['logs'])
        self.assertIn('Task review completed successfully.', notes)
        self.assertNotIn('could not be reviewed', notes)

    def test_legacy_boundary_survives_abort_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp, 'script.json')
            entries = [{'speaker': 'A', 'text': 'one'}, {'speaker': 'B', 'text': 'two'},
                       {'speaker': 'C', 'text': 'three'}]
            digest = get_review_entries_fingerprint(entries)
            stats = {key: 0 for key in review.REVIEW_EDIT_COUNTERS}
            review.save_checkpoint(str(output), 1, 3, 3, 0, entries[:1], stats, entries[:1],
                                   [], [], digest, source_offsets=[1], execution_sha256='exec')
            path = Path(str(output) + '.review_checkpoint.json')
            data = json.loads(path.read_text())
            data['batch_lengths'] = []
            path.write_text(json.dumps(data))
            loaded = review._load_resume_state(str(output), 3, 3, 0, entries, [], stats,
                execution_sha256='exec')
            self.assertEqual([1], review.get_review_resume_source_offsets(loaded[-1], entries, 3))
            self.assertEqual(1, loaded[3])
            self.assertTrue(path.exists())

    def test_in_wave_skip_counts_source_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script_path = root / 'annotated_script.json'
            script_path.write_text(json.dumps(SOURCE))
            log = IncompletePublicationTests().run_review(root, script_path,
                                                          vram_ok=False, dedupe=False)
            self.assertIn('3 entries were left unreviewed', log)
            marker = json.loads(next(line.split(': ', 1)[1] for line in log.splitlines()
                                     if line.startswith('REVIEW_DISPOSITION_JSON: ')))
            self.assertEqual(3, marker['unreviewed_entries'])
            self.assertFalse(marker['post_processing_abort'])

    def test_mixed_two_book_aggregate_separates_final_check_aborts(self):
        first = {**core._new_review_totals(), 'review_incomplete': True,
                 'post_processing_abort': True, 'final_check_aborted': 1,
                 'unreviewed_entries': 0, 'books_done': 0}
        second = {**core._new_review_totals(), 'review_incomplete': True,
                  'unreviewed_entries': 3, 'books_done': 0}
        combined = core._combine_pass_stats(first, second)
        combined['books_done'] = 2
        self.assertEqual(1, combined['final_check_aborted'])
        self.assertEqual(3, combined['unreviewed_entries'])
        summary = core._format_pass_summary('Overall', combined, [], False)
        self.assertIn('Final checks stopped for 1 book(s); unreviewed rows: 3', summary)


class NotificationOutcomeJsTests(unittest.TestCase):
    SETUP = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const core=fs.readFileSync(process.argv[1],'utf8');
const notices=[];
const c={window:{Notification:class{constructor(title,options){notices.push({title,options});}}},
 document:{visibilityState:'hidden',hasFocus:()=>false},console,
 t:(key,english,values=Object())=>Object.keys(values).reduce((text,name)=>text.split('{'+name+'}').join(String(values[name])),String(english))};
c.Notification=c.window.Notification;c.Notification.permission='granted';
c.assert=assert;c.notices=notices;vm.createContext(c);
let start=core.indexOf('function isTaskFailed(');
vm.runInContext(core.slice(start,core.indexOf('// --- Desktop notifications ---',start)),c);
start=core.indexOf('function getTaskCompletionOutcome(');
vm.runInContext(core.slice(start,core.indexOf('function notifyJobDone(',start)),c);
start=core.indexOf('function notifyJobDone(');
vm.runInContext(core.slice(start,core.indexOf('// Ask for permission only',start)),c);
c.getTaskLabel=()=> 'Review';
'''

    def test_incomplete_has_distinct_notification_outcome(self):
        source = Path(__file__).resolve().parents[1] / 'static/js/app-core.js'
        code = self.SETUP + r'''
vm.runInContext(`assert.strictEqual(notifyJobDone('review','', 'finished',{status:'incomplete',logs:[]}),undefined);
assert.match(notices.at(-1).title,/review incomplete/i);
assert.match(notices.at(-1).options.body,/still unfinished/i);
notifyJobDone('review','', 'finished',{status:'done',logs:['Task review completed successfully.']});
assert.match(notices.at(-1).title,/finished/i);
notifyJobDone('review','', 'finished',{status:'failed',logs:[]});
assert.match(notices.at(-1).title,/failed/i);
notifyJobDone('review','', 'finished',{status:'cancelled',logs:[]});
assert.match(notices.at(-1).title,/cancelled/i);
notifyJobDone('batch_review','', 'finished',{status:'done',tasks:[{status:'failed'},{status:'incomplete'}],logs:[]});
assert.match(notices.at(-1).title,/failed/i);
notifyJobDone('batch_review','', 'finished',{status:'done',tasks:[{status:'cancelled'},{status:'incomplete'}],logs:[]});
assert.match(notices.at(-1).title,/cancelled/i);
notifyJobDone('batch_review','', 'finished',{status:'done',tasks:[{status:'incomplete'},{status:'done'}],logs:[]});
assert.match(notices.at(-1).title,/incomplete/i);`,c);
'''
        result = subprocess.run(['node', '-e', code, str(source)], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == '__main__':
    unittest.main()
