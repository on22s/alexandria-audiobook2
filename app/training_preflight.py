"""Read-only training resource evidence, shared by batch and Voice Lab."""
import importlib.util
import json
import math
import sys
from device_utils import normalize_device, resolve_device


def get_training_vram_error(device, free_gb):
    """Apply Voice Lab's existing eight-GB training floor when measurable."""
    if device == 'cpu' or free_gb is None:
        return None
    if not isinstance(free_gb, (int, float)) or isinstance(free_gb, bool) or not math.isfinite(free_gb) or free_gb < 0:
        return 'Training free VRAM measurement is invalid.'
    if free_gb < 8:
        return 'Training requires at least 8 GB of free VRAM.'
    return None


def get_training_disk_error(free_bytes, required_bytes=0):
    """Retain the two-GB disk floor and account for declared scratch needs."""
    if free_bytes < 2 * 1024 ** 3 + required_bytes:
        return 'Insufficient free disk for training preflight and output (minimum 2 GB).'
    return None


def get_training_runtime_preflight(requested_device):
    """Probe the executing interpreter, never load a speech model."""
    requested = normalize_device(requested_device)
    missing = [name for name in ('torch', 'librosa', 'peft', 'soundfile', 'numpy', 'qwen_tts')
               if importlib.util.find_spec(name) is None]
    if missing:
        raise ValueError('Missing training dependencies: ' + ', '.join(missing))
    import torch
    device = resolve_device(requested)
    free_gb = None
    if device.startswith('cuda'):
        if not torch.cuda.is_available():
            raise ValueError('Selected CUDA/ROCm device is unavailable')
        index = int(device.split(':')[1]) if ':' in device else 0
        if index >= torch.cuda.device_count():
            raise ValueError('Selected CUDA/ROCm device index is unavailable')
        free, _ = torch.cuda.mem_get_info(index)
        free_gb = free / 1024 ** 3
    elif device == 'mps' and not torch.backends.mps.is_available():
        raise ValueError('Selected MPS device is unavailable')
    error = get_training_vram_error(device, free_gb)
    if error:
        raise ValueError(error)
    return {'python': sys.executable, 'device': device, 'torch': torch.__version__,
            'free_vram_gb': free_gb,
            'vram_status': 'measured' if free_gb is not None else (
                'not_applicable_cpu' if device == 'cpu' else 'unavailable_unified_memory')}


def get_selected_interpreter_preflight(python, zip_paths, device):
    """Validate every archive using the interpreter selected for training."""
    import os
    import subprocess
    command = [python, os.path.abspath(__file__), '--batch']
    payload = json.dumps({'zip_paths': zip_paths, 'device': device})
    try:
        result = subprocess.run(command, input=payload, capture_output=True,
                                text=True, timeout=300, check=False)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        report = json.loads(lines[-1]) if lines else None
        if not isinstance(report, dict) or report.get('status') not in ('ready', 'failed'):
            raise ValueError('Selected interpreter returned no valid preflight receipt')
        if not isinstance(report.get('datasets'), list) or not isinstance(report.get('errors'), list):
            raise ValueError('Selected interpreter returned malformed archive results')
        if report['status'] == 'ready':
            names = [row.get('archive') for row in report['datasets'] if isinstance(row, dict)]
            expected = [os.path.basename(path) for path in zip_paths]
            if (report['errors'] or len(names) != len(expected) or sorted(names) != sorted(expected)
                    or not isinstance(report.get('runtime'), dict)):
                raise ValueError('Ready receipt does not verify every requested archive and runtime')
        elif not report['errors']:
            raise ValueError('Failed receipt contains no error evidence')
        if result.returncode != 0 and report['status'] != 'failed':
            raise ValueError('Selected interpreter failed without a failed receipt')
        return report
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        diagnostic = str(error)
        if 'result' in locals() and result.stderr.strip():
            diagnostic += ': ' + result.stderr[-1000:].strip()
        return {'status': 'failed', 'datasets': [], 'errors': [
            {'archive': '(selected interpreter)', 'error': diagnostic}]}


if __name__ == '__main__':
    if sys.argv[1:] == ['--batch']:
        import os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tools.voice_lab.batch_train_lora import get_batch_archive_preflight
        try:
            payload = json.load(sys.stdin)
            result = get_batch_archive_preflight(payload['zip_paths'])
            try:
                result['runtime'] = get_training_runtime_preflight(payload['device'])
            except Exception as error:
                result['errors'].append({'archive': '(selected interpreter)', 'error': str(error)})
            result['status'] = 'failed' if result['errors'] else 'ready'
        except Exception as error:
            result = {'status': 'failed', 'datasets': [], 'errors': [
                {'archive': '(preflight)', 'error': str(error)}]}
        print(json.dumps(result))
        sys.exit(0 if result['status'] == 'ready' else 1)
    try:
        result = get_training_runtime_preflight(sys.argv[1])
        print(json.dumps({'status': 'ready', 'runtime': result}))
    except Exception as error:
        print(json.dumps({'status': 'failed', 'error': str(error)}))
        sys.exit(1)
