import builtins
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import gold_set_builder as gold
import compare_attribution_arms as compare


class BatchReaderFileLifetimeTests(unittest.TestCase):
    def test_real_rejudge_cli_closes_all_inputs_and_preserves_selected_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, new, filled, output = [root / name for name in ('old.json','new.json','filled.json','output.json')]
            old_row = {'id':'line','passage_before':'Old','passage_after':'After','line':'Hello','entry_index':0}
            new_row = dict(old_row, passage_before='Wider')
            old.write_text(json.dumps({'rows':[old_row]})); new.write_text(json.dumps({'rows':[new_row]}))
            filled.write_text(json.dumps({'rows':[{'id':'line','ANSWER':'AMBIGUOUS'}]}))
            inputs = {str(p) for p in (old,new,filled)}; retained=[]
            def tracked_open(path, *args, **kwargs):
                handle = builtins.open(path, *args, **kwargs)
                if str(path) in inputs: retained.append(handle)
                return handle
            try:
                with patch.object(gold,'open',side_effect=tracked_open,create=True), contextlib.redirect_stdout(io.StringIO()):
                    gold.main(['rejudge','fixture','--old',str(old),'--new',str(new),'--filled',str(filled),'--out',str(output)])
                self.assertEqual([new_row], json.loads(output.read_text())['rows'])
                self.assertEqual(3,len(retained))
                self.assertTrue(all(handle.closed for handle in retained))
            finally:
                for handle in retained:handle.close()

    def test_comparison_loader_closes_valid_and_malformed_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,'entries.json'); retained=[]
            def tracked_open(*args, **kwargs):
                handle=builtins.open(*args,**kwargs);retained.append(handle);return handle
            with patch.object(compare,'open',side_effect=tracked_open,create=True):
                path.write_text(json.dumps([None, {'text':'Hello','speaker':'A'}]))
                self.assertEqual([None, {'text':'Hello','speaker':'A'}],compare.load_attribution_entries(path))
                path.write_text('{broken')
                with self.assertRaises(json.JSONDecodeError):compare.load_attribution_entries(path)
            self.assertTrue(all(handle.closed for handle in retained))

    def test_batch_iterator_closes_before_yield_and_on_decode_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, bad = Path(tmp, 'first.json'), Path(tmp, 'bad.json')
            first.write_text('[{"id":"x","ANSWER":"Alice"}]'); bad.write_bytes(b'{broken')
            retained=[]
            def tracked_open(*args, **kwargs):
                handle=builtins.open(*args,**kwargs);retained.append(handle);return handle
            with patch.object(gold,'open',side_effect=tracked_open,create=True):
                iterator=gold.read_batches([first,bad])
                self.assertEqual([{'id':'x','ANSWER':'Alice'}],next(iterator))
                self.assertTrue(retained[0].closed)
                with self.assertRaises(json.JSONDecodeError):next(iterator)
                self.assertTrue(all(handle.closed for handle in retained))
                self.assertEqual({'x':{'answer':'ALICE','reasoning':''}},gold.read_filled([first]))
