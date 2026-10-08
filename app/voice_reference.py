"""Choose the reference clip a LoRA dataset will be anchored to.

WHY THIS EXISTS. `train_lora.py` extracts the speaker embedding from ONE
reference clip and uses it for every training sample, per the official Qwen3-TTS
fine-tuning approach. The dataset builder chose that clip as `ref_index = 0` -
whatever sample happened to be first - and nothing checked it against the rest
of the dataset.

Measured across the 75 shipped adapters on 2026-08-07:

    correlation(reference matches its dataset, adapter quality) = +0.76
    reference mismatched (<0.3):  7 adapters, 6 of them poor  (86%)
    reference matching:          67 adapters, 9 of them poor  (13%)

A mismatched reference makes an adapter 6.4x more likely to fail. The worst
case, `husky_baritone_20s_m_anime`, was anchored to a clip scoring **-0.026**
against its own dataset - actively not that speaker - and produced an adapter
scoring 0.004. A representative clip scoring 0.882 was sitting in the same data.

THE MEDOID IS THE FIX. The medoid is the clip most similar to all the others,
so it is representative by construction and robust to a minority of bad clips -
which is the failure mode, since a dataset with a few misdiarized clips still
has a clear majority speaker. Across 74 datasets the medoid beat the existing
reference by a median of +0.07, and by more than 0.15 on 14 of them.

DEGRADES RATHER THAN BREAKS. The speaker model lives in the sibling
interpreter, not in `app/env`. When it is unavailable this returns None and the
caller keeps its existing behaviour, because making dataset creation
hard-depend on a second environment would be a worse failure than an
occasionally poor reference. The caller logs which path was taken.
"""
import json
import math
import os
import sys
import statistics
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path
from lora_evidence import get_file_sha256
from utils import is_path_inside, get_runtime_data_dir

APP = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(APP)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from app_venv import get_app_python

SIBLING_PY = os.environ.get(
    "ALEXANDRIA_SIBLING_PYTHON",
    get_app_python(os.path.join(os.path.dirname(REPO), "alexandria-audiobook.git")))


def get_speaker_model_python(voicelab_config=None):
    """The interpreter that has speechbrain, or None.

    Since 2026-09-11 speechbrain is in app/env (requirements.txt), so the
    running interpreter is preferred: the ECAPA worker then has no cross-repo
    dependency. Older installs fall back to the Voice Lab's configured
    `rocm_python`, then the sibling repo's env. This is the one place that
    decision is made - `voice_drift` and `_speaker_similarities` both use it."""
    import importlib.util
    if importlib.util.find_spec("speechbrain") is not None:
        return sys.executable
    candidate = (voicelab_config or {}).get("rocm_python") or ""
    if candidate and os.path.exists(candidate):
        return candidate
    return SIBLING_PY if os.path.exists(SIBLING_PY) else None

# Bounded on purpose: this runs inside a save request. 12 clips is 66 pairwise
# comparisons, enough to identify the majority speaker, and the cost grows
# quadratically.
MAX_CLIPS = 12
REFERENCE_SCORE_CACHE_LIMIT = 64
REFERENCE_SCORE_CACHE_TTL = 600.0
_REFERENCE_SCORE_CACHE = OrderedDict()
_REFERENCE_WORKER_LOCK = threading.Lock()
_REFERENCE_CACHE_LOCK = threading.Lock()


def _apply_reference_score_cache_lookup(key):
    """Expire old entries and touch a valid hit under the short cache lock."""
    with _REFERENCE_CACHE_LOCK:
        now = time.monotonic()
        for old_key, (created, _) in list(_REFERENCE_SCORE_CACHE.items()):
            if now - created >= REFERENCE_SCORE_CACHE_TTL:
                del _REFERENCE_SCORE_CACHE[old_key]
        if key is not None and key in _REFERENCE_SCORE_CACHE:
            _REFERENCE_SCORE_CACHE.move_to_end(key)
            return list(_REFERENCE_SCORE_CACHE[key][1])
    return None


