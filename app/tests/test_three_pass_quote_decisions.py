"""Run the real gate once per original chunk, then reuse its exact decision."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from generate_script import LLMGenParams
from tests.test_three_pass_generate import _load_orchestration_fixture_cast

if os.environ.get('THREE_PASS_QUOTE_SOURCE'):
    spec = importlib.util.spec_from_file_location('tp_quote_saved', os.environ['THREE_PASS_QUOTE_SOURCE'])
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)


class ThreePassQuoteDecisionTests(unittest.TestCase):
    def test_deterministic_and_model_paths_use_one_original_chunk_decision(self):
        dialogue = 'Alice stood. "Hello." Alice waited. "Goodbye." Alice left.'
        for mode, source in (('quotes', dialogue), ('lexical', dialogue), ('auto', dialogue),
                             ('auto', 'Alice stood. Alice waited. Alice left.')):
            with self.subTest(mode=mode, source=source), tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                requests = []
                def create(**kwargs):
                    prompt = kwargs['messages'][-1]['content']; requests.append(prompt)
                    match = re.search(r'\[\{"n":', prompt)
                    if match:
                        rows = json.JSONDecoder().raw_decode(prompt[match.start():])[0]
                        attribute = 'type' in rows[0]
                        payload = [{'n':row['n'], 'head':' '.join(row['text'].split()[:3]),
                                    **({'speaker':'ALICE' if row['type']=='SPOKEN' else 'NARRATOR'}
                                       if attribute else {'instruct':'Neutral.'})} for row in rows]
                    else:
                        payload = [{'type':'NARRATOR', 'text':source}]
                    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)), finish_reason='stop')], usage=None)
                client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                output = str(Path(tmp) / 'book.json'); params = LLMGenParams(max_tokens=500, segmentation=mode, structured_output='off')
                decision = tp.quote_regions_decision
                with patch.object(tp,'quote_regions_decision', wraps=decision) as gate:
                    entries = tp.run_three_pass(client,'fixture',source,params,6000,output_path=output,
                                               cast=_load_orchestration_fixture_cast(('ALICE',)))
                checkpoint = json.loads(Path(tp.three_pass_checkpoint_path(output)).read_text())
                manifest = json.loads(Path(tp.three_pass_manifest_path(output)).read_text())
                self.assertEqual(1, gate.call_count)
                self.assertTrue(requests)
                self.assertEqual('complete', manifest['status'])
                self.assertEqual(entries, checkpoint['annotated'])
                self.assertEqual([entry['text'] for entry in checkpoint['segmented']], [entry['text'] for entry in entries])
                self.assertEqual(['NARRATOR','ALICE','NARRATOR','ALICE','NARRATOR'] if source==dialogue else ['NARRATOR'], [entry['speaker'] for entry in entries])
                self.assertEqual('clean' if source!=dialogue else 'quote_presegmented', manifest['chunks'][0]['resolution'])
