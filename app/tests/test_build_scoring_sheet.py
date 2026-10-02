import unittest

from build_scoring_sheet import build_sheet, neighbour_context


class BuildSheetTest(unittest.TestCase):
    """One shared sample scored once per model, rather than pairwise arms:
    two identical runs disagree on 37.4% of speakers, which swamps any real
    between-model difference."""

    def _runs(self):
        return {
            "modelA": [{"speaker": "ERIS", "text": "Hello there."},
                       {"speaker": "NARRATOR", "text": "The wind blew."},
                       {"speaker": "ROXY", "text": "Good morning."}],
            "modelB": [{"speaker": "ERIS", "text": "Hello  there."},
                       {"speaker": "NARRATOR", "text": "The wind blew."},
                       {"speaker": "SYLPHY", "text": "Good morning."}],
        }

    def test_only_shared_lines_are_sampled(self):
        runs = self._runs()
        runs["modelB"].append({"speaker": "ERIS", "text": "Only in B."})
        rows = build_sheet(runs, size=10)
        self.assertNotIn("Only in B.", [r["text"] for r in rows])

    def test_whitespace_variation_still_counts_as_shared(self):
        rows = build_sheet(self._runs(), size=10)
        self.assertIn("Hello there.", [r["text"] for r in rows])

    def test_narrator_only_lines_are_excluded(self):
        rows = build_sheet(self._runs(), size=10)
        self.assertNotIn("The wind blew.", [r["text"] for r in rows])

    def test_disagreement_is_marked(self):
        rows = build_sheet(self._runs(), size=10)
        row = next(r for r in rows if r["text"] == "Good morning.")
        self.assertFalse(row["models_agree"])
        self.assertEqual(row["answers"], {"modelA": "ROXY", "modelB": "SYLPHY"})

    def test_agreement_is_marked(self):
        rows = build_sheet(self._runs(), size=10)
        row = next(r for r in rows if r["text"] == "Hello there.")
        self.assertTrue(row["models_agree"])

    def test_correct_speaker_starts_blank(self):
        rows = build_sheet(self._runs(), size=10)
        self.assertTrue(all(r["correct_speaker"] == "" for r in rows))

    def test_sampling_is_reproducible(self):
        runs = {"m": [{"speaker": "X", "text": f"line {i}"} for i in range(200)]}
        first = build_sheet(runs, size=20, seed=3)
        second = build_sheet(runs, size=20, seed=3)
        self.assertEqual([r["text"] for r in first], [r["text"] for r in second])

    def test_no_runs_yields_no_rows(self):
        self.assertEqual(build_sheet({}, size=10), [])


class NeighbourContextTest(unittest.TestCase):
    """A line alone is often unanswerable - "Huh, what is it?" names nobody.
    The surrounding narration is what identifies the speaker.

    Searching the source text for the line was tried and abandoned: 35 of 50
    lines could not be located, and a short common line matched the wrong
    occurrence entirely."""

    ENTRIES = [
        {"speaker": "NARRATOR", "text": "Roxy turned away from the window."},
        {"speaker": "NARRATOR", "text": "She had been waiting for hours."},
        {"speaker": "ROXY", "text": "Huh, what is it?"},
        {"speaker": "NARRATOR", "text": "Rudeus looked up, startled by her tone."},
        {"speaker": "RUDEUS", "text": "Nothing important."},
    ]

    def test_context_surrounds_the_line(self):
        before, after = neighbour_context(self.ENTRIES, 2, window=2)
        self.assertIn("She had been waiting for hours.", before)
        self.assertIn("Rudeus looked up, startled by her tone.", after)

    def test_window_is_respected(self):
        before, after = neighbour_context(self.ENTRIES, 2, window=1)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 1)

    def test_start_of_book_has_no_leading_context(self):
        before, after = neighbour_context(self.ENTRIES, 0, window=3)
        self.assertEqual(before, [])
        self.assertTrue(after)

    def test_end_of_book_has_no_trailing_context(self):
        before, after = neighbour_context(self.ENTRIES, 4, window=3)
        self.assertTrue(before)
        self.assertEqual(after, [])

    def test_neighbour_speakers_are_not_exposed(self):
        # They are model output and may be wrong; showing them would bias the
        # judgement being asked for.
        before, after = neighbour_context(self.ENTRIES, 2, window=2)
        self.assertTrue(all(isinstance(item, str) for item in before + after))
        self.assertNotIn("RUDEUS", " ".join(after))

    def test_rows_carry_context(self):
        runs = {"m": self.ENTRIES}
        rows = build_sheet(runs, size=10, window=2)
        row = next(r for r in rows if r["text"] == "Huh, what is it?")
        self.assertIn("She had been waiting for hours.", row["context_before"])
        self.assertIn("Rudeus looked up, startled by her tone.", row["context_after"])


if __name__ == "__main__":
    unittest.main()


