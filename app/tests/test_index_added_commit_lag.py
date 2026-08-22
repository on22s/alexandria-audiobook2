"""The one index difference that cannot be avoided by regenerating first.

`added_commit` is `git log --diff-filter=A` for the artifact, so it is EMPTY
until the commit that adds the file exists. Every commit introducing an
artifact therefore leaves the index stale by exactly that field, and CI fails
on a commit that could not have been written any other way. It cost five CI
round trips across #396, #397, #399, #404 and #405.

The tolerance is deliberately narrow: only empty -> value, only in that one
column, per row. A wrong sha, a changed sha, a removed sha, a new row, a
missing row or any other cell is still stale. These tests exist to keep it
narrow - a forgiving check that forgives too much is worse than the round
trips it saves.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

HEAD = "artifact,tier,added_commit,note"


def csv(*rows):
    return "\n".join([HEAD] + list(rows)) + "\n"


def _fn():
    """Import the helper without executing collect_results' main body."""
    import ast
    src = open(os.path.join(REPO, "collect_results.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_only_added_commit_filled_in":
            module = ast.Module(body=[node], type_ignores=[])
            scope = {}
            exec(compile(module, "<helper>", "exec"), scope)   # noqa: S102
            return scope["_only_added_commit_filled_in"]
    raise AssertionError("_only_added_commit_filled_in not found")


class AddedCommitLagTests(unittest.TestCase):
    def setUp(self):
        self.only = _fn()

    def test_empty_becoming_a_sha_is_forgiven(self):
        old = csv("a.json,tier,,note")
        new = csv("a.json,tier,dde12143,note")
        self.assertTrue(self.only(old, new))

    def test_identical_files_are_forgiven(self):
        same = csv("a.json,tier,dde12143,note")
        self.assertTrue(self.only(same, same))

    def test_a_changed_sha_is_NOT_forgiven(self):
        old = csv("a.json,tier,aaaaaaaa,note")
        new = csv("a.json,tier,bbbbbbbb,note")
        self.assertFalse(self.only(old, new))

    def test_a_sha_being_REMOVED_is_not_forgiven(self):
        old = csv("a.json,tier,dde12143,note")
        new = csv("a.json,tier,,note")
        self.assertFalse(self.only(old, new))

    def test_a_change_in_any_other_column_is_not_forgiven(self):
        old = csv("a.json,tier,,note")
        new = csv("a.json,OTHER,dde12143,note")
        self.assertFalse(self.only(old, new))

    def test_a_new_row_is_not_forgiven(self):
        old = csv("a.json,tier,,note")
        new = csv("a.json,tier,dde12143,note", "b.json,tier,,note")
        self.assertFalse(self.only(old, new))

    def test_a_removed_row_is_not_forgiven(self):
        old = csv("a.json,tier,,note", "b.json,tier,,note")
        new = csv("a.json,tier,dde12143,note")
        self.assertFalse(self.only(old, new))

    def test_a_changed_header_is_not_forgiven(self):
        old = csv("a.json,tier,,note")
        new = "artifact,tier,added_commit,CHANGED\na.json,tier,dde12143,note\n"
        self.assertFalse(self.only(old, new))

    def test_a_file_without_the_column_is_not_forgiven(self):
        old = "artifact,tier\na.json,tier\n"
        new = "artifact,tier\na.json,OTHER\n"
        self.assertFalse(self.only(old, new))

    def test_empty_input_is_not_forgiven(self):
        self.assertFalse(self.only("", ""))

    def test_several_rows_filling_in_at_once_are_forgiven(self):
        # A commit adding three artifacts fills three shas.
        old = csv("a.json,t,,n", "b.json,t,,n", "c.json,t,,n")
        new = csv("a.json,t,11111111,n", "b.json,t,22222222,n", "c.json,t,33333333,n")
        self.assertTrue(self.only(old, new))

    def test_one_good_fill_and_one_bad_change_is_not_forgiven(self):
        # The mixed case: forgiving this would let a real change ride along.
        old = csv("a.json,t,,n", "b.json,t,aaaaaaaa,n")
        new = csv("a.json,t,11111111,n", "b.json,t,bbbbbbbb,n")
        self.assertFalse(self.only(old, new))
