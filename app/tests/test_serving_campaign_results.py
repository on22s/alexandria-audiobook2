"""Final serving evidence and actual cache decisions with CPU-only providers."""
import copy
import json
import unittest

from experiments.serving_results import get_complete_serving_rows
from tests import test_campaign_reporting as campaign


def get_document():
    return {
        'meta': {'finished': 1, 'validation': 'ok'},
        'summary': {'base': {'n': 1, 'correct': 0}, 'lora': {'n': 1, 'correct': 1}},
        'rows': [dict(arm=arm, id='index18:0', line='Hello', expected='ALICE',
                      predicted=None if arm == 'base' else 'ALICE', correct=arm == 'lora',
                      candidates=['ALICE'], in_candidates=True)
                 for arm in ('base', 'lora')],
    }


class ServingResultTests(unittest.TestCase):
    def test_actual_final_schema_is_accepted_without_mutation(self):
        document = get_document()
        before = copy.deepcopy(document)
        document['meta']['checkpoint'] = {'temporary': True}
        before['meta']['checkpoint'] = {'temporary': True}
        self.assertEqual(document['rows'], get_complete_serving_rows(document))
        self.assertEqual(before, document)

    def test_incomplete_or_inconsistent_evidence_is_rejected(self):
        changes = [
            lambda d: d.pop('meta'),
            lambda d: d['meta'].pop('finished'),
            lambda d: d['meta'].update(finished=True),
            lambda d: d['meta'].update(finished=float('nan')),
            lambda d: d['meta'].update(validation=['not complete']),
            lambda d: d.update(rows=[]),
            lambda d: d['rows'].pop(),
            lambda d: d['rows'].append(copy.deepcopy(d['rows'][0])),
            lambda d: d['rows'][1].update(id='index18:other'),
            lambda d: d['rows'][1].update(arm='tuned'),
            lambda d: d['rows'][1].update(expected='BOB'),
            lambda d: d['rows'][1].update(line='Other text'),
            lambda d: d['rows'][1].update(correct=1),
            lambda d: d['rows'][1].update(predicted=[]),
            lambda d: d['summary']['base'].update(n=1000),
            lambda d: d['summary']['base'].update(n=True),
            lambda d: d['summary']['base'].update(correct=1),
            lambda d: d['summary'].pop('lora'),
        ]
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                document = get_document()
                change(document)
                with self.assertRaises(ValueError):
                    get_complete_serving_rows(document)


class ServingCacheTests(unittest.TestCase):
    setUp = campaign.CampaignReportingTests.setUp
    _write = campaign.CampaignReportingTests._write
    _run = campaign.CampaignReportingTests._run

    def get_artifact(self):
        return self.root / 'ab_test_runtime/experiments/lora_serving_eval__unanswered-stratification-20260830.json'

    def test_nonempty_interrupted_cache_is_regenerated(self):
        for cached in ('{"rows":', json.dumps({'rows': get_document()['rows']})):
            with self.subTest(cached=cached):
                self.get_artifact().write_text(cached)
                result = self._run('unanswered_stratification_20260830.sh')
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertNotIn('SKIP', result.stdout)
                get_complete_serving_rows(json.loads(self.get_artifact().read_text()))
                self.assertIn('COMPLETE', result.stdout)

    def test_valid_cache_is_byte_identical_and_skips_before_startup(self):
        original = json.dumps(get_document(), indent=2)
        self.get_artifact().write_text(original)
        self._write('ensure_llama_server.sh', '#!/bin/sh\nexit 99\n')
        result = self._run('unanswered_stratification_20260830.sh')
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('SKIP', result.stdout)
        self.assertFalse((self.root / 'dispatch.log').exists())
        self.assertEqual(original, self.get_artifact().read_text())

    def test_evaluator_success_without_complete_rows_is_failure(self):
        self._write('app/experiments/lora_serving_eval.py', '''import pathlib, sys
root=pathlib.Path(__file__).parents[2]
tag=sys.argv[sys.argv.index('--tag')+1]
(root/'ab_test_runtime/experiments'/('lora_serving_eval__'+tag+'.json')).write_text('{"rows": []}')
''')
        result = self._run('unanswered_stratification_20260830.sh')
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertNotIn('COMPLETE', result.stdout)
        self.assertNotIn('empty of', result.stdout)
        self.assertIn('REFUSING incomplete serving result', result.stderr)


if __name__ == '__main__':
    unittest.main()