class ScoringSheetArtifactTests(unittest.TestCase):
    def test_checkpoint_container_and_named_rows_are_validated_before_scoring(self):
        import json, tempfile
        from pathlib import Path
        import build_scoring_sheet as sheet
        invalid = ('checkpoint', 7, None, {'named': {}}, {'named': ''}, {'named': 0},
                   {'named': ['bad']}, {'named': [42]}, {'named': [False]},
                   {'named': [{'speaker': 'ANN', 'text': 'Hi.'}, ['bad']]})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'checkpoint.json'
            for data in invalid:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    with self.assertRaises(ValueError):
                        sheet.load_named(path)
            row = {'speaker': 'ANN', 'text': 'Hi.'}
            for data, expected in (([], []), ({}, []), ({'named': None}, []), ({'named': []}, []),
                                   ({'named': [None, row, None]}, [row])):
                path.write_text(json.dumps(data))
                self.assertEqual(expected, sheet.load_named(path))

    def test_failed_sheet_serialization_preserves_previous_artifact(self):
        import json, tempfile, sys
        from pathlib import Path
        from unittest.mock import patch
        import build_scoring_sheet as sheet
        import utils
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'm' / 'book' / 'result.json.threepass_checkpoint.json'
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(json.dumps({'named': [{'speaker': 'ANN', 'text': 'Hello, friend.'}]}))
            output = Path(tmp) / 'scoring.json'
            original = b'{"checked": "Keep the human answers"}'
            output.write_bytes(original)
            def interrupted(_data, handle, **_kwargs):
                handle.write('{"partial":')
                raise OSError('disk failure during JSON serialization')
            argv = ['build_scoring_sheet.py', tmp, 'book', '--output', str(output)]
            with patch.object(sys, 'argv', argv), patch.object(utils.json, 'dump', side_effect=interrupted), patch.object(utils.time, 'sleep'):
                with self.assertRaises(OSError):
                    sheet.main()
            self.assertEqual(original, output.read_bytes())
            self.assertEqual(['m', output.name], sorted(p.name for p in Path(tmp).iterdir()))
            with patch.object(sys, 'argv', argv):
                sheet.main()
            saved = json.loads(output.read_text())
            self.assertEqual('book', saved['book'])
            self.assertEqual(1, saved['sampled'])
            self.assertEqual({'m': 'ANN'}, saved['rows'][0]['answers'])


class ScoringSheetCandidateValidationTests(unittest.TestCase):
    def test_blank_spoken_candidates_are_excluded_from_saved_sheet(self):
        import json, tempfile, sys
        from pathlib import Path
        from unittest.mock import patch
        import build_scoring_sheet as sheet
        with tempfile.TemporaryDirectory() as tmp:
            for model, blank in (('A', ''), ('B', ' \t\n')):
                checkpoint = Path(tmp) / model / 'book' / 'result.json.threepass_checkpoint.json'
                checkpoint.parent.mkdir(parents=True)
                checkpoint.write_text(json.dumps({'named': [
                    {'speaker': 'ANN', 'text': blank},
                    {'speaker': 'ANN', 'text': 'A spoken line.'},
                ]}))
            output = Path(tmp) / 'sheet.json'
            with patch.object(sys, 'argv', ['build_scoring_sheet.py', tmp, 'book', '--output', str(output)]):
                sheet.main()
            data = json.loads(output.read_text())
            self.assertEqual(1, data['sampled'])
            self.assertEqual(['A spoken line.'], [row['text'] for row in data['rows']])
            self.assertEqual({'A': 2, 'B': 2}, data['entries_per_model'])
            self.assertEqual({'A': 'ANN', 'B': 'ANN'}, data['rows'][0]['answers'])

    def test_negative_sampling_options_are_cli_errors_before_loading_or_writing(self):
        import contextlib, io, tempfile, sys
        from pathlib import Path
        from unittest.mock import patch
        import build_scoring_sheet as sheet
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'sheet.json'
            original = b'{"human_answer": "ANN"}'
            output.write_bytes(original)
            for option in ('--size', '--window'):
                with self.subTest(option=option), patch.object(sys, 'argv', [
                        'build_scoring_sheet.py', tmp, 'book', option, '-1', '--output', str(output)]), \
                     patch.object(sheet, 'find_model_runs', return_value={}) as load, \
                     contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit) as caught:
                        sheet.main()
                    self.assertEqual(2, caught.exception.code)
                    self.assertIn(option + ' must be nonnegative', errors.getvalue())
                    load.assert_not_called()
                self.assertEqual(original, output.read_bytes())

    def test_zero_size_and_context_window_remain_valid(self):
        import json, tempfile, sys
        from pathlib import Path
        from unittest.mock import patch
        import build_scoring_sheet as sheet
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / 'A' / 'book' / 'result.json.threepass_checkpoint.json'
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text(json.dumps({'named': [
                {'speaker': 'NARRATOR', 'text': 'Ann walked in.'},
                {'speaker': 'ANN', 'text': 'Hello.'},
                {'speaker': 'NARRATOR', 'text': 'Ann waved.'}]}))
            output = Path(tmp) / 'sheet.json'
            for size in (0, 1):
                with patch.object(sys, 'argv', ['build_scoring_sheet.py', tmp, 'book',
                        '--size', str(size), '--window', '0', '--output', str(output)]):
                    sheet.main()
                data = json.loads(output.read_text())
                self.assertEqual(size, data['sampled'])
                if size:
                    self.assertEqual('Hello.', data['rows'][0]['text'])
                    self.assertEqual([], data['rows'][0]['context_before'])
                    self.assertEqual([], data['rows'][0]['context_after'])
