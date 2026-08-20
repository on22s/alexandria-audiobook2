"""A build that its consumer cannot read is not a build.

library_eval_build.py writes the held-out set that ljspeech_generate.py
consumes. The first version emitted id, human_wav and text - and the consumer
reads id, book, text, human_wav and seconds. Fourteen of fifteen stages died on
`KeyError: 'book'` after the chain had already taken the GPU.

I had checked that the build contained twenty lines. Counting rows says nothing
about whether the row SHAPE is right, so this test reads the keys the consumer
actually indexes, out of the consumer's own source, rather than a list I would
have to remember to update.
"""
import ast
import json
import os
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

from experiments.library_eval_build import held_out  # noqa: E402

CONSUMER = os.path.join(APP, "experiments", "ljspeech_generate.py")


def keys_the_consumer_reads(path=CONSUMER):
    """-> every literal key indexed off a variable called `row`.

    Read from the consumer's AST so this cannot drift: if someone adds
    row["speaker"] there, this test starts requiring it here.
    """
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "row"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            found.add(node.slice.value)
    return found


class BuildShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset = os.path.join(self.tmp.name, "voice_name", "data")
        os.makedirs(os.path.join(self.dataset, "val"))
        wav = os.path.join(self.dataset, "val", "sample_1.wav")
        open(wav, "wb").close()
        with open(os.path.join(self.dataset, "val", "metadata.jsonl"), "w",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({"audio_filepath": "val/sample_1.wav",
                                     "text": "A held out line.",
                                     "duration": 2.5}) + "\n")

    def test_every_key_the_consumer_reads_is_present(self):
        needed = keys_the_consumer_reads()
        self.assertIn("book", needed, "the consumer really does read row['book']")
        rows = held_out(self.dataset, "voice_name")
        self.assertTrue(rows)
        missing = sorted(needed - set(rows[0]))
        self.assertEqual([], missing,
                         "ljspeech_generate.py indexes these off each row and "
                         "the build does not provide them: %s" % missing)

    def test_the_book_is_the_voice_it_came_from(self):
        rows = held_out(self.dataset, "voice_name")
        self.assertEqual("voice_name", rows[0]["book"])

    def test_a_line_with_no_text_is_dropped_not_shipped_empty(self):
        with open(os.path.join(self.dataset, "val", "metadata.jsonl"), "a",
                  encoding="utf-8") as handle:
            handle.write(json.dumps({"audio_filepath": "val/sample_1.wav",
                                     "text": "   "}) + "\n")
        rows = held_out(self.dataset, "voice_name")
        self.assertEqual(1, len(rows))

    def test_a_missing_val_split_is_refused(self):
        empty = os.path.join(self.tmp.name, "no_val", "data")
        os.makedirs(empty)
        with self.assertRaises(SystemExit):
            held_out(empty, "no_val")


if __name__ == "__main__":
    unittest.main()
