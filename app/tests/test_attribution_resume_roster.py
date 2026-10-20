"""Duplicate coloring must not change the roster when a run is resumed."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from generate_script import LLMGenParams
import three_pass_generate as tp


class AttributionResumeRosterTests(unittest.TestCase):
    def test_next_prompt_matches_after_each_colored_batch_under_all_policies(self):
        texts = ['Alice and Bob waited together.', 'Alice and Bob waited together.',
                 'Charlie spoke quietly.', 'Dora waved goodbye.']
        segmented = [{'type':'SPOKEN','text':text} for text in texts]
        source = ' '.join(texts)
        params = LLMGenParams(max_tokens=500,segmentation='llm')
        for policy in ('fail','keep','fallback'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                def execute(path, interrupt_after=None):
                    seen = []
                    def attribute(client, model, batch, request_params, **kwargs):
                        seen.append((list(kwargs['roster']),tp.get_attribute_sizing_request(
                            batch,request_params,kwargs['roster'],kwargs['neighbor_contexts'],
                            kwargs['surround'],'default',None,None,None)))
                        if interrupt_after is not None and len(seen) > interrupt_after:
                            raise RuntimeError('fixture interruption')
                        if len(batch) == 2:names = ['ALICE','CHARLIE']
                        elif batch[0]['text'] == texts[1]:names = ['BOB']
                        else:names = ['DORA']
                        return [{**row,'speaker':name} for row,name in zip(batch,names)], [1.] * len(batch)
                    def instruct(client, model, batch, request_params, **kwargs):
                        return [{**entry,'instruct':'Read naturally.'} for entry in batch]
                    with patch.object(tp,'segment_chunk_adaptively',return_value=segmented), \
                         patch.object(tp,'attribute_batch_voted',side_effect=attribute), \
                         patch.object(tp,'instruct_batch',side_effect=instruct):
                        if interrupt_after is not None:
                            with self.assertRaisesRegex(RuntimeError,'fixture interruption'):
                                tp.run_three_pass(object(),'fixture',source,params,3000,
                                    on_exhaustion=policy,output_path=str(path),attribute_batch_size=3)
                            result = None
                        else:
                            result = tp.run_three_pass(object(),'fixture',source,params,3000,
                                on_exhaustion=policy,output_path=str(path),attribute_batch_size=3)
                    return seen,result
                live, result = execute(Path(directory,'live.json'))
                self.assertEqual(['ALICE','BOB','CHARLIE','DORA'],[row['speaker'] for row in result])
                self.assertEqual(['ALICE','BOB','CHARLIE'],live[2][0])
                for accepted in (1,2):
                    path = Path(directory,f'paused_{accepted}.json')
                    interrupted,_ = execute(path,accepted)
                    resumed,resumed_result = execute(path)
                    self.assertEqual(live[accepted],interrupted[-1])
                    self.assertEqual(live[accepted],resumed[0])
                    self.assertEqual(result,resumed_result)


if __name__ == '__main__':unittest.main()
