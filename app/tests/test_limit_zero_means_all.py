"""`--limit 0` means all, in every script that shares an artifact.

ljspeech_generate writes `rows`, prosody_fidelity reads them, and a chain
passes the same --limit to both. In the producer 0 meant "all"; in the consumer
`rows[:0]` was the empty list, so on 2026-08-21 a chain passing --limit 0 read
an artifact whose 20 rows all had their human and generated wavs present on
disk, scored none of them, and exited "no human/generated pair could be
resolved". The failure was loud and pointed at the wrong thing.

Two scripts sharing an artifact must share the convention for reading it.
"""
import ast
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
EXPERIMENTS = REPO / "app" / "experiments"
# Scripts that read `rows` from an artifact another one produced.
SHARE_THE_CONVENTION = ["prosody_fidelity.py", "ljspeech_generate.py",
                        "ljspeech_score.py"]


class LimitConventionTest(unittest.TestCase):
    def test_no_script_slices_rows_by_a_bare_limit(self):
        """`x[:args.limit]` without a falsy guard turns 0 into 'none'."""
        offenders = []
        for name in SHARE_THE_CONVENTION:
            path = EXPERIMENTS / name
            if not path.exists():
                continue
            src = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Subscript):
                    continue
                sl = node.slice
                if not isinstance(sl, ast.Slice) or sl.lower is not None:
                    continue
                upper = ast.unparse(sl.upper) if sl.upper else ""
                if "limit" not in upper:
                    continue
                # Guarded forms read `a[:limit] if limit else a`
                parent_ok = f"if args.limit else" in src[
                    max(0, node.col_offset):src.index("\n", src.index(upper)) + 80] \
                    if upper in src else False
                if not parent_ok:
                    offenders.append(f"{name}: [:{upper}]")
        self.assertEqual([], offenders,
                         "0 must mean all - guard with `if limit else rows`")

    def test_the_help_text_says_what_zero_does(self):
        src = (EXPERIMENTS / "prosody_fidelity.py").read_text(encoding="utf-8")
        self.assertIn("0 means all", src)

    def test_slicing_semantics_directly(self):
        rows = [1, 2, 3]
        for limit, expected in ((0, rows), (2, [1, 2]), (99, rows)):
            with self.subTest(limit=limit):
                self.assertEqual(expected,
                                 rows[:limit] if limit else rows)


if __name__ == "__main__":
    unittest.main()
