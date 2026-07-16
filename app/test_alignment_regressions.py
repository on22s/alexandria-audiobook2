import importlib.util
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent.parent


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"test_{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


alignment = load_script("alexandria_alignment")
sys.modules.setdefault("alexandria_alignment", alignment)
compare = load_script("alexandria_compare")


class AlignmentCliRegressionTests(unittest.TestCase):
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
             patch.object(compare, "load_checkpoint", return_value={}), \
             patch.object(compare, "estimate_alignment_quality",
                          return_value=(1.0, 1, 0, 0)) as estimate, \
             patch.object(compare, "run") as run:
            compare.main()

        self.assertEqual(0.83, estimate.call_args.kwargs["threshold"])
        self.assertEqual(0.83, run.call_args.kwargs["threshold"])


if __name__ == "__main__":
    unittest.main()
