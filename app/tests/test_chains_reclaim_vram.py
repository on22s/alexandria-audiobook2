"""A chain that starts llama-server must reclaim the card before a GPU stage.

`ensure_llama_server` deliberately OUTLIVES the job that started it, so
consecutive LLM stages share one 8.4 GB load. Nothing stops it afterwards, so
any later stage needing real VRAM is refused by gpu_job's floor.

TWICE NOW, both recorded in run_chains/lib/stage.sh and in the queue log:

  2026-08-19  continuation_20260819.sh started a server, then five consecutive
              TTS stages were refused with 1568 MiB free against 4096.
  2026-08-31  overnight_20260830b.sh started a server in stage 1, then stage 2
              was refused at 01:49 with 1350 MiB free. The chain printed
              exit=0 and the card idled SEVEN HOURS until a person noticed.

`run_stage --needs-vram` is the remedy and it polls for the memory rather than
sleeping. This test fails a chain that starts a server and later invokes
gpu_job.sh WITHOUT going through it.
"""
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAINS = os.path.join(REPO, "run_chains")

# A stage that only asks an already-loaded server for tokens needs no reclaim;
# these are the stages that need the CARD, and they are the ones that break.
GPU_HEAVY = ("library_voice_fidelity", "train_lora", "batch_train_lora",
             "voice_profiler", "ljspeech_generate", "tts_")


def chain_files():
    for name in sorted(os.listdir(CHAINS)):
        if name.endswith(".sh") and not name.startswith("lib"):
            yield name, os.path.join(CHAINS, name)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class ChainsReclaimVramTest(unittest.TestCase):

    def test_a_chain_that_starts_a_server_reclaims_before_a_gpu_heavy_stage(self):
        offenders = []
        for name, path in chain_files():
            src = read(path)
            if "ensure_llama_server" not in src:
                continue
            if not any(tool in src for tool in GPU_HEAVY):
                continue
            if "--needs-vram" in src or "pkill -x llama-server" in src:
                continue
            offenders.append(name)
        self.assertEqual(
            [], offenders,
            "these chains start llama-server and later run a stage that needs "
            "the card, without reclaiming it. gpu_job will refuse the stage "
            "and the chain will report success: " + ", ".join(offenders))

    def test_no_chain_reclaims_with_pkill_dash_f(self):
        """Rule 22: -f matches the matcher's own command line."""
        offenders = []
        for name, path in chain_files():
            for n, line in enumerate(read(path).splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if re.search(r"pkill\s+(-\w+\s+)*-\w*f", line):
                    offenders.append(f"{name}:{n}")
        self.assertEqual([], offenders,
                         "pkill -f can match the shell doing the matching: "
                         + ", ".join(offenders))

    def test_the_detector_fires_on_the_chain_that_failed(self):
        """The 2026-08-31 shape, so this cannot quietly stop discriminating."""
        bad = ('ensure_llama_server.sh "$ADAPTER"\n'
               './gpu_job.sh x ./app/env/bin/python '
               'app/experiments/library_voice_fidelity.py\n')
        self.assertIn("ensure_llama_server", bad)
        self.assertTrue(any(t in bad for t in GPU_HEAVY))
        self.assertNotIn("--needs-vram", bad)

    def test_the_remedy_is_present_in_the_shared_runner(self):
        src = read(os.path.join(CHAINS, "lib", "stage.sh"))
        self.assertIn("--needs-vram", src)
        self.assertIn("pkill -x llama-server", src,
                      "the reclaim must use -x, never -f")


if __name__ == "__main__":
    unittest.main()
