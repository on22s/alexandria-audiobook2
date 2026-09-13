"""RECIPES.md is the one place that says which settings produced working
results. A chain that trains a voice adapter must use a learning rate that
file lists as working.

WHY. On 2026-09-13 a chain copied its recipe from
`ljspeech_eval/adapter/training_meta.json` - the adapter that never learned
to stop - because that file is indistinguishable from the working adapter's.
The trainer default was already right and already tested
(test_training_defaults.py); the chain simply passed an explicit --lr and
bypassed it. Three GPU-hours would have gone to 163.8-second lines.
"""
import glob
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import train_lora

REPO = Path(__file__).parent.parent.parent
RECIPES = REPO / "RECIPES.md"

# A voice-adapter row is one whose recipe cell names a learning rate and whose
# status cell is exactly `works`.
_ROW = re.compile(r"^\|(?P<cells>.*)\|\s*$")
_LR = re.compile(r"lr\s+([0-9.]+e-?[0-9]+|[0-9.]+)")


def working_learning_rates(text):
    rates = set()
    for line in text.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group("cells").split("|")]
        if len(cells) < 3 or cells[2] != "works":
            continue
        for lr in _LR.findall(cells[1]):
            rates.add(float(lr))
    return rates


def chain_learning_rates(root):
    """-> {(chain, lr)} for every explicit --lr handed to train_lora.py."""
    found = set()
    for path in sorted(glob.glob(os.path.join(root, "run_chains", "*.sh"))):
        text = Path(path).read_text(encoding="utf-8")
        if "train_lora.py" not in text:
            continue
        for m in re.finditer(r"train_lora\.py(?P<args>(?:[^\n\\]|\\\n)*)", text):
            for lr in re.findall(r"--lr\s+([0-9.]+e-?[0-9]+|[0-9.]+)", m.group("args")):
                found.add((os.path.basename(path), float(lr)))
    return found


class RecipesFileTest(unittest.TestCase):
    def test_the_file_lists_the_trainer_default_as_working(self):
        rates = working_learning_rates(RECIPES.read_text(encoding="utf-8"))
        self.assertIn(train_lora.DEFAULT_LEARNING_RATE, rates,
                      "RECIPES.md must list the trainer's default rate as a "
                      "working recipe, or the file and the code disagree")

    def test_the_runaway_rate_is_not_listed_as_working(self):
        rates = working_learning_rates(RECIPES.read_text(encoding="utf-8"))
        self.assertNotIn(5e-6, rates)

    def test_the_parser_reads_status_not_prose(self):
        """A row whose status is not `works` contributes nothing, even if its
        recipe cell names a rate - the rejecting case."""
        text = ("| use | recipe | status | evidence |\n"
                "|---|---|---|---|\n"
                "| a | lr 1e-6 | works | x |\n"
                "| b | lr 5e-6 | runs away | y |\n"
                "| c | lr 3e-6 | **collapses** | z |\n")
        self.assertEqual({1e-6}, working_learning_rates(text))


class ChainsUseWorkingRatesTest(unittest.TestCase):
    def test_every_chain_that_trains_uses_a_listed_rate(self):
        rates = working_learning_rates(RECIPES.read_text(encoding="utf-8"))
        offenders = [(chain, lr) for chain, lr in chain_learning_rates(REPO)
                     if lr not in rates]
        self.assertEqual([], offenders,
                         "these chains pass a --lr that RECIPES.md does not "
                         "list as working; add the row with evidence first")

    def test_the_scanner_sees_a_multiline_invocation(self):
        """The rejecting case for the scanner itself: the exact shape of the
        2026-09-13 chain, with --lr on a continuation line."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "run_chains"))
            Path(tmp, "run_chains", "bad.sh").write_text(
                'run_stage train 1h -- \\\n'
                '    "$python" -u "$REPO/app/train_lora.py" \\\n'
                '    --data_dir "$d" --output_dir "$o" \\\n'
                '    --epochs 6 --lr 5e-6 --lora_r 32\n', encoding="utf-8")
            self.assertEqual({("bad.sh", 5e-6)}, chain_learning_rates(tmp))


if __name__ == "__main__":
    unittest.main()
