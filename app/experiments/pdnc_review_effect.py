"""Offline, source-position-bound PDNC review measurement; emits aggregates only.

Example: python app/experiments/pdnc_review_effect.py --novel DIR
  --manifest S0.json.threepass_manifest.json --baseline S0.json
  --arm full=S1.json --arm labels=S2.json --source-profile apostrophe-repair
  --output result.json

The explicit source profile must match the generation manifest before scoring.
Unlocated/coarse rows never acquire guessed offsets. All quotation parts must
have complete alphanumeric coverage; predictions use row-majority attribution.
Corpus text, names, per-quote predictions and private paths are not published.
"""
import argparse
import ast
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path
import re
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apostrophe_repair import restore_stripped_apostrophes
from dialogue_spans import _normalize
from experiments.pdnc_fixture import is_pseudo_speaker
from experiments.provenance import file_sha256
from experiments.scoring import normalize, same_speaker
from experiments.stats import exact_mcnemar
from utils import atomic_json_write


class ScoringError(ValueError):
    """A text-free input/coverage failure, safe to report without source data."""


def get_sha256(data):
    return hashlib.sha256(data).hexdigest()


def get_prepared_source(raw_bytes, manifest, profile):
    raw = raw_bytes.decode('utf-8')
    if profile == 'raw':
        prepared = raw
    elif profile == 'apostrophe-repair':
        prepared, _ = restore_stripped_apostrophes(raw)
    else:
        raise ScoringError('unsupported_source_profile')
    if len(raw) != len(prepared) or any(
            a != b and (a.isalnum() or b.isalnum()) for a, b in zip(raw, prepared)):
        raise ScoringError('unsupported_source_transformation')
    if get_sha256(prepared.encode('utf-8')) != manifest.get('fingerprint', {}).get('source_sha256'):
        raise ScoringError('prepared_source_hash_mismatch')
    return raw, prepared


def get_span(value, limit):
    if (type(value) not in (list, tuple) or len(value) != 2
            or any(type(n) is not int for n in value)
            or not 0 <= value[0] < value[1] <= limit):
        raise ScoringError('invalid_span')
    return tuple(value)


def get_byte_boundaries(source):
    boundaries = {0: 0}
    cursor = 0
    for index, char in enumerate(source, 1):
        cursor += len(char.encode('utf-8'))
        boundaries[cursor] = index
    return boundaries


def get_literal(value):
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError, TypeError):
        raise ScoringError('malformed_gold_literal') from None


def get_gold_quotes(records, raw):
    boundaries = get_byte_boundaries(raw)
    byte_count = len(raw.encode('utf-8'))
    quotes, seen = [], set()
    for record in records:
        quote_id = record.get('quoteID')
        if not isinstance(quote_id, str) or not quote_id.strip() or quote_id in seen:
            raise ScoringError('invalid_or_duplicate_gold_id')
        seen.add(quote_id)
        spans = get_literal(record.get('quoteByteSpans'))
        pieces = get_literal(record.get('subQuotationList'))
        if (type(spans) is not list or type(pieces) is not list
                or not spans or len(spans) != len(pieces)):
            raise ScoringError('inconsistent_gold_pieces')
        positions, previous_end = [], 0
        for span, piece in zip(spans, pieces):
            start, end = get_span(span, byte_count)
            if start < previous_end or start not in boundaries or end not in boundaries:
                raise ScoringError('overlap_or_invalid_utf8_boundary')
            previous_end = end
            start, end = boundaries[start], boundaries[end]
            if not isinstance(piece, str) or _normalize(piece) != _normalize(raw[start:end]):
                raise ScoringError('gold_piece_source_mismatch')
            positions.append(tuple(i for i in range(start, end) if raw[i].isalnum()))
        speaker = record.get('speaker', '')
        if not isinstance(speaker, str):
            raise ScoringError('invalid_gold_speaker')
        quotes.append({'id': quote_id, 'pieces': positions, 'speaker': speaker})
    if not quotes:
        raise ScoringError('empty_gold_population')
    return quotes


def get_alias_groups(records):
    groups = []
    for record in records:
        main = record.get('Main Name', '')
        aliases = get_literal(record.get('Aliases') or '[]')
        if (not isinstance(main, str) or type(aliases) not in (list, tuple, set)
                or any(not isinstance(alias, str) for alias in aliases)):
            raise ScoringError('invalid_alias_record')
        members = {normalize(name) for name in [main, *aliases] if normalize(name)}
        if len(members) > 1:
            groups.append(members)
    return groups


