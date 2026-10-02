"""Run the captured Qwen3-8B window trainer with the shared prompt module."""
import importlib.util
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import attribution_prompt_variants

# The captured cloud trainer predates the prompt module's move to app/.
# Keep one implementation and preserve the original trainer bytes as evidence.
sys.modules['experiments.attribution_prompt_variants'] = attribution_prompt_variants
source = APP.parent / 'ab_test_runtime/evidence/thunder_campaign_20260929/distill_train_gemma4_michel2_full_20260923c.py'
spec = importlib.util.spec_from_file_location('captured_window_trainer', source)
trainer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trainer)

if __name__ == '__main__':
    trainer.main()
