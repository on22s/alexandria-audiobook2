"""Read local serving provenance without inventing hardware or runtime settings."""
import argparse
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experiments.manifest import EnvironmentCaptureError, lmstudio_state
from lmstudio_settings import get_gpu_name_and_backend


def get_serving_environment(base_url, model):
    """Capture server settings and a separately labelled local hardware probe."""
    state = lmstudio_state(model, base_url)
    if state.get("runtime") != "llama.cpp":
        raise EnvironmentCaptureError("campaign endpoint is not verified llama.cpp")
    for field in ("context_length", "parallel"):
        if type(state.get(field)) is not int or state[field] < 1:
            raise EnvironmentCaptureError(f"server did not report a valid {field}")
    gpu, probe_backend = get_gpu_name_and_backend()
    return {**state, "host": platform.node(), "server": state["runtime"],
            "gpu": gpu, "gpu_probe_backend": probe_backend, "backend": None,
            "gpu_observation": "local primary GPU diagnostic; active inference device/backend not verified"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    try:
        environment = get_serving_environment(args.base_url, args.model)
    except EnvironmentCaptureError as error:
        print(f"REFUSING unverified serving environment: {error}", file=sys.stderr)
        return 1
    print(json.dumps(environment))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
