"""Enrichment with no category selected is refused instead of running as a silent no-op."""
import asyncio
import io
import json
import sys
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

import preparer_enrichment_settings as enrichment
import routers.preparer as preparer_router
from tests import test_preparer_run_state as support

preparer = support.preparer
CATEGORIES = ('enrich_speaker_attribution', 'enrich_narration_style', 'enrich_emotional_tone')


class StopAfterEnrichmentCheck(RuntimeError):
    pass


class PreparerEnrichmentSettingsTests(unittest.TestCase):
    def test_rule_refuses_enrichment_with_nothing_selected(self):
        for settings in ({'enrich_with_llm': True}, {'enrich_with_llm': True, **{name: False for name in CATEGORIES}}):
            with self.subTest(settings=settings):
                with self.assertRaisesRegex(ValueError, 'Select at least one enrichment category'):
                    enrichment.validate_preparer_enrichment_settings(settings)

    def test_one_category_is_enough_and_disabled_enrichment_ignores_categories(self):
        for name in CATEGORIES:
            with self.subTest(category=name):
                self.assertIsNone(enrichment.validate_preparer_enrichment_settings({'enrich_with_llm': True, name: True}))
        for settings in ({}, {'enrich_with_llm': False}, {'enrich_with_llm': False, CATEGORIES[0]: True}):
            self.assertIsNone(enrichment.validate_preparer_enrichment_settings(settings))

    def test_rule_does_not_mutate_its_input(self):
        settings = {'enrich_with_llm': True, CATEGORIES[1]: True}
        before = dict(settings)
        enrichment.validate_preparer_enrichment_settings(settings)
        self.assertEqual(before, settings)

    def start_request(self, **fields):
        config = {'audio_filename': 'book.wav', 'enrich_with_llm': True, 'llm_model_path': 'llm.gguf', **fields}
        return preparer_router.preparer_start(BackgroundTasks(), config_json=json.dumps(config),
                                              audio_file=None, source_file=None)

    def test_api_refuses_with_the_same_400_before_any_admission(self):
        with patch.object(preparer_router, 'check_global_gpu_lock') as lock, \
             patch.object(preparer_router, '_resolve_preparer_interpreter') as interpreter:
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(self.start_request())
        self.assertEqual(400, caught.exception.status_code)
        self.assertEqual('Select at least one enrichment category.', caught.exception.detail)
        lock.assert_not_called()
        interpreter.assert_not_called()

    def test_api_with_a_category_gets_past_the_enrichment_check(self):
        with patch.object(preparer_router, 'ensure_preparer_diarization_token', side_effect=StopAfterEnrichmentCheck):
            with self.assertRaises(StopAfterEnrichmentCheck):
                asyncio.run(self.start_request(enrich_narration_style=True))

    def test_cli_refuses_before_any_work_and_names_the_flags(self):
        argv = ['preparer', '--audio', 'book.wav', '--model', 'model.gguf',
                '--enrich-with-llm', '--llm-model-path', 'llm.gguf']
        with patch.object(sys, 'argv', argv), patch.object(preparer, 'LLAMA_CPP_AVAILABLE', True), \
             patch.object(sys, 'stderr', io.StringIO()) as error:
            with self.assertRaises(SystemExit) as caught:
                preparer.main()
        self.assertEqual(2, caught.exception.code)
        # argparse also prints the usage text, which lists every flag; look at the error line only.
        message = next(line for line in error.getvalue().splitlines() if ': error:' in line)
        for expected in ('Select at least one enrichment category', '--enrich-speaker-attribution',
                         '--enrich-narration-style', '--enrich-emotional-tone'):
            self.assertIn(expected, message)


if __name__ == '__main__':
    unittest.main()
