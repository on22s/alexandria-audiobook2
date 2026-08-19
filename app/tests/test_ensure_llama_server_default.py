"""The server script knows which model it serves.

unseen_books.sh starts a server only when none is running, and did not pass
LLAMA_MODEL. On 2026-08-19 that aborted the stage with "LLAMA_MODEL is
required" at the exact moment a server was needed, giving the slot back to
nobody. Four chains had each pasted the same GGUF literal inline to work
around the same check (Rule 15).
"""
import os
import subprocess
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

        Needs a real llama-server binary present: the script checks the binary
        BEFORE the model, quite correctly, so on a machine without one it
        exits 2 saying "no binary at ..." and never reaches the model check.
        That is the script behaving properly, not the behaviour under test, so
        the precondition is stated rather than asserted around. CI has no
        binary and this skips there.
        """
        import shutil as _shutil
        binary = os.environ.get("LLAMA_BIN") or _shutil.which("llama-server")
        if not (binary and os.access(binary, os.X_OK)):
            self.skipTest("no llama-server binary; the model check is "
                          "unreachable on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            absent = os.path.join(tmp, "not-here.gguf")
            result = subprocess.run(
                ["bash", SCRIPT], capture_output=True, text=True, timeout=120,
                env=self._env(LLAMA_MODEL=absent, LLAMA_BIN=binary))
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("no model file at", result.stderr)

    def test_no_chain_needs_to_paste_the_path_to_make_it_work(self):
        """Documents why the default exists: the literal was in four files."""
        with open(SCRIPT, encoding="utf-8") as handle:
            self.assertIn("DEFAULT_MODEL=", handle.read())


if __name__ == "__main__":
    unittest.main()
