"""A chain's --books names must be names the harness can find gold for.

`three_pass_vs_single.py` builds its answer key path straight from the book
name: `app/fixtures/attribution_gold_<book>.json`. On 2026-08-21 the 5.3 chain
passed `--books TheGambler ...` - the PDNC *directory* spelling - while the
fixtures are `attribution_gold_pdnc_thegambler.json`. The 10h stage claimed its
slot at 07:45:20Z and died at 07:45:20Z with FileNotFoundError, and the queue
moved on. Nothing about the failure was visible until the log was read.

The mistake costs a stage slot, not a wrong number, which is why nothing else
here catches it: the run is refused, not corrupted. This file checks the two
names line up before the card is committed.
"""
import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS = sorted(glob.glob(os.path.join(REPO, "run_chains", "*.sh")))
FIXTURES = os.path.join(REPO, "app", "fixtures")

# `--books a b c \` possibly continued over further backslash-terminated lines.
BOOKS = re.compile(r"--books\s+((?:[^\n\\]|\\\n)+)")


def strip_comments(source):
    """Drop comment lines: this file's own explanation quotes a --books line."""
    return "\n".join(l for l in source.splitlines()
                      if not l.lstrip().startswith("#"))


def books_in(source):
    """-> every book name passed to a --books flag in one chain."""
    out = []
    for match in BOOKS.finditer(strip_comments(source)):
        run = match.group(1).replace("\\\n", " ")
        for word in run.split():
            if word.startswith("-") or word.startswith("$") or '"' in word:
                break
            out.append(word)
    return out


class ChainBookFixtureTest(unittest.TestCase):
    def test_every_books_name_has_a_gold_fixture(self):
        missing = []
        for path in CHAINS:
            with open(path, encoding="utf-8") as handle:
                source = handle.read()
            if "three_pass_vs_single" not in source:
                continue
            for book in books_in(source):
                fixture = os.path.join(
                    FIXTURES, "attribution_gold_%s.json" % book)
                if not os.path.exists(fixture):
                    missing.append("%s: --books %s -> no %s" % (
                        os.path.basename(path), book,
                        os.path.basename(fixture)))
        self.assertEqual([], missing, "\n".join(missing))

    def test_the_parser_reads_a_continued_books_list(self):
        """The 5.3 chain's list spans two lines; a one-line regex misses half."""
        source = (
            '    --books pdnc_thegambler pdnc_thesignofthefour \\\n'
            '            pdnc_ahandfulofdust \\\n'
            '    --inputs "$inputs" --work "$work"\n')
        self.assertEqual(
            ["pdnc_thegambler", "pdnc_thesignofthefour", "pdnc_ahandfulofdust"],
            books_in(source))

    def test_the_old_spelling_would_have_been_caught(self):
        """The exact line that died, against the fixtures actually present."""
        source = '    --books TheGambler TheSignOfTheFour\n'
        found = books_in(source)
        self.assertEqual(["TheGambler", "TheSignOfTheFour"], found)
        for book in found:
            self.assertFalse(os.path.exists(os.path.join(
                FIXTURES, "attribution_gold_%s.json" % book)))


class ArchivedChainRootTests(unittest.TestCase):
    def test_actual_archived_chains_dispatch_from_repository_root(self):
        import json
        from pathlib import Path
        import subprocess
        import tempfile
        for name, expected_jobs, expected_status in (
                ("asr_large_chain.sh", ["asr_large_ljspeech", "asr_large_kokoro", "asr_large_aishell3"], 0),
                ("extended_chain.sh", ["retrain_honest"], 0),
                ("tpvs2_chain.sh", ["three_pass_vs_single_v2"], 17)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp, "repository with spaces")
                archive = root / "run_chains/archive"
                archive.mkdir(parents=True)
                chain = archive / name
                chain.write_bytes(Path(REPO, "run_chains/archive", name).read_bytes())
                (root / "app/env/bin").mkdir(parents=True)
                (root / "whisper.cpp/models").mkdir(parents=True)
                (root / "whisper.cpp/models/ggml-large-v3.bin").write_bytes(b"fake model marker")
                (root / "ab_test_runtime/logs").mkdir(parents=True)
                wrappers = Path(tmp, "standins")
                wrappers.mkdir()
                capture = Path(tmp, "commands.jsonl")
                job = root / "gpu_job.sh"
                job.write_text("#!/usr/bin/env python3\nimport json,os,sys\nwith open(os.environ['CAPTURE'], 'a') as out:out.write(json.dumps({'cwd':os.getcwd(),'argv':sys.argv[1:]})+'\\n')\nprint('CPU stand-in refused inference')\nraise SystemExit(17)\n")
                job.chmod(0o755)
                for tool, body in (("sleep", "exit 0"), ("stat", "echo 3000000000"),
                                   ("pgrep", "exit 1")):
                    path = wrappers / tool
                    path.write_text("#!/bin/bash\n" + body + "\n")
                    path.chmod(0o755)
                env = dict(os.environ, PATH=str(wrappers) + os.pathsep + os.environ["PATH"],
                           CAPTURE=str(capture), GPU_LOCK=str(root / "ab_test_runtime/logs/test.lock"))
                result = subprocess.run(["bash", str(chain)], cwd=tmp, env=env,
                                        capture_output=True, text=True, timeout=10)
                self.assertTrue(capture.exists(), result.stdout + result.stderr)
                commands = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(expected_jobs, [row['argv'][0] for row in commands])
                self.assertEqual(expected_status, result.returncode, result.stdout + result.stderr)
                expected_cwd = root if name == "tpvs2_chain.sh" else root / "app"
                self.assertEqual([str(expected_cwd)] * len(commands), [row['cwd'] for row in commands])
                self.assertFalse((archive / "ab_test_runtime").exists())
                if name == "asr_large_chain.sh":
                    for row, book, language in zip(commands, ("ljspeech", "kokoro", "aishell3"), ("en", "ja", "zh")):
                        args = row['argv']
                        self.assertIn(str(root / "app/env/bin/python"), args)
                        self.assertEqual(str(root / f"ab_test_runtime/{book}_eval/build.json"), args[args.index('--build')+1])
                        self.assertEqual(language, args[args.index('--lang')+1])
                        self.assertEqual(str(root / "whisper.cpp/models/ggml-large-v3.bin"), args[args.index('--whisper-cpp-model')+1])
                elif name == "extended_chain.sh":
                    self.assertIn(str(root / "app/env/bin/python"), commands[0]['argv'])
                    self.assertIn("experiments/retrain_honest.py", commands[0]['argv'])
                else:
                    self.assertIn(str(root), commands[0]['argv'][-1])