def _save_reference_score_cache(key, values):
    with _REFERENCE_CACHE_LOCK:
        _REFERENCE_SCORE_CACHE[key] = (time.monotonic(), tuple(values))
        while len(_REFERENCE_SCORE_CACHE) > REFERENCE_SCORE_CACHE_LIMIT:
            _REFERENCE_SCORE_CACHE.popitem(last=False)


def _get_reference_score_key(pairs, python_bin, script):
    """Bind reuse to audio bytes, selected runtime, worker and local model assets."""
    files = {"python": Path(python_bin), "worker": Path(script)}
    prefixes = {Path(python_bin).absolute().parent.parent,
                Path(python_bin).resolve().parent.parent}
    sites = {Path(part) for part in os.environ.get("PYTHONPATH", "").split(os.pathsep) if part}
    sites.update(Path.home().glob(".local/lib/python*/site-packages"))
    for prefix in prefixes:
        config = prefix / "pyvenv.cfg"
        if config.is_file():
            files[str(config)] = config
        sites.update(prefix.glob("lib/python*/site-packages"))
        sites.add(prefix / "Lib/site-packages")
    for site in sites:
        for package in ("speechbrain", "torch", "torchaudio", "numpy", "scipy", "librosa",
                        "soundfile", "hyperpyyaml", "huggingface_hub"):
            for metadata in site.glob(package + "-*.dist-info/METADATA"):
                files[str(metadata)] = metadata
            for entry in (site / package / "__init__.py", site / (package + ".py")):
                if entry.is_file():
                    files[str(entry)] = entry
    from experiments._ecapa_batch import get_ecapa_model_dir
    assets = Path(get_ecapa_model_dir(REPO))
    if assets.exists():
        for parent, directories, names in os.walk(assets):
            if any((Path(parent) / name).is_symlink() for name in directories):
                raise ValueError("Cannot fingerprint a linked ECAPA asset directory")
            for name in names:
                path = Path(parent) / name
                files[str(path)] = path
    environment = tuple((name, os.environ.get(name)) for name in (
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "HF_HOME", "HUGGINGFACE_HUB_CACHE",
        "TORCH_HOME", "OMP_NUM_THREADS"))
    hashes = {path: get_file_sha256(path) for path in sorted({path for pair in pairs for path in pair})}
    return (os.path.abspath(python_bin), tuple(sorted((name, get_file_sha256(path))
                                                    for name, path in files.items())),
            environment, tuple((hashes[a], hashes[b]) for a, b in pairs))


def _is_complete_reference_scores(values, count):
    return (isinstance(values, list) and len(values) == count
            and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) for value in values))


def get_reference_audio_path(path, dataset_root):
    """Resolve reference audio within its caller's trusted dataset boundary."""
    resolved = os.path.realpath(os.path.join(dataset_root, os.fspath(path)))
    if not is_path_inside(resolved, dataset_root):
        raise ValueError(f"reference audio is outside the dataset: {path}")
    return resolved


def _speaker_similarities(pairs, timeout=600, *, dataset_root=None):
    """Cosine similarity per pair, or None if the model is unavailable."""
    from config_settings import load_app_config
    from utils import get_app_config_path, get_runtime_data_dir
    config_path = get_app_config_path(get_runtime_data_dir(REPO), REPO, APP)
    voicelab_config = load_app_config(config_path).get("voicelab")
    if voicelab_config is not None and not isinstance(voicelab_config, dict):
        print("WARNING: Invalid Voice Lab configuration; reference selection declined.",
              flush=True)
        return None
    python_bin = get_speaker_model_python(voicelab_config)
    if not pairs or not python_bin:
        return None
    script = os.path.join(APP, "experiments", "_ecapa_batch.py")
    if not os.path.exists(script):
        return None
    root = dataset_root if dataset_root is not None else get_runtime_data_dir(REPO)
    resolved_pairs = [[get_reference_audio_path(a, root), get_reference_audio_path(b, root)]
                      for a, b in pairs]
    if any(not os.path.isfile(path) for pair in resolved_pairs for path in pair):
        return None
    deadline = time.monotonic() + timeout
    try:
        key = _get_reference_score_key(resolved_pairs, python_bin, script)
    except (OSError, ValueError):
        key = None
    cached = _apply_reference_score_cache_lookup(key)
    if cached is not None:
        return cached
    if not _REFERENCE_WORKER_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        return None
    try:
        try:
            key = _get_reference_score_key(resolved_pairs, python_bin, script)
        except (OSError, ValueError):
            key = None  # missing provenance disables reuse, not the existing scorer
        cached = _apply_reference_score_cache_lookup(key)
        if cached is not None:
            return cached
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            out = subprocess.run(
                [python_bin, script], input=json.dumps(resolved_pairs),
                capture_output=True, text=True, timeout=remaining, cwd=APP)
        except (subprocess.SubprocessError, OSError):
            return None
        if out.returncode != 0:
            return None
        try:
            values = json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return None
        try:
            complete = _is_complete_reference_scores(values, len(resolved_pairs))
        except OverflowError:
            complete = False
        if key is not None and complete:
            try:
                after = _get_reference_score_key(resolved_pairs, python_bin, script)
            except (OSError, ValueError):
                after = None
            if key == after:
                _save_reference_score_cache(key, values)
        return values
    finally:
        _REFERENCE_WORKER_LOCK.release()


