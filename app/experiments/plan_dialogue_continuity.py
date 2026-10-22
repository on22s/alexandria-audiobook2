"""Export a bounded continuity experiment plan, without models or rendering.

Run from app/: python -m experiments.plan_dialogue_continuity --input INPUT --out PLAN
Input is {version:1,narrator_speakers:[...],entries:[...]}. Use synthetic or
rights-cleared material for any artifact that will be shared publicly.
An entry's instruct is protected unless instruction_origin is explicitly
"generated". That declaration allows appending a suggestion to the baseline
note; "manual" (or no origin) preserves even an intentionally empty note.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from adapter_publication import save_adapter_publication_bytes
import dialogue_continuity
from project_scope_files import capture_scope_file
from utils import file_lock

MAX_PLAN_BYTES = 16 * 1024**2


def get_continuity_experiment_packet(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= dialogue_continuity.MAX_CONTINUITY_BYTES:
        raise ValueError('Continuity input must contain at most 2 MiB')
    try:
        document = json.loads(raw)
    except (RecursionError, UnicodeError) as error:
        raise ValueError('Continuity input must be bounded, decodable JSON') from error
    if (not isinstance(document, dict) or set(document) != {'version', 'narrator_speakers', 'entries'}
            or type(document['version']) is not int or document['version'] != 1):
        raise ValueError('Invalid continuity experiment input schema')
    code = dialogue_continuity._LOADED_CONTINUITY_CODE_SHA256
    if not isinstance(code, str) or len(code) != 64:
        raise ValueError('Loaded continuity planner identity is unavailable')
    return {'version': 1, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'planner_loaded_code_sha256': code,
            'plan': dialogue_continuity.get_dialogue_continuity_plan(
                document['entries'], narrator_speakers=document['narrator_speakers']),
            'qualification': 'plan_only; native rendering, alignment, drift and listening remain unqualified'}


def save_continuity_experiment_plan(source_path, output_path):
    source, output = Path(source_path), Path(os.path.abspath(output_path))
    if source.resolve() == output.resolve():
        raise ValueError('Plan output must not replace its input')
    facts, raw = capture_scope_file(source.parent.resolve(), source.name,
        limit=dialogue_continuity.MAX_CONTINUITY_BYTES, include_bytes=True)
    if raw is None:
        raise ValueError('Continuity input does not exist')
    packet = get_continuity_experiment_packet(raw)
    encoded = (json.dumps(packet, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
    if len(encoded) > MAX_PLAN_BYTES:
        raise ValueError('Continuity plan exceeds the 16 MiB output limit')
    with file_lock(str(output)):
        if os.path.lexists(output):
            raise FileExistsError('Choose a new plan output; an existing file will not be replaced')
        current, _ = capture_scope_file(source.parent.resolve(), source.name,
            limit=dialogue_continuity.MAX_CONTINUITY_BYTES)
        if current != facts:
            raise ValueError('Continuity input changed while planning')
        save_adapter_publication_bytes(str(output), encoded)
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, help='Explicit bounded experiment-input JSON')
    parser.add_argument('--out', required=True, help='New output file; existing files are preserved')
    args = parser.parse_args(argv)
    try:
        packet = save_continuity_experiment_plan(args.input, args.out)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps({'status': 'planned', 'source_sha256': packet['source_sha256'],
                      'rows': len(packet['plan']['rows']), 'pending_arms': packet['plan']['pending_arms']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