class ArchivedQueueLockTests(unittest.TestCase):
    def test_three_pass_archive_uses_wrapper_lock_and_preserves_explicit_override(self):
        import json
        from pathlib import Path
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "repository")
            archive = root / "run_chains/archive"
            archive.mkdir(parents=True)
            wrapper = root / "gpu_job.sh"
            wrapper.write_bytes(Path(REPO, "gpu_job.sh").read_bytes())
            (root / "alexandria_file_lock.py").write_bytes(Path(REPO, "alexandria_file_lock.py").read_bytes())
            wrapper.chmod(0o755)
            chain = archive / "tpvs2_chain.sh"
            original = Path(REPO, "run_chains/archive/tpvs2_chain.sh").read_text()
            # Run the real header, before any waiting, server or model work.
            prefix = original[:original.index('WAIT_PID=')]
            chain.write_text(prefix + '\nexec "$REPO/gpu_job.sh" --print-lock\n')
            base_env = {key:value for key,value in os.environ.items() if key != "GPU_LOCK"}
            for custom in (None, str(root / "operator.lock")):
                env = dict(base_env)
                if custom is not None:
                    env["GPU_LOCK"] = custom
                expected = subprocess.run(["bash", str(wrapper), "--print-lock"], env=env,
                                          capture_output=True, text=True, timeout=5)
                actual = subprocess.run(["bash", str(chain)], cwd=tmp, env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(0, expected.returncode, expected.stderr)
                self.assertEqual(0, actual.returncode, actual.stderr)
                self.assertEqual(expected.stdout, actual.stdout)
                self.assertEqual(custom or str(root / "ab_test_runtime/logs/alexandria_gpu.lock"), actual.stdout.strip())


class ArchivedBatchInterpreterTests(unittest.TestCase):
    def test_new_batch_uses_checkout_interpreter_and_preserves_explicit_override(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        for explicit in (False, True):
            with self.subTest(explicit=explicit), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp, "repository with spaces")
                archive = root / "run_chains/archive"
                archive.mkdir(parents=True)
                (root / "run_chains/lib").mkdir()
                for relative in ("run_chains/archive/run_new_batch.sh", "run_chains/lib/queue.sh"):
                    target = root / relative
                    target.write_bytes(Path(REPO, relative).read_bytes())
                capture = root / "commands.jsonl"
                wrappers = root / "bin"
                wrappers.mkdir()
                sleep = wrappers / "sleep"
                sleep.write_text("#!/bin/sh\nexit 0\n")
                sleep.chmod(0o755)
                interpreter = root / ("custom interpreter" if explicit else "app/env/bin/python")
                interpreter.parent.mkdir(parents=True, exist_ok=True)
                interpreter.write_text("#!" + sys.executable + "\n"
                    "import json,os,sys\n"
                    "with open(os.environ['CAPTURE'], 'a') as f:"
                    "f.write(json.dumps({'argv':sys.argv[1:], 'pythonpath':os.environ['PYTHONPATH']})+'\\n')\n")
                interpreter.chmod(0o755)
                prep = root / ("custom preparer.py" if explicit else "alexandria_preparer_rocm_compatible.py")
                prep.write_text("# CPU command fixture; no inference")
                env = {key:value for key,value in os.environ.items() if key not in ("PREP", "PY")}
                env.update(NEW_DIR=str(root / "input pairs"), OUT_BASE=str(root / "outputs"),
                           CAPTURE=str(capture), PYTHONPATH="existing-modules",
                           PATH=str(wrappers) + os.pathsep + os.environ["PATH"])
                if explicit:
                    env.update(PREP=str(prep), PY=str(interpreter))
                result = subprocess.run(["bash", str(archive / "run_new_batch.sh")], cwd=tmp,
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue(capture.exists(), result.stdout + result.stderr)
                commands = [json.loads(line) for line in capture.read_text().splitlines()]
                self.assertEqual(26, len(commands))
                self.assertEqual(26, len({row['argv'][row['argv'].index('--audio')+1] for row in commands}))
                for row in commands:
                    self.assertEqual(str(prep), row['argv'][0])
                    self.assertEqual(str(root) + ':existing-modules', row['pythonpath'])
                    args = row['argv']
                    self.assertNotIn('--resume', args)
                    self.assertTrue(args[args.index('--audio')+1].startswith(env['NEW_DIR'] + '/'))
                    self.assertTrue(args[args.index('--output')+1].startswith(env['OUT_BASE'] + '/'))
                self.assertFalse((archive / "outputs").exists())