def get_validated_rows(rows, prepared):
    if type(rows) is not list:
        raise ScoringError('invalid_script')
    excluded, candidates = Counter(), []
    for index, row in enumerate(rows):
        if type(row) is not dict:
            excluded['malformed_row'] += 1
            continue
        if 'source_span' not in row:
            excluded['missing_span'] += 1
            continue
        try:
            span = get_span(row['source_span'], len(prepared))
        except ScoringError:
            excluded['invalid_span'] += 1
            continue
        candidates.append((index, span, row))
    counts = Counter(span for _, span, _ in candidates)
    valid = []
    for index, span, row in candidates:
        if counts[span] > 1:
            excluded['coarse_or_repeated_span'] += 1
        elif not isinstance(row.get('text'), str):
            excluded['malformed_row'] += 1
        elif _normalize(row['text']) != _normalize(prepared[span[0]:span[1]]):
            excluded['text_mismatch'] += 1
        elif not isinstance(row.get('speaker', ''), str):
            excluded['invalid_speaker'] += 1
        else:
            valid.append((index, span, row.get('speaker', '')))
    previous_end = 0
    owners = [-1] * len(prepared)
    speakers = {}
    for index, (start, end), speaker in sorted(valid, key=lambda row: row[1]):
        if start < previous_end:
            raise ScoringError('overlapping_validated_rows')
        previous_end = end
        speakers[index] = speaker
        for position in range(start, end):
            if prepared[position].isalnum():
                owners[position] = index
    return owners, speakers, {'total_rows': len(rows), 'validated_rows': len(valid),
                             'excluded_rows': dict(sorted(excluded.items()))}


def score_rows(rows, quotes, prepared, groups=()):
    """Return aggregates and in-memory private predictions; serialize only aggregates."""
    owners, speakers, row_summary = get_validated_rows(rows, prepared)
    summary = Counter(total_quotes=len(quotes), pseudo_gold=0, empty_gold=0,
                      matched=0, correct=0, multirow=0, ties=0, partial=0,
                      unmatched=0, unscorable=0, missing_speaker=0, unknown_speaker=0,
                      required_positions=0, covered_positions=0, total_pieces=0, covered_pieces=0)
    predictions = []
    for quote in quotes:
        pieces = quote['pieces']
        votes = Counter(owners[i] for piece in pieces for i in piece if owners[i] >= 0)
        required = sum(map(len, pieces))
        covered = sum(votes.values())
        covered_pieces = sum(bool(piece) and all(owners[i] >= 0 for i in piece) for piece in pieces)
        unscorable = any(not piece for piece in pieces)
        matched = not unscorable and covered_pieces == len(pieces)
        winner = min(votes, key=lambda i: (-votes[i], i)) if matched else None
        speaker = speakers[winner] if matched else ''
        summary['pseudo_gold'] += is_pseudo_speaker(quote['speaker'])
        summary['empty_gold'] += not bool(normalize(quote['speaker']))
        for name, value in [('matched', matched), ('required_positions', required),
                            ('covered_positions', covered), ('total_pieces', len(pieces)),
                            ('covered_pieces', covered_pieces)]:
            summary[name] += value
        if unscorable:
            summary['unscorable'] += 1
        elif not matched:
            summary['partial' if covered else 'unmatched'] += 1
        if matched:
            summary['multirow'] += len(votes) > 1
            summary['ties'] += sum(value == votes[winner] for value in votes.values()) > 1
            summary['missing_speaker'] += not bool(normalize(speaker))
            summary['unknown_speaker'] += normalize(speaker) == 'UNKNOWN'
        correct = bool(matched and normalize(speaker) != 'UNKNOWN'
                       and same_speaker(quote['speaker'], speaker, groups))
        summary['correct'] += correct
        predictions.append({'matched': matched, 'correct': correct, 'speaker': speaker})
    result = dict(summary)
    result.update(row_summary)
    result['accuracy_all_gold_percent'] = 100 * result['correct'] / len(quotes)
    result['accuracy_matched_percent'] = (100 * result['correct'] / result['matched']
                                          if result['matched'] else None)
    return result, predictions


def get_paired_comparison(baseline, arm, groups=(), *, common_coverage=False):
    if len(baseline) != len(arm):
        raise ScoringError('paired_population_mismatch')
    pairs = [(a, b) for a, b in zip(baseline, arm)
             if not common_coverage or (a['matched'] and b['matched'])]
    repairs = sum(not a['correct'] and b['correct'] for a, b in pairs)
    damages = sum(a['correct'] and not b['correct'] for a, b in pairs)
    p, _, _ = exact_mcnemar(damages, repairs)
    decision = ('flat' if repairs + damages < 10 or p >= .05
                else 'helps' if repairs > damages else 'hurts')
    return {'quotes': len(pairs), 'repairs': repairs, 'damages': damages,
            'baseline_correct': sum(a['correct'] for a, _ in pairs),
            'arm_correct': sum(b['correct'] for _, b in pairs),
            'wrong_to_other_wrong': sum(
                not a['correct'] and not b['correct'] and a['matched'] and b['matched']
                and bool(normalize(a['speaker'])) and bool(normalize(b['speaker']))
                and not same_speaker(a['speaker'], b['speaker'], groups) for a, b in pairs),
            'coverage_gains': sum(not a['matched'] and b['matched'] for a, b in pairs),
            'coverage_losses': sum(a['matched'] and not b['matched'] for a, b in pairs),
            'repairs_with_new_coverage': sum(not a['matched'] and b['correct'] for a, b in pairs),
            'damages_with_lost_coverage': sum(a['correct'] and not b['matched'] for a, b in pairs),
            'discordant': repairs + damages, 'exact_mcnemar_p': p, 'decision': decision}


