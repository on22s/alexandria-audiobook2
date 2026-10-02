"""Durable source positions survive per-chunk and buffered checkpoint writes."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_preparer_run_state import preparer
from alexandria_run_manifest import get_run_identity, ensure_run_manifest


class SourceResumeCursorTests(unittest.TestCase):
    def state(self, words, cursor=0):
        return {'orig_match': words, 'orig_display': words, 'cursor': cursor,
                'proper_nouns': set()}

    def test_latest_position_not_maximum_restores_backward_reanchor(self):
        state = self.state(['one', 'two', 'three'])
        identity = preparer.get_source_checkpoint_identity(state)
        rows = [{'source_position': {'identity': identity, 'cursor': i}} for i in (3, 1)]
        self.assertEqual(1, preparer.get_resumed_source_cursor(rows, state, identity))
        self.assertEqual(0, state['cursor'])

    def test_corrupt_or_incompatible_positions_fail_loud(self):
        state = self.state(['one', 'two'])
        identity = preparer.get_source_checkpoint_identity(state)
        for saved in (None, [], {'identity': {**identity, 'version': True}, 'cursor': 1},
                      {'identity': identity, 'cursor': True},
                      {'identity': identity, 'cursor': -1},
                      {'identity': identity, 'cursor': 3},
                      {'identity': {**identity, 'words_sha256': 'wrong'}, 'cursor': 1}):
            with self.subTest(saved=saved), self.assertRaises(preparer.RunStateError):
                preparer.get_resumed_source_cursor([{'source_position': saved}], state, identity)

    def test_legacy_unique_exact_span_recovers_but_ambiguous_or_asr_only_refuses(self):
        state = self.state(['a', 'b', 'a', 'c'])
        identity = preparer.get_source_checkpoint_identity(state)
        self.assertEqual(4, preparer.get_resumed_source_cursor([{'text': '*a* c...'}], state, identity))
        for text, keep in (('a', False), ('not present', False), ('a c', True)):
            with self.subTest(text=text, keep=keep), self.assertRaisesRegex(preparer.RunStateError, 'unique exact'):
                preparer.get_resumed_source_cursor([{'text': text}], state, identity, keep)
        self.assertIsNone(preparer.get_resumed_source_cursor([{'text': 'whatever'}], None, None))

    def test_actual_chunker_resume_and_batch_positions_match_uninterrupted_artifacts(self):
        import numpy as np
        import soundfile as sf
        for batch_size in (1, 2):
            with self.subTest(batch_size=batch_size), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'source.txt'
                source.write_text('alpha bravo charlie. delta echo foxtrot. golf hotel india.')
                samples = np.sin(np.arange(120000) * .05).astype('float32') * .1
                audio = root / 'audio.wav'; sf.write(audio, samples, 24000)
                identity = get_run_identity(SimpleNamespace(audio=str(audio), source=str(source)))
                words = [{'word': word, 'start': i*.5, 'end': (i+1)*.5,
                          'confidence': 1, 'speaker': 'ONLY'}
                         for i, word in enumerate(source.read_text().split())]
                llm = SimpleNamespace(create_chat_completion=Mock(side_effect=lambda **kw:
                    {'choices': [{'message': {'content': kw['messages'][1]['content'].split('Annotate this segment:\n',1)[1]}}]}))
                previous = Path.cwd(); os.chdir(root)
                try:
                    ensure_run_manifest(root/'dataset_temp', identity, fresh=True)
                    patches = [patch.object(preparer, '_load_llm', return_value=llm),
                               patch.object(preparer, 'log_gpu_stats'),
                               patch.object(preparer, '_calculate_chunk_snr', return_value=80),
                               patch.object(preparer, '_annotate_batch', side_effect=lambda llm, items, *args: [(None, item['text']) for item in items])]
                    from contextlib import ExitStack
                    with ExitStack() as stack:
                        for mocked in patches: stack.enter_context(mocked)
                        state = preparer._build_source_state(str(source), no_auto_anchor=True)
                        preparer.annotate_chunks(words, 'CPU fixture.gguf', 1.2, samples,
                            source_state=state, source_threshold=1, min_chunk_duration=1,
                            run_identity=identity, batch_size=batch_size)
                        checkpoint = root/'dataset_temp/metadata.jsonl'
                        rows = [json.loads(line) for line in checkpoint.read_text().splitlines()]
                        self.assertEqual([3,6,9], [row['source_position']['cursor'] for row in rows])
                        waveforms = [(root/'dataset_temp'/row['audio_filepath']).read_bytes() for row in rows]
                        save = preparer._save_chunk_metadata
                        def interrupt_after_first(*args, **kwargs):
                            save(*args, **kwargs)
                            raise KeyboardInterrupt('fixture interruption after durable write')
                        with patch.object(preparer, '_save_chunk_metadata', side_effect=interrupt_after_first):
                            state = preparer._build_source_state(str(source), no_auto_anchor=True)
                            with self.assertRaises(KeyboardInterrupt):
                                preparer.annotate_chunks(words, 'CPU fixture.gguf', 1.2, samples,
                                    source_state=state, source_threshold=1, min_chunk_duration=1,
                                    run_identity=identity, batch_size=batch_size)
                        self.assertEqual([rows[0]], [json.loads(line) for line in checkpoint.read_text().splitlines()])
                        state = preparer._build_source_state(str(source), no_auto_anchor=True)
                        preparer.annotate_chunks(words, 'CPU fixture.gguf', 1.2, samples,
                            source_state=state, source_threshold=1, min_chunk_duration=1,
                            run_identity=identity, batch_size=batch_size, resume=True)
                        resumed = [json.loads(line) for line in checkpoint.read_text().splitlines()]
                        self.assertEqual(rows, resumed)
                        self.assertEqual(waveforms, [(root/'dataset_temp'/row['audio_filepath']).read_bytes() for row in resumed])
                finally:
                    os.chdir(previous)

    def test_resume_rejects_bad_position_before_model_or_orphan_mutation(self):
        import soundfile as sf
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); audio = root/'audio.wav'; source = root/'source.txt'
            sf.write(audio, np.zeros(24000), 24000); source.write_text('one two three')
            identity = get_run_identity(SimpleNamespace(audio=str(audio), source=str(source)))
            work = root/'dataset_temp'; ensure_run_manifest(work, identity, fresh=True)
            sf.write(work/'sample_0000.wav', np.zeros(24000), 24000)
            (work/'sample_9999.wav').write_bytes(b'orphan preserved on rejected resume')
            state = preparer._build_source_state(str(source), no_auto_anchor=True)
            position = preparer.get_source_checkpoint_identity(state)
            row = {'audio_filepath': 'sample_0000.wav', 'start': 0, 'end': 1,
                   'duration': 1, 'text': 'one two three',
                   'source_position': {'identity': position, 'cursor': 999}}
            (work/'metadata.jsonl').write_text(json.dumps(row)+'\n')
            before = {p.name: p.read_bytes() for p in work.iterdir()}
            previous = Path.cwd(); os.chdir(root)
            try:
                with patch.object(preparer, '_load_llm') as load:
                    with self.assertRaisesRegex(preparer.RunStateError, 'outside'):
                        preparer.annotate_chunks([], 'never-load.gguf', 3, np.zeros(24000),
                            source_state=state, resume=True, run_identity=identity)
                    load.assert_not_called()
                self.assertEqual(before, {p.name: p.read_bytes() for p in work.iterdir()})
                self.assertEqual(0, state['cursor'])
                row['source_position']['cursor'] = 3
                (work/'metadata.jsonl').write_text(json.dumps(row)+'\n')
                changed = preparer._build_source_state(str(source), no_auto_anchor=True)
                changed['orig_match'] = ['different', 'words', 'same-count']
                with patch.object(preparer, '_load_llm') as load:
                    with self.assertRaisesRegex(preparer.RunStateError, 'word sequence'):
                        preparer.annotate_chunks([], 'never-load.gguf', 3, np.zeros(24000),
                            source_state=changed, resume=True, run_identity=identity)
                    load.assert_not_called()
            finally:
                os.chdir(previous)

    def test_truncated_tail_restores_last_durable_position(self):
        import soundfile as sf
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); state = self.state(['one', 'two', 'three'])
            identity = preparer.get_source_checkpoint_identity(state)
            sf.write(root/'sample_0000.wav', np.zeros(24000), 24000)
            row = {'audio_filepath':'sample_0000.wav', 'start':0, 'end':1,
                   'duration':1, 'text':'one two',
                   'source_position':{'identity':identity,'cursor':2}}
            checkpoint = root/'metadata.jsonl'
            checkpoint.write_text(json.dumps(row)+'\n{"source_position":')
            entries, time, next_index = preparer._load_existing_checkpoint(root)
            self.assertEqual((1,1),(time,next_index))
            self.assertEqual(2,preparer.get_resumed_source_cursor(entries,state,identity))
            self.assertEqual(json.dumps(row)+'\n',checkpoint.read_text())

    def test_actual_resume_uses_later_passage_instead_of_confident_early_match(self):
        import soundfile as sf
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root/'source.txt'; audio = root/'audio.wav'
            source.write_text('repeat opening words. unique early words. '+
                              ' '.join(['filler']*1500)+' repeat opening words. unique later words.')
            samples = np.sin(np.arange(72000)*.05).astype('float32')*.1
            sf.write(audio,samples,24000)
            identity = get_run_identity(SimpleNamespace(audio=str(audio),source=str(source)))
            work = root/'dataset_temp';ensure_run_manifest(work,identity,fresh=True)
            state = preparer._build_source_state(str(source),no_auto_anchor=True)
            position = preparer.get_source_checkpoint_identity(state)
            sf.write(work/'sample_0000.wav',samples[:36000],24000)
            row = {'audio_filepath':'sample_0000.wav','start':0,'end':1.5,
                   'duration':1.5,'text':'repeat opening words.',
                   'source_position':{'identity':position,'cursor':1509}}
            (work/'metadata.jsonl').write_text(json.dumps(row)+'\n')
            words = [{'word':word,'start':i*.5,'end':(i+1)*.5,
                      'speaker':'ONLY','confidence':1}
                     for i,word in enumerate('repeat opening words. unique later words.'.split())]
            llm = SimpleNamespace(create_chat_completion=Mock(side_effect=lambda **kw:
                {'choices':[{'message':{'content':kw['messages'][1]['content'].split('Annotate this segment:\n',1)[1]}}]}))
            previous = Path.cwd();os.chdir(root)
            try:
                with patch.object(preparer,'_load_llm',return_value=llm), \
                     patch.object(preparer,'log_gpu_stats'), \
                     patch.object(preparer,'_calculate_chunk_snr',return_value=80):
                    result = preparer.annotate_chunks(words,'CPU fixture.gguf',1.2,samples,
                        source_state=state,source_threshold=.6,min_chunk_duration=1,
                        run_identity=identity,resume=True)
                self.assertEqual(['repeat opening words.','unique later words.'],[row['text'] for row in result])
                self.assertEqual(1512,state['cursor'])
                saved = [json.loads(line) for line in (work/'metadata.jsonl').read_text().splitlines()]
                self.assertEqual([1509,1512],[row['source_position']['cursor'] for row in saved])
            finally:os.chdir(previous)
