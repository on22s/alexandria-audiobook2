"""Opt-in, review-only comparison of saved attribution checkpoints."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
from utils import atomic_json_write, file_lock
from experiments.provenance import provenance

PROMPT = ('Compare two proposed speaker labels using only the supplied evidence. '
          'Do not invent identities or treat a provisional reference as ground truth. '
          'Return JSON with same_identity (true, false, or null when uncertain) '
          'and reason (a nonempty explanation grounded in the evidence).')
MODES = ('none', 'low')
MAX_ATTEMPTS = 40


def get_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def get_entries(path):
    value = json.loads(Path(path).read_text())
    return value['entries'] if isinstance(value, dict) else value


def get_checkpoint(path, allow_partial=False):
    raw = Path(path).read_bytes()
    lines = raw.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b'\n'):
        if not allow_partial:
            raise ValueError('Checkpoint has an unfinished final line')
        lines.pop()
    return [json.loads(line) for line in lines if line.strip()]


def validate_entries(entries, labelled=False):
    if not isinstance(entries, list):
        raise ValueError('Entries must be a list')
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('text'), str)
                or entry.get('type') not in ('SPOKEN', 'NARRATOR')):
            raise ValueError('Invalid text/type entry')
        if labelled and (not isinstance(entry.get('speaker'), str)
                         or not entry['speaker'].strip()):
            raise ValueError('Missing speaker label')


def get_candidates(source, prediction, reference, allow_partial=False, max_pairs=20):
    if not 1 <= max_pairs <= 20:
        raise ValueError('max_pairs must be between 1 and 20')
    validate_entries(source)
    validate_entries(prediction, labelled=True)
    validate_entries(reference, labelled=True)
    indices = [row.get('entry_index') for row in prediction]
    if (any(type(index) is not int for index in indices)
            or set(indices) != set(range(len(prediction)))
            or len(prediction) > len(source)):
        raise ValueError('Checkpoint must contain unique contiguous prefix indices')
    for row in prediction:
        original = source[row['entry_index']]
        if any(row[key] != original[key] for key in ('text', 'type')):
            raise ValueError('Checkpoint differs from source text/type')
    complete = len(prediction) == len(source)
    if not complete and not allow_partial:
        raise ValueError('Incomplete checkpoint requires --allow-partial')
    lookup = {}
    for index, row in enumerate(reference):
        lookup.setdefault((row['text'], row['type']), []).append(index)
    by_index = {row['entry_index']: row for row in prediction}
    candidates, skipped, seen = [], [], set()
    for row in prediction:
        if row['type'] != 'SPOKEN':
            continue
        matches = lookup.get((row['text'], row['type']), [])
        if len(matches) != 1:
            skipped.append({'entry_index': row['entry_index'],
                            'reason': 'ambiguous' if matches else 'unmatched'})
            continue
        ref_index = matches[0]
        pair = tuple(sorted((row['speaker'], reference[ref_index]['speaker'])))
        if pair[0] == pair[1] or pair in seen or len(candidates) >= max_pairs:
            continue
        seen.add(pair)
        index = row['entry_index']
        context = [{'text': source[i]['text'], 'type': source[i]['type'],
                    'speaker': by_index[i]['speaker'] if i in by_index else None}
                   for i in range(max(0, index - 2), min(len(source), index + 3))]
        candidates.append({'entry_index': index, 'labels': list(pair),
                           'prediction_label': row['speaker'],
                           'reference_label': reference[ref_index]['speaker'],
                           'context': context,
                           'reference_context': reference[max(0, ref_index - 2):ref_index + 3]})
    return candidates, skipped, complete


def validate_verdict(value):
    if (not isinstance(value, dict) or 'same_identity' not in value
            or (value['same_identity'] is not None and type(value['same_identity']) is not bool)
            or not isinstance(value.get('reason'), str) or not value['reason'].strip()):
        raise ValueError('Invalid review verdict')


def apply_reviews(output, candidates, skipped, complete, settings, reviewer, prompt=PROMPT):
    """Persist every attempt before calling; successful unchanged evidence is reusable."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(str(output / 'cache.json'), timeout=1):
        source = provenance(__file__, argparse.Namespace(
            settings_digest=get_digest(settings), evidence_digest=get_digest(candidates),
            complete=complete, prompt_digest=get_digest(prompt), max_attempts=MAX_ATTEMPTS))
        cache_path = output / 'cache.json'
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {
            'schema': 1, 'attempts': 0, 'reviews': {}}
        if (cache.get('schema') != 1 or type(cache.get('attempts')) is not int
                or not 0 <= cache['attempts'] <= MAX_ATTEMPTS
                or not isinstance(cache.get('reviews'), dict)):
            raise ValueError('Invalid cache')
        cache['provenance'] = source
        results = []
        for candidate in candidates:
            for mode in MODES:
                key = get_digest({'schema': 1, 'evidence': candidate, 'settings': settings,
                                  'mode': mode, 'prompt': prompt})
                result = {'key': key, 'entry_index': candidate['entry_index'], 'mode': mode}
                if key in cache['reviews']:
                    validate_verdict(cache['reviews'][key])
                    result['verdict'] = cache['reviews'][key]
                elif cache['attempts'] >= MAX_ATTEMPTS:
                    result['error'] = 'Review attempt ceiling reached; use a new private run directory'
                else:
                    cache['attempts'] += 1
                    atomic_json_write(cache, str(cache_path), allow_nonatomic_fallback=False)
                    try:
                        verdict = reviewer(candidate, mode, prompt)
                        validate_verdict(verdict)
                    except Exception as exc:
                        # Exception text can include credentials/provider payloads.
                        result['error'] = type(exc).__name__
                    else:
                        cache['reviews'][key] = verdict
                        atomic_json_write(cache, str(cache_path), allow_nonatomic_fallback=False)
                        result['verdict'] = verdict
                results.append(result)
        report = {'schema': 1, 'phase': 'reconciled' if complete else 'provisional',
                  'review_only': True, 'attempts': cache['attempts'],
                  'candidates': candidates, 'reviews': results, 'skipped': skipped,
                  'provenance': source}
        atomic_json_write(report, str(output / 'report.json'), allow_nonatomic_fallback=False)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'prediction', 'reference', 'config', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--allow-partial', action='store_true')
    parser.add_argument('--allow-network', action='store_true')
    args = parser.parse_args()
    if not args.allow_network:
        parser.error('Paid/remote requests require --allow-network')
    if args.output.resolve().is_relative_to(APP.parent.resolve()):
        parser.error('Private review output must be outside the repository')
    targets = {(args.output / name).resolve() for name in ('cache.json', 'report.json')}
    if any(path.resolve() in targets for path in
           (args.source, args.prediction, args.reference, args.config)):
        parser.error('Output files must not overwrite input files')
    os.umask(0o077)
    candidates, skipped, complete = get_candidates(
        get_entries(args.source), get_checkpoint(args.prediction, args.allow_partial),
        get_entries(args.reference), args.allow_partial)
    from lmstudio_settings import get_active_llm_config, is_remote_llm
    from llm_provider import make_llm_client
    from generate_script import LLMGenParams, call_llm_for_object
    from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock
    config = json.loads(args.config.read_text())
    profile = get_active_llm_config(config)
    if config.get('llm_failover') or profile.get('transport') == 'manual':
        parser.error('This experiment requires one explicit API profile without failover')
    if (not profile.get('model_name') or not profile.get('base_url')
            or type(profile.get('context_length')) is not int
            or profile['context_length'] <= 0):
        parser.error('Set an explicit model_name and context_length in the active profile')
    # Hash the entire profile so private credentials/headers never appear in artifacts.
    settings = {'profile_digest': get_digest(profile), 'max_tokens': 512,
                'temperature': 0, 'reasoning_allowance': 512, 'structured_output': 'off'}
    local = not is_remote_llm(config.get('llm_mode'), profile.get('base_url', ''))
    handle = acquire_gpu_lock() if local or profile.get('on_this_gpu') else None
    client = None
    try:
        client = make_llm_client(profile, timeout=120)
        def review(candidate, mode, prompt):
            params = LLMGenParams(max_tokens=512, hard_max_tokens=1024, temperature=0,
                                  context_length=profile['context_length'],
                                  reasoning_effort=mode, reasoning_allowance=512 if mode == 'low' else 0,
                                  structured_output='off', api_retry_limit=0)
            return call_llm_for_object(client, profile['model_name'], prompt,
                                       json.dumps(candidate, ensure_ascii=False), params,
                                       label='incremental alias review', validate_object=validate_verdict,
                                       max_retries=0)
        report = apply_reviews(args.output, candidates, skipped, complete, settings, review)
        print(json.dumps({'phase': report['phase'], 'attempts': report['attempts'],
                          'errors': sum('error' in row for row in report['reviews'])}))
        return 1 if any('error' in row for row in report['reviews']) else 0
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            release_gpu_lock(handle)


if __name__ == '__main__':
    raise SystemExit(main())
