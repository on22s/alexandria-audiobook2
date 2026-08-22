"""Static faults that make an experiment script unrunnable, across all of them.

WHY THIS EXISTS. On 2026-08-21 two_stage_attribution.main() read `narrators`
eleven lines before assigning it, so every invocation raised UnboundLocalError
before issuing a single request. It shipped in #368 and was found only when all
five arms of a chain failed rc=1 in under four seconds each - hours of GPU time
queued behind a script that could never run.

Nothing in the suite exercised main(). Most of these scripts cannot be unit
tested: they need a GPU, a server, or a corpus. But the fault that broke #400
is visible in the AST without running anything, and so are its neighbours. This
sweeps all 223 of them for the cheap classes.

WHAT IT DELIBERATELY DOES NOT DO. It does not import the modules. Importing
223 experiment scripts pulls in torch, transformers and whisper, takes minutes,
and fails on machines without them - a test that cannot run is not a test.
"""
import ast
import os
import unittest

EXPERIMENTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "experiments")

# Comprehensions get their own scope in Python 3, so a name bound inside one is
# unrelated to a same-named local outside it. A first version of this check did
# not exclude them and reported four false positives on the first file it read.
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# A nested def or lambda is a SEPARATE scope. Walking into one produced 17
# false positives on the first real run - `def contour(path)` nested inside a
# function that later assigns its own `path` is not a fault, and neither is
# `lambda i: ...` beside a later `i = 0`. Only a function's OWN scope counts.
NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def scripts():
    for name in sorted(os.listdir(EXPERIMENTS)):
        if name.endswith(".py") and not name.startswith("_"):
            yield name, os.path.join(EXPERIMENTS, name)


def parse(path):
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _other_scope_node_ids(func):
    """-> ids of nodes belonging to a scope other than `func`'s own."""
    ids = set()
    for node in ast.walk(func):
        if node is func:
            continue
        if isinstance(node, COMPREHENSIONS + NESTED_SCOPES):
            for inner in ast.walk(node):
                ids.add(id(inner))
            # The nested function's NAME is bound in this scope, so keep it.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ids.discard(id(node))
    return ids


def read_before_assignment(func):
    """-> [(name, read_line, assigned_line)] for locals used too early.

    Only flags names that ARE assigned somewhere in the function: a name never
    assigned locally is a global or an import, not this fault.
    """
    skip = _other_scope_node_ids(func)
    params = {a.arg for a in func.args.args}
    params |= {a.arg for a in getattr(func.args, "kwonlyargs", [])}
    if func.args.vararg:
        params.add(func.args.vararg.arg)
    if func.args.kwarg:
        params.add(func.args.kwarg.arg)
    # A name bound by `global`/`nonlocal` is not a local at all.
    for node in ast.walk(func):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            params.update(node.names)

    # TWO PASSES, because ast.walk is BREADTH-first: it can yield a Store on a
    # later line before a Load on an earlier one, which made a single-pass
    # version of this silently return nothing for the very fault it was
    # written to catch. Collect the earliest assignment for every name first.
    assigned = {}
    for node in ast.walk(func):
        # `except ... as e` binds e for the handler's duration and Python then
        # DELETES it. Without this, an unrelated later `e = ...` made the
        # handler's own use of `e` look like a read-before-assignment.
        if isinstance(node, ast.ExceptHandler) and node.name:
            line = assigned.get(node.name)
            if line is None or node.lineno < line:
                assigned[node.name] = node.lineno
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node is not func and id(node) not in skip):
            line = assigned.get(node.name)
            if line is None or node.lineno < line:
                assigned[node.name] = node.lineno
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
                and id(node) not in skip):
            line = assigned.get(node.id)
            if line is None or node.lineno < line:
                assigned[node.id] = node.lineno

    offenders = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Name) or id(node) in skip:
            continue
        if isinstance(node.ctx, ast.Load) and node.id not in params:
            at = assigned.get(node.id)
            if at is not None and node.lineno < at:
                offenders.append((node.id, node.lineno, at))
    return sorted(set(offenders))


class ReadBeforeAssignmentTests(unittest.TestCase):
    def test_no_experiment_function_reads_a_local_before_assigning_it(self):
        """The exact fault that made every attribution arm fail on 2026-08-21."""
        faults = []
        for name, path in scripts():
            tree = parse(path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for var, read, at in read_before_assignment(node):
                        faults.append("%s:%d %s() reads %r, assigned at %d"
                                      % (name, read, node.name, var, at))
        self.assertEqual(faults, [], "read-before-assignment:\n  " +
                         "\n  ".join(faults))

    def test_the_check_catches_a_planted_fault(self):
        """A checker that cannot fail is not a check."""
        broken = ast.parse("def f():\n    x = y\n    y = 1\n").body[0]
        self.assertEqual([o[0] for o in read_before_assignment(broken)], ["y"])

    def test_an_exception_binding_is_not_a_fault(self):
        # tts_output_validation.py: `except ... as e` beside an unrelated
        # `e = sum(...)` forty lines later.
        ok = ast.parse("def f():\n"
                       "    try:\n"
                       "        pass\n"
                       "    except ValueError as e:\n"
                       "        print(e)\n"
                       "    e = 1\n"
                       "    return e\n").body[0]
        self.assertEqual(read_before_assignment(ok), [])

    def test_a_nested_function_parameter_is_not_a_fault(self):
        # ljspeech_score.py: `def contour(path)` nested in a function that
        # later assigns its own `path`. Two scopes, no fault.
        ok = ast.parse("def f(a):\n"
                       "    def g(path):\n"
                       "        return path\n"
                       "    path = g(a)\n"
                       "    return path\n").body[0]
        self.assertEqual(read_before_assignment(ok), [])

    def test_a_lambda_parameter_is_not_a_fault(self):
        # ecapa_duration_confound.py: `lambda i: values[i]` beside `i = 0`.
        ok = ast.parse("def f(vs):\n"
                       "    o = sorted(vs, key=lambda i: vs[i])\n"
                       "    i = 0\n"
                       "    return o, i\n").body[0]
        self.assertEqual(read_before_assignment(ok), [])

    def test_a_comprehension_variable_is_not_a_fault(self):
        # `p` here is comprehension-scoped and unrelated to the later `p`.
        ok = ast.parse("def f(xs):\n    a = [q for q in xs]\n    q = 1\n"
                       "    return a, q\n").body[0]
        self.assertEqual(read_before_assignment(ok), [])

    def test_a_parameter_is_not_a_fault(self):
        ok = ast.parse("def f(n):\n    print(n)\n    n = 2\n").body[0]
        self.assertEqual(read_before_assignment(ok), [])

    def test_a_global_is_not_a_fault(self):
        ok = ast.parse("def f():\n    global g\n    print(g)\n    g = 1\n").body[0]
        self.assertEqual(read_before_assignment(ok), [])
