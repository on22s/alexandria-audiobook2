"""Does unloading a local TTS model family leave live VRAM behind? (#1217)

One process, the app's own TTSEngine, uncompiled. Each cycle renders one
synthetic line with CustomVoice, Base clone, VoiceDesign and two LoRA adapters
in turn, so every step after the first goes through the app's own eviction
(_release_inactive_local_models) or adapter switch. After each step it records
allocated/reserved/free bytes and whether any model or prompt tensor the engine
no longer owns is still alive. The engine is then dropped and the final
allocation compared with the baseline taken before any model loaded.

Allocator reserve and the framework's BLAS workspace are reported, never
counted as a leak: both baselines are taken after clearing the workspace.

Usage (one GPU job):
    ./gpu_job.sh vram1217 app/env/bin/python tools/diagnostics/tts_vram_family_cycle.py \
        --adapter lora_models/<voice_a> --adapter lora_models/<voice_b> --out <artifact.json>
"""
import argparse
import gc
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import weakref

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "app"), str(ROOT)]

FAMILIES = ("custom", "clone", "design", "lora")
STEPS = ("custom", "clone", "design", "lora_a", "lora_b")
TEXT = "The lantern by the gate had gone out, so we walked home by starlight."
DESCRIPTION = "A calm adult voice with clear diction and a steady, moderate pace"
MIB = 1024 * 1024
TOLERANCE_BYTES = MIB
INSTRUMENT_BYTES = 64 * MIB


def get_cycle_verdict(samples, baseline_allocated, end_allocated, tolerance=TOLERANCE_BYTES):
    """LEAK when a step's allocation keeps growing after the first cycle, the
    dropped engine leaves more allocated than the baseline, or a tensor the
    engine no longer owns is still alive. Reserved bytes never decide.

    The first cycle is warm-up: a one-time allocation made the first time any
    family runs shows up as cycle 1 -> 2 growth and is reported separately in
    `warmup_deltas`. Accumulation is growth from cycle 2 to the last cycle, so
    a verdict needs at least three cycles."""
    reasons = []
    step_deltas = {}
    warmup_deltas = {}
    cycles = sorted({sample["cycle"] for sample in samples})
    if len(cycles) < 3:
        raise ValueError("need at least three cycles: one warm-up and two to compare")
    for step in dict.fromkeys(sample["step"] for sample in samples):
        by_cycle = {sample["cycle"]: sample["allocated"] for sample in samples
                    if sample["step"] == step}
        warmup_deltas[step] = by_cycle[cycles[1]] - by_cycle[cycles[0]]
        delta = by_cycle[cycles[-1]] - by_cycle[cycles[1]]
        step_deltas[step] = delta
        if delta > tolerance:
            reasons.append(f"{step}: allocated grew {delta} bytes from cycle "
                           f"{cycles[1]} to cycle {cycles[-1]}")
    for sample in samples:
        if sample.get("orphans"):
            reasons.append(f"cycle {sample['cycle']} {sample['step']}: "
                           f"{len(sample['orphans'])} released object(s) still alive")
    end_delta = end_allocated - baseline_allocated
    if end_delta > tolerance:
        reasons.append(f"engine dropped: {end_delta} bytes above the baseline")
    return {"verdict": "LEAK" if reasons else "CLEAN", "reasons": reasons,
            "step_deltas": step_deltas, "warmup_deltas": warmup_deltas,
            "end_delta": end_delta}


def get_tensors(value, seen=None):
    """Every torch tensor reachable through lists, tuples, dicts and objects."""
    import torch
    seen = set() if seen is None else seen
    if id(value) in seen:
        return []
    seen.add(id(value))
    if isinstance(value, torch.Tensor):
        return [value]
    if isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    elif hasattr(value, "__dict__") and not isinstance(value, type):
        items = list(vars(value).values())
    else:
        return []
    return [tensor for item in items for tensor in get_tensors(item, seen)]