MIN_USABLE_SIMILARITY = 0.60
"""Below this, the best available clip is not a good reference either.

Measured 2026-08-07 retraining ten adapters with an explicit medoid. The
pattern held everywhere except one: `breathy_baritone_30s_m_fantasy` went
0.705 -> 0.597, and its medoid scored only 0.49 - the lowest in the batch. On a
dataset where even the most representative clip is mediocre, replacing an
existing reference that happened to be good makes things worse.

So a medoid this weak is reported but not recommended: the caller keeps what it
had. Distinguishing "I found a good reference" from "the best of a bad lot" is
the point - returning the latter as though it were the former is how a fix
becomes a regression.
"""


def rank_reference_samples(wav_paths, max_clips=MAX_CLIPS, *, dataset_root=None):
    """Return candidate ``(original_index, median_similarity)`` pairs best first.

    An empty list means the model was unavailable, too few clips were usable,
    or the similarity result was incomplete. Indices always address the
    original input list, including when missing files were filtered out.
    dataset_root is the caller's trusted extraction/work root; direct callers
    default to the configured runtime data root. Escaping paths raise ValueError.
    """
    root = dataset_root if dataset_root is not None else get_runtime_data_dir(REPO)
    resolved = [(i, get_reference_audio_path(p, root)) for i, p in enumerate(wav_paths) if p]
    usable = [(i, p) for i, p in resolved if os.path.isfile(p)]
    sample = usable[:max_clips]
    if len(sample) < 3:
        return []
    pairs, index = [], []
    for a in range(len(sample)):
        for b in range(a + 1, len(sample)):
            pairs.append((sample[a][1], sample[b][1]))
            index.append((a, b))
    sims = _speaker_similarities(pairs, dataset_root=root)
    if not sims or len(sims) != len(pairs):
        return []
    scores = {a: [] for a in range(len(sample))}
    for (a, b), value in zip(index, sims):
        if value is None:
            continue
        try:
            valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value))
        except OverflowError:
            valid = False
        if not valid:
            print("WARNING: ECAPA returned an invalid similarity; reference selection declined.",
                  flush=True)
            return []
        scores[a].append(value)
        scores[b].append(value)
    medians = {a: statistics.median(v) for a, v in scores.items() if v}
    if not medians:
        return []
    return [(sample[index][0], round(score, 4))
            for index, score in sorted(
                medians.items(), key=lambda item: (-item[1], item[0]))]


def select_reference_sample(wav_paths, max_clips=MAX_CLIPS,
                            reference_rank=0, *, dataset_root=None):
    """Return one ranked reference candidate, declining weak candidates."""
    ranked = rank_reference_samples(wav_paths, max_clips=max_clips, dataset_root=dataset_root)
    if not ranked or reference_rank < 0 or reference_rank >= len(ranked):
        return None, None
    best, score = ranked[reference_rank]
    if score < MIN_USABLE_SIMILARITY:
        # Found one, but it is the best of a bad lot. Report the score so the
        # caller can log it, and decline to recommend - overriding a reference
        # that happened to be fine with a mediocre medoid cost 0.108 on
        # breathy_baritone_30s_m_fantasy.
        return None, score
    return best, score
