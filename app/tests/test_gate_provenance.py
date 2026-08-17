"""Every artifact a promotion decision reads must name the code that made it.

87 gate artifacts exist in this tree with no provenance block at all - no
commit, no host, no model, not even a dirty flag - and goal 2.7 is built on
them:

    "All 21 were retrained on the honest split and independently gated;
     9 were promoted with a rollback receipt"
    "breathy_alto_50s_f_fantasy failed at reference-rank 1 (0.404) and
     passed at rank 2 (0.503)"

Those figures are read straight out of gate_reference_rank1__ and rank2__,
which hold 0.4044 and 0.5034 and cannot say what produced them.
`promote_adapters.py` ships voices on the strength of these files, so this is
the one experiment output where a missing provenance block has a product
consequence rather than only a record-keeping one.
"""
import ast
import os
import unittest


def _source(name):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "experiments", name), encoding="utf-8") as fh:
        return fh.read()


class GateWritesProvenanceTests(unittest.TestCase):
    # There is deliberately no separate "is provenance recorded" test: the
    # ordering test below indexes the same string and fails when it is missing,
    # so a bare assertIn added nothing. Verified by deleting the line from the
    # gate and watching this test go red.
    def test_provenance_is_attached_before_the_document_is_written(self):
        """Attaching it after json.dump would look right and record nothing."""
        source = _source("verify_adapter_identity.py")
        attached = source.index('doc["provenance"] = provenance')
        written = source.index("json.dump(doc")
        self.assertLess(attached, written)

    def test_a_provenance_failure_is_recorded_rather_than_swallowed(self):
        # The repo-wide pattern: 30 of 33 broad excepts around imports write
        # the failure into the artifact. A silent pass would leave exactly the
        # hole this test exists to close.
        source = _source("verify_adapter_identity.py")
        self.assertIn('doc["provenance"] = {"error"', source)

    def test_the_gate_still_exits_nonzero_on_failure(self):
        # Rule 9: the gate's contract is that a chain refuses to promote a
        # voice resembling nobody. Provenance must not have disturbed it.
        source = _source("verify_adapter_identity.py")
        self.assertIn("sys.exit(0 if ok else 3)", source)


class ProvenanceHelperTests(unittest.TestCase):
    def test_the_helper_exists_and_takes_file_and_args(self):
        source = _source("provenance.py")
        tree = ast.parse(source)
        func = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "provenance"),
                    None)
        self.assertIsNotNone(func, "provenance() must exist for the gate to call")
        names = [a.arg for a in func.args.args]
        self.assertEqual(["script_file", "args"], names[:2])


if __name__ == "__main__":
    unittest.main()