def get_owned(engine):
    """Objects the engine holds right now: the four model roots and every prompt tensor."""
    owned = {}
    for family in FAMILIES:
        model = getattr(engine, f"_local_{family}_model")
        if model is not None:
            owned[f"model:{family}"] = [model]
    clone = [tensor for _key, prompt in engine._clone_prompt_cache.values()
             for tensor in get_tensors(prompt)]
    lora = [tensor for prompt in engine._lora_prompt_cache.values()
            for tensor in get_tensors(prompt)]
    if clone:
        owned["prompt:clone"] = clone
    if lora:
        owned["prompt:lora"] = lora
    return owned


def get_recorded_owners(engine, released):
    """Ids the engine owns now, and `released` extended with weakrefs to them.

    Returns ids, never the objects: a strong reference kept here would hold
    the evicted model alive through the next load and read as a leak."""
    owned = get_owned(engine)
    recorded = dict(released)
    for label, objects in owned.items():
        for obj in objects:
            recorded.setdefault(id(obj), (label, weakref.ref(obj)))
    return {key for key, (_label, ref) in recorded.items()
            if any(ref() is obj for objects in owned.values() for obj in objects)}, recorded


def get_live_cuda_tensors():
    import torch
    return [obj for obj in gc.get_objects()
            if isinstance(obj, torch.Tensor) and obj.is_cuda]


def get_tensor_rows(tensors):
    """Shape, dtype, bytes and the referrer types of each tensor, largest first."""
    rows = []
    for tensor in tensors:
        referrers = sorted({type(ref).__name__ for ref in gc.get_referrers(tensor)
                            if ref is not tensors})
        rows.append({"shape": list(tensor.shape), "dtype": str(tensor.dtype),
                     "bytes": tensor.untyped_storage().nbytes(), "referrers": referrers})
    return sorted(rows, key=lambda row: -row["bytes"])


def ensure_released_memory(device):
    import torch
    torch.cuda.synchronize(device)
    gc.collect()
    torch.cuda.empty_cache()


def ensure_workspace_cleared():
    """Clear the BLAS workspaces when this torch exposes the private hook."""
    import torch
    clear = getattr(torch._C, "_cuda_clearCublasWorkspaces", None)
    if clear is not None:
        clear()
    return clear is not None


def get_memory(device):
    import torch
    free, total = torch.cuda.mem_get_info(device)
    return {"allocated": torch.cuda.memory_allocated(device),
            "reserved": torch.cuda.memory_reserved(device),
            "free": free, "total": total}


