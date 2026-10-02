import unittest

from experiments.source_span_coverage import get_experiment_chunks
from generate_script import split_into_chunks
from source_span_coverage import (format_tagged_source, get_source_spans,
                                  get_span_coverage_findings)


class SourceSpanCoverageTests(unittest.TestCase):
    def test_tagged_source_keeps_embedded_newlines_on_one_line(self):
        self.assertEqual("[S001] first line second line",
                         format_tagged_source([{"id": "S001", "text": "first line\nsecond line"}]))

    def test_experiment_uses_production_preprocessing(self):
        raw = "The story begins. " + "echo " * 20 + "The story continues."
        self.assertNotEqual(split_into_chunks(raw, 6000),
                            get_experiment_chunks(raw, 6000))
        self.assertLess(get_experiment_chunks(raw, 6000)[0].count("echo"), 20)

    def test_spans_keep_order_and_stable_ids(self):
        spans = get_source_spans("First sentence. Second?\n\nThird paragraph!")
        self.assertEqual(["S001", "S002", "S003"], [span["id"] for span in spans])
        self.assertEqual("[S001] First sentence.\n[S002] Second?\n[S003] Third paragraph!",
                         format_tagged_source(spans))

    def test_complete_declarations_pass(self):
        spans = get_source_spans("First. Second.")
        entries = [
            {"speaker": "NARRATOR", "text": "First.", "instruct": "",
             "source_span_ids": ["S001"]},
            {"speaker": "NARRATOR", "text": "Second.", "instruct": "",
             "source_span_ids": ["S002"]},
        ]
        self.assertEqual([], get_span_coverage_findings(spans, entries))

    def test_missing_unknown_and_malformed_declarations_fail_loudly(self):
        spans = get_source_spans("First. Second. Third.")
        entries = [
            {"text":"First.","source_span_ids": ["S001", "S999"]},
            {"text":"Second. Third.","source_span_ids": "S002"},
        ]
        findings = get_span_coverage_findings(spans, entries)
        self.assertEqual(
            {"invalid_source_span_ids", "unknown_source_span_ids", "uncovered_source_spans", "misassigned_source_span_ids"},
            {finding["code"] for finding in findings})
        missing = next(item for item in findings if item["code"] == "uncovered_source_spans")
        self.assertEqual(["S002", "S003"], missing["span_ids"])


if __name__ == "__main__":
    unittest.main()


class QuotedSentenceSpanTests(unittest.TestCase):
    def test_closing_quotes_and_brackets_stay_with_the_preceding_sentence(self):
        for quoted in ('"Stop."', '“Stop.”', "‘Stop!’", "(Stop.)", "[Stop?]", "{Stop!}",
                       '“Stop!”)]', '«Stop!»', '‹Stop?›'):
            for whitespace in (' ', '  ', '\n', '\t'):
                with self.subTest(quoted=quoted, whitespace=repr(whitespace)):
                    raw = quoted + whitespace + "Then she left."
                    spans = get_source_spans(raw)
                    self.assertEqual([{"id": "S001", "text": quoted},
                                      {"id": "S002", "text": "Then she left."}], spans)
                    self.assertEqual(f"[S001] {quoted}\n[S002] Then she left.", format_tagged_source(spans))
                    declarations = [{"text":span["text"],"source_span_ids": [span["id"]]} for span in spans]
                    self.assertEqual([], get_span_coverage_findings(spans, declarations))

    def test_nonterminal_quotes_do_not_create_a_boundary_and_paragraphs_survive(self):
        raw = 'He called her "friend" and waited.\n\n"Go." Then he left!'
        self.assertEqual(['He called her "friend" and waited.', '"Go."', 'Then he left!'],
                         [span['text'] for span in get_source_spans(raw)])
