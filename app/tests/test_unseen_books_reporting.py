"""A book that failed must not let the run report DONE.

grimgar06 died on 2026-08-19 with rc=1 ("chunk 29/70 failed validation after
retries"), wrote no output, and the chain printed UNSEEN BOOKS DONE and exited
0 - so gpu_job.sh logged OK and the driver counted the stage as a pass. One of
four books produced nothing and every layer above it said success.
"""
import os
import subprocess
import tempfile
import unittest
import shlex
import re
from tests.unseen_fixture_support import COMPLETION_FIXTURE_CODE

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAIN = os.path.join(REPO, "run_chains", "unseen_books_20260819b.sh")


@unittest.skipUnless(os.path.exists(CHAIN), "chain not present")
class UnseenBooksReportingTest(unittest.TestCase):
    """Runs the real loop with a stub generator, so exit codes are exercised."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _harness(self, outcomes, partial_book=None):
        """outcomes: {book: "ok"|"fail"|"empty"} -> (returncode, stdout)."""
        out_dir = os.path.join(self.tmp.name, "out")
        log_dir = os.path.join(self.tmp.name, "logs")
        os.makedirs(out_dir)
        os.makedirs(log_dir)
        if partial_book:
            import json
            with open(os.path.join(out_dir,partial_book+'.json'),'w') as stream:
                json.dump([{'text':str(i)} for i in range(51)],stream)
        for book in outcomes:
            Path = __import__('pathlib').Path
            Path(self.tmp.name,book+'.txt').write_text('source for '+book)
        producer = os.path.join(self.tmp.name, 'producer.py')
        with open(producer,'w') as stream:
            stream.write(COMPLETION_FIXTURE_CODE + "\nimport sys\nsave_complete_fixture(sys.argv[1],sys.argv[2],[{'text':str(n)} for n in range(60)])\n")
        with open(CHAIN, encoding="utf-8") as handle:
            source = handle.read()

        def block(marker, opener):
            start = source.index(opener)
            depth, i = 0, start
            while True:
                if source[i] == "{":
                    depth += 1
                elif source[i] == "}":
                    depth -= 1
                    if depth == 0:
                        return source[start:i + 1]
                i += 1

        loop_start = source.index("failed_books=0;")
        loop_end = source.index('echo "UNSEEN BOOKS DONE')
        tail_end = source.index("\n", loop_end)
        body = source[loop_start:tail_end]

        script = "\n".join([
            "set -uo pipefail",
            "OUT=%r; L=%r; IN=%r; PY=python3" % (out_dir, log_dir, self.tmp.name),
            block("book_complete", "book_complete() {"),
            # Stub the generator: writes a complete book, an empty one, or fails.
            "generate() { case $1 in %s esac; }" % " ".join(
                "%s) %s;;" % (book, {
                    "ok": "PYTHONPATH=" + shlex.quote(os.path.join(REPO,'app')) + " python3 " + shlex.quote(producer) + ' "$2" "$IN/$1.txt"; return 0',
                    "fail": "return 1",
                    "empty": "return 0",
                }[state]) for book, state in outcomes.items()),
            body.replace(
                'timeout --kill-after=120s 43200 "$PY" -u generate_script.py "$IN/$book.txt" \\\n'
                '        --output "$OUT/$book.json" > "$L/unseen_$book.log" 2>&1',
                'generate "$book" "$OUT/$book.json" > "$L/unseen_$book.log" 2>&1'),
        ])
        result = subprocess.run(["bash", "-c", script], capture_output=True,
                                text=True, timeout=120)
        return result

    def test_a_failed_book_makes_the_run_fail(self):
        result = self._harness({"mushoku18": "ok", "grimgar06": "fail",
                                "mushoku23": "ok", "arc4_volume10wn": "ok"})
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("INCOMPLETE", result.stdout)
        self.assertIn("grimgar06", result.stdout)
        self.assertNotIn("UNSEEN BOOKS DONE", result.stdout)

    def test_a_book_that_exits_zero_but_writes_nothing_also_fails(self):
        """rc=0 is not proof of an artifact - check the file too."""
        result = self._harness({"mushoku18": "ok", "grimgar06": "empty",
                                "mushoku23": "ok", "arc4_volume10wn": "ok"})
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn("grimgar06", result.stdout)

    def test_partial_51_entry_book_is_regenerated_by_the_actual_chain_loop(self):
        result=self._harness({b:'ok' for b in ('mushoku18','grimgar06','mushoku23','arc4_volume10wn')},
                             partial_book='mushoku18')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertNotIn('SKIP - already generated',result.stdout)
        self.assertEqual(4,result.stdout.count('rc=0'))
        self.assertIn('UNSEEN BOOKS DONE',result.stdout)

    def test_all_books_succeeding_reports_done(self):
        result = self._harness({b: "ok" for b in
                                ("mushoku18", "grimgar06", "mushoku23",
                                 "arc4_volume10wn")})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("UNSEEN BOOKS DONE", result.stdout)
        self.assertNotIn("INCOMPLETE", result.stdout)


if __name__ == "__main__":
    unittest.main()


class UnseenTimeoutBackstopTests(unittest.TestCase):
    def test_actual_timeout_options_kill_an_owned_child_ignoring_term(self):
        from pathlib import Path
        import shlex
        import signal
        import sys
        import time
        source = Path(CHAIN).read_text()
        start = source.index('timeout ', source.index('failed_books=0;'))
        end = source.index('"$PY"', start)
        options = shlex.split(source[start:end])
        self.assertEqual('timeout', options[0])
        self.assertEqual('43200', options[-1])
        # Keep the actual signal/escalation arguments, shortening only durations.
        arguments = [argument.replace('120s', '0.2s') for argument in options]
        arguments[-1] = '0.2s'
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp, 'started')
            program = ('import os,pathlib,signal,sys,time; '
                       'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                       'signal.signal(signal.SIGINT, signal.SIG_IGN); '
                       'pathlib.Path(sys.argv[1]).write_text(str(os.getpgrp())); '
                       'time.sleep(30)')
            process = subprocess.Popen(arguments + [sys.executable, '-c', program, str(marker)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            did_timeout = False
            try:
                try:
                    stdout, stderr = process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    did_timeout = True
            finally:
                if process.poll() is None:
                    # Only this test-owned session; never match a process name or command line.
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=5)
            self.assertTrue(marker.is_file(), 'The CPU fixture never reached its signal handlers')
            self.assertEqual(str(process.pid), marker.read_text())
            self.assertFalse(did_timeout, 'A TERM-ignoring child survived the timeout with no escalation')
            self.assertEqual(-signal.SIGKILL, process.returncode)


class UnseenCheckoutPathsTests(unittest.TestCase):
    def _run_checkout(self, force_initial_root=False, failed_book=''):
        import json
        import shlex
        import sys
        from pathlib import Path
        with tempfile.TemporaryDirectory(prefix='unseen checkout ') as tmp:
            base = Path(tmp)
            checkout = base / 'owned checkout'
            decoy = base / 'other checkout'
            app = checkout / 'app'
            logs = checkout / 'ab_test_runtime/logs'
            inputs = checkout / 'ab_test_runtime/results/collect_all_20260722-155801/inputs'
            output = checkout / 'ab_test_runtime/unseen_books'
            binaries = base / 'bin'
            for directory in (checkout / 'run_chains', app / 'env/bin', logs, inputs,
                              binaries, decoy / 'app'):
                directory.mkdir(parents=True, exist_ok=True)
            config = b'{"llm":{"model_name":"original"},"llm_local":{"model_name":"local"},"keep":17}\n'
            (app / 'config.json').write_bytes(config)
            (decoy / 'app/config.json').write_bytes(config)
            source = Path(CHAIN).read_text()
            # Adapt an older literal checkout; the fixed chain resolves its own root.
            initial_root = checkout if force_initial_root else decoy
            match = re.search(r'(?m)^REPO=(/[^\n ]+)$', source)
            if match:
                original_checkout = match.group(1)
                source = source.replace('REPO=' + original_checkout,
                                        'REPO=' + shlex.quote(str(initial_root)))
                source = source.replace(original_checkout + '/app/config.json',
                                        str(decoy / 'app/config.json'))
            chain = checkout / 'run_chains/unseen_books_20260819b.sh'
            chain.write_text(source)
            chain.chmod(0o755)
            wrapper = checkout / 'gpu_job.sh'
            wrapper.write_text('#!/bin/bash\nset -eu\nprintf "%s\\n" "$1" >> "$(dirname "$0")/wrapper_calls"\nif [ "$1" = --check-lock-owner ]; then exit 0; fi\nshift\nexec "$@"\n')
            wrapper.chmod(0o755)
            wrong_wrapper = decoy / 'gpu_job.sh'
            wrong_wrapper.write_text('#!/bin/bash\nprintf wrong > "$(dirname "$0")/wrong_root"\nexit 91\n')
            wrong_wrapper.chmod(0o755)
            library=checkout/'run_chains/lib';library.mkdir()
            (library/'config_backup.sh').write_bytes(Path(REPO,'run_chains/lib/config_backup.sh').read_bytes())
            for module_name in ('generation_completion.py', 'source_encoding.py'):
                (app / module_name).write_bytes(Path(REPO, 'app', module_name).read_bytes())
            (app / 'env/bin/python').symlink_to(sys.executable)
            curl = binaries / 'curl'
            curl.write_text('#!/bin/bash\nprintf qwen3\n')
            curl.chmod(0o755)
            books = ('mushoku18', 'grimgar06', 'mushoku23', 'arc4_volume10wn')
            for book in books:
                (inputs / (book + '.txt')).write_text('source for ' + book)
            producer = app / 'generate_script.py'
            producer.write_text(COMPLETION_FIXTURE_CODE + """import argparse,json,os,pathlib,sys
