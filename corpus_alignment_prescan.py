"""Run the existing locked ASR pre-scan and report only validated pair results."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from alexandria_run_manifest import get_file_identity, write_json_atomic


def get_alignment_report_quality(path, audio, source, output):
    document = json.loads(path.read_text(encoding='utf-8'))
    if (not isinstance(document, dict) or type(document.get('version')) is not int
            or document.get('version') != 1
            or document.get('scope') != 'initial_provisional_chunks'):
        raise ValueError('unsupported alignment report')
    identity = document['identity']
    if (identity['audio'] != get_file_identity(audio)
            or identity['source'] != get_file_identity(source)):
        raise ValueError('alignment report inputs changed or belong to another pair')
    options = identity['options']
    if (options.get('alignment_report') != str(path)
            or options.get('output') != str(output)
            or options.get('chunk_size') != 10.0 or options.get('lang') != 'en'):
        raise ValueError('alignment report settings belong to another invocation')
    quality = document['quality']
    count = quality['sampled']
    average = quality['average_ratio']
    if (type(count) is not int or count <= 0
            or type(average) not in (int, float) or not math.isfinite(average)
            or not 0 <= average <= 1):
        raise ValueError('alignment report has invalid measured samples')
    low, review = quality['below_60_percent'], quality['review_needed']
    if (type(low) is not int or type(review) is not int
            or not 0 <= low <= review <= count):
        raise ValueError('alignment report has invalid sample counts')
    return dict(quality)


def save_alignment_markdown(rows, path):
    def cell(value):
        return str(value).replace('|', r'\|').replace('\n', ' ').replace('\r', ' ')
    lines = ['# Test corpus dry-run report', '',
        'ASR-only pre-scan of initial provisional chunks; no LLM annotation or dataset export.',
        'ASR takes time and writes its normal scratch/checkpoint files. These samples do not measure whole-book alignment.', '',
        '| audio | source | status | avg ratio | n sampled | below 60% | review-needed |',
        '|---|---|---|---:|---:|---:|---:|']
    for row in rows:
        quality = row.get('quality', {})
        average = f"{quality['average_ratio']:.3f}" if quality else '-'
        values = [Path(row['audio']).name, Path(row['source']).name if row['source'] else '-',
                  row['status'], average, quality.get('sampled', '-'),
                  quality.get('below_60_percent', '-'), quality.get('review_needed', '-')]
        lines.append('| ' + ' | '.join(cell(value) for value in values) + ' |')
    descriptor, temporary = tempfile.mkstemp(prefix='.dry-report-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, required=True)
    parser.add_argument('--pairs', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    repo, output_dir = args.repo.resolve(), args.output_dir.resolve()
    pairs = json.loads(args.pairs.read_text(encoding='utf-8'))
    if not isinstance(pairs, list):
        parser.error('pairs must be a JSON array')
    output_dir.mkdir(parents=True, exist_ok=True)
    reports_root = output_dir / 'alignment_reports'
    reports_root.mkdir(exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix='prescan-', dir=reports_root))
    rows, failures, attempted = [], 0, 0
    for pair in pairs:
        row = {'audio': pair['audio'], 'source': pair['source'], 'status': 'no source match'}
        rows.append(row)
        if not pair['source']:
            continue
        attempted += 1
        try:
            audio, source = Path(pair['audio']).resolve(strict=True), Path(pair['source']).resolve(strict=True)
            name = pair['output_name']
            if not isinstance(name, str) or Path(name).name != name or name in ('', '.', '..'):
                raise ValueError('unsafe pair output name')
            output = output_dir / name
            report = run_dir / (name + '.alignment.json')
            result = subprocess.run([str(repo / 'run_with_restart.sh'), '--audio', str(audio),
                '--source', str(source), '--phase', 'asr', '--alignment-report', str(report),
                '--output', str(output), '--chunk-size', '10.0', '--lang', 'en'], cwd=repo)
            if result.returncode:
                raise ValueError(f'ASR worker exited {result.returncode}')
            row['quality'] = get_alignment_report_quality(report, audio, source, output)
            row['report_path'] = str(report)
            row['status'] = 'measured'
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            failures += 1
            row['status'] = 'failed: ' + str(error)
            print(f"FAILED: {row['audio']}: {error}", flush=True)
    write_json_atomic({'version': 1, 'rows': rows}, output_dir / 'dry_run_report.json')
    save_alignment_markdown(rows, output_dir / 'dry_run_report.md')
    print(f'Dry-run: {attempted} pair(s), {failures} failure(s); report: {output_dir / "dry_run_report.md"}')
    return 1 if failures or not attempted else 0


if __name__ == '__main__':
    raise SystemExit(main())
