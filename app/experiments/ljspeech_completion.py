"""Read-only structural admission for cached LJSpeech evaluation stages."""
import argparse
import json
from pathlib import Path
import sys

APP = str(Path(__file__).resolve().parents[1])
if APP not in sys.path:
    sys.path.insert(0, APP)


def get_document(path):
    with open(path, encoding='utf-8') as handle:
        document = json.load(handle)
    if not isinstance(document, dict):
        raise ValueError('expected a JSON object')
    return document


def get_rows(document, split):
    rows = document.get(split)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'{split} requires nonempty rows')
    ids = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']:
            raise ValueError(f'{split} requires clip IDs')
        ids.append(row['id'])
    if len(set(ids)) != len(ids):
        raise ValueError(f'{split} contains duplicate IDs')
    return rows


def validate_audio(path, rate=None):
    import soundfile
    info = soundfile.info(str(path))
    if info.frames <= 0 or (rate is not None and info.samplerate != rate):
        raise ValueError(f'incomplete or wrong-rate audio: {path}')


def validate_split(path, repo, corpus, test_books):
    document = get_document(path)
    if (repo / document['root']).resolve() != corpus.resolve():
        raise ValueError('split names a different corpus')
    train, test = get_rows(document, 'train'), get_rows(document, 'test')
    native_rate = document['sample_rate_native']
    if type(native_rate) is not int or native_rate <= 0:
        raise ValueError('split requires a positive native sample rate')
    if set(document['test_books']) != set(test_books):
        raise ValueError('split names different held-out books')
    if {row['id'] for row in train} & {row['id'] for row in test}:
        raise ValueError('split contains overlapping clip IDs')
    train_books = {row['book'] for row in train}
    if train_books & set(test_books) or not {row['book'] for row in test} <= set(test_books):
        raise ValueError('split contains overlapping or incorrect source books')
    for row in train + test:
        if not isinstance(row.get('normalized'), str) or not row['normalized'].strip():
            raise ValueError('split requires normalized text')
        validate_audio(corpus / 'wavs' / (row['id'] + '.wav'), native_rate)
    return document


def validate_build(path, repo, split):
    document, source = get_document(path), get_document(split)
    train, test = get_rows(document, 'train'), get_rows(document, 'test')
    if {r['id'] for r in train} != {r['id'] for r in get_rows(source, 'train')} or {r['id'] for r in test} != {r['id'] for r in get_rows(source, 'test')}:
        raise ValueError('build does not cover the split')
    if document['native_rate'] != source['sample_rate_native']:
        raise ValueError('build native rate differs from split')
    rate = document['target_rate']
    if type(rate) is not int or rate != 24000:
        raise ValueError('build requires 24000 Hz audio')
    train_dir = repo / document['train_dir']
    with (repo / document['metadata']).open(encoding='utf-8') as handle:
        metadata = [json.loads(line) for line in handle if line.strip()]
    expected = {r['id'] + '.wav': r['normalized'] for r in source['train']}
    if len(metadata) != len(expected) or {r['audio_filepath']: r['text'] for r in metadata} != expected:
        raise ValueError('training metadata does not match the split')
    for row in metadata:
        validate_audio(train_dir / row['audio_filepath'], rate)
    expected_test = {r['id']: r for r in source['test']}
    for row in test:
        original = expected_test[row['id']]
        if row['text'] != original['normalized'] or row['book'] != original['book']:
            raise ValueError('held-out metadata does not match the split')
        validate_audio(repo / row['human_wav'], rate)
    if document['ref_source_id'] + '.wav' not in expected or document['ref_text'] != expected[document['ref_source_id'] + '.wav']:
        raise ValueError('reference prompt is not from training')
    validate_audio(repo / document['ref_sample'], rate)
    validate_audio(train_dir / 'ref.wav', rate)
    if (train_dir / 'ref_text.txt').read_text(encoding='utf-8') != document['ref_text']:
        raise ValueError('training reference text differs from build')


def validate_library_build(path, repo, dataset):
    from experiments.library_eval_build import held_out
    document = get_document(path)
    book = dataset.parent.name
    expected = held_out(str(dataset), book, str(repo), require_audio=True)
    if not expected or document.get('test') != expected:
        raise ValueError('build does not cover the held-out dataset')
    if document.get('target_rate') != 24000 or document.get('test_books') != [book]:
        raise ValueError('build rate or source book differs from dataset')
    if (repo / document['ref_sample']).resolve() != (dataset / 'ref.wav').resolve() or document['ref_text'] != (dataset / 'ref_text.txt').read_text(encoding='utf-8').strip():
        raise ValueError('reference prompt differs from dataset')
    validate_audio(dataset / 'ref.wav', 24000)
    for row in expected:
        validate_audio(repo / row['human_wav'], 24000)


def validate_generation(path, repo, build):
    document, source = get_document(path), get_document(build)
    expected = {r['id']: r for r in get_rows(source, 'test')}
    rows = get_rows(document, 'rows')
    if document.get('failures') != [] or set(document.get('arms', [])) != {'lora', 'clone'} or document.get('seed') != 1234:
        raise ValueError('generation has failures or different arms/seed')
    if {r['id'] for r in rows} != set(expected):
        raise ValueError('generation does not cover the build')
    for row in rows:
        original = expected[row['id']]
        if row.get('human_seconds') != original['seconds'] or any(row.get(key) != original[key] for key in ('book', 'text', 'human_wav')):
            raise ValueError('generated row differs from held-out source')
        for arm in ('lora', 'clone'):
            validate_audio(repo / row[arm + '_wav'], 24000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('prepare', 'build', 'train', 'library_build', 'generate'))
    parser.add_argument('path')
    parser.add_argument('--repo', required=True)
    parser.add_argument('--corpus')
    parser.add_argument('--split')
    parser.add_argument('--test-books', nargs='+')
    parser.add_argument('--dataset')
    parser.add_argument('--build')
    args = parser.parse_args()
    try:
        if args.kind == 'prepare':
            validate_split(args.path, Path(args.repo), Path(args.corpus), args.test_books)
        elif args.kind == 'build':
            validate_build(args.path, Path(args.repo), args.split)
        elif args.kind == 'library_build':
            validate_library_build(args.path, Path(args.repo), Path(args.dataset))
        elif args.kind == 'generate':
            validate_generation(args.path, Path(args.repo), args.build)
        else:
            from adapter_artifacts import validate_adapter_artifacts
            validate_adapter_artifacts(args.path, require_training_meta=True)
    except ImportError as error:
        print('CANNOT VALIDATE: ' + str(error), file=sys.stderr)
        return 2
    except Exception as error:
        from adapter_artifacts import AdapterValidationDependencyError
        if isinstance(error, AdapterValidationDependencyError):
            print('CANNOT VALIDATE: ' + str(error), file=sys.stderr)
            return 2
        print('INCOMPLETE: ' + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
