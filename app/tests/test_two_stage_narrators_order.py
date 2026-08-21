"""two_stage_attribution must not read `narrators` before it is built.

The line recording the narrator map into the artifact sat ~11 lines ABOVE the
loop that fills it, so every invocation raised UnboundLocalError before making
a single request. It shipped in #368 and was found on 2026-08-21 when all five
arms of the explicit_hint chain failed rc=1 within four seconds each.

Nothing in the unit suite exercised `main()`, so nothing caught it. This test
does not run main either - it needs a server - but it pins the ORDERING fault
that made main unrunnable, which is cheap and would have caught it.
"""
import ast
import os
import unittest

MODULE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "experiments", "two_stage_attribution.py")


def _main_body():
    tree = ast.parse(open(MODULE, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "main":
            return node
    raise AssertionError("two_stage_attribution has no main()")


class NarratorOrderingTests(unittest.TestCase):
    def test_narrators_is_assigned_before_every_read(self):
        main = _main_body()
        first_assign, first_read = None, None
        for node in ast.walk(main):
            if isinstance(node, ast.Name) and node.id == "narrators":
                if isinstance(node.ctx, ast.Store):
                    if first_assign is None or node.lineno < first_assign:
                        first_assign = node.lineno
                elif isinstance(node.ctx, ast.Load):
                    if first_read is None or node.lineno < first_read:
                        first_read = node.lineno
        self.assertIsNotNone(first_assign, "narrators is never assigned")
        self.assertIsNotNone(first_read, "narrators is never read")
        self.assertLess(
            first_assign, first_read,
            "narrators is read at line %s but not assigned until line %s - "
            "main() raises UnboundLocalError before making a request"
            % (first_read, first_assign))

    def test_no_local_in_main_is_read_before_assignment(self):
        """The general form, so the NEXT one of these is caught too.

        Comprehensions must be excluded: they have their OWN scope in Python 3,
        so a `p` inside `[... for p in xs]` is unrelated to a `p` assigned
        later in the function. A first version of this test did not exclude
        them and reported four such names as read-before-assignment - a check
        that cries wolf is worse than no check, and this one nearly shipped.
        """
        main = _main_body()
        comprehension_nodes = set()
        for node in ast.walk(main):
            if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                                 ast.GeneratorExp)):
                for inner in ast.walk(node):
                    comprehension_nodes.add(id(inner))

        assigned, offenders = {}, []
        params = {a.arg for a in main.args.args}
        for node in ast.walk(main):
            if not isinstance(node, ast.Name) or id(node) in comprehension_nodes:
                continue
            if isinstance(node.ctx, ast.Store):
                assigned.setdefault(node.id, node.lineno)
            elif isinstance(node.ctx, ast.Load) and node.id not in params:
                at = assigned.get(node.id)
                if at is not None and node.lineno < at:
                    offenders.append((node.id, node.lineno, at))
        self.assertEqual(offenders, [], "read before assignment in main(): %r"
                         % (offenders,))
