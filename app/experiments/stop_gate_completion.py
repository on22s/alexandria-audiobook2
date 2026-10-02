"""Read the exact passing verdict before reusing an adapter stop gate."""
import json
import sys


def is_stop_gate_passed(path):
    try:
        with open(path, encoding='utf-8') as handle:
            gate = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(gate, dict) and gate.get('passed') is True


if __name__ == '__main__':
    sys.exit(0 if is_stop_gate_passed(sys.argv[1]) else 1)
