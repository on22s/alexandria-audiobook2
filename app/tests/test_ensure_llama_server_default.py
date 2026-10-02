"""The server script knows which model it serves.

unseen_books.sh starts a server only when none is running, and did not pass
LLAMA_MODEL. On 2026-08-19 that aborted the stage with "LLAMA_MODEL is
required" at the exact moment a server was needed, giving the slot back to
nobody. Four chains had each pasted the same GGUF literal inline to work
around the same check (Rule 15).
"""
import os
import subprocess
import sys
import socket
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "ensure_llama_server.sh")


def read_model(env):
    """Run the script far enough to learn which model it resolved.

    It is sourced with a stubbed PATH so nothing is actually launched; the
    resolution happens in the first few lines.
    """
    probe = ('MODEL=""; DEFAULT_MODEL=""\n'
             'eval "$(sed -n "/^DEFAULT_MODEL=/,/^MODEL=/p" %r)"\n'
             'echo "$MODEL"' % SCRIPT)
    return subprocess.run(["bash", "-c", probe], capture_output=True,
                          text=True, timeout=60, env=env).stdout.strip()


@unittest.skipUnless(os.path.exists(SCRIPT), "ensure_llama_server.sh missing")
class DefaultModelTest(unittest.TestCase):
    def _env(self, **overrides):
        env = {k: v for k, v in os.environ.items()
               if k not in ("LLAMA_MODEL", "ALEXANDRIA_QWEN3_MODEL")}
        env.update(overrides)
        return env

    def test_it_resolves_a_model_with_no_environment_at_all(self):
        model = read_model(self._env())
        self.assertTrue(model, "no default: a caller that forgets LLAMA_MODEL "
                               "aborts at the moment it needs a server")
        self.assertTrue(model.endswith(".gguf"), model)

    def test_an_explicit_model_still_wins(self):
        model = read_model(self._env(LLAMA_MODEL="/tmp/explicit.gguf"))
        self.assertEqual("/tmp/explicit.gguf", model)

    def test_the_project_override_is_honoured(self):
        model = read_model(self._env(ALEXANDRIA_QWEN3_MODEL="/tmp/override.gguf"))
        self.assertEqual("/tmp/override.gguf", model)

    def test_a_missing_model_file_is_refused_rather_than_served(self):
        """A default pointing at nothing would fail later, somewhere less
        obvious than the line that chose it.

        LLAMA_BIN is pointed at /bin/true so the binary guard - which runs
        first, and rightly, since there is no point resolving a model for a
        server that cannot start - is satisfied without needing a real
        llama-server. The first version of this test skipped when no binary was
        present, which CI's verifier correctly refused: it counts a skipped
        test as a failure, and a test that quietly does not run on the machine
        that matters is not a test.
        """
        satisfied_binary = "/bin/true"
        if not os.access(satisfied_binary, os.X_OK):   # pragma: no cover
            satisfied_binary = sys.executable
        with tempfile.TemporaryDirectory() as tmp:
            absent = os.path.join(tmp, "not-here.gguf")
            result = subprocess.run(
                ["bash", SCRIPT], capture_output=True, text=True, timeout=120,
                env=self._env(LLAMA_MODEL=absent, LLAMA_BIN=satisfied_binary))
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("no model file at", result.stderr)
            self.assertIn(absent, result.stderr,
                          "the message must name the path it looked for")

    def test_no_chain_needs_to_paste_the_path_to_make_it_work(self):
        """Documents why the default exists: the literal was in four files."""
        with open(SCRIPT, encoding="utf-8") as handle:
            self.assertIn("DEFAULT_MODEL=", handle.read())

    def test_file_identity_changes_when_same_path_is_replaced(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "adapter.gguf")
            with open(target, "wb") as handle:
                handle.write(b"old")
            snippet = ("source <(sed -n '/^file_identity()/,/^}/p' %s); "
                       "file_identity %s" % (repr(SCRIPT), repr(target)))
            first = subprocess.run(["bash", "-c", snippet], capture_output=True,
                                   text=True, check=True).stdout
            with open(target, "wb") as handle:
                handle.write(b"replacement")
            second = subprocess.run(["bash", "-c", snippet], capture_output=True,
                                    text=True, check=True).stdout
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()


