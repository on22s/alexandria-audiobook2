"""Run the shared evaluator against the separately frozen 19-book fixtures."""
import argparse
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from experiments import lora_serving_eval


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--fixtures-root', required=True)
    args, remaining = parser.parse_known_args()
    # APP is used only for fixture reads and their recorded hashes. Keep the
    # evaluator's implementation and output repository unchanged.
    lora_serving_eval.APP = str(Path(args.fixtures_root).resolve()) + '/'
    sys.argv = [sys.argv[0], *remaining]
    lora_serving_eval.main()


if __name__ == '__main__':
    main()
