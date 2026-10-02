"""Real campaign cleanup/trap stanzas with disposable CPU-only children."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest


REPO = Path(__file__).resolve().parents[2]


class CampaignSignalTests(unittest.TestCase):
    def _wait_for(self, path, process):
        deadline = time.monotonic() + 5
        while not path.exists():
            if process.poll() is not None:
                self.fail("fixture exited before its ready marker")
            if time.monotonic() > deadline:
                self.fail("fixture did not create its ready marker")
            time.sleep(0.01)

    def _run(self, chain, sent_signal, adopted=False):
        if chain == "pdnc_context_evidence.sh":
            from tests import test_pdnc_campaign_ownership as ownership
            fixture = ownership.ContextCampaignOwnershipTests('runTest')
            fixture.setUp()
            try:
                fixture.run_signal(sent_signal, reused=adopted)
            finally:
                fixture.doCleanups()
            return
        source = (REPO / "run_chains" / chain).read_text(encoding="utf-8")
        function = "restore" if chain.startswith("unseen") else "cleanup"
        start = source.index(function + "() {")
        # The next comment starts immediately after the real trap installation.
        end = source.index("\n# ", source.index("trap ", start))
        stanza = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app").mkdir()
            config = root / "app/config.json"
            original = b'{"llm": {"model_name": "original"}}\n'
            config.write_bytes(original)
            backup = root / "backup.json"
            fixture = root / "fixture.sh"
            setup = ""
            if function == "restore":
                setup = 'cp "$REPO/app/config.json" "$BACKUP"\nprintf modified > "$REPO/app/config.json"\n'
            else:
                setup = ('sleep 60 >/dev/null 2>&1 &\nchild_pid=$!\nprintf "%s" "$child_pid" > "$REPO/child.pid"\n'
                         + ('server_pid=\n' if adopted else 'server_pid=$child_pid\n'))
            fixture.write_text(
                'set -uo pipefail\nREPO=$1; BACKUP=$2; server_pid=\n'
                + ('source "' + str(REPO / "run_chains/lib/config_backup.sh") + '"\n' if function == "restore" else "")
                + stanza + "\n" + setup
                + 'touch "$REPO/ready"\n'
                + 'while [ ! -f "$REPO/release" ]; do sleep 0.02; done\n'
                + 'touch "$REPO/continued"\nexit 0\n', encoding="utf-8")
            process = subprocess.Popen(["bash", str(fixture), str(root), str(backup)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, start_new_session=True)
            child_pid = None
            try:
                self._wait_for(root / "ready", process)
                if (root / "child.pid").exists():
                    child_pid = int((root / "child.pid").read_text())
                if sent_signal is not None:
                    os.kill(process.pid, sent_signal)
                    # Release the old handler if it returns instead of exiting.
                    time.sleep(0.08)
                (root / "release").touch()
                stdout, stderr = process.communicate(timeout=10)
                expected = 0 if sent_signal is None else 128 + sent_signal
                self.assertEqual(expected, process.returncode, stdout + stderr)
                self.assertEqual(sent_signal is None, (root / "continued").exists())
                if function == "restore":
                    self.assertEqual(original, config.read_bytes())
                    self.assertFalse(backup.exists())
                else:
                    self.assertEqual(1, stdout.count("CAMPAIGN_EXIT "), stdout)
                    if adopted:
                        os.kill(child_pid, 0)
                    else:
                        with self.assertRaises(ProcessLookupError):
                            os.kill(child_pid, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_unseen_signals_restore_config_and_stop(self):
        for chain in ("unseen_books.sh", "unseen_books_20260819b.sh"):
            for sent_signal in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(chain=chain, signal=sent_signal):
                    self._run(chain, sent_signal)

    def test_unseen_normal_exit_still_restores_config(self):
        self._run("unseen_books_20260819b.sh", None)

    def test_pdnc_signals_stop_and_cleanup_owned_server(self):
        for sent_signal in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sent_signal):
                self._run("pdnc_context_evidence.sh", sent_signal)

    def test_pdnc_signals_clean_up_reused_configured_server(self):
        for sent_signal in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sent_signal):
                self._run("pdnc_context_evidence.sh", sent_signal, adopted=True)

    def test_pdnc_normal_exit_cleans_up_owned_server(self):
        self._run("pdnc_context_evidence.sh", None)
