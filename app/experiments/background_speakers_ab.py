"""Issue #653 A/B: does the name gate cause UNKNOWN for unnamed speakers?

Phase 0 found that pass 2 never emits a descriptive speaker label in any of
six three-pass books, while the models proposed them (THE MARINER, rejected
30 times). Both gates that judge a speaker - the output check
`speaker_not_in_source` in pass_quality.validate_attribution and the roster
admission in three_pass_generate.attested_new_speakers - ask
`is_attested_name`, which requires the label to be written capitalised in the
book. "the mariner" never is.

ARMS, everything else identical:
  base               the gates as shipped.
  admit_descriptive  `is_attested_name` additionally accepts a label that is
                     descriptive in form (background_speakers.is_descriptive_label)
                     and whose description occurs in the book, e.g. THE MARINER
                     when the text says "the mariner". Invented names (FUTURE_ME,
                     SILAS DURGAN) are not descriptive in form and still fail.

THE PRODUCT GATE IS NOT CHANGED. The admit arm patches the function in this
process only (Rule 9: a guard is not relaxed in the product to measure it).

SHARED PASS 1. Both arms start from a copy of the same segmentation
checkpoint, so only pass 2 differs. That checkpoint was written under older
prompts, so its fingerprint no longer matches; the copy is re-stamped with
today's fingerprint after checking the source hash and model match and pass 1
is complete, and the original fingerprint is recorded beside the output. If
any of that fails the run stops before its first request.
"""
import argparse
import json
import os
import re
import shutil
import sys

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import pass_quality                                           # noqa: E402
import three_pass_generate                                    # noqa: E402
from experiments.background_speakers import (                # noqa: E402
    _NUMBERED, get_loose_form, is_descriptive_label)
from experiments.provenance import provenance                 # noqa: E402

ARMS = ("base", "admit_descriptive")


def is_described_in_source(name, source_text):
    """A descriptive label whose description the book actually writes."""
    if not source_text or not is_descriptive_label(name):
        return False
    form = _NUMBERED.sub("", get_loose_form(name)).strip()
    return bool(form) and re.search(
        r"(?<!\w)" + re.escape(form) + r"(?!\w)", source_text,
        re.IGNORECASE) is not None


def get_admitting_gate(original):
    def gate(name, source_text, *args, **kwargs):
        return (original(name, source_text, *args, **kwargs)
                or is_described_in_source(name, source_text))
    return gate


def install_arm(arm):
    if arm == "admit_descriptive":
        gate = get_admitting_gate(pass_quality.is_attested_name)
        # Both references: pass_quality's own module global (output gate) and
        # the name three_pass_generate imported (roster gate).
        pass_quality.is_attested_name = gate
        three_pass_generate.is_attested_name = gate


def install_restamp(record_path):
    """Re-stamp the copied pass-1 checkpoint once, or refuse to start."""
    original_load = three_pass_generate._load_three_pass_checkpoint

    def load(output_path, fingerprint, chunk_count=None):
        path = three_pass_generate.three_pass_checkpoint_path(output_path)
        data = three_pass_generate.load_generation_delta_checkpoint(path)
        stored = data.get("fingerprint") or {}
        if stored.get("settings_sha256") != fingerprint.get("settings_sha256"):
            problems = [k for k in ("source_sha256", "model_name", "pipeline")
                        if stored.get(k) != fingerprint.get(k)]
            if problems or data.get("stage") != "segment" or data.get("named"):
                raise SystemExit(f"refusing to re-stamp {path}: mismatch "
                                 f"{problems}, stage={data.get('stage')}")
            with open(record_path, "w", encoding="utf-8") as handle:
                json.dump({"original_fingerprint": stored,
                           "restamped_to": fingerprint}, handle, indent=1)
            data["fingerprint"] = fingerprint
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
        loaded = original_load(output_path, fingerprint, chunk_count)
        if loaded is None:
            raise SystemExit(f"{path}: did not resume; refusing to re-segment")
        return loaded

    three_pass_generate._load_three_pass_checkpoint = load


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--pass1-checkpoint", required=True)
    parser.add_argument("--output", required=True)
    args, passthrough = parser.parse_known_args(argv)

    checkpoint = three_pass_generate.three_pass_checkpoint_path(args.output)
    if not os.path.exists(checkpoint):
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        shutil.copyfile(args.pass1_checkpoint, checkpoint)
    with open(args.output + ".ab_provenance.json", "w", encoding="utf-8") as handle:
        json.dump(provenance(__file__, args, passthrough=passthrough), handle,
                  indent=1)
    install_arm(args.arm)
    install_restamp(args.output + ".restamp.json")
    sys.argv = ["three_pass_generate.py", args.source, "--output", args.output,
                *passthrough]
    three_pass_generate.main()


if __name__ == "__main__":
    main()
