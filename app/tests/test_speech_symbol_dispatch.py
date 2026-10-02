"""Known spoken words at chunk and actual TTS dispatch, without synthesis."""
import copy
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import speech_text
import verbalization
from project import get_speakable_entries
import tts
from speech_policy import ELONGATION_HINT, SUNG_HINT, SCENE_BREAK_CHARS


if os.environ.get('SPEECH_SYMBOL_BASELINE'):
    source = Path(os.environ['SPEECH_SYMBOL_BASELINE'])
    exec(compile(source.read_text(), str(source), 'exec'), speech_text.__dict__)


class SpeechSymbolDispatchTests(unittest.TestCase):
    def test_known_symbols_use_identical_words_in_chunk_and_direct_voice_paths(self):
        symbols = {'→': 'right arrow', '←': 'left arrow', '↑': 'up arrow', '↓': 'down arrow',
                   '★': 'star', '☆': 'star', '♥': 'heart', '≤': 'at most', '√': 'the square root of',
                   '∞': 'infinity', '&': 'and', '@': 'at', '†': ''}
        engine = object.__new__(tts.TTSEngine)
        config = {'A': {'type': 'custom', 'speaker': 'Ryan'}}
        for symbol, word in symbols.items():
            with self.subTest(symbol=symbol):
                entry = {'speaker': 'A', 'text': f'Alpha {symbol} Beta.', 'instruct': 'Calm.'}
                original = copy.deepcopy(entry)
                parts = get_speakable_entries([entry])
                self.assertEqual(1, len(parts))
                expected = ' '.join(f'Alpha {word} Beta.'.split())
                self.assertEqual(expected, parts[0]['text'])
                seen = []
                def synth(text, *args, **kwargs):
                    seen.append(text)
                    return True
                with patch.object(engine, 'get_voice_backend', return_value='local'), \
                     patch.object(engine, '_local_generate_custom', side_effect=synth):
                    self.assertTrue(engine.generate_custom_voice(entry['text'], entry['instruct'], 'A', config, 'unused.wav'))
                    self.assertTrue(engine.generate_custom_voice(parts[0]['text'], parts[0]['instruct'], 'A', config, 'unused.wav'))
                self.assertEqual([expected, expected], seen)
                self.assertEqual(original, entry)

    def test_classifier_uses_the_shared_rendering_object(self):
        self.assertIs(speech_text.SPOKEN_SYMBOLS, verbalization.VERBALIZE)
        for symbol in speech_text.SPOKEN_SYMBOLS:
            self.assertEqual('verbalize', verbalization.classify(symbol))

    def test_direct_custom_dispatch_preserves_the_same_cues_as_prepared_entries(self):
        engine = object.__new__(tts.TTSEngine)
        config = {'A': {'type': 'custom', 'speaker': 'Ryan'}}
        for source, expected, hints in (('Yaaay~', 'Yaaay.', [ELONGATION_HINT]),
                                        ('Yaaay～', 'Yaaay.', [ELONGATION_HINT]),
                                        ('♪ Sing~ ♪', 'Sing.', [SUNG_HINT, ELONGATION_HINT])):
            with self.subTest(source=source):
                entry = {'speaker': 'A', 'text': source, 'instruct': 'Calm.'}
                part = get_speakable_entries([entry])[0]
                captured = []
                def synth(text, instruction, *args, **kwargs):
                    captured.append((text, instruction))
                    return True
                with patch.object(engine, 'get_voice_backend', return_value='local'), \
                     patch.object(engine, '_local_generate_custom', side_effect=synth):
                    engine.generate_custom_voice(source, 'Calm.', 'A', config, 'unused')
                    engine.generate_custom_voice(part['text'], part['instruct'], 'A', config, 'unused')
                self.assertEqual(captured[0], captured[1])
                self.assertEqual(expected, captured[0][0])
                for hint in hints:
                    self.assertEqual(1, captured[0][1].count(hint))
                self.assertIn('Calm.', captured[0][1])

    def test_native_batch_handoff_preserves_cues_without_mutating_source(self):
        engine = object.__new__(tts.TTSEngine)
        engine._compile_codec_enabled = False
        chunks = [{'index': 0, 'speaker': 'A', 'text': '♪ Sing~ ♪', 'instruct': 'Calm.'}]
        original = copy.deepcopy(chunks)
        seen = []
        def batch(rows, *args):
            seen.extend(copy.deepcopy(rows))
            return {'completed': [0], 'failed': []}
        with patch.object(engine, 'get_voice_backend', return_value='local'), \
             patch.object(engine, '_local_batch_custom', side_effect=batch), \
             patch.object(engine, '_clear_gpu_cache'):
            result = engine.generate_batch(chunks, {'A': {'type': 'custom', 'speaker': 'Ryan'}}, 'unused')
        self.assertEqual([0], result['completed'])
        self.assertEqual('Sing.', seen[0]['text'])
        self.assertEqual(1, seen[0]['instruct'].count(SUNG_HINT))
        self.assertEqual(1, seen[0]['instruct'].count(ELONGATION_HINT))
        self.assertEqual(original, chunks)

    def test_scene_rules_match_chunk_pauses_and_direct_normalized_boundaries(self):
        for divider in sorted(SCENE_BREAK_CHARS) + ['***', '___', '*_*', '* * *', '🐉 🐉 🐉']:
            with self.subTest(divider=divider):
                source = f'Alpha {divider} Beta'
                parts = get_speakable_entries([{'speaker': 'A', 'text': source, 'instruct': ''}])
                self.assertEqual(['Alpha', 'Beta'], [part['text'] for part in parts])
                self.assertGreater(parts[0].get('pause_after', 0), 0)
                self.assertEqual('Alpha. Beta.', speech_text.normalize_for_speech(source))
        for source in ('Use snake_case.', 'Use A*B.', 'Use _hello_ softly.'):
            self.assertEqual(source, get_speakable_entries([{'speaker': 'A', 'text': source}])[0]['text'])
            self.assertEqual(source, speech_text.normalize_for_speech(source))

    def test_unknown_glyph_review_and_final_drop_share_the_same_classification(self):
        from project import split_on_unspeakable
        for glyph in ('⌘', '\ue000', '\x07', '\ufffd'):
            with self.subTest(glyph=repr(glyph)):
                self.assertEqual('review', verbalization.classify(glyph))
                entry = {'speaker': 'A', 'text': f'Alpha {glyph} Beta'}
                original = copy.deepcopy(entry)
                parts, review = split_on_unspeakable(entry, 1000)
                self.assertIn(glyph, review)
                rendered = speech_text.get_speech_normalization(parts[0]['text'])
                self.assertEqual('Alpha Beta.', rendered['text'])
                drops = [change for change in rendered['transformations'] if change['type'] == 'dropped_unspeakable']
                self.assertIn(glyph, drops[0]['symbols'])
                self.assertEqual(original, entry)
