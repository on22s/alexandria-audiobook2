import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import run_stage4_checkpoint as stage4
import run_stage7_pitch as stage7
from results_index_validation import require_results_index_entries


NAMES = ('pitch_profile_matrix_pilot.json', 'pitch_profile_matrix.json')


def markdown(names=NAMES):
    return '| artifact | why |\n|---|---|\n' + ''.join(f'| `{name}` | TTS provenance artifact |\n' for name in names)


def csv(names=NAMES):
    return 'artifact,note\n' + ''.join(f'{name},TTS provenance artifact\n' for name in names)


class ResultsIndexValidationTests(unittest.TestCase):
    def test_only_exact_canonical_records_are_accepted(self):
        bad_md = (' '.join(NAMES), markdown(tuple(name+'.old' for name in NAMES)),
                  '<!--\n'+markdown()+'-->', '```markdown\n'+markdown()+'```',
                  '~~~\n'+markdown()+'~~~', '| note | why |\n|---|---|\n'+markdown().split('\n', 2)[2])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'results_index.csv').write_text(csv())
            for text in bad_md:
                with self.subTest(md=text):
                    (root/'RESULTS_INDEX.md').write_text(text)
                    with self.assertRaisesRegex(RuntimeError, 'RESULTS_INDEX.md.*missing artifact records'):
                        require_results_index_entries(root, *NAMES)
            (root/'RESULTS_INDEX.md').write_text(markdown())
            for text in ('note\n'+','.join(NAMES), 'artifact,note\nother.json,"'+','.join(NAMES)+'"\n',
                         csv(tuple(name+'.old' for name in NAMES))):
                with self.subTest(csv=text):
                    (root/'results_index.csv').write_text(text)
                    with self.assertRaisesRegex(RuntimeError, 'results_index.csv'):
                        require_results_index_entries(root, *NAMES)
            (root/'results_index.csv').write_text(csv())
            require_results_index_entries(root, *NAMES)

    def test_each_requested_record_must_be_in_both_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for missing in ('RESULTS_INDEX.md', 'results_index.csv'):
                (root/'RESULTS_INDEX.md').write_text(markdown(NAMES[:1] if missing.endswith('.md') else NAMES))
                (root/'results_index.csv').write_text(csv(NAMES[:1] if missing.endswith('.csv') else NAMES))
                with self.subTest(index=missing), self.assertRaisesRegex(RuntimeError, NAMES[1]):
                    require_results_index_entries(root, *NAMES)

    def test_stage4_preserves_validation_error_type_and_stage7_stops_before_unit_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'RESULTS_INDEX.md').write_text(' '.join(NAMES))
            (root/'results_index.csv').write_text(csv())
            with patch.object(stage4, 'REPO', str(root)):
                with self.assertRaises(stage4.ArtifactValidationError): stage4.require_index_entries(*NAMES)
            with patch.object(stage7, 'REPO', str(root)), \
                 patch.object(stage7, 'load_adapters', return_value=[{'adapter': 'fixture'}]*75), \
                 patch.object(stage7, 'ensure_pilot'), patch.object(stage7, 'ensure_full'), \
                 patch.object(stage7, 'run') as run, contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'missing artifact records'): stage7.main()
            self.assertEqual(1, run.call_count)
            self.assertEqual('refresh_indexes.py', run.call_args.args[0][1])

    def test_csv_quoted_note_mentions_do_not_supply_missing_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'RESULTS_INDEX.md').write_text(markdown())
            (root/'results_index.csv').write_text('artifact,note\n'+NAMES[0]+',"mention '+NAMES[1]+'\nmore text"\n')
            with self.assertRaisesRegex(RuntimeError, NAMES[1]): require_results_index_entries(root, *NAMES)
