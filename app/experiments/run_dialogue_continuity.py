"""Render one explicit continuity comparison using normal AA2 ownership.

Run from app/: python -m experiments.run_dialogue_continuity --help
Use synthetic or rights-cleared source. Every run uses a new output directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from adapter_publication import save_adapter_publication_bytes
from experiments.plan_dialogue_continuity import get_continuity_experiment_packet
from experiments.render_dialogue_continuity import save_continuity_render_pair
from project_scope_files import capture_scope_file


def _get_input(path):
    path = Path(path)
    _, raw = capture_scope_file(path.parent.resolve(), path.name, limit=2 * 1024**2,
                                include_bytes=True)
    if raw is None:
        raise ValueError('An explicit experiment input is missing')
    return raw


def get_continuity_output_path(root, path):
    out = Path(path).resolve()
    if out == root or root in out.parents:
        raise ValueError('Experiment output must be outside the source checkout')
    return out


def run_owned_continuity_pair(core, raw, packet, row, voices, destination):
    """Disposable-process only: failed cleanup terminates this whole process.

    Never return through core's unconditional claim release with native models
    still resident. OS teardown releases this process's resources and leases.
    """
    outcome = {}

    def render():
        try:
            engine = core.project_manager.get_engine()
            if engine is None:
                raise RuntimeError('Experiment engine initialization failed')
            outcome['receipt'] = save_continuity_render_pair(
                engine, raw, packet, row, voices, destination)
        except BaseException as error:
            outcome['error'] = error
        finally:
            try:
                if not core.project_manager.ensure_idle_local_models_released():
                    raise RuntimeError('Experiment model cleanup did not finish')
            except BaseException as error:
                try:
                    failure_path = Path(destination).with_name('cleanup-failure.json')
                    if not os.path.lexists(failure_path):
                        save_adapter_publication_bytes(str(failure_path),
                            b'{"status":"failed","reason":"model_cleanup_failed","exit_code":70}\n')
                finally:
                    # Even a full disk must not route through claim release.
                    os._exit(70)

    claim = core.reserve_background_task('audio')
    worker = core.start_claimed_task_thread('audio', claim, render)
    while worker.is_alive():
        try:
            worker.join(.25)
        except KeyboardInterrupt:
            # Same task mutex and flag used by Editor cancellation. Retain the
            # owner until the worker observes it and finishes model cleanup.
            with core._gpu_lock:
                core.process_state['audio']['cancel'] = True
    if 'error' in outcome:
        raise outcome['error']
    if 'receipt' not in outcome:
        raise RuntimeError('Experiment task exited without a result')
    return outcome['receipt']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'voices', 'config', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--row', required=True, type=int, help='Zero-based source row')
    args = parser.parse_args(argv)
    try:
        if 'core' in sys.modules or os.environ.get('ALEXANDRIA_DATA_DIR'):
            raise ValueError('Run in a fresh process without an existing data directory override')
        raw, voices_raw, config_raw = map(_get_input, (args.input, args.voices, args.config))
        packet = get_continuity_experiment_packet(raw)
        voices, config = json.loads(voices_raw), json.loads(config_raw)
        if (not isinstance(config, dict) or not isinstance(config.get('tts'), dict)
                or config['tts'].get('mode') != 'local'):
            raise ValueError('This experiment requires explicit local TTS settings')
        if not 0 <= args.row < len(packet['plan']['rows']):
            raise ValueError('Invalid experiment row')
        speaker = packet['plan']['rows'][args.row]['speaker']
        voice = voices.get(speaker) if isinstance(voices, dict) else None
        if not isinstance(voice, dict):
            raise ValueError('Declare a voice configuration for the selected speaker')
        root = Path(__file__).resolve().parents[2]
        def git(*arguments):
            return subprocess.check_output(['git', *arguments], cwd=root, text=True).strip()
        if git('status', '--porcelain', '--untracked-files=all'):
            raise ValueError('Native experiments require a clean committed checkout')
        identity = {'head': git('rev-parse', 'HEAD'), 'tree': git('rev-parse', 'HEAD^{tree}'),
                    'inputs': {name: hashlib.sha256(value).hexdigest() for name, value in
                               (('source', raw), ('voices', voices_raw), ('config', config_raw))}}
        out = get_continuity_output_path(root, args.out)
        out.mkdir(exist_ok=False)
        data = out / 'data'; data.mkdir()
        for name, value in (('source.json', raw), ('voices.json', voices_raw),
                            ('identity.json', (json.dumps(identity, sort_keys=True) + '\n').encode())):
            save_adapter_publication_bytes(str(out / name), value)
        save_adapter_publication_bytes(str(data / 'config.json'), config_raw)
        os.environ['ALEXANDRIA_DATA_DIR'] = str(data)
        import core
        receipt = run_owned_continuity_pair(core, raw, packet, args.row, voices, out / 'pair')
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(json.dumps({'status': 'captured', 'uid': receipt['uid'], 'out': str(out),
                      'qualification': receipt['qualification']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
