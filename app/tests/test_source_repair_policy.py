"""Both public residual composition orders and the actual CLI share repair decisions."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch
import three_pass_generate as three_pass
from repair_source_encoding import repair, preflight_source
from source_normalization import neutralize_lossy_residue
from three_pass_generate import prepare_source_text


class SourceRepairPolicyTests(unittest.TestCase):
    def test_default_residue_and_repair_composition_use_the_same_rules_in_either_order(self):
        for raw in ('don�t stop.', 'Magic�Fiction', 'unclear �', 'coup d��tat', 'unreadable ���'):
            with self.subTest(source=raw):
                canonical=repair(raw)[0]
                neutralized,count=neutralize_lossy_residue(raw)
                first=repair(neutralized)[0]
                second=neutralize_lossy_residue(canonical)[0]
                self.assertEqual(canonical,neutralized)
                self.assertEqual(first,second)
                self.assertEqual(raw.count('�')-neutralized.count('�'),count)
        self.assertIn('���',neutralize_lossy_residue('unreadable ���')[0])
        self.assertEqual(("unclear ?",1),neutralize_lossy_residue('unclear �',substitute='?'))

    def test_three_pass_cli_gate_matches_shared_preflight_without_consuming_evidence_first(self):
        padding='Ordinary source prose. '*100
        for damage in ('don�t stop.', 'unclear �', 'Magic�Fiction', 'a damaged ��� site'):
            with self.subTest(damage=damage),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'source.txt';path.write_text(padding+damage)
                before=path.read_bytes();raw=path.read_text()
                canonical=preflight_source(raw)['text']
                with contextlib.redirect_stdout(io.StringIO()):
                    actual,report=prepare_source_text(raw)
                self.assertEqual(canonical,actual)
                self.assertEqual(actual.count('�'),report.get('unresolved'))
                self.assertEqual(raw.count('�'),report['repaired']+report['residual']+report.get('unresolved'))
                self.assertEqual(before,path.read_bytes())

    def test_actual_cli_prepares_native_file_and_reports_unresolved_before_model_setup(self):
        class PreparedCheckpoint(Exception):
            pass
        source=' '.join(f'Ordinary unique sentence number {i}.' for i in range(100))+' A damaged ��� site.'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'source.txt';path.write_text(source);before=path.read_bytes()
            prepared=[]
            actual_prepare=three_pass.prepare_source_text
            def prepare(text):
                result=actual_prepare(text)
                prepared.append((text,result))
                return result
            with patch.object(sys,'argv',['three_pass_generate.py',str(path)]), \
                 patch.object(three_pass,'prepare_source_text',side_effect=prepare), \
                 patch.object(three_pass,'load_app_config',side_effect=PreparedCheckpoint), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(PreparedCheckpoint):
                    three_pass.main()
            self.assertEqual(1,len(prepared))
            input_text,(actual,report)=prepared[0]
            self.assertEqual(preflight_source(input_text)['text'],actual)
            self.assertIn('���',actual)
            self.assertEqual(3,report.get('unresolved'))
            self.assertIn('Retained 3 unresolved replacement character(s)',output.getvalue())
            self.assertEqual(before,path.read_bytes())