class ServerLogPreflightTests(unittest.TestCase):
    def test_real_launcher_creates_log_parent_and_refuses_unusable_targets_before_stop(self):
        from pathlib import Path
        import shlex
        for problem in (None, "parent-file", "log-directory"):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                model = root / "fixture.gguf"
                model.write_bytes(b"model stand-in")
                log = root / "nested/logs/server.log"
                if problem == "parent-file":
                    log.parent.parent.write_bytes(b"keep blocking parent bytes")
                elif problem == "log-directory":
                    log.mkdir(parents=True)
                wrappers = root / "standins"
                wrappers.mkdir()
                for name, body in (
                        ("curl", 'test -f "$FIXTURE_ROOT/launched" || exit 1\n'
                         + 'exec python3 -c ' + shlex.quote(
                             'import json,os,sys;url=sys.argv[-1];print(json.dumps('
                             '{"model_path":os.environ["LLAMA_MODEL"],"default_generation_settings":{"n_ctx":int(os.environ.get("LLAMA_CTX","32768")),"params":{"reasoning_format":"none"}},"total_slots":1} if url.endswith("/props") else '
                             '([] if url.endswith("/lora-adapters") else {"data":[{"id":"qwen3-14b","meta":{}}]})))')
                         + ' "$@"'),
                        ("pkill", 'printf "%s\\n" "$*" >> "$FIXTURE_ROOT/stopped"'),
                        ("sleep", 'exec /bin/sleep 0.01')):
                    path = wrappers / name
                    path.write_text("#!/bin/bash\n" + body + "\n")
                    path.chmod(0o755)
                binary = root / "fake-server"
                binary.write_text('#!/bin/bash\ntouch "$FIXTURE_ROOT/launched"\necho fake-server-log\n')
                binary.chmod(0o755)
                script = root / "ensure.sh"
                helper = root / "run_chains/lib/server_cleanup.sh"
                helper.parent.mkdir(parents=True)
                helper.write_bytes(Path(REPO, "run_chains/lib/server_cleanup.sh").read_bytes())
                (root / "app").mkdir()
                for name in ("llama_server_identity.py","llama_server_process.py","subprocess_ownership.py"):
                    (root / "app" / name).write_bytes(Path(REPO, "app", name).read_bytes())
                source = Path(SCRIPT).read_text()
                original = 'STAMP="$HOME/.llama_server_adapter"'
                self.assertEqual(1, source.count(original))
                source = source.replace(original, "STAMP=" + shlex.quote(str(root / "stamp")))
                script.write_text(source)
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
                env = dict(os.environ, LLAMA_PORT=str(port), FIXTURE_ROOT=str(root), LLAMA_BIN=str(binary),
                           LLAMA_MODEL=str(model), LLAMA_LOG=str(log),
                           PATH=str(wrappers) + os.pathsep + os.environ["PATH"])
                result = subprocess.run(["bash", str(script)], cwd=tmp, env=env,
                                        capture_output=True, text=True, timeout=10)
                if problem is None:
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("fake-server-log", log.read_text())
                    self.assertTrue((root / "launched").is_file())
                    self.assertFalse((root / "stopped").exists(), "startup must not kill global same-named processes")
                    self.assertTrue((root / "stamp").is_file())
                else:
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("cannot write server log", result.stderr)
                    self.assertIn(str(log), result.stderr)
                    self.assertFalse((root / "stopped").exists())
                    self.assertFalse((root / "launched").exists())
                    self.assertFalse((root / "stamp").exists())
                    if problem == "parent-file":
                        self.assertEqual(b"keep blocking parent bytes", log.parent.parent.read_bytes())
                    else:
                        self.assertTrue(log.is_dir())
                self.assertEqual(b"model stand-in", model.read_bytes())
