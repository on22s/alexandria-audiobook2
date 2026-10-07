import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alignment = load_script("alexandria_alignment")
sys.modules.setdefault("alexandria_alignment", alignment)
compare = load_script("alexandria_compare")


class AlignmentCliRegressionTests(unittest.TestCase):
    def test_proper_noun_boundary_policy_is_per_call(self):
        chunk = ["coodo", "anchor"]
        source = ["kudou", "anchor"]
        names_for_book_a = frozenset({"kudou"})
        names_for_book_b = frozenset({"othername"})
        self.assertEqual((0, 2), alignment.trim_span_to_alignment(
            chunk, source, 0, 2, names_for_book_a))
        self.assertEqual((1, 2), alignment.trim_span_to_alignment(
            chunk, source, 0, 2, names_for_book_b))
        self.assertEqual((0, 2), alignment.trim_span_to_alignment(
            chunk, source, 0, 2, names_for_book_a))

    def test_epub_preflight_rejects_resource_exhaustion_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.epub"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("first.xhtml", "A" * 1000)
                archive.writestr("second.xhtml", "B" * 1000)
            alignment.validate_epub_archive(str(path))
            for limit, value, message in (
                    ("EPUB_MAX_ARCHIVE_BYTES", 1, "input limit"),
                    ("EPUB_MAX_MEMBERS", 1, "too many entries"),
                    ("EPUB_MAX_EXPANDED_BYTES", 100, "expands beyond"),
                    ("EPUB_MAX_EXPANSION_RATIO", 5, "expansion ratio")):
                with self.subTest(limit=limit), patch.object(alignment, limit, value):
                    with self.assertRaisesRegex(ValueError, message):
                        alignment.validate_epub_archive(str(path))

    def test_epub_loader_rejects_archive_before_ebooklib_reads_it(self):
        with patch.object(alignment, "EPUB_AVAILABLE", True), \
             patch.object(alignment, "validate_epub_archive",
                          side_effect=ValueError("unsafe EPUB")):
            with self.assertRaisesRegex(ValueError, "unsafe EPUB"):
                alignment.load_epub("book.epub")

    def test_epub_preflight_counts_decompressed_bytes_not_only_zip_headers(self):
        class UnderreportedMember:
            file_size = 1
            compress_size = 1
            def is_dir(self):
                return False

        class UnderreportedArchive:
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def infolist(self):
                return [UnderreportedMember()]
            def open(self, _member):
                return io.BytesIO(b"A" * 200)

        with patch.object(alignment.os.path, "getsize", return_value=1), \
             patch.object(alignment.zipfile, "ZipFile", return_value=UnderreportedArchive()), \
             patch.object(alignment, "EPUB_MAX_EXPANDED_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "expands beyond"):
                alignment.validate_epub_archive("underreported.epub")

    def test_mashup_word_does_not_duplicate_pause_markers(self):
        self.assertEqual(
            ".... *Trull* *Sengar* ....",
            alignment.merge_annotations_with_source(
                ".... *Tralesengar* ....", ["Trull", "Sengar"]),
        )

    def test_repeated_exact_phrase_prefers_match_nearest_cursor(self):
        phrase = "one two three four five".split()
        source = phrase + ["pad", "pad"] + phrase + ["pad", "pad"] + ["tail"] * 7
        self.assertEqual((7, 12, 1.0), alignment.find_best_match(
            phrase, source, cursor=8, window=30, backtrack=20))

    def test_auto_anchor_prefers_strong_prose_over_weak_intro_match(self):
        entries = [{"text": "intro " * 10}, {"text": "chapter " * 10}]
        with patch.object(alignment, "find_anchor_position",
                          side_effect=[(500, 510, 0.41), (10, 20, 0.95)]):
            self.assertEqual((1, 10, 0.95), alignment.auto_anchor(entries, ["source"] * 600))

    def test_auto_anchor_keeps_earliest_high_confidence_prose(self):
        entries = [{"text": "first " * 10}, {"text": "later " * 10}]
        with patch.object(alignment, "find_anchor_position",
                          side_effect=[(10, 20, 0.92), (100, 110, 1.0)]) as find:
            self.assertEqual((0, 10, 0.92), alignment.auto_anchor(entries, ["source"] * 120))
        self.assertEqual(1, find.call_count)

    def test_quality_estimate_advances_at_the_callers_threshold(self):
        entries = [{"text": "one two three four five"},
                   {"text": "six seven eight nine ten"}]
        with patch.object(alignment, "find_best_match",
                          side_effect=[(2, 7, 0.40), (7, 12, 0.80)]) as find:
            alignment.estimate_alignment_quality(
                entries, ["source"] * 20, initial_cursor=0, threshold=0.35)
        self.assertEqual(0, find.call_args_list[0].args[2])
        self.assertEqual(7, find.call_args_list[1].args[2])

    def test_missing_epub_dependency_exits_with_install_instructions(self):
        with patch.object(alignment, "EPUB_AVAILABLE", False):
            with self.assertRaisesRegex(SystemExit, "EPUB support requires"):
                alignment.load_epub("book.epub")

    def test_fresh_compare_uses_cli_threshold_for_quality_estimate(self):
        args = SimpleNamespace(
            jsonl="book.jsonl", source="book.txt", output="out.jsonl",
            threshold=0.83, review_all=False, reset=False, reset_entry=None,
            reset_from=None, reset_range=None, also_clear_log=False,
            source_start=None, source_start_text=None, no_auto_anchor=True,
            review_preanchor=False,
        )
        entries = [{"text": "one two three"}]

        with patch.object(compare.argparse.ArgumentParser, "parse_args", return_value=args), \
             patch.object(compare, "load_jsonl", return_value=entries), \
             patch.object(compare, "load_source", return_value="one two three"), \
             patch.object(compare, "get_checkpoint_identity", return_value={}), \
             patch.object(compare, "load_checkpoint", return_value={}), \
             patch.object(compare, "get_alignment_quality_prescan",
                          return_value=SimpleNamespace(metrics=(1.0, 1, 0, 0))) as estimate, \
             patch.object(compare, "run") as run:
            compare.main()

        self.assertEqual(0.83, estimate.call_args.kwargs["threshold"])
        self.assertEqual(0.83, run.call_args.kwargs["threshold"])

    def test_skipped_entry_is_prompted_again_on_resume(self):
        decisions = {"0": {"action": "skip", "text": "old", "ratio": 0.2,
                           "cursor_after": 99}}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(compare, "find_best_match", return_value=(0, 5, 1.0)) as find, \
             patch.object(compare, "save_checkpoint", return_value="fixture-generation"), \
             patch.object(compare, "write_output"), \
             patch.object(compare, "log_decision"), \
             patch("builtins.input", return_value="k") as prompt:
            compare.run([{"text": "one two three four five"}],
                        ["one", "two", "three", "four", "five"],
                        ["one", "two", "three", "four", "five"],
                        decisions, 0, 0.9, True, str(Path(tmp) / "book.jsonl"),
                        str(Path(tmp) / "out.jsonl"), Path(tmp) / "review.log")
        self.assertEqual(1, prompt.call_count)
        self.assertEqual(0, find.call_args.args[2])
        self.assertEqual("keep", decisions["0"]["action"])

    def test_preanchor_keeps_do_not_suppress_quality_check(self):
        args = SimpleNamespace(
            jsonl="book.jsonl", source="book.txt", output="out.jsonl",
            threshold=0.9, review_all=False, reset=False, reset_entry=None,
            reset_from=None, reset_range=None, also_clear_log=False,
            source_start=None, source_start_text=None, no_auto_anchor=False,
            review_preanchor=False,
        )
        entries = [{"text": "intro"}, {"text": "prose"}]
        with patch.object(compare.argparse.ArgumentParser, "parse_args", return_value=args), \
             patch.object(compare, "load_jsonl", return_value=entries), \
             patch.object(compare, "load_source", return_value="intro prose"), \
             patch.object(compare, "get_checkpoint_identity", return_value={}), \
             patch.object(compare, "load_checkpoint", return_value={}), \
             patch.object(compare, "auto_anchor", return_value=(1, 0, 0.95)), \
             patch.object(compare, "save_checkpoint"), \
             patch.object(compare, "get_alignment_quality_prescan",
                          return_value=SimpleNamespace(metrics=(1.0, 1, 0, 0))) as estimate, \
             patch.object(compare, "run"):
            compare.main()
        self.assertEqual(1, estimate.call_args.kwargs["start_entry_idx"])

    def test_compare_rejects_unsafe_auto_approval_thresholds(self):
        args = SimpleNamespace(threshold=0.0)
        with patch.object(compare.argparse.ArgumentParser, "parse_args", return_value=args), \
             patch.object(compare, "load_jsonl") as load:
            for value in (0, -0.1, 1.1, float("nan"), float("inf")):
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    args.threshold = value
                    compare.main()
        load.assert_not_called()

    def test_targeted_reset_uses_nonblank_entry_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.jsonl"
            output = Path(tmp) / "corrected.jsonl"
            source.write_text('\n{"text":"first"}\n\n{"text":"second"}\n')
            output.write_text('{"text":"edited first"}\n{"text":"edited second"}\n')
            compare.apply_targeted_reset(str(source), str(output),
                                         Path(tmp) / "review.log", {1}, False)
            self.assertEqual(["edited first", "second"],
                             [entry["text"] for entry in compare.load_jsonl(str(output))])

    def test_targeted_reset_recovers_from_corrupt_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "book.jsonl"
            output = Path(tmp) / "corrected.jsonl"
            source.write_text('{"text":"original"}\n')
            output.write_text('{"text":"edited"}\n')
            checkpoint = compare.checkpoint_path(str(source))
            checkpoint.write_text('{"decisions":')
            compare.apply_targeted_reset(str(source), str(output),
                                         Path(tmp) / "review.log", {0}, False)
            self.assertEqual("original", compare.load_jsonl(str(output))[0]["text"])
            self.assertEqual({}, json.loads(checkpoint.read_text())["decisions"])

    def test_failed_jsonl_write_preserves_prior_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "corrected.jsonl"
            output.write_text('{"text":"prior complete output"}\n')
            with self.assertRaises(TypeError):
                compare.write_output([{"text": "first"}, {"text": object()}],
                                     {}, str(output))
            self.assertEqual('{"text":"prior complete output"}\n', output.read_text())
            self.assertEqual([], list(Path(tmp).glob(".corrected.jsonl.*.tmp")))

    def test_checkpoint_rejects_changed_source_input_or_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            jsonl = Path(tmp) / "book.jsonl"
            source.write_text("edition A")
            jsonl.write_text('{"text":"chapter"}\n')
            identity = compare.get_checkpoint_identity(
                str(jsonl), str(source), str(Path(tmp) / "out.jsonl"))
            compare.save_checkpoint(str(jsonl), {"0": {"action": "keep"}},
                                    12, identity)
            self.assertEqual(12, compare.load_checkpoint(str(jsonl), identity)["cursor"])
            for changed_file, changed_text in ((source, "edition B"),
                                               (jsonl, '{"text":"new chapter"}\n')):
                with self.subTest(changed_file=changed_file):
                    original = changed_file.read_text()
                    changed_file.write_text(changed_text)
                    changed_identity = compare.get_checkpoint_identity(
                        str(jsonl), str(source), str(Path(tmp) / "out.jsonl"))
                    with self.assertRaisesRegex(SystemExit, "different or older inputs"):
                        compare.load_checkpoint(str(jsonl), changed_identity)
                    changed_file.write_text(original)
            changed_output = compare.get_checkpoint_identity(
                str(jsonl), str(source), str(Path(tmp) / "other.jsonl"))
            with self.assertRaisesRegex(SystemExit, "different or older inputs"):
                compare.load_checkpoint(str(jsonl), changed_output)

    def test_targeted_reset_refuses_mismatched_checkpoint_before_output_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            jsonl = Path(tmp) / "book.jsonl"
            output = Path(tmp) / "corrected.jsonl"
            source.write_text("edition A")
            jsonl.write_text('{"text":"original"}\n')
            output.write_text('{"text":"edited"}\n')
            identity = compare.get_checkpoint_identity(str(jsonl), str(source), str(output))
            compare.save_checkpoint(str(jsonl), {"0": {"action": "edit"}}, 5, identity)
            source.write_text("edition B")
            changed = compare.get_checkpoint_identity(str(jsonl), str(source), str(output))
            with self.assertRaisesRegex(SystemExit, "different or older inputs"):
                compare.apply_targeted_reset(str(jsonl), str(output),
                                             Path(tmp) / "review.log", {0}, False, changed)
            self.assertEqual('{"text":"edited"}\n', output.read_text())


if __name__ == "__main__":
    unittest.main()


class SourceDiacriticCleanupTests(unittest.TestCase):
    def test_abbreviation_initials_stay_separate_and_detached_endings_still_rejoin(self):
        cases={'café e.g.':'café e.g.','résumé a.m.':'résumé a.m.',
               'fiancé i.e. arrived':'fiancé i.e. arrived',
               'fianc é':'fiancé','fiancé e':'fiancée',
               'fiancé e. Next.':'fiancée. Next.',
               'fiancé e, then':'fiancée, then','fiancé event':'fiancé event'}
        for source,expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(expected,alignment.clean_source_text(source))
                # The comparison CLI imports the same two cleanup expressions.
                cleaned=compare._DIACRITIC_REJOIN.sub(r'\1\2',source)
                cleaned=compare._DIACRITIC_REJOIN_TAIL.sub(r'\1\2',cleaned)
                self.assertEqual(expected,cleaned)


class SpokenYearAlignmentTests(unittest.TestCase):
    def test_years_match_digits_and_preserve_both_source_boundaries(self):
        for words,year in ((['nineteen','ninety'],1990),(['twenty','twenty-four'],2024),
                           (['twenty','twenty','four'],2024),(['eighteen','sixty','five'],1865),
                           (['ten','sixty-six'],1066)):
            with self.subTest(words=words):
                self.assertEqual(year,alignment._parse_number(words))
                self.assertTrue(alignment._num_eq(words,[str(year)]))
                self.assertFalse(alignment._num_eq(words,[str(sum(
                    alignment._parse_number([word]) for word in words))]))
                self.assertEqual((0,2),alignment.trim_span_to_alignment(['in',*words],['in',str(year)],0,2))
                self.assertEqual((0,2),alignment.trim_span_to_alignment([*words,'began'],[str(year),'began'],0,2))

    def test_conventional_cardinals_keep_their_existing_value(self):
        for words,value in ((['twenty','five'],25),(['ninety','nine'],99),
                            (['two','thousand','and','eight'],2008),
                            (['sixteen','hundred','eleven'],1611),(['forty-seven'],47)):
            with self.subTest(words=words):
                self.assertEqual(value,alignment._parse_number(words))
        self.assertIsNone(alignment._parse_number(['nineteen','bananas']))
        self.assertFalse(alignment._num_eq(['twenty','five'],['2005']))


class CheckpointLocaleTests(unittest.TestCase):
    def test_checkpoint_reconstructs_exact_utf8_edits_under_non_utf8_default_locale(self):
        original_read=Path.read_text
        def locale_read(path,encoding=None,errors=None):
            return original_read(path,encoding=encoding or 'cp1252',errors=errors)
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp,'metadata.jsonl'))
            decisions={'0':{'action':'edit','text':'café résumé 日本語'}}
            compare.save_checkpoint(path,decisions,1)
            with patch.object(Path,'read_text',locale_read):
                recovered=compare.load_checkpoint(path)
            self.assertEqual(decisions,recovered['decisions'])
            self.assertEqual(1,recovered['cursor'])