def get_csv_records(data):
    return list(csv.DictReader(io.StringIO(data.decode('utf-8'), newline='')))


def get_arm_inputs(values):
    arms, seen = [], {'baseline'}
    for value in values:
        name, separator, path = value.partition('=')
        if (not separator or not path or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', name)
                or name in seen):
            raise ScoringError('invalid_or_duplicate_arm_name')
        seen.add(name)
        arms.append((name, Path(path)))
    if not arms:
        raise ScoringError('empty_arm_population')
    return arms


def get_provenance(input_hashes, code_hashes, profile):
    """Content-bound offline provenance, without Git/model/host or CLI probes."""
    return {'script': 'pdnc_review_effect.py', 'mode': 'offline_source_position',
            'source_profile': profile, 'scorer_sha256': code_hashes['scorer'],
            'input_fingerprint_sha256': get_sha256(json.dumps(
                input_hashes, sort_keys=True, separators=(',', ':')).encode('utf-8')),
            'environment_capture': 'not performed; saved-output CPU scoring'}


def get_review_effect(novel, manifest_path, baseline_path, arms, profile):
    """Read frozen inputs, return aggregate-only evidence without filesystem writes."""
    root = Path(novel)
    source = (root / 'novel_text.txt').read_bytes()
    gold_data = (root / 'quotation_info.csv').read_bytes()
    character_data = (root / 'character_info.csv').read_bytes()
    manifest_data = Path(manifest_path).read_bytes()
    baseline_data = Path(baseline_path).read_bytes()
    names = [name for name, _ in arms]
    get_arm_inputs([name + '=' + str(path) for name, path in arms])
    raw, prepared = get_prepared_source(source, json.loads(manifest_data), profile)
    quotes = get_gold_quotes(get_csv_records(gold_data), raw)
    groups = get_alias_groups(get_csv_records(character_data))
    baseline_summary, baseline_predictions = score_rows(json.loads(baseline_data), quotes, prepared, groups)
    summaries, comparisons, arm_hashes = {}, {}, {}
    for name, path in arms:
        data = Path(path).read_bytes()
        summaries[name], predictions = score_rows(json.loads(data), quotes, prepared, groups)
        comparisons[name] = {
            'all_gold_preregistered': get_paired_comparison(baseline_predictions, predictions, groups),
            'common_coverage_sensitivity': get_paired_comparison(
                baseline_predictions, predictions, groups, common_coverage=True)}
        arm_hashes[name] = get_sha256(data)
    app = Path(__file__).resolve().parent.parent
    helpers = ['dialogue_spans.py', 'apostrophe_repair.py', 'experiments/scoring.py',
               'experiments/stats.py', 'experiments/pdnc_fixture.py', 'experiments/provenance.py']
    input_hashes = {'raw_source': get_sha256(source),
                    'prepared_source': get_sha256(prepared.encode('utf-8')),
                    'gold': get_sha256(gold_data), 'characters': get_sha256(character_data),
                    'manifest': get_sha256(manifest_data), 'baseline': get_sha256(baseline_data),
                    'arms': arm_hashes}
    code_hashes = {'scorer': file_sha256(__file__),
                   **{name: file_sha256(app / name) for name in helpers}}
    return {'schema_version': 1, 'status': 'complete', 'source_profile': profile,
            'prediction_rule': 'most covered alphanumeric positions by row; earlier row breaks tie',
            'decision_rule': 'flat if discordant<10 or p>=0.05; otherwise repairs>damages helps, else hurts',
            'baseline': baseline_summary, 'arms': summaries, 'comparisons': comparisons,
            'input_sha256': input_hashes, 'code_sha256': code_hashes,
            'provenance': get_provenance(input_hashes, code_hashes, profile),
            'scope': 'one saved experiment; row-majority attribution, coverage and label effects are separate',
            'arm_order': names}


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ScoringError('invalid_arguments')


def is_input_destination(output, inputs):
    destination = Path(output).resolve()
    return any(destination == Path(path).resolve()
               or (destination.exists() and Path(path).exists() and destination.samefile(path))
               for path in inputs)


def main(argv=None):
    parser = PrivateArgumentParser(description=__doc__)
    for name in ('novel', 'manifest', 'baseline', 'output'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--arm', action='append', required=True)
    parser.add_argument('--source-profile', choices=('raw', 'apostrophe-repair'), required=True)
    try:
        args = parser.parse_args(argv)
        arms = get_arm_inputs(args.arm)
        inputs = [Path(args.novel) / name for name in
                  ('novel_text.txt', 'quotation_info.csv', 'character_info.csv')]
        inputs += [args.manifest, args.baseline, *[path for _, path in arms]]
        if is_input_destination(args.output, inputs):
            raise ScoringError('output_aliases_input')
        result = get_review_effect(args.novel, args.manifest, args.baseline,
                                   arms, args.source_profile)
        atomic_json_write(result, args.output, allow_nonatomic_fallback=False, trailing_newline=True)
    except Exception:
        print('PDNC review scoring failed; inputs or publication did not validate.', file=sys.stderr)
        return 2
    print('PDNC review scoring complete; aggregate result saved.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
