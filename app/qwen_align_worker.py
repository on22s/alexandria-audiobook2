# coding=utf-8
"""Qwen3-ForcedAligner worker (#1844) — runs ONLY in the qwen-env interpreter.

Protocol: argv[1] = job JSON, argv[2] = result JSON. Heavy imports are
lazy and local-only: the model is loaded with local_files_only=True from
a provisioned local directory, and the parent enforces HF_HUB_OFFLINE=1,
so this worker never downloads weights or touches credentials.

The alignment call follows the official Qwen3-ASR API
(QwenLM/Qwen3-ASR, pinned commit 7c6daf77a2421100f5fb066495372c00129d39ff;
Hugging Face Qwen/Qwen3-ForcedAligner-0.6B):
``Qwen3ForcedAligner.from_pretrained(model_dir, dtype=..., device_map=...)``
then ``model.align(audio=path, text=text, language=name)`` returning one
ForcedAlignResult whose items carry .text/.start_time/.end_time seconds.
No model inference happens here beyond that official call; validation of
the returned tokens lives in the parent (qwen_align).
"""
import json
import sys
from pathlib import Path


def _write_result(path, doc):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False)


def _worker_error(message):
    return {"ok": False, "error": str(message)}


def main(argv):
    if len(argv) != 3:
        print("usage: qwen_align_worker.py <job.json> <result.json>", file=sys.stderr)
        return 2
    job_path, result_path = argv[1], argv[2]
    try:
        with open(job_path, encoding="utf-8") as handle:
            job = json.load(handle)
    except (OSError, ValueError) as error:
        _write_result(result_path, _worker_error("invalid job: %s" % error))
        return 1

    try:
        import torch
        from qwen_asr import Qwen3ForcedAligner
    except ImportError as error:
        _write_result(result_path, _worker_error(
            "missing module %s; install qwen-asr in the qwen-env interpreter "
            "(app/requirements-qwen.txt)" % getattr(error, "name", error)))
        return 1

    model_dir = job.get("model_dir")
    device = job.get("device") or "cpu"
    language = job.get("language")
    rows = job.get("rows") or []
    if not isinstance(model_dir, str) or not model_dir or not Path(model_dir).is_dir():
        _write_result(result_path, _worker_error("model directory is unavailable."))
        return 1
    if not isinstance(rows, list) or not rows:
        _write_result(result_path, _worker_error("job has no rows."))
        return 1
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
           or not isinstance(row.get("wav_path"), str)
           or not isinstance(row.get("text"), str)
           or not Path(row["wav_path"]).is_file() for row in rows):
        _write_result(result_path, _worker_error("job rows are malformed."))
        return 1

    dtype_name = job.get("dtype")
    try:
        torch = None
        import torch as _torch
        torch = _torch
    except Exception:
        torch = None
    if torch is not None:
        if dtype_name == "float16":
            dtype = torch.float16
        elif dtype_name == "bfloat16":
            dtype = torch.bfloat16
        else:
            dtype = torch.float32
        if str(device).startswith("cuda") and dtype_name not in ("float16", "bfloat16"):
            # Fallback for job files without an explicit dtype: older cards
            # need fp16, Ampere+ supports bf16 (donor _resolve_dtype rule).
            try:
                major, _minor = torch.cuda.get_device_capability(device)
            except Exception:
                major = 0
            dtype = torch.bfloat16 if major >= 8 else torch.float16
    else:
        dtype = None
    try:
        model = Qwen3ForcedAligner.from_pretrained(
            model_dir, dtype=dtype, device_map=device, local_files_only=True)
    except Exception as error:
        _write_result(result_path, _worker_error(
            "could not load the local model (%s): %s" % (model_dir, error)))
        return 1

    tokens = {}
    for row in rows:
        try:
            results = model.align(audio=row["wav_path"], text=row["text"],
                                  language=language)
        except Exception as error:
            _write_result(result_path, _worker_error(
                "alignment failed on row %s: %s" % (row["id"], error)))
            return 1
        if not isinstance(results, list) or len(results) != 1:
            _write_result(result_path, _worker_error(
                "alignment returned an unexpected result count on row %s." % row["id"]))
            return 1
        items = []
        for item in results[0]:
            items.append({"text": str(item.text),
                          "start_time": float(item.start_time),
                          "end_time": float(item.end_time)})
        tokens[row["id"]] = items

    version = "unknown"
    try:
        from importlib import metadata as _metadata
        version = _metadata.version("qwen-asr")
    except Exception:
        pass
    _write_result(result_path, {
        "ok": True, "tokens": tokens,
        "provider": {"qwen_asr_version": version, "device": device,
                     "dtype": getattr(dtype, "__name__", str(dtype)) if dtype else None}})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