def run_step(engine, step, adapters, work, cycle):
    output = os.path.join(work, f"c{cycle}_{step}.wav")
    if step == "custom":
        config = {"S": {"type": "custom", "voice": "Ryan", "seed": 1217}}
        ok = engine.generate_custom_voice(TEXT, "", "S", config, output)
        shutil.copy(output, os.path.join(work, "clone_ref.wav"))
    elif step == "clone":
        config = {"S": {"type": "clone", "ref_audio": os.path.join(work, "clone_ref.wav"),
                        "ref_text": TEXT, "seed": 1217}}
        ok = engine.generate_clone_voice(TEXT, "S", config, output)
    elif step == "design":
        ok = engine.generate_design_voice(TEXT, "", {"description": DESCRIPTION, "seed": 1217}, output)
    else:
        adapter = adapters[0 if step == "lora_a" else 1]
        ok = engine.generate_lora_voice(TEXT, "", {"adapter_path": adapter, "seed": 1217}, output)
    if not ok or not os.path.isfile(output):
        raise RuntimeError(f"{step} render failed in cycle {cycle}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--adapter", action="append", required=True,
                        help="LoRA adapter directory; give exactly two")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if len(args.adapter) != 2:
        parser.error("give exactly two --adapter directories")
    if args.cycles < 3:
        parser.error("--cycles must be at least 3: one warm-up and two to compare")

    work = tempfile.mkdtemp(prefix="vram1217_")
    # Design previews and copied adapters stay in a private data dir.
    os.environ["ALEXANDRIA_DATA_DIR"] = work
    from lora_evidence import GENERATION_FILES
    adapters = []
    for index, source in enumerate(args.adapter):
        target = os.path.join(work, f"adapter_{index}")
        os.makedirs(target)
        for name in GENERATION_FILES:
            shutil.copy(os.path.join(source, name), target)
        adapters.append(target)

    import torch
    from tts import TTSEngine
    device = torch.device("cuda", torch.cuda.current_device())
    revision = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    torch.ones(256, 256, device=device) @ torch.ones(256, 256, device=device)
    ensure_released_memory(device)
    baseline_warm = get_memory(device)
    workspace_hook = ensure_workspace_cleared()
    ensure_released_memory(device)
    baseline = get_memory(device)

    engine = TTSEngine({"tts": {"mode": "local", "device": "auto", "compile_codec": False,
                                "max_new_tokens": 2048, "language": "English"}})
    released = {}  # id -> (label, weakref) for everything the engine has owned
    samples = []
    for cycle in range(1, args.cycles + 1):
        for step in STEPS:
            torch.cuda.reset_peak_memory_stats(device)
            started = time.time()
            run_step(engine, step, adapters, work, cycle)
            seconds = round(time.time() - started, 1)
            owned_ids, released = get_recorded_owners(engine, released)
            ensure_released_memory(device)
            orphans = sorted({label for key, (label, ref) in released.items()
                              if ref() is not None and key not in owned_ids})
            released = {key: value for key, value in released.items() if value[1]() is not None}
            sample = {"cycle": cycle, "step": step, "seconds": seconds,
                      "resident": [f for f in FAMILIES
                                   if getattr(engine, f"_local_{f}_model") is not None],
                      "clone_prompts": len(engine._clone_prompt_cache),
                      "lora_prompts": len(engine._lora_prompt_cache),
                      "orphans": orphans, "peak_allocated": torch.cuda.max_memory_allocated(device),
                      **get_memory(device)}
            samples.append(sample)
            print(f"cycle {cycle} {step:7s} {seconds:6.1f}s alloc {sample['allocated'] / MIB:9.1f} MiB "
                  f"peak {sample['peak_allocated'] / MIB:9.1f} MiB reserved {sample['reserved'] / MIB:9.1f} MiB resident {sample['resident']} "
                  f"orphans {orphans}", flush=True)

    del engine
    ensure_released_memory(device)
    after_drop = get_memory(device)
    ensure_workspace_cleared()
    ensure_released_memory(device)
    end = get_memory(device)
    leftover = get_tensor_rows(get_live_cuda_tensors())

    # Instrument check: a deliberately retained tensor must read as a leak.
    held = torch.empty(INSTRUMENT_BYTES, dtype=torch.uint8, device=device)
    ensure_released_memory(device)
    held_end = get_memory(device)
    held_rows = get_tensor_rows(get_live_cuda_tensors())
    del held
    ensure_released_memory(device)

    verdict = get_cycle_verdict(samples, baseline["allocated"], end["allocated"])
    control = get_cycle_verdict(samples, baseline["allocated"], held_end["allocated"])
    artifact = {
        "issue": 1217, "revision": revision, "device": torch.cuda.get_device_name(device),
        "torch": torch.__version__, "hip": getattr(torch.version, "hip", None),
        "text": TEXT, "adapters": [os.path.basename(os.path.normpath(a)) for a in args.adapter],
        "workspace_clear_available": workspace_hook,
        "baseline_before_workspace_clear": baseline_warm, "baseline": baseline,
        "samples": samples, "after_engine_drop": after_drop, "end": end,
        "leftover_cuda_tensors": leftover, "verdict": verdict,
        "instrument_check": {"held_bytes": INSTRUMENT_BYTES, "end": held_end,
                             "verdict": control["verdict"], "reasons": control["reasons"],
                             "census_found_held": any(row["bytes"] == INSTRUMENT_BYTES
                                                      for row in held_rows)},
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(artifact, handle, indent=2)
    shutil.rmtree(work, ignore_errors=True)
    print(f"verdict {verdict['verdict']}: {verdict['reasons'] or 'no growth, no survivors'}")
    print(f"instrument check with {INSTRUMENT_BYTES // MIB} MiB held: {control['verdict']}")
    # The check must flag what it was built to flag, or the verdict means nothing.
    return 0 if control["verdict"] == "LEAK" and artifact["instrument_check"]["census_found_held"] else 2


if __name__ == "__main__":
    sys.exit(main())