class SkippedEntryCursorTests(unittest.TestCase):
    def test_real_skip_resume_retains_exact_entry_text_and_source_position(self):
        import contextlib
        for initial,source,entries,choices in ((0,['one'],[{'text':'one'}],['s']),
                (0,['one','two'],[{'text':'one'},{'text':'two'}],['s','k']),
                (2,['One','other','one'],[{'text':'one'}],['s'])):
            with self.subTest(initial=initial,entries=entries),tempfile.TemporaryDirectory() as tmp:
                jsonl=str(Path(tmp,'metadata.jsonl'));output=str(Path(tmp,'output.jsonl'))
                Path(jsonl).write_text(''.join(json.dumps(entry)+'\n' for entry in entries),encoding='utf-8')
                kwargs=dict(entries=entries,orig_display=source,orig_match=[word.lower() for word in source],decisions={},
                    cursor=initial,threshold=.9,review_all=True,jsonl_path=jsonl,output_path=output,
                    log_path=compare.review_log_path(output))
                with patch('builtins.input',side_effect=choices),contextlib.redirect_stdout(io.StringIO()):
                    compare.run(**kwargs)
                checkpoint=compare.load_checkpoint(jsonl)
                kwargs.update(decisions=checkpoint['decisions'],cursor=checkpoint['cursor'])
                with patch('builtins.input',return_value='a'),contextlib.redirect_stdout(io.StringIO()):
                    compare.run(**kwargs)
                self.assertEqual([entry['text'] for entry in entries],
                                 [json.loads(line)['text'] for line in Path(output).read_text(encoding='utf-8').splitlines()])
                records=[json.loads(line) for line in compare.review_log_path(output).read_text(encoding='utf-8').splitlines()]
                record=next(row for row in records if row['entry_idx']==0)
                self.assertEqual('one',record['original'])
                self.assertGreater(record['ratio'],.99)
