"""Execute measurement chains with isolated CPU generator and audio fixtures."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]


class ChainMeasurementScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name, 'repository with spaces')
        for directory in ('run_chains/lib', 'app/env/bin', 'app/experiments',
                          'app/fixtures', 'bin', 'ab_test_runtime/experiments'):
            (self.root / directory).mkdir(parents=True)
        for relative in ('run_chains/dialogue_map_5_3_20260826.sh',
                         'run_chains/anchor_and_separator_table_20260826.sh',
                         'app/experiments/respelling_completion.py',
                         'run_chains/lib/stage.sh', 'run_chains/lib/queue.sh', 'run_chains/lib/server_cleanup.sh'):
            shutil.copyfile(REPO / relative, self.root / relative)
        self._write('bin/pgrep', '#!/bin/sh\nexit 1\n')
        self._write('gpu_job.sh', '#!/bin/bash\nif [ "${1:-}" = --print-source-state ]; then exec bash '+shlex.quote(str(REPO/'gpu_job.sh'))+' "$@"; fi\nshift\nexec "$@"\n')
        # Dispatch model stages to tiny JSON fixtures, but execute the actual
        # fidelity CLI and actual pause-scoring row selection/publication code.
        self._write('app/env/bin/python', '#!' + sys.executable + '\n' + r"""
import importlib.util,json,os,pathlib,sys
from unittest.mock import patch
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
source=pathlib.Path(os.environ['SOURCE_REPO'])
args=sys.argv[1:]
if args[0]=='-':
    os.execv(sys.executable,[sys.executable]+args)
if args[0]=='-u':args=args[1:]
name=pathlib.Path(args.pop(0)).name
if name=='three_pass_vs_single.py':
    start=args.index('--books')+1
    books=[]
    for item in args[start:]:
        if item.startswith('--'):break
        books.append(item)
    work=pathlib.Path(args[args.index('--work')+1]);work.mkdir(parents=True,exist_ok=True)
    for book in books:
        for arm in ('single','three_pass'):
            (work/(book+'__'+arm+'.json')).write_text(json.dumps([{'text':'"Hello."'}]))
elif name=='script_text_fidelity.py':
    os.execv(sys.executable,[sys.executable,str(source/'app/experiments'/name)]+args)
elif name=='respelling_completion.py':
    os.execv(sys.executable,[sys.executable,str(source/'app/experiments'/name)]+args)
elif name=='measure_pauses.py':
    spec=importlib.util.spec_from_file_location('fixture_measure_pauses',source/'app/experiments'/name)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    sys.argv=[name]+args
    # These fixtures verify requested row scope, not acoustic measurements.
    with patch.object(mod,'REPO',str(root)),patch.object(mod,'internal_pauses',return_value=(0,0.0)),patch.object(mod,'provenance',return_value={'fixture':'CPU scope test'}):
        mod.main()
""")
        (self.root / 'fixture.txt').write_text('native Git fixture\n')
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),
                     ('config','user.email','fixture@example.com'),('config','core.hooksPath',str(self.root/'no-hooks')),
                     ('add','fixture.txt'),('commit','-q','-m','fixture baseline')):
            subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        self.env = dict(os.environ, PATH=str(self.root / 'bin') + os.pathsep + os.environ['PATH'],
                        FIXTURE_ROOT=str(self.root), SOURCE_REPO=str(REPO))

    def _write(self, relative, source):
        path = self.root / relative
        path.write_text(source, encoding='utf-8')
        path.chmod(0o755)

    def _run(self, name, expected_status=0):
        # Commit the private fixture sources; real preflight must see a clean tree.
        subprocess.run(['git', '-C', str(self.root), 'add', '--', '.', ':(exclude)ab_test_runtime'],
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', str(self.root), 'commit', '--allow-empty', '-qm', 'fixture sources'],
                       check=True, capture_output=True)
        result = subprocess.run(['bash', str(self.root / 'run_chains' / name)],
                                cwd=self.root, env=self.env, capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(expected_status, result.returncode, result.stdout + result.stderr)
        return result

    def test_fidelity_artifact_has_source_retention_for_every_generated_book(self):
        public = ('TheGambler', 'TheSignOfTheFour', 'TheMysteriousAffairAtStyles', 'AHandfulOfDust')
        expected = {'mushoku16', 'index18', 'owarimonogatari3'}
        source_text = '"Hello." Narration. "Goodbye."'
        for book in public:
            path = self.root / 'ab_test_runtime/pdnc/data' / book / 'novel_text.txt'
            path.parent.mkdir(parents=True)
            path.write_text(source_text)
            name = 'pdnc_' + book.lower()
            expected.add(name)
            (self.root / 'app/fixtures' / ('attribution_gold_' + name + '.json')).write_text('{}')
        inputs = self.root / 'ab_test_runtime/tpvs_inputs'
        inputs.mkdir()
        for book in ('mushoku16', 'index18', 'owarimonogatari3'):
            (inputs / (book + '.txt')).write_text(source_text)
        for script in ('dialogue_map_compare.py', 'script_text_fidelity.py', 'retrofit_dialogue_map.py'):
            (self.root / 'app/experiments' / script).write_text('# fixture dispatch')
        (self.root / 'app/three_pass_generate.py').write_text('# dialogue_spans fixture')
        self._run('dialogue_map_5_3_20260826.sh')
        out = self.root / 'ab_test_runtime/experiments/script_text_fidelity_fresh.json'
        books = json.loads(out.read_text())['books']
        self.assertEqual(expected, set(books))
        for book, rows in books.items():
            with self.subTest(book=book):
                self.assertEqual(2, rows.get('source_quoted_spans'))
                for arm in ('single', 'three_pass'):
                    self.assertEqual(1, rows[arm]['quote'])
                    self.assertEqual(0.5, rows[arm].get('quote_retention_vs_source'))
        for book in public:
            self.assertEqual(source_text, (self.root / 'ab_test_runtime/pdnc/data' / book / 'novel_text.txt').read_text())

    def test_separator_artifact_includes_all_1600_paired_terms_including_tail(self):
        runtime = self.root / 'ab_test_runtime'
        arms = ('respelling_measure', 'respelling_none_allrows', 'respelling_space_allrows',
                'respelling_dot_allrows', 'respelling_hyphen_allrows')
        for arm in arms:
            directory = runtime / arm
            directory.mkdir()
            suffix = '_plain.wav' if arm == 'respelling_measure' else '_respelled.wav'
            for index in range(1600):
                (directory / ('term%04d' % index + suffix)).touch()
        dot = runtime / 'experiments/respelling_dot_allrows_n1600.json'
        prior = json.dumps({'status':'complete', 'candidates_considered':1600,
                            'results':[{'term':'term%04d' % i} for i in range(1600)]})
        dot.write_text(prior)
        self._run('anchor_and_separator_table_20260826.sh')
        out = runtime / 'experiments/respelling_pauses_allrows_4arm.json'
        document = json.loads(out.read_text())
        self.assertEqual('complete', document['status'])
        self.assertEqual(1600, document['candidates_considered'])
        self.assertEqual(1600, len(document['results']))
        self.assertEqual('term1599', document['results'][-1]['term'])
        for row in document['results']:
            self.assertEqual({'term','plain','none','space','dot','hyphen_wide'}, set(row))
        self.assertEqual(prior, dot.read_text())