p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('--output');a=p.parse_args()
source=pathlib.Path(a.source);root=pathlib.Path(__file__).parent.parent
config=json.loads((root/'app/config.json').read_text())
assert config['llm']['model_name']=='qwen3-14b',config
assert config['llm_local']['model_name']=='qwen3-14b',config
assert source.read_text()=='source for '+source.stem
with (root/'observations').open('a') as f:f.write(json.dumps({'source':str(source),'out':a.output,'config':config})+'\\n')
if source.stem==os.environ.get('FAILED_BOOK'):sys.exit(2)
save_complete_fixture(a.output,source,[{'i':i,'text':source.stem} for i in range(60)])
print('Got 60 entries')
""")
            env = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                       FAILED_BOOK=failed_book)
            env.pop('ALEXANDRIA_GPU_LOCK_HELD', None)
            result = subprocess.run(['bash', str(chain)], cwd=base, env=env,
                                    capture_output=True, text=True, timeout=15)
            diagnostic = result.stdout + result.stderr
            self.assertEqual(1 if failed_book else 0, result.returncode, diagnostic)
            self.assertFalse((decoy / 'wrong_root').exists(), diagnostic)
            self.assertEqual('unseen_books_b\n--check-lock-owner\n', (checkout / 'wrapper_calls').read_text())
            self.assertEqual(config, (app / 'config.json').read_bytes())
            self.assertEqual(config, (decoy / 'app/config.json').read_bytes())
            self.assertFalse((logs / 'config.json.unseen_backup').exists())
            observations = [json.loads(line) for line in (checkout / 'observations').read_text().splitlines()]
            self.assertEqual(4, len(observations))
            for book, observation in zip(books, observations):
                self.assertEqual(str(inputs / (book + '.txt')), observation['source'])
                self.assertEqual(str(output / (book + '.json')), observation['out'])
                self.assertEqual(17, observation['config']['keep'])
                self.assertEqual('source for ' + book, (inputs / (book + '.txt')).read_text())
                artifact = output / (book + '.json')
                if book == failed_book:
                    self.assertFalse(artifact.exists())
                    self.assertIn(book, result.stdout)
                    self.assertIn('INCOMPLETE', result.stdout)
                    self.assertNotIn('UNSEEN BOOKS DONE', result.stdout)
                else:
                    entries = json.loads(artifact.read_text())
                    self.assertEqual(60, len(entries))
                    self.assertEqual({book}, {entry['text'] for entry in entries})
            if not failed_book:
                artifact_bytes = {book: (output / (book + '.json')).read_bytes() for book in books}
                rerun = subprocess.run(['bash', str(chain)], cwd=base, env=env,
                                       capture_output=True, text=True, timeout=15)
                self.assertEqual(0, rerun.returncode, rerun.stdout + rerun.stderr)
                self.assertIn('4 already present', rerun.stdout)
                self.assertEqual(4, len((checkout / 'observations').read_text().splitlines()))
                for book in books:
                    self.assertEqual(artifact_bytes[book], (output / (book + '.json')).read_bytes())
                self.assertEqual(config, (app / 'config.json').read_bytes())
                self.assertEqual(config, (decoy / 'app/config.json').read_bytes())
                self.assertFalse((logs / 'config.json.unseen_backup').exists())

    def test_chain_resolves_its_own_checkout_and_reuses_completed_books(self):
        self._run_checkout()

    def test_config_patch_uses_the_same_checkout_as_the_chain(self):
        self._run_checkout(force_initial_root=True)

    def test_failed_generator_restores_only_its_own_checkout_config(self):
        self._run_checkout(failed_book='grimgar06')


class OriginalUnseenBookStatusTests(unittest.TestCase):
    def test_each_nonzero_book_status_contributes_to_failed_chain_exit(self):
        import json
        import shlex
        import sys
        from pathlib import Path
        source = Path(REPO, 'run_chains/unseen_books.sh').read_text()
        start = source.index('for book in mushoku18')
        # Include the real counters added immediately before the original loop.
        if 'failed_books=0;' in source[:start]:
            start = source.index('failed_books=0;')
        body = source[start:]
        books = ('mushoku18', 'grimgar06', 'mushoku23', 'arc4_volume10wn')
        for statuses in ({book: 0 for book in books},
                         {book: 2 if book == 'grimgar06' else 0 for book in books},
                         {book: 2 for book in books},
                         {book: 124 for book in books}):
            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory(prefix='book status ') as tmp:
                root = Path(tmp)
                app = root / 'app'
                inputs, output, logs = root / 'inputs', root / 'output', root / 'logs'
                for folder in (app, inputs, output, logs):
                    folder.mkdir()
                (app / 'statuses.json').write_text(json.dumps(statuses))
                for book in books:
                    (inputs / (book + '.txt')).write_text('text for ' + book)
                (app / 'generate_script.py').write_text("""import argparse,json,pathlib,sys
