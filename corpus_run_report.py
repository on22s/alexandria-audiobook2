"""Run and report only the preparer attempts owned by one corpus output directory."""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from alexandria_run_manifest import get_file_identity, write_json_atomic
from alexandria_file_lock import acquire_exclusive_file_lock


def get_annotation_metrics(row):
    path = Path(row['summary_path'])
    if get_file_identity(path) != row['summary_identity']:
        raise ValueError('annotation summary changed')
    if get_file_identity(row['output']) != row['dataset_identity']:
        raise ValueError('dataset changed')
    document = json.loads(path.read_text(encoding='utf-8'))
    if (type(document.get('version')) is not int or document['version'] != 1
            or document.get('phase') != 'annotation_complete'
            or document.get('counter_scope') != 'current_attempt'):
        raise ValueError('unsupported annotation summary')
    identity = document['identity']
    for name in ('audio', 'source'):
        if identity[name] != row[name + '_identity'] or get_file_identity(row[name]) != identity[name]:
            raise ValueError('annotation summary inputs changed or belong to another pair')
    options = identity['options']
    for key, value in row['options'].items():
        if options.get(key) != value:
            raise ValueError('annotation summary belongs to another invocation')
    totals = document['totals']
    for key in ('segments_total', 'segments_this_run', 'resumed_segments'):
        if type(totals[key]) is not int or totals[key] < 0:
            raise ValueError('invalid segment counts')
    if totals['segments_total'] != totals['segments_this_run'] + totals['resumed_segments']:
        raise ValueError('inconsistent resumed segment counts')
    seconds = totals['dataset_seconds']
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('invalid dataset duration')
    counters = document['counters']
    for key in ('llm_success', 'llm_fail', 'sanitize_changed', 'realign_events', 'reanchor_events'):
        if type(counters[key]) is not int or counters[key] < 0:
            raise ValueError('invalid annotation counter')
    for key in ('cut_strategy', 'source_action'):
        if not isinstance(counters[key], dict) or any(not isinstance(k, str) or type(v) is not int or v < 0
                                                     for k, v in counters[key].items()):
            raise ValueError('invalid annotation histogram')
    cursor = document['source_cursor']
    if cursor is not None:
        if any(type(cursor[k]) is not int or cursor[k] < 0 for k in ('word', 'total_words')):
            raise ValueError('invalid source cursor')
    return document


def save_corpus_report(index, output_dir):
    """Validate owned artifacts and replace the report, retaining every failed row."""
    if type(index.get('version')) is not int or index['version'] != 1 or not isinstance(index.get('rows'), list):
        raise ValueError('unsupported corpus attempt index')
    run_dir = Path(index['run_dir']).resolve(strict=True)
    if run_dir.parent != (output_dir / 'annotation_reports').resolve() or run_dir.name != index['run_id']:
        raise ValueError('attempt index belongs to another corpus')
    def cell(value):
        return str(value).replace('|', r'\|').replace('\n', ' ').replace('\r', ' ')
    lines = ['# Aggregated preparer-run report', '',
        'Corpus run: ' + cell(index['run_id']), '',
        'Only this corpus attempt index is read. Counters describe the final successful annotation attempt; '
        'segment and audio totals include resumed checkpoint entries. Earlier failed retry counters are not included.', '',
        '| run | audio | status | segs | new/resumed | replace% | drop% | realign | re-anchor | LLM ok/fail | sanitised | dataset sec | cursor end |',
        '|---|---|---|---:|---|---:|---:|---:|---:|---|---:|---:|---|']
    metrics, rates, warnings, failures, attempted = [], [], [], 0, 0
    for row in index['rows']:
        status = row['status']
        values = [index['run_id'], Path(row['audio']).name, status] + ['-'] * 10
        if status != 'no source match':
            attempted += 1
            try:
                if status != 'completed':
                    raise ValueError(status)
                if Path(row['summary_path']).resolve().parent != run_dir:
                    raise ValueError('summary is outside the owned run directory')
                data = get_annotation_metrics(row)
                totals, counters = data['totals'], data['counters']
                actions = counters['source_action']
                count = sum(actions.values())
                replace = 100 * actions.get('replace', 0) / count if count else 0
                dropped = 100 * sum(v for k, v in actions.items() if k.startswith('dropped')) / count if count else 0
                cursor = data['source_cursor']
                values[3:] = [totals['segments_total'], f"{totals['segments_this_run']}/{totals['resumed_segments']}",
                    f'{replace:.1f}', f'{dropped:.1f}', counters['realign_events'], counters['reanchor_events'],
                    f"{counters['llm_success']}/{counters['llm_fail']}", counters['sanitize_changed'],
                    totals['dataset_seconds'], f"{cursor['word']}/{cursor['total_words']}" if cursor else '-']
                metrics.append(data)
                rates.append((replace, dropped))
                label = cell(Path(row['audio']).name)
                if dropped > 20:
                    warnings.append(f'{label}: drop rate {dropped:.1f}%; inspect source/audio pairing.')
                if counters['llm_fail'] > 5:
                    warnings.append(f"{label}: {counters['llm_fail']} LLM failures.")
                if counters['realign_events'] > 10:
                    warnings.append(f"{label}: {counters['realign_events']} realign events; inspect cursor recovery.")
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                failures += 1
                values[2] = 'failed: ' + str(error)
                print(f"FAILED: {row['audio']}: {error}", file=sys.stderr, flush=True)
        lines.append('| ' + ' | '.join(cell(v) for v in values) + ' |')
    lines += ['', '## Aggregate stats across completed pairs', '',
        f"- Total segments emitted: **{sum(d['totals']['segments_total'] for d in metrics)}**",
        f"- Total dataset audio:    **{sum(d['totals']['dataset_seconds'] for d in metrics):.0f}s**"]
    for offset, label in ((0, 'replace'), (1, 'drop')):
        average = sum(rate[offset] for rate in rates) / len(rates) if rates else 0
        lines.append(f'- Avg {label} rate: **{average:.1f}%**')
    for key, label in (('realign_events', 'Realign events'), ('reanchor_events', 'Re-anchor events'),
                       ('llm_fail', 'LLM failures'), ('sanitize_changed', 'LLM sanitised')):
        lines.append(f"- {label} total: **{sum(d['counters'][key] for d in metrics)}**")
    lines.append(f"- LLM ok responses: **{sum(d['counters']['llm_success'] for d in metrics)}**")
    if warnings:
        lines += ['', '## Anomalies to inspect', ''] + ['- ' + warning for warning in warnings]
    lines += ['', f'{failures} pair(s) failed; {len(metrics)} completed; {attempted} attempted.', '']
    path = output_dir / 'aggregated_report.md'
    fd, temporary = tempfile.mkstemp(prefix='.corpus-report-', dir=output_dir)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if failures:
        print(f'{failures} pair(s) failed; report: {path}', file=sys.stderr)
    return 1 if failures or not attempted else 0


