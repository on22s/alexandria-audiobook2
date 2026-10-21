"""Call-local internal synthesis windows, with one staged parent output."""
import copy
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import tempfile

from synthesis_windows import get_synthesis_plan_text, plan_synthesis_windows
from synthesis_audio import (assemble_synthesis_windows, get_synthesis_audio_info,
                              validate_synthesis_audio, get_synthesis_pcm_shapes, SynthesisJoinPolicy)

_active_root = ContextVar('active_synthesis_window_root', default=False)
_receipts = ContextVar('synthesis_window_receipts', default=None)


@contextmanager
def ensure_synthesis_receipts():
    receipts = []
    token = _receipts.set(receipts)
    try:
        yield receipts
    finally:
        _receipts.reset(token)


def get_synthesis_window_plan(text, settings, budget_keys):
    if _active_root.get() or not settings or not settings['enabled']:
        return None
    budgets = [settings['budgets'][key] for key in budget_keys if key in settings['budgets']]
    if not budgets:
        return None
    words = [budget['max_words'] for budget in budgets if budget['max_words'] is not None]
    plan = plan_synthesis_windows(text, max_chars=min(budget['max_chars'] for budget in budgets),
                                 max_words=min(words) if words else None)
    return plan if len(plan.windows) > 1 else None


def render_synthesis_windows(plan, render, output_path, settings, cancel_check=None):
    """Render exact prepared spans; failure/cancel leaves prior parent bytes intact."""
    from audio_mastering import get_audio_sha256
    from tts import ensure_audio_export_active

    get_synthesis_plan_text(plan)
    if len(plan.windows) < 2:
        raise ValueError('Internal synthesis orchestration needs more than one window')
    settings = copy.deepcopy(settings)
    policy = SynthesisJoinPolicy(**settings['seams'])
    policy.validate()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    ensure_audio_export_active(cancel_check)
    token = _active_root.set(True)
    try:
        with tempfile.TemporaryDirectory(prefix='.synthesis-render-', dir=output.parent) as directory:
            results, windows, metadata = [], [], []
            for index, window in enumerate(plan.windows):
                ensure_audio_export_active(cancel_check)
                path = Path(directory)/f'window-{index}.wav'
                try:
                    success = render(window.text, str(path))
                except Exception:
                    ensure_audio_export_active(cancel_check)
                    raise
                ensure_audio_export_active(cancel_check)
                if not success:
                    raise RuntimeError(f'Internal synthesis window {index + 1} failed')
                info = get_synthesis_audio_info(path)
                metadata.append(info)
                get_synthesis_pcm_shapes(metadata, policy)
                validate_synthesis_audio(path, f'Internal synthesis window {index + 1}', window.text, cancel_check)
                windows.append(dict(index=index, start=window.start, end=window.end, boundary=window.boundary,
                                    frames=info.frames, sample_rate=info.samplerate, channels=info.channels,
                                    audio_sha256=get_audio_sha256(path, cancel_check)))
                results.append((index, path))
            receipt = assemble_synthesis_windows(plan, results, output, policy=policy, cancel_check=cancel_check)
            receipt.update(version=1, policy=settings, windows=windows)
            captured = _receipts.get()
            if captured is not None:
                captured.append(copy.deepcopy(receipt))
            return receipt
    finally:
        _active_root.reset(token)