p=argparse.ArgumentParser();p.add_argument('source');p.add_argument('--output');a=p.parse_args()
source=pathlib.Path(a.source);book=source.stem
assert source.read_text()=='text for '+book
with pathlib.Path('dispatch.log').open('a') as f:f.write(book+'\\n')
rc=json.loads(pathlib.Path('statuses.json').read_text())[book]
if rc:sys.exit(rc)
pathlib.Path(a.output).write_text(json.dumps({'entries':[{'book':book,'i':i} for i in range(60)]}))
print('Got 60 entries')
""")
                script = 'set -uo pipefail\n' + '\n'.join(
                    name + '=' + shlex.quote(str(value)) for name, value in
                    (('PY', sys.executable), ('IN', inputs), ('OUT', output), ('L', logs))) + '\n' + body
                result = subprocess.run(['bash', '-c', script], cwd=app,
                                        capture_output=True, text=True, timeout=10)
                failed = [book for book in books if statuses[book]]
                self.assertEqual(1 if failed else 0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(list(books), (app / 'dispatch.log').read_text().splitlines())
                if failed:
                    self.assertIn(f'{len(failed)} of 4 failed', result.stdout)
                    self.assertIn('UNSEEN BOOKS INCOMPLETE', result.stdout)
                    self.assertNotIn('UNSEEN BOOKS DONE', result.stdout)
                    for book in failed:
                        self.assertIn(book, result.stdout)
                else:
                    self.assertIn('UNSEEN BOOKS DONE', result.stdout)
                for book in books:
                    self.assertEqual('text for ' + book, (inputs / (book + '.txt')).read_text())
                    artifact = output / (book + '.json')
                    if statuses[book]:
                        self.assertFalse(artifact.exists())
                    else:
                        entries = json.loads(artifact.read_text())['entries']
                        self.assertEqual(60, len(entries))
                        self.assertEqual({book}, {row['book'] for row in entries})
                    self.assertIn('rc=' + str(statuses[book]), result.stdout)