def run_corpus(repo, pairs_path, output_dir, model, fallback):
    pairs = json.loads(pairs_path.read_text(encoding='utf-8'))
    if not isinstance(pairs, list):
        raise ValueError('pairs must be a JSON array')
    reports = output_dir / 'annotation_reports'
    reports.mkdir(exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    run_dir = Path(tempfile.mkdtemp(prefix=stamp + '-', dir=reports))
    index = {'version': 1, 'run_id': run_dir.name, 'run_dir': str(run_dir), 'rows': []}
    def save_index():
        write_json_atomic(index, run_dir / 'attempts.json')
        write_json_atomic(index, output_dir / 'corpus_attempts.json')
    save_index()
    for pair in pairs:
        row = {'audio': pair['audio'], 'source': pair['source'], 'status': 'no source match'}
        index['rows'].append(row)
        if not pair['source']:
            save_index()
            continue
        row['status'] = 'pending'
        save_index()
        try:
            audio, source = Path(pair['audio']).resolve(strict=True), Path(pair['source']).resolve(strict=True)
            name = pair['output_name']
            if not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..'):
                raise ValueError('unsafe pair output name')
            output, summary = output_dir / name, run_dir / (name + '.summary.json')
            row.update(audio=str(audio), source=str(source), output=str(output), summary_path=str(summary),
                audio_identity=get_file_identity(audio), source_identity=get_file_identity(source),
                options={'summary_output': str(summary), 'output': str(output), 'model': model,
                         'fallback_model': fallback, 'chunk_size': 10.0, 'lang': 'en'})
            save_index()
            result = subprocess.run([str(repo / 'run_with_restart.sh'), '--audio', str(audio), '--source', str(source),
                '--model', model, '--fallback-model', fallback, '--output', str(output),
                '--summary-output', str(summary), '--chunk-size', '10.0', '--lang', 'en'], cwd=repo)
            row['worker_exit'] = result.returncode
            if result.returncode:
                raise ValueError(f'worker exit {result.returncode}')
            row['summary_identity'] = get_file_identity(summary)
            row['dataset_identity'] = get_file_identity(output)
            get_annotation_metrics(row)
            row['status'] = 'completed'
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            row['status'] = 'failed: ' + str(error)
        save_index()
    return save_corpus_report(index, output_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--pairs', type=Path)
    parser.add_argument('--model')
    parser.add_argument('--fallback')
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with (output_dir / '.corpus.lock').open('a') as lock:
            acquire_exclusive_file_lock(lock.fileno())
            if args.pairs:
                return run_corpus(args.repo.resolve(), args.pairs, output_dir, args.model, args.fallback)
            path = output_dir / 'corpus_attempts.json'
            if not path.is_file():
                raise ValueError('No owned corpus attempt index; run --run first')
            return save_corpus_report(json.loads(path.read_text(encoding='utf-8')), output_dir)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(f'Corpus report failed: {error}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
